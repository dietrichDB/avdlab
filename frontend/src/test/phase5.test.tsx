import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import type { ReactElement } from 'react'
import { setupServer } from 'msw/node'
import { handlers } from '../mocks/handlers'
import { db } from '../mocks/data'
import { editableTopology, p5, resetPhase5 } from '../mocks/phase5'
import { JobDrawerProvider } from '../components/JobDrawer'
import LabDetailPage from '../pages/lab/LabDetail'
import ChangelogPage from '../pages/Changelog'
import AboutPage from '../pages/About'
import FeedbackAdminPage from '../pages/FeedbackAdmin'
import { PortPicker, UplinkPopover } from '../components/TopologyEditor'
import { VersionBadge } from '../components/Brand'
import { FeedbackProvider } from '../components/Feedback'
import { describeOp, editPayload, endpointOp, freePortsFor, leafGroups, retargetUplink, uplinksOf } from '../lib/topologyEdit'
import { feedbackPayload } from '../lib/feedback'
import { hasUnseen, LAST_SEEN_KEY } from '../lib/whatsNew'
import { renderBlocks } from '../lib/markdown'
import type { EditOp, Meta, TopologyPorts } from '../api/types'

const server = setupServer(...handlers)
const origin = globalThis.location?.origin ?? 'http://localhost'
beforeAll(() => {
  server.listen({ onUnhandledRequest: 'bypass' })
  const f = globalThis.fetch
  globalThis.fetch = ((u: RequestInfo | URL, init?: RequestInit) => f(typeof u === 'string' && u.startsWith('/') ? origin + u : u, init)) as typeof fetch
  db.authed = true
})
afterAll(() => server.close())
beforeEach(() => { resetPhase5(); localStorage.clear() })

function mount(path: string, element: ReactElement, route: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <JobDrawerProvider><FeedbackProvider><Routes><Route path={route} element={element} /></Routes></FeedbackProvider></JobDrawerProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}
const last = <T,>(xs: T[]): T | undefined => xs[xs.length - 1]
const lab = (name: string) => db.labs.find((l) => l.name === name)!

// ---------------------------------------------------------------- §1 editable topology
describe('topology edit ops', () => {
  it('ops list → /edit payload and descriptions', () => {
    const ops: EditOp[] = [
      { op: 'set_uplink', node: 'dc1-leaf1a', index: 0, switch: 'dc1-spine2', switch_port: 'Ethernet5' },
      { op: 'add_leaf_pair' },
      { op: 'set_spine_count', n: 3 },
    ]
    expect(editPayload(ops, true)).toEqual({ ops, dry_run: true })
    expect(ops.map(describeOp)).toEqual(['dc1-leaf1a: uplink #1 → dc1-spine2 Et5', '+ leaf pair', 'spine count = 3'])
    const add = endpointOp({ name: 'srv9', type: 'servers', switches: ['dc1-leaf1a', 'dc1-leaf1b'], switch_ports: ['Ethernet7', 'Ethernet7'], endpoint_ports: ['eth1', ''], mode: 'trunk', vlans: '11-12', native_vlan: '11', port_channel: true })
    expect(add).toEqual({ op: 'add_endpoint', name: 'srv9', type: 'servers', adapters: [{ switches: ['dc1-leaf1a', 'dc1-leaf1b'], switch_ports: ['Ethernet7', 'Ethernet7'], endpoint_ports: ['eth1', 'eth2'], mode: 'trunk', vlans: '11-12', native_vlan: 11, port_channel: { mode: 'active' } }] })
  })

  it('uplinks, retarget and leaf groups derive from the graph', () => {
    const t = editableTopology(lab('dc1-lab'))
    expect(uplinksOf(t, 'dc1-leaf1a').map((u) => `${u.index}:${u.switch}`)).toEqual(['0:dc1-spine1', '1:dc1-spine2'])
    const li = Number(uplinksOf(t, 'dc1-leaf1a')[0].edgeKey)
    // drag the spine end of leaf1a's first uplink onto spine2
    expect(retargetUplink(t, li, 'dc1-spine2', 'dc1-leaf1a')).toEqual({ node: 'dc1-leaf1a', index: 0, switch: 'dc1-spine2' })
    // onto a leaf → not an uplink move
    expect(retargetUplink(t, li, 'dc1-leaf2a', 'dc1-leaf1a')).toBeNull()
    expect(leafGroups(t).slice(0, 2).map((g) => g.label)).toEqual(['dc1-leaf1a + dc1-leaf1b', 'dc1-leaf2a + dc1-leaf2b'])
  })

  it('port picker offers only free ports (minus ports reserved by pending ops)', () => {
    const ports: TopologyPorts = { 'dc1-spine1': { used: ['Ethernet1', 'Ethernet2'], free: ['Ethernet3', 'Ethernet4', 'Ethernet5'] } }
    const ops: EditOp[] = [{ op: 'set_uplink', node: 'dc1-leaf1a', index: 0, switch: 'dc1-spine1', switch_port: 'Ethernet4' }]
    expect(freePortsFor(ports, 'dc1-spine1', ops)).toEqual(['Ethernet3', 'Ethernet5'])
    render(<PortPicker node="dc1-spine1" ports={ports} ops={ops} value="" onChange={() => {}} />)
    const opts = within(screen.getByTestId('port-picker-dc1-spine1')).getAllByRole('option').map((o) => o.getAttribute('value')).filter(Boolean)
    expect(opts).toEqual(['Ethernet3', 'Ethernet5'])
  })

  it('uplink popover builds set_uplink from a free spine port', () => {
    const t = editableTopology(lab('dc1-lab'))
    const ports: TopologyPorts = { 'dc1-spine2': { used: ['Ethernet1'], free: ['Ethernet6', 'Ethernet7'] }, 'dc1-spine1': { used: [], free: ['Ethernet8'] } }
    let added: EditOp | null = null
    render(<UplinkPopover topo={t} ports={ports} ops={[]} node="dc1-leaf1a" index={0} initialSwitch="dc1-spine2" onAdd={(o) => { added = o }} onCancel={() => {}} />)
    const pick = screen.getByTestId('uplink-port')
    expect(within(pick).getAllByRole('option').map((o) => o.getAttribute('value')).filter(Boolean)).toEqual(['Ethernet6', 'Ethernet7'])
    fireEvent.change(pick, { target: { value: 'Ethernet7' } })
    fireEvent.click(screen.getByTestId('uplink-add'))
    expect(added).toEqual({ op: 'set_uplink', node: 'dc1-leaf1a', index: 0, switch: 'dc1-spine2', switch_port: 'Ethernet7' })
  })

  it('edit mode: toolbar ops → dry run preview → apply → build offer', async () => {
    const l = lab('dc1-lab')
    mount(`/labs/${l.id}/topology`, <LabDetailPage />, '/labs/:id/:tab')
    fireEvent.click(await screen.findByTestId('edit-toggle'))
    fireEvent.click(screen.getByTestId('tb-add-pair'))
    fireEvent.change(screen.getByTestId('tb-spine-n'), { target: { value: '3' } })
    fireEvent.click(screen.getByTestId('tb-spine-set'))
    expect(within(screen.getByTestId('pending-ops')).getByText('+ leaf pair')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('ops-preview'))
    const dry = await screen.findByTestId('ops-dryrun')
    expect(dry.textContent).toContain('group_vars/FABRIC.yml')
    expect(last(p5.editRequests)).toEqual({ ops: [{ op: 'add_leaf_pair' }, { op: 'set_spine_count', n: 3 }], dry_run: true })
    expect(l.knobs?.leaf_pairs).toBe(2)
    fireEvent.click(screen.getByTestId('ops-apply'))
    await screen.findByTestId('applied-banner')
    expect(last(p5.editRequests)!.dry_run).toBe(false)
    expect(l.knobs?.leaf_pairs).toBe(3)
    expect(screen.getByTestId('applied-build')).toBeEnabled()
    expect(screen.queryByTestId('pending-ops')).toBeNull()
  })

  it('a 422 highlights the failing op with its violations', async () => {
    const l = lab('dc1-lab')
    mount(`/labs/${l.id}/topology`, <LabDetailPage />, '/labs/:id/:tab')
    fireEvent.click(await screen.findByTestId('edit-toggle'))
    fireEvent.click(screen.getByTestId('tb-add-pair'))
    fireEvent.change(screen.getByTestId('tb-spine-n'), { target: { value: '9' } })
    fireEvent.click(screen.getByTestId('tb-spine-set'))
    fireEvent.click(screen.getByTestId('ops-preview'))
    const v = await screen.findByTestId('ops-violations')
    expect(v.textContent).toContain('spine_count must be 1..4')
    expect(v.textContent).toContain('(change 2)')
    expect(screen.getByTestId('op-1').className).toContain('bg-red-50')
    expect(screen.queryByTestId('ops-apply')).toBeNull()
  })

  it('Add server form offers free leaf ports and queues add_endpoint', async () => {
    const l = lab('evpn-lab')
    mount(`/labs/${l.id}/topology`, <LabDetailPage />, '/labs/:id/:tab')
    fireEvent.click(await screen.findByTestId('edit-toggle'))
    fireEvent.click(screen.getByTestId('tb-add-server'))
    const form = await screen.findByTestId('add-endpoint-form')
    fireEvent.change(within(form).getByTestId('ep-name'), { target: { value: 'dc1-host9' } })
    fireEvent.change(within(form).getByTestId('ep-attach'), { target: { value: 'dc1-leaf2a + dc1-leaf2b' } })
    const pa = await within(form).findByTestId('port-picker-dc1-leaf2a')
    await waitFor(() => expect(within(pa).getAllByRole('option').length).toBeGreaterThan(1))
    const offered = within(pa).getAllByRole('option').map((o) => o.getAttribute('value')).filter(Boolean)
    // Et1-2 uplinks, Et3-4 MLAG, Et5 host2, Et6 host3 → first free is Et7
    expect(offered[0]).toBe('Ethernet7')
    expect(offered).not.toContain('Ethernet5')
    expect(offered).not.toContain('Ethernet6')
    fireEvent.change(pa, { target: { value: 'Ethernet7' } })
    fireEvent.change(within(form).getByTestId('port-picker-dc1-leaf2b'), { target: { value: 'Ethernet7' } })
    fireEvent.change(within(form).getByTestId('ep-vlans'), { target: { value: '11-12' } })
    fireEvent.click(within(form).getByTestId('ep-add'))
    expect(await within(screen.getByTestId('pending-ops')).findByText(/add server dc1-host9/)).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('ops-preview'))
    await screen.findByTestId('ops-dryrun')
    expect(last(p5.editRequests)!.ops[0]).toEqual({ op: 'add_endpoint', name: 'dc1-host9', type: 'servers', adapters: [{ switches: ['dc1-leaf2a', 'dc1-leaf2b'], switch_ports: ['Ethernet7', 'Ethernet7'], endpoint_ports: ['eth1', 'eth2'], mode: 'trunk', vlans: '11-12', port_channel: { mode: 'active' } }] })
  })

  it('layout round-trips through PUT /layout', async () => {
    const l = lab('dc1-lab')
    mount(`/labs/${l.id}/topology`, <LabDetailPage />, '/labs/:id/:tab')
    fireEvent.click(await screen.findByTestId('edit-toggle'))
    fireEvent.click(screen.getByTestId('tb-auto-layout'))
    fireEvent.click(screen.getByTestId('tb-lock'))
    fireEvent.click(screen.getByTestId('tb-save-layout'))
    await waitFor(() => expect(p5.layouts.get(l.id)?.locked).toBe(true))
    expect(Object.keys(p5.layouts.get(l.id)!.positions)).toContain('dc1-spine1')
  })
})

// ---------------------------------------------------------------- §2 changelog + §5 version badge
const meta = (v: string): Meta => ({ version: v, git_sha: 'abc1234def', build_date: null, avd_version: null, anta_version: null, python: null, changelog_latest: { version: v, date: null, sections: {} } })

describe('changelog and version badge', () => {
  it('hasUnseen compares latest with lastSeenVersion', () => {
    expect(hasUnseen(meta('0.6.0'), null)).toBe(true)
    expect(hasUnseen(meta('0.6.0'), '0.5.0')).toBe(true)
    expect(hasUnseen(meta('0.6.0'), '0.6.0')).toBe(false)
    expect(hasUnseen(undefined, null)).toBe(false)
  })

  it('renders entries with anchors and section badges, and marks the version seen', async () => {
    mount('/changelog', <ChangelogPage />, '/changelog')
    const e = await screen.findByTestId('changelog-0.6.0')
    expect(e.id).toBe('v0.6.0')
    expect(within(e).getByText('Added')).toBeInTheDocument()
    expect(within(e).getByText('Fixed')).toBeInTheDocument()
    expect(within(e).getByText('running')).toBeInTheDocument()
    expect(screen.getByTestId('changelog-0.1.0')).toBeInTheDocument()
    // inline markdown: `code` → <code>
    expect([...e.querySelectorAll('code')].map((c) => c.textContent)).toContain('docker cp')
    await waitFor(() => expect(localStorage.getItem(LAST_SEEN_KEY)).toBe('0.6.0'))
  })

  it('version badge: v<version>, sha tooltip, link to changelog, dot until seen', async () => {
    mount('/', <VersionBadge />, '/')
    const b = await screen.findByText('v0.6.0')
    const link = screen.getByTestId('version-badge')
    expect(link).toHaveAttribute('href', '/changelog')
    expect(link.getAttribute('title')).toContain('git 3385468')
    expect(b).toBeInTheDocument()
    expect(screen.getByTestId('whats-new-dot')).toBeInTheDocument()
  })

  it('version badge: no dot when the latest version was seen', async () => {
    localStorage.setItem(LAST_SEEN_KEY, '0.6.0')
    mount('/', <VersionBadge />, '/')
    await screen.findByText('v0.6.0')
    expect(screen.queryByTestId('whats-new-dot')).toBeNull()
  })
})

// ---------------------------------------------------------------- §3 feedback
describe('feedback', () => {
  it('trims the payload', () => {
    expect(feedbackPayload({ kind: 'bug', title: '  Crash ', body: ' boom\n', page: '/labs ', include_diagnostics: false }))
      .toEqual({ kind: 'bug', title: 'Crash', body: 'boom', page: '/labs', include_diagnostics: false })
  })

  it('modal: page prefilled, diagnostics preview, POST payload, toast', async () => {
    mount('/labs', <div>labs page</div>, '/labs')
    fireEvent.click(screen.getByTestId('feedback-fab'))
    expect(screen.getByTestId('fb-page')).toHaveValue('/labs')
    fireEvent.click(screen.getByTestId('fb-kind-suggestion'))
    fireEvent.change(screen.getByTestId('fb-title'), { target: { value: 'Dark topology' } })
    fireEvent.change(screen.getByTestId('fb-body'), { target: { value: 'Please add a dark theme to the graph.' } })
    fireEvent.click(screen.getByTestId('fb-diag-toggle'))
    await waitFor(() => expect(screen.getByTestId('fb-diag-preview').textContent).toContain('"version": "0.6.0"'))
    expect(screen.getByTestId('fb-diag-preview').textContent).toContain('"page": "/labs"')
    fireEvent.click(screen.getByTestId('fb-send'))
    expect(await screen.findByTestId('toast')).toHaveTextContent('feedback #3 sent')
    expect(last(p5.feedbackRequests)).toEqual({ kind: 'suggestion', title: 'Dark topology', body: 'Please add a dark theme to the graph.', page: '/labs', include_diagnostics: true })
    expect(screen.queryByTestId('feedback-form')).toBeNull()
  })

  it('admin page lists entries and patches status', async () => {
    mount('/feedback', <FeedbackAdminPage />, '/feedback')
    await screen.findByTestId('feedback-table')
    expect(screen.getByText('Export topology as PNG')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /issue/ })).toHaveAttribute('href', 'https://github.com/example/avdlab/issues/12')
    fireEvent.change(screen.getByTestId('feedback-status-1'), { target: { value: 'done' } })
    await waitFor(() => expect(p5.feedback.find((f) => f.id === 1)!.status).toBe('done'))
  })
})

// ---------------------------------------------------------------- §4 about
describe('about', () => {
  it('shows logo, version, copyright, notices table and trademark note', async () => {
    mount('/about', <AboutPage />, '/about')
    expect(await screen.findByTestId('about-copyright')).toHaveTextContent('© 2026 Dietrich De Bruyne — Systems Engineer, Arista Networks')
    expect(screen.getByTestId('logo').innerHTML).toContain('<svg')
    expect(await screen.findByTestId('about-version')).toHaveTextContent('v0.6.0')
    const notices = screen.getByTestId('about-notices')
    expect(within(notices).getByRole('table')).toBeInTheDocument()
    expect(within(notices).getByText('containerlab')).toBeInTheDocument()
    expect(screen.getByTestId('about-trademark')).toHaveTextContent('not an Arista product')
  })

  it('markdown renderer does not inject HTML', () => {
    const { container } = render(<div>{renderBlocks('# T\n\n<img src=x onerror=alert(1)> [x](javascript:alert(1))')}</div>)
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('a')).toBeNull()
  })
})

describe('meta shape tolerance', () => {
  it('normalizes {versions, raw} to {entries, markdown}', async () => {
    const { normalizeChangelog } = await import('../api/phase5')
    const e = { version: '0.6.0', date: '2026-09-26', sections: { Added: ['x'] } }
    expect(normalizeChangelog({ versions: [e], raw: '# C' })).toEqual({ entries: [e], markdown: '# C' })
    expect(normalizeChangelog({ entries: [e], markdown: '# C' })).toEqual({ entries: [e], markdown: '# C' })
  })
})
