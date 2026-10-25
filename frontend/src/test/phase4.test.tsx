import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { setupServer } from 'msw/node'
import { handlers } from '../mocks/handlers'
import { db, devices, templates } from '../mocks/data'
import { p4 } from '../mocks/phase4'
import { clientPlans } from '../mocks/phase3'
import { JobDrawerProvider } from '../components/JobDrawer'
import { KnobForm, defaultKnobValues } from '../components/KnobForm'
import LabDetailPage from '../pages/lab/LabDetail'
import CloudVisionPage from '../pages/CloudVision'
import { ZtpNodesTable } from '../pages/lab/ZtpPanel'
import { MssMatrix } from '../pages/lab/MssTab'
import { ztpSteps } from '../lib/provisioning'
import { syncRows, syncTotals } from '../lib/netbox'
import { decide, normalizeSegmentation, policyMatrix, policyVerdict } from '../lib/mss'
import type { ZtpNode } from '../api/types'
import { UI_MODE_KEY } from '../lib/uiMode'

const server = setupServer(...handlers)
const origin = globalThis.location?.origin ?? 'http://localhost'
beforeAll(() => {
  server.listen({ onUnhandledRequest: 'bypass' })
  const f = globalThis.fetch
  globalThis.fetch = ((u: RequestInfo | URL, init?: RequestInit) => f(typeof u === 'string' && u.startsWith('/') ? origin + u : u, init)) as typeof fetch
  db.authed = true
})
afterAll(() => server.close())

function mount(path: string, element = <LabDetailPage />, route = '/labs/:id/:tab') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <JobDrawerProvider><Routes><Route path={route} element={element} /></Routes></JobDrawerProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}
const lab = (name: string) => db.labs.find((l) => l.name === name)!
const node = (o: Partial<ZtpNode>): ZtpNode => ({ name: 'n', mac: null, ip: '10.0.0.1', lease: false, bootfile_fetched: false, eapi: false, bootfile: 'n.cfg', ...o })

// ---------------------------------------------------------------- A: ZTP
describe('ZTP timeline', () => {
  it('derives DHCP → bootfile → eAPI step states', () => {
    expect(ztpSteps(node({}))).toEqual({ dhcp: 'current', bootfile: 'pending', eapi: 'pending' })
    expect(ztpSteps(node({ lease: true }))).toEqual({ dhcp: 'done', bootfile: 'current', eapi: 'pending' })
    expect(ztpSteps(node({ lease: true, bootfile_fetched: true }))).toEqual({ dhcp: 'done', bootfile: 'done', eapi: 'current' })
    expect(ztpSteps(node({ lease: true, bootfile_fetched: true, eapi: true }))).toEqual({ dhcp: 'done', bootfile: 'done', eapi: 'done' })
    // eAPI seen (e.g. from the deploy poll) implies the earlier steps happened
    expect(ztpSteps(node({ eapi: true })).dhcp).toBe('done')
  })

  it('renders per-node timelines and the summary', () => {
    render(<ZtpNodesTable nodes={[
      node({ name: 'leaf1', lease: true, bootfile_fetched: true, eapi: true }),
      node({ name: 'leaf2', lease: true }),
      node({ name: 'spine1' }),
    ]} />)
    const t2 = screen.getByTestId('ztp-timeline-leaf2')
    expect(within(t2).getByTestId('ztp-step-dhcp')).toHaveAttribute('data-state', 'done')
    expect(within(t2).getByTestId('ztp-step-bootfile')).toHaveAttribute('data-state', 'current')
    expect(within(t2).getByTestId('ztp-step-eapi')).toHaveAttribute('data-state', 'pending')
    expect(within(screen.getByTestId('ztp-timeline-leaf1')).getByTestId('ztp-step-eapi')).toHaveAttribute('data-state', 'done')
    const s = screen.getByTestId('ztp-summary').textContent
    expect(s).toContain('DHCP 2/3')
    expect(s).toContain('bootfile 1/3')
    expect(s).toContain('eAPI 1/3')
  })

  it('Runtime tab shows the ZTP panel only in ztp mode', async () => {
    const l = lab('evpn-lab')
    const { unmount } = mount(`/labs/${l.id}/runtime`)
    await screen.findByTestId('runtime-table')
    expect(screen.queryByTestId('ztp-panel')).toBeNull()
    unmount()
    l.knobs = { ...l.knobs, provisioning: { mode: 'ztp', ztp_source: 'intended', ztp_server_ip: null } }
    mount(`/labs/${l.id}/runtime`)
    await screen.findByTestId('ztp-panel')
    const rows = await screen.findAllByTestId('ztp-row')
    expect(rows).toHaveLength(devices(l).length)
    expect(within(rows[0]).getByTestId('ztp-step-eapi')).toHaveAttribute('data-state', 'done') // deployed lab: all done
    expect(screen.getByText('server 172.16.20.254')).toBeInTheDocument()
    l.knobs = { ...l.knobs, provisioning: { mode: 'startup-config', ztp_source: 'intended', ztp_server_ip: null } }
  })

  it('knob form renders provisioning + act sub-forms and hides segmentation behind a summary', () => {
    const t = templates[0]
    const onChange = vi.fn()
    const { container } = render(<KnobForm knobs={t.knobs} values={defaultKnobValues(t.knobs)} onChange={onChange} labTarget="clab" />)
    expect(screen.getByTestId('provisioning-knob-form')).toBeInTheDocument()
    expect(screen.getByTestId('act-knob-form')).toHaveTextContent('Only used when the lab target is act')
    expect(screen.getByTestId('segmentation-summary')).toHaveTextContent('0 application(s)')
    const ip = container.querySelector('input[name="provisioning.ztp_server_ip"]') as HTMLInputElement
    expect(ip).toBeDisabled()
    fireEvent.change(container.querySelector('select[name="provisioning.mode"]')!, { target: { value: 'ztp' } })
    expect(onChange.mock.lastCall![0].provisioning).toEqual({ mode: 'ztp', ztp_source: 'intended', ztp_server_ip: null })
  })

  it('CloudVision target modal writes the ZTP script (write-only) and shows the badge', async () => {
    mount('/cloudvision', <CloudVisionPage />, '/cloudvision')
    const table = await screen.findByTestId('cv-targets')
    expect(within(table).getAllByTestId('ztp-script-badge')).toHaveLength(1)
    const row = within(table).getAllByRole('row').find((r) => r.textContent?.includes('cvp-lab'))!
    fireEvent.click(within(row).getByText('Edit'))
    const ta = screen.getByRole('textbox', { name: /ZTP bootstrap script/ }) as HTMLTextAreaElement
    expect(ta.value).toBe('')
    fireEvent.change(ta, { target: { value: '#!/usr/bin/env python3\nprint("ztp")\n' } })
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(db.cvTargets.find((t) => t.name === 'cvp-lab')!.has_ztp_script).toBe(true))
  })
})

// ---------------------------------------------------------------- B: NetBox
describe('NetBox', () => {
  it('orders and totals sync counts per type', () => {
    const r = { created: { device: 8, tag: 2 }, updated: { interface: 3 }, deleted: {}, unchanged: { device: 2 }, skipped: [], netbox_url: null }
    expect(syncRows(r).map((x) => x.type)).toEqual(['tag', 'device', 'interface'])
    expect(syncRows(r)[1]).toEqual({ type: 'device', created: 8, updated: 0, deleted: 0, unchanged: 2 })
    expect(syncTotals(r)).toEqual({ created: 10, updated: 3, deleted: 0, unchanged: 2 })
  })

  it('binds a target, syncs and shows created/updated counts, skipped and the NetBox link', async () => {
    const l = lab('dc1-lab')
    mount(`/labs/${l.id}/netbox`)
    const select = await screen.findByRole('combobox', { name: 'NetBox target' })
    expect(screen.getByTestId('netbox-sync')).toBeDisabled()
    fireEvent.change(select, { target: { value: 'nbt-local' } })
    await waitFor(() => expect(l.netbox_target_id).toBe('nbt-local'))
    await waitFor(() => expect(screen.getByTestId('netbox-sync')).toBeEnabled())
    fireEvent.click(screen.getByLabelText('include clients (as servers)')) // off → client cables skipped
    fireEvent.click(screen.getByTestId('netbox-sync'))
    const counts = await screen.findByTestId('netbox-counts', {}, { timeout: 8000 })
    const devRow = within(counts).getByTestId('netbox-count-device')
    expect(devRow.children[1].textContent).toBe(String(devices(l).length))
    expect(screen.getByTestId('netbox-totals')).toHaveTextContent('0 updated')
    expect(screen.getByTestId('netbox-open')).toHaveAttribute('href', `http://localhost:8000/dcim/devices/?tag=avdlab-${l.slug}`)
    expect(within(screen.getByTestId('netbox-skipped')).getAllByRole('listitem').length).toBe(clientPlans(l).reduce((n, c) => n + c.links.length, 0))
    // second run: updates only
    fireEvent.click(screen.getByTestId('netbox-sync'))
    await waitFor(() => expect(within(screen.getByTestId('netbox-count-interface')).getAllByRole('cell')[2].textContent).toBe('2'), { timeout: 8000 })
    expect(screen.getByTestId('netbox-totals')).toHaveTextContent('0 created')
    expect(p4.netboxTargets.length).toBe(2)
  }, 20000)
})

// ---------------------------------------------------------------- C: MSS
describe('MSS', () => {
  const seg = () => normalizeSegmentation(lab('evpn-lab').knobs?.segmentation)

  it('derives the policy matrix like labs/mss.py', () => {
    const m = policyMatrix(seg())
    expect(m.VRF10.web.app).toBe('web-only')
    expect(m.VRF10.app.web).toBe('deny-ssh')
    expect(m.VRF10.web.web).toBe('policy-drop-all') // no policy from web → fallback drop
    expect(decide(seg(), 'web-only', 'ssh')).toBe('drop')
    expect(decide(seg(), 'deny-ssh', 'web')).toBe('forward')
    expect(policyVerdict(seg(), 'policy-forward-all')).toBe('forward')
    expect(policyVerdict(seg(), 'web-only')).toBe('mixed')
    expect(policyVerdict(seg(), null)).toBeNull()
  })

  it('renders segment × segment cells with policy and verdict', () => {
    const s = seg()
    render(<MssMatrix matrix={{ ...policyMatrix(s), VRF11: { a: { a: null } } }} policies={s.policies} />)
    expect(screen.getByTestId('mss-cell-VRF10-web-app')).toHaveTextContent('web-only')
    expect(screen.getByTestId('mss-cell-VRF10-web-app')).toHaveAttribute('data-verdict', 'mixed')
    expect(screen.getByTestId('mss-cell-VRF10-web-web')).toHaveAttribute('data-verdict', 'drop')
    expect(screen.getByTestId('mss-cell-VRF11-a-a')).toHaveTextContent('—')
    expect(screen.getByTestId('mss-cell-VRF11-a-a')).toHaveAttribute('data-verdict', 'none')
  })

  it('MSS tab: foundation badge, editors write the knob, live matrix preview, verify → cases', async () => {
    const l = lab('evpn-lab')
    mount(`/labs/${l.id}/mss`)
    await screen.findByTestId('mss-tab')
    expect(screen.getByText('foundation — MSS design to be refined')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('mss-cell-VRF10-app-web')).toHaveTextContent('deny-ssh'))
    expect(within(screen.getByTestId('mss-rendered-on')).getByText('dc1-leaf1a')).toBeInTheDocument()
    // web ← app: switch the policy to the built-in forward-all → preview updates before saving
    fireEvent.change(screen.getByLabelText('segment web policy 1 policy'), { target: { value: 'policy-forward-all' } })
    expect(screen.getByTestId('mss-cell-VRF10-app-web')).toHaveAttribute('data-verdict', 'forward')
    expect(screen.getByText('preview (unsaved)')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('mss-save'))
    await waitFor(() => expect((l.knobs?.segmentation as { vrfs: { segments: { policies: { policy: string }[] }[] }[] }).vrfs[0].segments[0].policies[0].policy).toBe('policy-forward-all'))
    // verify (auto-derived)
    fireEvent.click(screen.getByTestId('mss-verify-run'))
    const cases = await screen.findByTestId('mss-cases', {}, { timeout: 8000 })
    const rows = within(cases).getAllByTestId('mss-case-row')
    expect(rows.length).toBeGreaterThan(3)
    expect(within(cases).getAllByText('fail')).toHaveLength(1)
    expect(screen.getByTestId('mss-verify-summary')).toHaveTextContent(`${rows.length - 1} passed`)
  }, 20000)

  it('raw YAML toggle shows the knob document', async () => {
    const l = lab('evpn-lab')
    mount(`/labs/${l.id}/mss`)
    await screen.findByTestId('mss-tab')
    fireEvent.click(screen.getByTestId('mss-raw-toggle'))
    expect(await screen.findByTestId('mss-raw')).toBeInTheDocument()
    expect(screen.queryByTestId('mss-apps')).toBeNull()
  })
})

// ---------------------------------------------------------------- D: ACT
describe('ACT', () => {
  it('ACT tab: prepared state, node roles, download, client scripts, hints; deploy needs an act host', async () => {
    const l = lab('act-lab')
    mount(`/labs/${l.id}/act`)
    await screen.findByTestId('act-tab')
    expect(await screen.findByText('prepared')).toBeInTheDocument()
    const rows = screen.getAllByTestId('act-node-row')
    const roles = rows.map((r) => within(r).getAllByRole('cell')[1].textContent)
    expect(roles.filter((x) => x === 'tools-server')).toHaveLength(1)
    expect(roles.filter((x) => x === 'switch')).toHaveLength(devices(l).length)
    expect(roles.filter((x) => x === 'client')).toHaveLength(clientPlans(l).length)
    expect(screen.getByTestId('act-download')).toHaveAttribute('href', `/api/labs/${l.id}/act/topology`)
    expect(screen.getByTestId('act-client-script').textContent).toContain('ip addr add')
    expect(within(screen.getByTestId('act-hints')).getByText(/act\.arista\.com/)).toBeInTheDocument()
    expect(screen.getByTestId('act-deploy')).toBeDisabled()
    expect(screen.getByText(/set the ACT tools server/)).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('act-prepare'))
    await waitFor(() => expect(screen.getByTestId('act-prepare')).toHaveTextContent('Prepare'))
    // the ACT tab only exists for act labs
    expect(screen.getByRole('tab', { name: 'ACT' })).toBeInTheDocument()
  })

  it('clab labs have no ACT tab; ACT labs disable traffic with the 409 reason', async () => {
    localStorage.setItem(UI_MODE_KEY, 'advanced') // the MSS tab is an advanced-mode tab (CONTRACT-8 B)
    const { unmount } = mount(`/labs/${lab('dc1-lab').id}/overview`)
    await screen.findByRole('tab', { name: 'MSS' })
    expect(screen.queryByRole('tab', { name: 'ACT' })).toBeNull()
    unmount()
    mount(`/labs/${lab('act-lab').id}/clients`)
    expect(await screen.findByTestId('act-traffic-reason')).toHaveTextContent('not supported on ACT')
    expect(screen.getByTestId('ping-mesh')).toBeDisabled()
    localStorage.removeItem(UI_MODE_KEY)
  })

  it('deploys an ACT lab through a host of kind act (no containerlab)', async () => {
    const l = lab('act-lab')
    db.hosts.push({ ...db.hosts[0], id: 'host-act', name: 'act-tools', kind: 'act', address: 'act-tools.example.net' })
    l.host_id = 'host-act'
    mount(`/labs/${l.id}/act`)
    const btn = await screen.findByTestId('act-deploy')
    await waitFor(() => expect(btn).toBeEnabled())
    fireEvent.click(btn)
    await waitFor(() => expect(l.state).toBe('deployed'), { timeout: 8000 })
    const job = db.jobs.find((j) => j.type === 'lab.deploy' && j.lab_id === l.id)!
    expect(job.log.join('\n')).toContain('containerlab skipped')
    expect(job.log.join('\n')).not.toContain('containerlab deploy')
    db.hosts = db.hosts.filter((h) => h.id !== 'host-act')
  }, 15000)
})
