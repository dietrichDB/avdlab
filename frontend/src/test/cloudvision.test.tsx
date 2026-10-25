import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import ErrorBoundary from '../components/ErrorBoundary'
import { useState } from 'react'
import { setupServer } from 'msw/node'
import { CloudVisionKnobForm } from '../components/CloudVisionKnobForm'
import { KnobForm, defaultKnobValues } from '../components/KnobForm'
import { CvLastDeployPanel, CvOnboardingTable } from '../pages/lab/CloudVisionTab'
import { CV_KNOB_DEFAULT, cvActions, normalizeCvKnob, type CloudVisionKnob } from '../lib/cloudvision'
import { handlers } from '../mocks/handlers'
import { db, templates } from '../mocks/data'
import { cvApi } from '../api/cloudvision'
import { labsApi } from '../api/labs'
import { ApiError } from '../api/client'
import type { CVLastDeploy, CVTarget, JobRef } from '../api/types'

function Harness({ initial, onChange }: { initial: unknown; onChange: (v: CloudVisionKnob) => void }) {
  const [v, setV] = useState<unknown>(initial)
  return <CloudVisionKnobForm value={v} onChange={(n) => { setV(n); onChange(n) }} />
}

const input = (c: HTMLElement, name: string) => c.querySelector(`[name="${name}"]`) as HTMLInputElement

describe('cloudvision knob sub-form', () => {
  it('renders a stored knob object and round-trips it unchanged', () => {
    const stored = { mode: 'cvaas', region: 'euwest-2', servers: [], disable_aaa: false, topology_hints: true, device_tags: { lab: 'evpn', owner: 'netops' } }
    const onChange = vi.fn()
    const { container } = render(<Harness initial={stored} onChange={onChange} />)
    expect(input(container, 'cloudvision.mode').value).toBe('cvaas')
    expect(input(container, 'cloudvision.region').value).toBe('euwest-2')
    expect(input(container, 'cloudvision.disable_aaa').checked).toBe(false)
    expect(input(container, 'cloudvision.device_tags.1.value').value).toBe('netops')
    expect(onChange).not.toHaveBeenCalled()
    // toggle and toggle back → exact same object
    fireEvent.click(input(container, 'cloudvision.topology_hints'))
    fireEvent.click(input(container, 'cloudvision.topology_hints'))
    expect(onChange.mock.lastCall![0]).toEqual(stored)
  })

  it('edits mode, servers and device tags into the knob object', () => {
    const onChange = vi.fn()
    const { container } = render(<Harness initial={CV_KNOB_DEFAULT} onChange={onChange} />)
    expect(container.querySelector('[data-testid="cv-tags"]')).toBeNull() // mode none: nothing else
    fireEvent.change(input(container, 'cloudvision.mode'), { target: { value: 'onprem' } })
    fireEvent.click(screen.getByText('Add server'))
    fireEvent.change(input(container, 'cloudvision.servers.0'), { target: { value: '192.0.2.10' } })
    fireEvent.click(screen.getByText('Add server'))
    fireEvent.change(input(container, 'cloudvision.servers.1'), { target: { value: '192.0.2.11' } })
    fireEvent.click(screen.getByText('Add tag'))
    fireEvent.change(input(container, 'cloudvision.device_tags.0.name'), { target: { value: 'site' } })
    fireEvent.change(input(container, 'cloudvision.device_tags.0.value'), { target: { value: 'lab1' } })
    fireEvent.click(screen.getByText('Add tag')) // empty row is not emitted
    expect(onChange.mock.lastCall![0]).toEqual({
      mode: 'onprem', region: 'auto', servers: ['192.0.2.10', '192.0.2.11'], disable_aaa: true, topology_hints: true, device_tags: { site: 'lab1' },
    })
    fireEvent.click(screen.getByLabelText('remove server 1'))
    expect(onChange.mock.lastCall![0].servers).toEqual(['192.0.2.11'])
    fireEvent.change(input(container, 'cloudvision.mode'), { target: { value: 'cvaas' } })
    fireEvent.change(input(container, 'cloudvision.region'), { target: { value: 'us-central1-b' } })
    expect(onChange.mock.lastCall![0]).toMatchObject({ mode: 'cvaas', region: 'us-central1-b', device_tags: { site: 'lab1' } })
  })

  it('normalises partial / list-form values', () => {
    expect(normalizeCvKnob(undefined)).toEqual(CV_KNOB_DEFAULT)
    expect(normalizeCvKnob({ mode: 'onprem', servers: '10.0.0.1', device_tags: [{ name: 'a', value: 1 }] }))
      .toEqual({ ...CV_KNOB_DEFAULT, mode: 'onprem', servers: ['10.0.0.1'], device_tags: { a: '1' } })
  })

  it('KnobForm uses the sub-form only for the cloudvision knob', () => {
    const t = templates[0]
    const onChange = vi.fn()
    const { container } = render(<KnobForm knobs={t.knobs} values={defaultKnobValues(t.knobs)} onChange={onChange} />)
    expect(screen.getAllByTestId('cv-knob-form')).toHaveLength(1)
    expect(input(container, 'pools.loopback')).not.toBeNull() // generic object knob untouched
    fireEvent.change(input(container, 'cloudvision.mode'), { target: { value: 'cvaas' } })
    expect(onChange.mock.lastCall![0].cloudvision).toEqual({ ...CV_KNOB_DEFAULT, mode: 'cvaas' })
  })
})

const deployResult: CVLastDeploy = {
  workspace: { id: 'ws-123', state: 'submitted', url: 'https://www.arista.io/cv/provisioning/workspaces?ws=ws-123' },
  change_control: { id: 'cc-123', state: 'pending approval' },
  devices: [
    { hostname: 'dc1-spine1', serial: 'EVPN0001', exists_on_cv: true, streaming: true },
    { hostname: 'dc1-leaf2b', serial: 'EVPN0014', exists_on_cv: false, streaming: false },
  ],
  errors: ['tag conflict on dc1-leaf1a'],
  warnings: ['device dc1-leaf2b (serial EVPN0014) not found on CloudVision — skipped'],
  finished_at: new Date().toISOString(),
}

describe('CloudVision status panels', () => {
  it('renders onboarding nodes with TerminAttr / streaming marks', () => {
    render(<CvOnboardingTable onboarding={{ checked_at: new Date().toISOString(), nodes: [
      { name: 'clab-x-dc1-spine1', serial: 'X0001', terminattr_running: true, streaming: true },
      { name: 'clab-x-dc1-leaf1a', serial: 'X0011', terminattr_running: true, streaming: false },
      { name: 'clab-x-dc1-leaf1b', serial: null, terminattr_running: false, streaming: false },
    ] }} />)
    const rows = screen.getAllByTestId('cv-onboard-row')
    expect(rows).toHaveLength(3)
    expect(within(rows[0]).getByLabelText('streaming')).toBeInTheDocument()
    expect(within(rows[1]).getByLabelText('not streaming')).toBeInTheDocument()
    expect(within(rows[2]).getByLabelText('TerminAttr not running')).toBeInTheDocument()
    const summary = screen.getByTestId('cv-onboard-summary').textContent
    expect(summary).toContain('TerminAttr 2/3')
    expect(summary).toContain('streaming 1/3')
  })

  it('renders the last deploy: workspace, change control, devices, warnings, errors', () => {
    render(<MemoryRouter><CvLastDeployPanel result={deployResult} /></MemoryRouter>)
    expect(screen.getByText('ws-123')).toBeInTheDocument()
    expect(screen.getByText('submitted')).toBeInTheDocument()
    expect(screen.getByText('cc-123')).toBeInTheDocument()
    expect(screen.getByText('open in CloudVision ↗')).toHaveAttribute('href', deployResult.workspace!.url)
    const rows = screen.getAllByTestId('cv-deploy-row')
    expect(rows).toHaveLength(2)
    expect(within(rows[1]).getByLabelText('missing on CloudVision')).toBeInTheDocument()
    fireEvent.click(within(screen.getByTestId('cv-deploy-warnings')).getByText(/Show 1 workspace build warning/))
    expect(within(screen.getByTestId('cv-deploy-warnings')).getByText(/not found on CloudVision/)).toBeInTheDocument()
    expect(within(screen.getByTestId('cv-deploy-errors')).getByText('tag conflict on dc1-leaf1a')).toBeInTheDocument()
  })

  it('0.8.0: a failed run without a workspace renders (used to crash the whole page)', () => {
    const failed: CVLastDeploy = { ok: false, workspace: null, change_control: null, devices: [], errors: ['pre-flight: 401 unauthorized'], warnings: [], finished_at: null, job_id: 'j1', at: 1_700_000_000 }
    render(<MemoryRouter><CvLastDeployPanel result={failed} jobHref="/jobs/j1" /></MemoryRouter>)
    const box = screen.getByTestId('cv-deploy-failed')
    expect(box.textContent).toContain('before CloudVision created a workspace')
    expect(within(box).getByText('open job')).toHaveAttribute('href', '/jobs/j1')
    expect(screen.getAllByText('not created')).toHaveLength(2) // workspace + change control
    expect(screen.queryByTestId('cv-deploy-devices')).toBeNull()
    // tolerant to a backend summary with missing arrays
    render(<MemoryRouter><CvLastDeployPanel result={{ workspace: { id: 'ws-9', state: 'submitted' }, change_control: null } as CVLastDeploy} /></MemoryRouter>)
    expect(screen.getByTestId('cv-no-changes').textContent).toContain('nothing to push')
    expect(screen.getByText('not needed (no config changes)')).toBeInTheDocument()
  })

  it('ErrorBoundary keeps a render error local and resets on key change', () => {
    const Boom = ({ on }: { on: boolean }) => { if (on) throw new Error('kaboom'); return <div>fine</div> }
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { rerender } = render(<ErrorBoundary name="the test tab" resetKey="a"><Boom on /></ErrorBoundary>)
    const eb = screen.getByTestId('error-boundary')
    expect(eb.textContent).toContain('the test tab')
    expect(eb.textContent).toContain('Error: kaboom')
    rerender(<ErrorBoundary name="the test tab" resetKey="b"><Boom on={false} /></ErrorBoundary>)
    expect(screen.getByText('fine')).toBeInTheDocument()
    spy.mockRestore()
  })
})

describe('cvActions', () => {
  const t = (o: Partial<CVTarget> = {}): CVTarget => ({ id: 't', name: 't', kind: 'cvaas', server: 'www.arista.io', verify_certs: true, has_token: true, has_onboarding_token: true, onboarding_token_meta: null, created_at: '', ...o })
  it('gates onboard on deployed + onboarding token, deploy on built + token', () => {
    expect(cvActions('built', t()).onboard.enabled).toBe(false)
    expect(cvActions('built', t()).deploy.enabled).toBe(true)
    expect(cvActions('deployed', t()).onboard.enabled).toBe(true)
    expect(cvActions('validated', t({ has_onboarding_token: false })).onboard.enabled).toBe(false)
    expect(cvActions('draft', t()).deploy.enabled).toBe(false)
    expect(cvActions('deployed', t({ has_token: false })).deploy.enabled).toBe(false)
    expect(cvActions('deployed', null).deploy.reason).toMatch(/bind/)
    expect(cvActions('deployed', t(), 'none').onboard.enabled).toBe(false)
  })
})

describe('CloudVision mock API', () => {
  const server = setupServer(...handlers)
  const origin = globalThis.location?.origin ?? 'http://localhost'
  const f0 = globalThis.fetch
  beforeAll(() => {
    server.listen({ onUnhandledRequest: 'error' })
    const f = globalThis.fetch
    globalThis.fetch = ((u: RequestInfo | URL, init?: RequestInit) => f(typeof u === 'string' && u.startsWith('/') ? origin + u : u, init)) as typeof fetch
    db.authed = true
  })
  afterAll(() => { server.close(); globalThis.fetch = f0 })

  const waitJob = async (ref: JobRef) => {
    for (let i = 0; i < 100; i++) {
      const j = db.jobs.find((x) => x.id === ref.job_id)!
      if (['succeeded', 'failed'].includes(j.status)) return j
      await new Promise((r) => setTimeout(r, 50))
    }
    throw new Error('job timeout')
  }

  it('targets CRUD keeps tokens write-only', async () => {
    const t = await cvApi.createTarget({ name: 'x', kind: 'onprem', server: '10.0.0.9', verify_certs: false, token: 'secret' })
    expect(t).toMatchObject({ has_token: true, has_onboarding_token: false })
    expect(JSON.stringify(t)).not.toContain('secret')
    const u = await cvApi.updateTarget(t.id, { onboarding_token: 'ob', onboarding_token_expires_at: '2030-01-01T00:00:00Z' })
    expect(u).toMatchObject({ has_token: true, has_onboarding_token: true, onboarding_token_meta: { expires_at: '2030-01-01T00:00:00Z' } })
    expect((await cvApi.testTarget(t.id)).ok).toBe(true)
    await cvApi.removeTarget(t.id)
    expect((await cvApi.targets()).some((x) => x.id === t.id)).toBe(false)
  })

  it('serves seeded status and updates it from onboard / deploy jobs', async () => {
    const lab = (await labsApi.list()).find((l) => l.name === 'evpn-lab')!
    const st = await cvApi.status(lab.id)
    expect(st.target?.name).toBe('cvaas-eu')
    expect(st.onboarding!.nodes.some((n) => !n.streaming)).toBe(true)
    expect(st.last_deploy!.warnings!.length).toBeGreaterThan(0)
    const onboard = await waitJob(await cvApi.onboard(lab.id))
    expect(onboard.status).toBe('succeeded')
    expect((await cvApi.status(lab.id)).onboarding!.nodes.every((n) => n.streaming)).toBe(true)
    const deploy = await waitJob(await cvApi.deploy(lab.id, { submit_workspace: true, run_change_control: true, skip_missing_devices: true, strict_tags: false }))
    expect(deploy.status).toBe('succeeded')
    const last = (await cvApi.status(lab.id)).last_deploy!
    expect(last.change_control?.state).toBe('completed')
    expect(last.warnings).toEqual([])
  }, 20_000)

  it('guards: binding needs mode != none, onboard needs a deployed lab', async () => {
    const draft = await labsApi.create({ name: 'cv-draft', template: 'single-dc-l3ls', host_id: 'host-lab1', knobs: {} })
    const e1 = (await labsApi.update(draft.id, { cv_target_id: 'cvt-cvaas' }).catch((e: ApiError) => e)) as ApiError
    expect(e1.status).toBe(409)
    await labsApi.update(draft.id, { knobs: { cloudvision: { ...CV_KNOB_DEFAULT, mode: 'cvaas' } } })
    expect((await labsApi.update(draft.id, { cv_target_id: 'cvt-cvaas' })).cv_target_id).toBe('cvt-cvaas')
    const e2 = (await cvApi.onboard(draft.id).catch((e: ApiError) => e)) as ApiError
    expect(e2.status).toBe(409)
  })
})
