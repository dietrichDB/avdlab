import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { setupServer } from 'msw/node'
import { handlers } from '../mocks/handlers'
import { db } from '../mocks/data'
import { JobDrawerProvider } from '../components/JobDrawer'
import LabDetailPage from '../pages/lab/LabDetail'
import HostDetailPage from '../pages/HostDetail'
import LabsPage from '../pages/Labs'
import ImagesPage from '../pages/Images'

const server = setupServer(...handlers)
const origin = globalThis.location?.origin ?? 'http://localhost'
beforeAll(() => {
  server.listen({ onUnhandledRequest: 'bypass' })
  const f = globalThis.fetch
  globalThis.fetch = ((u: RequestInfo | URL, init?: RequestInit) => f(typeof u === 'string' && u.startsWith('/') ? origin + u : u, init)) as typeof fetch
  db.authed = true
})
afterAll(() => server.close())

function mount(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <JobDrawerProvider>
          <Routes>
            <Route path="/labs" element={<LabsPage />} />
            <Route path="/images" element={<ImagesPage />} />
            <Route path="/labs/:id/:tab" element={<LabDetailPage />} />
            <Route path="/hosts/:id" element={<HostDetailPage />} />
          </Routes>
        </JobDrawerProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}
const evpn = () => db.labs.find((l) => l.name === 'evpn-lab')!

describe('phase 3 pages (smoke, against MSW)', () => {
  it('clients tab shows the plan and the last ping run', async () => {
    mount(`/labs/${evpn().id}/clients`)
    await waitFor(() => expect(screen.getAllByTestId('client-row')).toHaveLength(3))
    await waitFor(() => expect(screen.getByTestId('traffic-runs')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Show'))
    await waitFor(() => expect(screen.getByTestId('ping-matrix')).toBeInTheDocument())
    expect(screen.getAllByTestId('ping-cell').filter((c) => c.dataset.level === 'down')).toHaveLength(1)
  })

  it('devices tab shows running config and diff', async () => {
    mount(`/labs/${evpn().id}/devices`)
    await waitFor(() => expect(screen.getAllByTestId('device-item').length).toBeGreaterThan(5))
    await waitFor(() => expect(screen.getByTestId('running-config').textContent).toContain('hostname dc1-spine1'))
    fireEvent.click(screen.getByTestId('diff-toggle'))
    await waitFor(() => expect(screen.getByTestId('config-identical')).toBeInTheDocument())
  })

  it('git tab shows status and open links', async () => {
    mount(`/labs/${evpn().id}/git`)
    await waitFor(() => expect(screen.getByTestId('git-sync-label').textContent).toBe('2 ahead'))
    await waitFor(() => expect(screen.getByTestId('open-links')).toBeInTheDocument())
    expect(screen.getByTestId('git-push')).not.toBeDisabled()
  })

  it('host detail renders tables', async () => {
    mount('/hosts/host-lab1')
    await waitFor(() => expect(screen.getByTestId('processes')).toBeInTheDocument())
    await waitFor(() => expect(screen.getByTestId('containers').textContent).toContain('evpn-lab'))
    await waitFor(() => expect(screen.getByTestId('host-charts')).toBeInTheDocument(), { timeout: 5000 })
  })

  it('labs page import menu and images arch warning', async () => {
    mount('/labs')
    fireEvent.click(await screen.findByTestId('import-menu'))
    fireEvent.click(screen.getByText('Clone from git'))
    expect(screen.getByTestId('import-git-form')).toBeInTheDocument()
  })

  it('images page flags the arm64 image on x86_64 hosts', async () => {
    mount('/images')
    await waitFor(() => expect(screen.getAllByTestId('arch-mismatch').length).toBeGreaterThan(0))
    expect(screen.getAllByTestId('arch-mismatch')[0].textContent).toContain('host is x86_64, image is arm64')
  })
})

describe('fake terminal websocket', () => {
  it('prints a banner, echoes binary input and acknowledges resize frames', async () => {
    const ws = new WebSocket(`ws://localhost/api/labs/${evpn().id}/nodes/dc1-leaf1a/terminal?shell=cli&cols=80&rows=24`)
    ws.binaryType = 'arraybuffer'
    const dec = new TextDecoder()
    let out = ''
    ws.onmessage = (e) => { out += typeof e.data === 'string' ? e.data : dec.decode(e.data as ArrayBuffer) }
    await new Promise<void>((r) => { ws.onopen = () => r() })
    ws.send(new TextEncoder().encode('show ver\r'))
    ws.send(JSON.stringify({ resize: { cols: 100, rows: 30 } }))
    await waitFor(() => expect(out).toContain('resized to 100x30'))
    expect(out).toContain('docker exec -it dc1-leaf1a Cli (80x24)')
    expect(out).toContain('show ver')
    expect(out).toContain('dc1-leaf1a#')
    ws.close()
  })
})

describe('incremental push (0.10.0)', () => {
  it('runtime tab shows changed devices and pushes only those', async () => {
    mount(`/labs/${evpn().id}/runtime`)
    const card = await screen.findByTestId('push-configs')
    await waitFor(() => expect(within(card).getByTestId('push-count').textContent).toContain('2 of 6'))
    expect(card.textContent).toContain('dc1-leaf1a, dc1-leaf1b')
    fireEvent.click(within(card).getByTestId('push-changed'))
    await waitFor(() => expect(db.jobs.some((j) => j.type === 'lab.push_configs')).toBe(true))
    await waitFor(() => expect(within(screen.getByTestId('push-configs')).getByTestId('push-count').textContent).toContain('in sync'), { timeout: 6000 })
  })
})

describe('ANTA security advisories (0.10.0)', () => {
  it('validation tab runs anta psirt and shows the assessment table', async () => {
    mount(`/labs/${evpn().id}/validation`)
    const card = await screen.findByTestId('psirt-card')
    fireEvent.change(within(card).getByTestId('psirt-tests'), { target: { value: 'sa117, sa140' } })
    fireEvent.click(within(card).getByTestId('psirt-run'))
    await waitFor(() => expect(db.jobs.some((j) => j.type === 'lab.psirt')).toBe(true))
    expect(db.jobs.find((j) => j.type === 'lab.psirt')!.params).toEqual({ tests: ['SA117', 'SA140'] })
    await waitFor(() => expect(screen.getAllByTestId('psirt-row')).toHaveLength(4), { timeout: 6000 })
    expect(screen.getByTestId('psirt-summary').textContent).toContain('affected 1')
    expect(screen.getAllByTestId('psirt-row')[0].dataset.result).toBe('affected')
    fireEvent.click(within(screen.getAllByTestId('psirt-row')[0]).getByText('details'))
    expect(screen.getByTestId('psirt-detail').textContent).toContain('Upgrade to 4.34.3F')
    expect(screen.getByTestId('psirt-md')).toHaveAttribute('href', `/api/labs/${evpn().id}/psirt/report.md`)
  })
})

