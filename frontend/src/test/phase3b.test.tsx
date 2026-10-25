import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { setupServer } from 'msw/node'
import { handlers } from '../mocks/handlers'
import { db, type MockJob } from '../mocks/data'
import { JobDrawerProvider } from '../components/JobDrawer'
import { JobsTable } from '../components/JobsTable'
import { DeviceTagsEditor, type DeviceTags } from '../components/DeviceTagsEditor'
import { isKillable } from '../components/KillJobButton'
import { jobsApi } from '../api/jobs'
import LabDetailPage from '../pages/lab/LabDetail'
import HostDetailPage from '../pages/HostDetail'

const server = setupServer(...handlers)
const origin = globalThis.location?.origin ?? 'http://localhost'
beforeAll(() => {
  server.listen({ onUnhandledRequest: 'bypass' })
  const f = globalThis.fetch
  globalThis.fetch = ((u: RequestInfo | URL, init?: RequestInit) => f(typeof u === 'string' && u.startsWith('/') ? origin + u : u, init)) as typeof fetch
  db.authed = true
})
afterAll(() => server.close())

function wrap(ui: React.ReactNode, path = '/') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <JobDrawerProvider>{ui}</JobDrawerProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}
function mount(path: string) {
  return wrap(
    <Routes>
      <Route path="/labs/:id/:tab" element={<LabDetailPage />} />
      <Route path="/hosts/:id" element={<HostDetailPage />} />
    </Routes>,
    path,
  )
}
const evpn = () => db.labs.find((l) => l.name === 'evpn-lab')!

describe('device tags editor', () => {
  it('round-trips {hostname: {label: value}} through the table', () => {
    const seen: DeviceTags[] = []
    function Harness() {
      const [v, setV] = useState<DeviceTags>({ 'dc1-leaf1a': { rack: 'r1' } })
      return <DeviceTagsEditor value={v} hostnames={['dc1-leaf1a', 'dc1-leaf1b']} onChange={(n) => { seen.push(n); setV(n) }} />
    }
    render(<Harness />)
    expect(screen.getByLabelText('dc1-leaf1a rack')).toHaveValue('r1')
    fireEvent.change(screen.getByLabelText('New tag label'), { target: { value: 'env' } })
    fireEvent.click(screen.getByText('Add tag column'))
    fireEvent.change(screen.getByLabelText('dc1-leaf1b env'), { target: { value: 'dev' } })
    expect(seen[seen.length - 1]).toEqual({ 'dc1-leaf1a': { rack: 'r1' }, 'dc1-leaf1b': { env: 'dev' } })
    fireEvent.change(screen.getByLabelText('dc1-leaf1a rack'), { target: { value: '' } })
    expect(seen[seen.length - 1]).toEqual({ 'dc1-leaf1b': { env: 'dev' } })
    fireEvent.click(screen.getByLabelText('Remove tag env'))
    expect(seen[seen.length - 1]).toEqual({})
  })

  it('saves from Settings and shows merged tags on the Overview devices table', async () => {
    const lab = evpn()
    const { unmount } = mount(`/labs/${lab.id}/settings`)
    const editor = await screen.findByTestId('device-tags-editor')
    await waitFor(() => expect(within(editor).getByText('dc1-leaf1a')).toBeInTheDocument()) // rows from GET /devices
    for (const col of ['rack', 'owner']) {
      fireEvent.change(screen.getByLabelText('New tag label'), { target: { value: col } })
      fireEvent.click(screen.getByText('Add tag column'))
    }
    fireEvent.change(screen.getByLabelText('dc1-leaf1a rack'), { target: { value: 'r1' } })
    fireEvent.change(screen.getByLabelText('dc1-leaf1a owner'), { target: { value: 'me' } })
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(evpn().knobs?.device_tags).toEqual({ 'dc1-leaf1a': { rack: 'r1', owner: 'me' } }), { timeout: 5000 })
    unmount()
    mount(`/labs/${lab.id}/overview`)
    const cell = await screen.findByTestId('tags-dc1-leaf1a')
    expect(cell.textContent).toContain('rack:r1')
    expect(cell.textContent).toContain('owner:me') // per-device wins over the global owner=netops
    expect(cell.textContent).toContain('lab:evpn-lab')
    expect(screen.getByTestId('tags-dc1-leaf1b').textContent).toContain('owner:netops')
  })
})

const job = (id: string, status: MockJob['status']): MockJob => ({
  id, type: 'lab.deploy', status, lab_id: null, host_id: null, params: {}, result: {}, artefacts: [], error: null,
  created_at: new Date().toISOString(), started_at: null, ended_at: null, log: [],
})

describe('kill job', () => {
  it('shows Kill only for queued/running jobs and confirms before cancelling', async () => {
    expect(['queued', 'running', 'succeeded', 'failed', 'cancelled'].map(isKillable)).toEqual([true, true, false, false, false])
    const spy = vi.spyOn(jobsApi, 'cancel').mockResolvedValue({ cancelled: true, was_running: true })
    wrap(<JobsTable jobs={[job('j-q', 'queued'), job('j-r', 'running'), job('j-s', 'succeeded'), job('j-f', 'failed'), job('j-c', 'cancelled')]} />)
    const kills = screen.getAllByRole('button', { name: 'Kill' })
    expect(kills).toHaveLength(2)
    const runningRow = screen.getByText('j-r').closest('tr')!
    fireEvent.click(within(runningRow).getByRole('button', { name: 'Kill' }))
    expect(spy).not.toHaveBeenCalled() // first click only arms the confirmation
    fireEvent.click(within(runningRow).getByRole('button', { name: 'Kill job?' }))
    await waitFor(() => expect(spy).toHaveBeenCalledWith('j-r'))
    spy.mockRestore()
  })
})

describe('kill container', () => {
  it('requires typing the name for lab containers, then refreshes the list', async () => {
    mount('/hosts/host-lab1')
    const table = await screen.findByTestId('containers')
    await waitFor(() => expect(table.textContent).toContain('evpn-lab'))
    const labRow = within(table).getAllByRole('row').find((r) => r.textContent?.includes('evpn-lab'))!
    const name = labRow.querySelector('td')!.textContent!
    fireEvent.click(within(labRow).getByRole('button', { name: 'Kill' }))
    const dialog = await screen.findByTestId('container-action')
    const confirm = within(dialog).getByRole('button', { name: 'Kill (rm -f)' })
    expect(confirm).toBeDisabled()
    fireEvent.change(within(dialog).getByLabelText('Container name'), { target: { value: 'nope' } })
    expect(confirm).toBeDisabled()
    fireEvent.change(within(dialog).getByLabelText('Container name'), { target: { value: name } })
    expect(confirm).not.toBeDisabled()
    fireEvent.click(confirm)
    await waitFor(() => expect(screen.queryByTestId('container-action')).not.toBeInTheDocument())
    await waitFor(() => expect(screen.getByTestId('container-result').textContent).toContain(`docker rm -f ${name}: done`))
    await waitFor(() => expect(within(screen.getByTestId('containers')).queryByText(name)).not.toBeInTheDocument())

    // a non-lab container: Stop needs no typed confirmation
    const regRow = within(screen.getByTestId('containers')).getByText('registry').closest('tr')!
    fireEvent.click(within(regRow).getByRole('button', { name: 'Stop' }))
    const d2 = await screen.findByTestId('container-action')
    expect(within(d2).queryByLabelText('Container name')).not.toBeInTheDocument()
    fireEvent.click(within(d2).getByRole('button', { name: 'Stop' }))
    await waitFor(() => expect(screen.getByTestId('container-result').textContent).toContain('docker stop registry: done'))
  })
})
