import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import type { ReactElement } from 'react'
import { setupServer } from 'msw/node'
import { handlers } from '../mocks/handlers'
import { authDb, db, reset as resetDb, resetAuth } from '../mocks/data'
import { aiDb, resetAi } from '../mocks/handlersAi'
import { AuthProvider } from '../hooks/useAuth'
import { JobDrawerProvider } from '../components/JobDrawer'
import { TourProvider } from '../components/learn/Tour'
import DesignerPanel, { fmtValue, opLabel } from '../components/ai/DesignerPanel'
import { AiLinkSection, AiSharedSection } from '../components/ai/AiLinkForm'
import NewLabPage from '../pages/NewLab'
import LabDetailPage from '../pages/lab/LabDetail'

const server = setupServer(...handlers)
const origin = globalThis.location?.origin ?? 'http://localhost'
beforeAll(() => {
  server.listen({ onUnhandledRequest: 'bypass' })
  const f = globalThis.fetch
  globalThis.fetch = ((u: RequestInfo | URL, init?: RequestInit) => f(typeof u === 'string' && u.startsWith('/') ? origin + u : u, init)) as typeof fetch
})
afterAll(() => server.close())
beforeEach(() => { resetDb(); resetAuth(); resetAi(); db.authed = true; authDb.currentId = 'usr-admin'; localStorage.clear() })

function mount(path: string, element: ReactElement, route: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <AuthProvider><JobDrawerProvider><TourProvider>
          <Routes>
            <Route path={route} element={element} />
            <Route path="/account" element={<div data-testid="account-page" />} />
          </Routes>
        </TourProvider></JobDrawerProvider></AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const labId = () => db.labs.find((l) => l.name === 'dc1-lab')!.id
const send = async (text: string) => {
  fireEvent.change(await screen.findByTestId('ai-prompt'), { target: { value: text } })
  fireEvent.click(screen.getByTestId('ai-send'))
}

describe('AI designer panel', () => {
  it('shows the link call-to-action when no model is linked', async () => {
    resetAi(false)
    mount('/x', <DesignerPanel open onClose={() => {}} labId={labId()} />, '/x')
    const cta = await screen.findByTestId('ai-cta')
    expect(cta).toHaveTextContent('Link your Claude / OpenAI / local model')
    expect(screen.queryByTestId('ai-prompt')).not.toBeInTheDocument()
    fireEvent.click(within(cta).getByTestId('ai-cta-link'))
    await screen.findByTestId('account-page')
  })

  it('a shared default counts as linked', async () => {
    resetAi(false)
    aiDb.shared = { ...aiDb.shared, ai_default_provider: 'openai', ai_default_model: 'llama3.1', ai_shared_allowed: true, configured: true }
    mount('/x', <DesignerPanel open onClose={() => {}} labId={labId()} />, '/x')
    await screen.findByTestId('ai-prompt')
    expect(screen.queryByTestId('ai-cta')).not.toBeInTheDocument()
    expect(await screen.findByText(/shared · llama3.1/)).toBeInTheDocument()
  })

  it('renders a validated proposal and Apply calls the endpoint (Topology tab)', async () => {
    const id = labId()
    mount(`/labs/${id}/topology`, <LabDetailPage />, '/labs/:id/:tab')
    fireEvent.click(await screen.findByTestId('ai-open'))
    expect(await screen.findByText(/The AI only proposes AVD inputs/)).toBeInTheDocument() // beginner hint
    await send('add a third leaf pair, tenant ACME with VLANs 110/120 and one server on pair 1')
    const card = await screen.findByTestId('ai-proposal')
    expect(within(card).getByTestId('ai-valid')).toHaveTextContent('validated')
    expect(within(card).getByTestId('ai-summary')).toHaveTextContent('third MLAG leaf pair')
    expect(within(card).getByTestId('ai-knob-leaf_pairs')).toHaveTextContent('leaf_pairs: 2 → 3')
    expect(within(card).getByTestId('ai-ops')).toHaveTextContent('add server srv-ai-1 → dc1-leaf1a:Ethernet7, dc1-leaf1b:Ethernet7')
    expect(within(card).getByTestId('ai-assumptions')).toHaveTextContent('VRF ACME_PROD')
    expect(within(card).getByTestId('ai-changed-files')).toHaveTextContent('group_vars/CONNECTED_ENDPOINTS.yml')
    fireEvent.click(within(card).getByTestId('ai-diff-toggle'))
    expect(within(card).getByTestId('diff-viewer')).toHaveTextContent('srv-ai-1')
    expect(aiDb.designCalls[0].lab_id).toBe(id)
    fireEvent.click(within(card).getByTestId('ai-apply'))
    await waitFor(() => expect(within(card).getByTestId('ai-status')).toHaveTextContent('Applied'))
    expect(aiDb.applyCalls).toHaveLength(1)
    expect(aiDb.applyCalls[0]).toMatchObject({ lab_id: id, knobs: { leaf_pairs: 3 }, topology_ops: [{ op: 'add_endpoint', name: 'srv-ai-1' }] })
    const l = db.labs.find((x) => x.id === id)!
    expect(l.knobs?.leaf_pairs).toBe(3)
    expect((l.knobs?.servers as { name: string }[]).some((s) => s.name === 'srv-ai-1')).toBe(true)
  })

  it('question round-trip sends the history; invalid proposals cannot be applied; discard', async () => {
    const id = labId()
    mount(`/labs/${id}/settings`, <LabDetailPage />, '/labs/:id/:tab')
    fireEvent.click(await screen.findByTestId('ai-open'))
    await send('add a server')
    expect(await screen.findByTestId('ai-question')).toHaveTextContent('Which leaf pair')
    await send('leaf pair 1, VLANs 110 and 120')
    await screen.findByTestId('ai-proposal')
    const second = aiDb.designCalls[1]
    expect(second.history).toEqual([
      { role: 'user', content: 'add a server' },
      { role: 'assistant', content: 'Which leaf pair should the server connect to, and which VLANs does it need?' },
    ])
    expect(second.prompt).toBe('leaf pair 1, VLANs 110 and 120')
    fireEvent.click(screen.getByTestId('ai-discard'))
    expect(await screen.findByTestId('ai-status')).toHaveTextContent('Discarded')
    await send('make it invalid')
    await waitFor(() => expect(screen.getAllByTestId('ai-proposal')).toHaveLength(2))
    const bad = screen.getAllByTestId('ai-proposal')[1]
    expect(within(bad).getByTestId('ai-valid')).toHaveTextContent('invalid')
    expect(within(bad).getByTestId('ai-violations')).toHaveTextContent('must be <= 4')
    expect(within(bad).getByTestId('ai-apply')).toBeDisabled()
    expect(aiDb.designCalls[2].history).toHaveLength(4)
  })

  it('New lab wizard: a proposal fills the knob form', async () => {
    mount('/labs/new', <NewLabPage />, '/labs/new')
    fireEvent.click(within(await screen.findByTestId('template-single-dc-l3ls')).getByText('Use'))
    await screen.findByTestId('knob-form')
    const spines = document.querySelector('input[name="spine_count"]') as HTMLInputElement
    expect(spines.value).toBe('2')
    fireEvent.click(screen.getByTestId('ai-open'))
    await send('3 spines, 4 leaf pairs with MLAG, tenant ACME with VLANs 110/120, CloudVision cvaas')
    const card = await screen.findByTestId('ai-proposal')
    expect(screen.queryByTestId('ai-apply')).not.toBeInTheDocument()
    expect(aiDb.designCalls[0]).toMatchObject({ template: 'single-dc-l3ls' })
    expect(aiDb.designCalls[0].current_knobs?.spine_count).toBe(2)
    fireEvent.click(within(card).getByTestId('ai-use-knobs'))
    expect(within(card).getByTestId('ai-status')).toHaveTextContent('Knobs copied')
    await waitFor(() => expect(spines.value).toBe('3'))
    expect((document.querySelector('input[name="leaf_pairs"]') as HTMLInputElement).value).toBe('4')
    expect(aiDb.applyCalls).toHaveLength(0)
  })
})

describe('AI settings', () => {
  it('account: link a model (key write-only), test, remove', async () => {
    resetAi(false)
    mount('/account', <AiLinkSection />, '/account')
    expect(await screen.findByTestId('ai-link-status')).toHaveTextContent('not linked')
    fireEvent.change(screen.getByTestId('ai-provider'), { target: { value: 'openai' } })
    fireEvent.change(screen.getByTestId('ai-base-url'), { target: { value: 'http://ollama:11434/v1' } })
    fireEvent.change(screen.getByTestId('ai-model'), { target: { value: 'llama3.1' } })
    fireEvent.click(screen.getByTestId('ai-test'))
    expect(await screen.findByTestId('ai-test-result')).toHaveTextContent('connected · 2 model(s)')
    fireEvent.change(screen.getByTestId('ai-api-key'), { target: { value: 'sk-local' } })
    fireEvent.click(screen.getByTestId('ai-save'))
    await screen.findByTestId('ai-saved')
    expect(screen.getByTestId('ai-link-status')).toHaveTextContent('linked: openai · llama3.1')
    expect((screen.getByTestId('ai-api-key') as HTMLInputElement).value).toBe('')
    expect(aiDb.link).toMatchObject({ provider: 'openai', base_url: 'http://ollama:11434/v1', model: 'llama3.1', has_api_key: true })
    fireEvent.click(screen.getByText('Remove'))
    fireEvent.click(await screen.findByText('Remove link and key?'))
    await waitFor(() => expect(screen.getByTestId('ai-link-status')).toHaveTextContent('not linked'))
  })

  it('admin: shared default section saves', async () => {
    mount('/admin/settings', <AiSharedSection />, '/admin/settings')
    fireEvent.change(await screen.findByTestId('ai-shared-provider'), { target: { value: 'anthropic' } })
    fireEvent.change(screen.getByTestId('ai-shared-model'), { target: { value: 'claude-sonnet-4-5' } })
    fireEvent.change(screen.getByTestId('ai-shared-key'), { target: { value: 'sk-ant-shared' } })
    fireEvent.click(screen.getByTestId('ai-shared-allowed'))
    fireEvent.click(screen.getByTestId('ai-shared-save'))
    await screen.findByTestId('ai-shared-saved')
    expect(aiDb.shared).toMatchObject({ ai_default_provider: 'anthropic', ai_default_model: 'claude-sonnet-4-5', ai_shared_allowed: true, has_ai_default_api_key: true, configured: true })
    fireEvent.click(screen.getByTestId('ai-shared-test'))
    expect(await screen.findByTestId('ai-shared-test-result')).toHaveTextContent('connected')
  })

  it('formats values and ops', () => {
    expect(fmtValue(undefined)).toBe('—')
    expect(fmtValue({ mode: 'cvaas' })).toBe('{"mode":"cvaas"}')
    expect(fmtValue('x'.repeat(200)).length).toBe(140)
    expect(opLabel({ op: 'set_spine_count', n: 3 })).toBe('spines = 3')
    expect(opLabel({ op: 'remove_last_leaf_pair' })).toBe('remove last leaf pair')
  })
})

describe('new lab from a sentence (0.10.0)', () => {
  it('picks the template, fills the knobs and jumps to step 2 with a note', async () => {
    aiDb.link = { provider: 'anthropic', base_url: '', model: 'claude-sonnet-4-5', has_api_key: true, created_at: '', updated_at: '' }
    mount('/labs/new', <NewLabPage />, '/labs/new')
    const card = await screen.findByTestId('ai-from-scratch')
    fireEvent.change(within(card).getByTestId('ai-scratch-prompt'), { target: { value: '3 leaf pairs for ACME' } })
    fireEvent.click(within(card).getByTestId('ai-scratch-go'))
    const note = await screen.findByTestId('ai-from-scratch-note')
    expect(note.textContent).toContain('AI picked single-dc-l3ls')
    expect(aiDb.designCalls[aiDb.designCalls.length - 1]).toMatchObject({ prompt: '3 leaf pairs for ACME' })
    expect((screen.getByDisplayValue('ai-lab') as HTMLInputElement).value).toBe('ai-lab')
  })
})
