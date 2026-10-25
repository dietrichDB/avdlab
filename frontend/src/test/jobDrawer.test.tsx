import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { JobDrawer } from '../components/JobDrawer'
import { LogPane } from '../components/LogPane'

class FakeWS {
  static last: FakeWS | null = null
  url: string
  onopen: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  constructor(url: string) { this.url = url; FakeWS.last = this; setTimeout(() => this.onopen?.(), 0) }
  send() {}
  close() { this.onclose?.() }
  push(frame: unknown) { this.onmessage?.({ data: JSON.stringify(frame) }) }
}

let status = 'running'
beforeEach(() => {
  status = 'running'
  vi.stubGlobal('WebSocket', FakeWS as unknown as typeof WebSocket)
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const body = url.endsWith('/log') ? { log: '' } : {
      id: 'job1', type: 'host.probe', status, lab_id: null, host_id: 'h1', params: {}, result: {}, artefacts: [], error: null,
      created_at: new Date().toISOString(), started_at: new Date().toISOString(), ended_at: null,
    }
    return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })
  }))
})
afterEach(() => vi.unstubAllGlobals())

function renderDrawer() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}><MemoryRouter><JobDrawer jobId="job1" onClose={() => undefined} /></MemoryRouter></QueryClientProvider>,
  )
}

describe('job drawer', () => {
  it('opens the stream socket for the job', async () => {
    renderDrawer()
    await waitFor(() => expect(FakeWS.last?.url).toMatch(/\/api\/jobs\/job1\/stream$/))
  })

  it('renders streamed {line} frames in order and finishes on {done}', async () => {
    renderDrawer()
    await waitFor(() => expect(FakeWS.last).not.toBeNull())
    await waitFor(() => expect(screen.getByText('host.probe')).toBeInTheDocument())
    act(() => {
      FakeWS.last!.push({ line: '[apply] docker' })
      FakeWS.last!.push({ line: '[ok] docker' })
      FakeWS.last!.push({ line: '[FAIL] sysctls' })
    })
    await waitFor(() => expect(screen.getByText('[FAIL] sysctls')).toBeInTheDocument())
    const rows = screen.getByTestId('log-pane').querySelectorAll('div')
    expect([...rows].map((r) => r.textContent?.replace(/^\s*\d+/, ''))).toEqual(['[apply] docker', '[ok] docker', '[FAIL] sysctls'])
    expect(screen.getByText('[ok] docker').className).toContain('emerald')
    expect(screen.getByText('[FAIL] sysctls').className).toContain('red')
    status = 'succeeded'
    act(() => FakeWS.last!.push({ done: true }))
    await waitFor(() => expect(screen.getByText('succeeded')).toBeInTheDocument())
    expect(screen.queryByText('Cancel')).not.toBeInTheDocument()
  })
})

describe('log pane', () => {
  it('numbers lines and colours by marker', () => {
    render(<LogPane lines={['[skip] base packages', 'plain', 'WARN: low memory']} />)
    expect(screen.getByText('[skip] base packages').className).toContain('zinc-500')
    expect(screen.getByText('WARN: low memory').className).toContain('amber')
    expect(screen.getByTestId('log-pane').textContent).toContain('   2plain')
  })
  it('shows a placeholder when empty', () => {
    render(<LogPane lines={[]} />)
    expect(screen.getByText('(no output yet)')).toBeInTheDocument()
  })
})
