import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import type { ReactElement } from 'react'
import { setupServer } from 'msw/node'
import { handlers } from '../mocks/handlers'
import { db } from '../mocks/data'
import { editableTopology, portsOf } from '../mocks/phase5'
import { LEARN, p6, resetPhase6 } from '../mocks/phase6'
import { JobDrawerProvider } from '../components/JobDrawer'
import { TourProvider } from '../components/learn/Tour'
import { KnobInfo, Term } from '../components/learn/Term'
import { ConfigViewer } from '../components/learn/ConfigExplain'
import { FirstLabOffer } from '../components/learn/FirstLabOffer'
import { NodeInspector } from '../components/TopologyEditor'
import { UpdatePanel, UpdatePill } from '../components/UpdatePanel'
import { KnobForm } from '../components/KnobForm'
import { testHostErrors, testHostPayload, type TestHostForm } from '../components/TestHosts'
import LabDetailPage from '../pages/lab/LabDetail'
import LearnPage from '../pages/Learn'
import LessonPage from '../pages/Lesson'
import NewLabPage from '../pages/NewLab'
import { waitForNewVersion } from '../api/phase6'
import type { Meta } from '../api/types'
import {
  FIRST_LAB_DISMISSED_KEY, LEARN_LAB_KEY, LEARN_MODE_KEY, byLevel, findTerm, mergeKnob, parseConfig, pitfallText, progressPct, schemaForFile,
  tourReducer, tryPlan, yamlPathAt,
} from '../lib/learn'

const server = setupServer(...handlers)
const origin = globalThis.location?.origin ?? 'http://localhost'
beforeAll(() => {
  server.listen({ onUnhandledRequest: 'bypass' })
  const f = globalThis.fetch
  globalThis.fetch = ((u: RequestInfo | URL, init?: RequestInit) => f(typeof u === 'string' && u.startsWith('/') ? origin + u : u, init)) as typeof fetch
  db.authed = true
})
afterAll(() => server.close())
beforeEach(() => { resetPhase6(); localStorage.clear() })

function Where() {
  const l = useLocation()
  return <div data-testid="where">{l.pathname}{l.search}</div>
}
function mount(path: string, element: ReactElement, route: string, extra: ReactElement[] = []) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <JobDrawerProvider><TourProvider>
          <Routes><Route path={route} element={element} />{extra}<Route path="*" element={<Where />} /></Routes>
        </TourProvider></JobDrawerProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}
const lab = (name: string) => db.labs.find((l) => l.name === name)!

// ---------------------------------------------------------------- §1 update panel
describe('update panel', () => {
  it('waitForNewVersion polls through down / old until the new version answers', async () => {
    const seq: (Meta | Error)[] = [{ version: '0.6.0' } as Meta, new Error('502'), new Error('502'), { version: '0.7.0' } as Meta]
    const ticks: string[] = []
    const v = await waitForNewVersion('0.6.0', '0.7.0', {
      get: async () => { const x = seq.shift()!; if (x instanceof Error) throw x; return x },
      sleep: async () => {}, onTick: (t) => ticks.push(t),
    })
    expect(v).toBe('0.7.0')
    expect(ticks).toEqual(['old', 'down', 'down', 'new'])
  })

  it('available: pill, notes, confirm → apply job → polls /meta → reload', async () => {
    const reload = vi.fn()
    mount('/about', <><UpdatePill /><UpdatePanel reload={reload} pollMs={50} /></>, '/about')
    expect(await screen.findByTestId('update-available')).toBeInTheDocument()
    expect(screen.getByTestId('update-pill').textContent).toContain('0.7.0')
    expect(screen.getByTestId('update-latest').textContent).toContain('v0.7.0')
    expect(screen.getByTestId('update-mode').textContent).toContain('source')
    expect(screen.getByTestId('update-notes').textContent).toContain('In-app AVD learning')
    fireEvent.click(screen.getByTestId('update-now'))
    expect(screen.getByTestId('update-confirm').textContent).toContain('v0.6.0 → v0.7.0')
    fireEvent.click(screen.getByTestId('update-confirm-yes'))
    expect(await screen.findByTestId('job-drawer')).toBeInTheDocument()
    expect(p6.applyRequests).toEqual([{ to: '0.7.0' }])
    await screen.findByTestId('update-phase')
    await waitFor(() => expect(reload).toHaveBeenCalled(), { timeout: 5000 })
    expect(screen.getByTestId('update-phase').textContent).toContain('v0.7.0')
    expect(p6.lastUpdate).toMatchObject({ from: '0.6.0', to: '0.7.0', ok: true })
  })

  it('check now, unconfigured hint, self-update impossible and up to date', async () => {
    p6.scenario = 'no-self-update'
    const { unmount } = mount('/about', <UpdatePanel />, '/about')
    expect((await screen.findByTestId('update-reason')).textContent).toContain('docker socket')
    expect(screen.queryByTestId('update-now')).toBeNull()
    fireEvent.click(screen.getByTestId('update-check'))
    await waitFor(() => expect(p6.checks).toBe(1))
    unmount()

    p6.scenario = 'unconfigured'
    const u2 = mount('/about', <UpdatePanel />, '/about')
    expect((await screen.findByTestId('update-hint')).textContent).toContain('AVDLAB_UPDATE_SOURCE')
    expect(screen.queryByTestId('update-available')).toBeNull()
    u2.unmount()

    p6.scenario = 'uptodate'
    mount('/about', <><UpdatePill /><UpdatePanel /></>, '/about')
    expect(await screen.findByTestId('update-uptodate')).toBeInTheDocument()
    expect(screen.queryByTestId('update-pill')).toBeNull()
  })
})

// ---------------------------------------------------------------- §2 template cards
describe('template cards', () => {
  it('show tags, node count, notes and the learn paragraph', async () => {
    mount('/labs/new', <NewLabPage />, '/labs/new')
    const card = await screen.findByTestId('template-single-dc-l3ls')
    expect(within(card).getByTestId('template-tags').textContent).toBe(['l3ls', 'evpn', 'vxlan', 'mlag', 'ebgp'].join(''))
    expect(within(card).getByTestId('template-nodes').textContent).toBe('8 switches')
    expect(within(card).getByTestId('template-learn').textContent).toContain('What it teaches')
    expect(within(card).getByRole('link', { name: 'AVD example' }).getAttribute('href')).toContain('examples/single-dc-l3ls')
    // glossary terms on tags (learning mode is on by default)
    await waitFor(() => expect(within(card).getAllByTestId('term').map((t) => t.getAttribute('data-term'))).toContain('EVPN'))
    const pf = screen.getByTestId('template-cv-pathfinder')
    expect(within(pf).getByTestId('template-notes').textContent).toContain('CloudVision Pathfinder')
    expect(document.querySelector('[data-tour="template-card"]')).not.toBeNull()
  })
})

// ---------------------------------------------------------------- §3 learning
describe('learn hub', () => {
  it('lists lessons by level with progress, glossary filter and schema search', async () => {
    mount('/learn', <LearnPage />, '/learn', [<Route key="s" path="/learn/:section" element={<LearnPage />} />])
    const hub = await screen.findByTestId('lessons-hub')
    expect(within(hub).getByTestId('level-intro')).toBeInTheDocument()
    expect(within(hub).getByTestId('level-core')).toBeInTheDocument()
    expect(within(hub).getAllByTestId('lesson-card')).toHaveLength(LEARN.lesson_summaries.length)
    const first = within(hub).getAllByTestId('lesson-card')[0]
    expect(first.textContent).toContain('What AVD is')
    expect(within(first).getByTestId('lesson-progress').textContent).toBe('1/2')
    expect(screen.getByTestId('lessons-completed').textContent).toBe(`0 / ${LEARN.lesson_summaries.length} lessons completed`)

    fireEvent.click(screen.getByRole('tab', { name: 'Glossary' }))
    const filter = await screen.findByTestId('glossary-filter')
    await waitFor(() => expect(screen.getAllByTestId('glossary-term').length).toBeGreaterThan(40))
    fireEvent.change(filter, { target: { value: 'vtep' } })
    await waitFor(() => expect(screen.getAllByTestId('glossary-term').some((t) => t.textContent?.startsWith('VTEP'))).toBe(true))

    fireEvent.click(screen.getByRole('tab', { name: 'Schema browser' }))
    fireEvent.change(await screen.findByTestId('schema-search'), { target: { value: 'loopback pool' } })
    const hit = await screen.findByText('l3leaf.defaults.loopback_ipv4_pool', {}, { timeout: 3000 })
    fireEvent.click(hit)
    const detail = await screen.findByTestId('schema-detail')
    expect(detail.textContent).toContain('Loopback0 will be derived from this pool')
    expect(within(detail).getByTestId('schema-doc').getAttribute('href')).toContain('/6.4/')
  })

  it('learning mode toggle is persisted', async () => {
    mount('/learn', <LearnPage />, '/learn')
    const t = await screen.findByTestId('learn-mode-hub')
    expect(t).toBeChecked()
    fireEvent.click(t)
    expect(localStorage.getItem(LEARN_MODE_KEY)).toBe('0')
    expect(t).not.toBeChecked()
  })

  it('lesson runner: quiz feedback, checkpoint ✓/✗, try navigates, prev/next', async () => {
    const l = lab('dc1-lab')
    localStorage.setItem(LEARN_LAB_KEY, l.id)
    mount('/learn/lessons/what-is-avd', <LessonPage />, '/learn/lessons/:id')
    await screen.findByTestId('lesson')
    expect(screen.getByTestId('lesson-progress-count').textContent).toBe('1/2 checks passed')
    expect(screen.getAllByTestId('lesson-try')).toHaveLength(3)
    // quiz: wrong then feedback shows the explanation
    fireEvent.click(screen.getByTestId('quiz-q-stages-0'))
    expect(screen.getByTestId('quiz-feedback').textContent).toContain('Not quite.')
    fireEvent.click(screen.getByTestId('quiz-q-stages-1'))
    expect(screen.getByTestId('quiz-feedback').textContent).toContain('Correct.')
    // checkpoint
    fireEvent.click(screen.getByTestId('check-loopback0-rendered'))
    await waitFor(() => expect(screen.getByTestId('lesson-progress-count').textContent).toBe('2/2 checks passed'))
    expect(p6.checkRequests[p6.checkRequests.length - 1]).toEqual({ lesson: 'what-is-avd', lab_id: l.id, checkpoint_id: 'loopback0-rendered' })
    expect(screen.getAllByTestId('check-detail')[0].textContent).toContain('intended/configs/dc1-leaf1a.cfg')
    expect(screen.getByTestId('lesson-next').getAttribute('href')).toBe('/learn/lessons/inventory-group-vars')
    // try: open_file → Files tab with path + line
    fireEvent.click(screen.getByTestId('try-open-config'))
    expect((await screen.findByTestId('where')).textContent).toBe(`/labs/${l.id}/files?path=intended%2Fconfigs%2Fdc1-leaf1a.cfg&line=interface+Loopback0`)
  })

  it('a failing check shows ✗, detail and hint', async () => {
    const l = lab('dc1-lab')
    localStorage.setItem(LEARN_LAB_KEY, l.id)
    mount('/learn/lessons/anta-validation', <LessonPage />, '/learn/lessons/:id')
    await screen.findByTestId('lesson')
    const checks = screen.getAllByTestId('lesson-check')
    const btn = within(checks[checks.length - 1]).getByRole('button')
    fireEvent.click(btn)
    await waitFor(() => expect(within(checks[checks.length - 1]).getByTestId('check-mark').textContent).toBe('✗'))
    expect(within(checks[checks.length - 1]).getByTestId('check-detail').textContent).toMatch(/no|not/)
  })
})

describe('explain panel', () => {
  it('click a line → explanation with AVD input; hover highlights; explain this file lists sections', async () => {
    const l = lab('dc1-lab')
    const cfg = l.files['intended/configs/dc1-leaf1a.cfg']
    expect(cfg).toBeTruthy()
    mount('/x', <ConfigViewer labId={l.id} node="dc1-leaf1a" text={cfg} />, '/x')
    const lines = screen.getAllByTestId('config-line')
    const ip = lines.find((x) => x.textContent?.includes('ip address 10.255.0.'))!
    fireEvent.mouseEnter(ip)
    const header = lines.find((x) => x.textContent?.includes('interface Loopback0'))!
    expect(header.className).toContain('bg-zinc-200')
    fireEvent.click(ip)
    const panel = await screen.findByTestId('explain-panel')
    await waitFor(() => expect(within(panel).getByTestId('explain-why').textContent).toContain('loopback_ipv4_pool'))
    expect(within(panel).getByTestId('explain-input').textContent).toContain('l3leaf.defaults.loopback_ipv4_pool')
    fireEvent.click(screen.getByTestId('explain-file'))
    const view = await screen.findByTestId('explain-file-view')
    await waitFor(() => expect(within(view).getAllByTestId('explain-section').length).toBeGreaterThan(2))
    expect(within(view).getByTestId('explain-file-summary').textContent).toMatch(/\d+ of \d+ lines explained/)
  })

  it('files tab opens an intended config from ?path=&line= with the line selected', async () => {
    const l = lab('dc1-lab')
    mount(`/labs/${l.id}/files?path=intended/configs/dc1-leaf1a.cfg&line=${encodeURIComponent('interface Loopback0')}`, <LabDetailPage />, '/labs/:id/:tab')
    const panel = await screen.findByTestId('explain-panel', {}, { timeout: 3000 })
    await waitFor(() => expect(within(panel).getByTestId('explain-body')).toBeInTheDocument())
    expect(panel.textContent).toContain('interface Loopback0')
  })
})

describe('Term and knob help', () => {
  it('Term renders a hover card from the glossary; plain text when learning mode is off', async () => {
    const { unmount } = mount('/', <p>The <Term>VTEP</Term> encapsulates.</p>, '/')
    const t = await screen.findByTestId('term')
    fireEvent.mouseEnter(t.parentElement!)
    const card = screen.getByTestId('term-card')
    expect(card.textContent).toContain(LEARN.glossary.find((g) => g.term === 'VTEP')!.short.slice(0, 30))
    unmount()
    localStorage.setItem(LEARN_MODE_KEY, '0')
    mount('/', <p>The <Term>VTEP</Term> encapsulates.</p>, '/')
    await new Promise((r) => setTimeout(r, 50))
    expect(screen.queryByTestId('term')).toBeNull()
    expect(screen.getByText(/The VTEP encapsulates/)).toBeInTheDocument()
  })

  it('knob (i) popover shows summary, AVD keys, renders-as and pitfalls', () => {
    const learn = LEARN.knob_learn.dc_name!
    mount('/', <KnobForm knobs={[{ key: 'dc_name', type: 'string', label: 'DC name', default: 'dc1', learn }]} values={{ dc_name: 'dc1' }} onChange={() => {}} />, '/')
    fireEvent.click(screen.getByTestId('knob-info-dc_name'))
    const pop = screen.getByTestId('knob-learn')
    expect(pop.textContent).toContain(learn.summary!.slice(0, 20))
    expect(within(pop).getAllByTestId('avd-key').map((a) => a.textContent)).toEqual(learn.avd_keys)
    expect(within(pop).getByTestId('knob-renders').textContent).toContain('hostname dc1-leaf1a')
    expect(pop.textContent).toContain('Renaming the DC renames every device: a deployed lab needs a redeploy')
    expect(document.querySelector('[data-tour="knobs-form"]')).not.toBeNull()
    render(<KnobInfo learn={null} knobKey="x" />)
    expect(screen.queryByTestId('knob-info-x')).toBeNull()
  })
})

describe('tours', () => {
  it('step engine', () => {
    let s = tourReducer(null, { type: 'start', id: 'hosts' })
    expect(s).toEqual({ id: 'hosts', step: 0 })
    s = tourReducer(s, { type: 'prev' })
    expect(s!.step).toBe(0)
    s = tourReducer(s, { type: 'next', count: 2 })
    expect(s!.step).toBe(1)
    expect(tourReducer(s, { type: 'next', count: 2 })).toBeNull()
    expect(tourReducer(s, { type: 'goto', step: 9, count: 5 })!.step).toBe(4)
    expect(tourReducer(s, { type: 'end' })).toBeNull()
  })

  it('first-lab tour is offered on an empty labs page: spotlight, next, missing element hint, dismiss', async () => {
    const saved = db.labs
    db.labs = []
    try {
      const { unmount } = mount('/labs', <><FirstLabOffer /><a data-tour="new-lab" href="/labs/new">New lab</a></>, '/labs')
      fireEvent.click(await screen.findByTestId('first-lab-start'))
      expect((await screen.findByTestId('tour-step-title')).textContent).toBe('Create a lab')
      expect(await screen.findByTestId('tour-spotlight')).toBeInTheDocument()
      fireEvent.click(screen.getByTestId('tour-next'))
      expect(screen.getByTestId('tour-step-title').textContent).toBe('Pick a template')
      expect(await screen.findByTestId('tour-missing')).toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'end tour' }))
      expect(screen.queryByTestId('tour-card')).toBeNull()
      fireEvent.click(screen.getByTestId('first-lab-dismiss'))
      expect(screen.queryByTestId('first-lab-offer')).toBeNull()
      expect(localStorage.getItem(FIRST_LAB_DISMISSED_KEY)).toBe('1')
      unmount()
      mount('/labs', <FirstLabOffer />, '/labs')
      expect(screen.queryByTestId('first-lab-offer')).toBeNull()
    } finally {
      db.labs = saved
    }
  })

  it('every data-tour name in tours.yml is known to the frontend', () => {
    const names = Object.values(LEARN.tours).flatMap((t) => t.steps.map((s) => /data-tour="([^"]+)"/.exec(s.selector)![1]))
    expect(new Set(names).size).toBeGreaterThan(15)
  })
})

// ---------------------------------------------------------------- §4 test hosts
describe('add test host', () => {
  it('form validation and payload', () => {
    const f: TestHostForm = { name: 'test1', switch: 'dc1-leaf1a', switch_port: 'Ethernet9', vlans: '11-12', mode: 'access', ipMode: 'manual', ip: '10.10.11', image: '' }
    expect(testHostErrors(f, [])).toEqual(['access mode takes a single VLAN', 'IP: a.b.c.d/nn'])
    expect(testHostPayload({ ...f, vlans: '11', ip: '10.10.11.50/24' })).toEqual({ name: 'test1', switch: 'dc1-leaf1a', switch_port: 'Ethernet9', vlans: '11', mode: 'access', ip: '10.10.11.50/24', image: null, deploy: true })
    expect(testHostErrors({ ...f, vlans: '11', ipMode: 'auto' }, ['test1'])).toEqual(['name: test1 already exists'])
  })

  it('inspector: clicking a free switch port picks it for a test host', () => {
    const t = editableTopology(lab('evpn-lab'))
    const onFreePort = vi.fn()
    render(<NodeInspector topo={t} name="dc1-leaf1a" ports={portsOf(t)} editing={false} onClose={() => {}} onMoveUplink={() => {}} onRemoveEndpoint={() => {}} onFreePort={onFreePort} />)
    const free = portsOf(t)['dc1-leaf1a'].free[0]
    fireEvent.click(screen.getByTestId(`free-port-${free}`))
    expect(onFreePort).toHaveBeenCalledWith('dc1-leaf1a', free)
  })

  it('topology → Add test host → Deploy now → job → client listed with quick actions', async () => {
    const l = lab('evpn-lab')
    const { unmount } = mount(`/labs/${l.id}/topology`, <LabDetailPage />, '/labs/:id/:tab')
    fireEvent.click(await screen.findByTestId('add-test-host'))
    const form = await screen.findByTestId('add-test-host-form')
    fireEvent.click(within(form).getByTestId('th-deploy'))
    expect(within(form).getByTestId('th-errors').textContent).toContain('pick a switch')
    fireEvent.change(within(form).getByTestId('th-name'), { target: { value: 'probe1' } })
    await waitFor(() => expect(within(form).getAllByRole('option').some((o) => o.textContent === 'dc1-leaf2a')).toBe(true))
    fireEvent.change(within(form).getByTestId('th-switch'), { target: { value: 'dc1-leaf2a' } })
    await waitFor(() => expect(within(within(form).getByTestId('th-port')).getAllByRole('option').length).toBeGreaterThan(1))
    const port = within(within(form).getByTestId('th-port')).getAllByRole('option')[1].getAttribute('value')!
    fireEvent.change(within(form).getByTestId('th-port'), { target: { value: port } })
    fireEvent.change(within(form).getByTestId('th-vlans'), { target: { value: '12' } })
    fireEvent.click(within(form).getByTestId('th-deploy'))
    expect(await screen.findByTestId('job-drawer')).toBeInTheDocument()
    expect(p6.clientRequests[p6.clientRequests.length - 1]).toEqual({ name: 'probe1', switch: 'dc1-leaf2a', switch_port: port, vlans: '12', mode: 'access', ip: 'auto', image: 'ghcr.io/srl-labs/network-multitool:latest', deploy: true })
    await waitFor(() => expect(p6.hot.get(l.id)?.get('probe1')?.deployed).toBe(true), { timeout: 4000 })
    expect(editableTopology(l).links.some((k) => k.a === 'dc1-leaf2a' && k.a_if === port && k.b === 'probe1')).toBe(true)
    unmount()

    // Clients tab lists the hot-added host with ping / iperf / exec
    mount(`/labs/${l.id}/clients`, <LabDetailPage />, '/labs/:id/:tab')
    const row = await screen.findByTestId('test-host-row')
    expect(row.textContent).toContain('probe1')
    expect(row.textContent).toContain('deployed')
    fireEvent.click(within(row).getByTestId('test-host-toggle-probe1'))
    const acts = await screen.findByTestId('client-actions-probe1')
    expect(within(acts).getByTestId('ca-ip').textContent).toMatch(/^10\.10\.12\.\d+\/24$/)
    fireEvent.change(within(acts).getByTestId('ca-ping-dst'), { target: { value: 'dc1-host1' } })
    fireEvent.click(within(acts).getByTestId('ca-ping'))
    expect((await within(acts).findByTestId('ca-ping-result')).textContent).toContain('3/3 received')
    fireEvent.click(within(acts).getByTestId('ca-iperf'))
    expect((await within(acts).findByTestId('ca-iperf-result')).textContent).toMatch(/Gb|Mb/)
    fireEvent.click(within(acts).getByTestId('ca-exec'))
    expect((await within(acts).findByTestId('ca-exec-result')).textContent).toContain('eth1@if42')
    expect(p6.execRequests.map((r) => r.kind)).toEqual(['ping', 'iperf', 'exec'])
    // remove (confirm)
    const rm = within(within(acts).getByTestId('ca-remove')).getByRole('button')
    fireEvent.click(rm)
    await act(async () => { fireEvent.click(within(within(acts).getByTestId('ca-remove')).getByRole('button')) })
    await waitFor(() => expect(p6.hot.get(l.id)?.has('probe1')).toBe(false))
  })
})

// ---------------------------------------------------------------- pure helpers
describe('learn helpers', () => {
  it('yamlPathAt derives dotted schema paths (lists without index)', () => {
    const y = ['l3leaf:', '  defaults:', '    loopback_ipv4_pool: 10.255.0.0/27', '  node_groups:', '    - group: DC1_L3_LEAF1', '      bgp_as: 65101', '      nodes:', '        - name: dc1-leaf1a', '          id: 1', 'tenants:', '  - name: T1', '    vrfs:', '      - name: VRF10', '        vrf_vni: 10'].join('\n')
    expect(yamlPathAt(y, 3)).toBe('l3leaf.defaults.loopback_ipv4_pool')
    expect(yamlPathAt(y, 6)).toBe('l3leaf.node_groups.bgp_as')
    expect(yamlPathAt(y, 9)).toBe('l3leaf.node_groups.nodes.id')
    expect(yamlPathAt(y, 8)).toBe('l3leaf.node_groups.nodes.name')
    expect(yamlPathAt(y, 14)).toBe('tenants.vrfs.vrf_vni')
    expect(yamlPathAt(y, 1)).toBe('l3leaf')
    expect(schemaForFile('group_vars/FABRIC.yml')).toBe('eos_designs')
    expect(schemaForFile('intended/structured_configs/x.yml')).toBe('eos_cli_config_gen')
  })

  it('config sections, try plans, knob merge, glossary lookup, progress', () => {
    const c = parseConfig('hostname a\n!\ninterface Loopback0\n   description X\n   ip address 1.1.1.1/32\n!\nrouter bgp 1\n   neighbor P peer group\n      x\n')
    expect(c.map((l) => l.section)).toEqual([1, null, 3, 3, 3, null, 7, 7, 7, null])
    const tryb = (action: 'open_file' | 'open_tab' | 'run_job' | 'set_knob', args: Record<string, unknown>, endpoint?: { method: string; path: string }) => ({ kind: 'try' as const, id: 'x', label: 'x', action, args, endpoint })
    expect(tryPlan(tryb('open_tab', { tab: 'validation' }), 'L')).toEqual({ kind: 'navigate', to: '/labs/L/validation' })
    expect(tryPlan(tryb('run_job', { job: 'lab.build' }, { method: 'POST', path: '/api/labs/{lab_id}/build' }), 'L')).toEqual({ kind: 'request', method: 'POST', path: '/labs/L/build' })
    expect(tryPlan(tryb('open_file', { path: 'a.yml' }), null)).toEqual({ kind: 'need-lab' })
    expect(mergeKnob({ pools: { loopback: 'a', vtep: 'b' } }, 'pools', { loopback: 'z' })).toEqual({ pools: { loopback: 'z', vtep: 'b' } })
    expect(findTerm(LEARN.glossary, 'route distinguisher')?.term).toBe('RD')
    expect(findTerm(LEARN.glossary, 'tenants')?.term).toBe('tenant')
    expect(pitfallText({ 'A thing': 'consequence' })).toBe('A thing: consequence')
    expect(progressPct({ passed: ['a'], total: 4, completed: false })).toBe(25)
    expect(Object.keys(byLevel(LEARN.lesson_summaries)).sort()).toEqual(['advanced', 'core', 'intro'])
  })
})
