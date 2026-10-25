import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { setupServer } from 'msw/node'
import { handlers } from '../mocks/handlers'
import { db, reset as resetDb } from '../mocks/data'
import { JobDrawerProvider } from '../components/JobDrawer'
import { TourProvider } from '../components/learn/Tour'
import { FeedbackProvider } from '../components/Feedback'
import { UiModeProvider } from '../lib/uiMode'
import GettingStartedPage from '../pages/GettingStarted'
import { feedbackMailto } from '../lib/feedback'

const server = setupServer(...handlers)
const origin = globalThis.location?.origin ?? 'http://localhost'
beforeAll(() => {
  server.listen({ onUnhandledRequest: 'bypass' })
  const f = globalThis.fetch
  globalThis.fetch = ((u: RequestInfo | URL, init?: RequestInit) => f(typeof u === 'string' && u.startsWith('/') ? origin + u : u, init)) as typeof fetch
  db.authed = true
})
afterAll(() => server.close())
beforeEach(() => { resetDb(); db.authed = true; localStorage.clear() })

function mount(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <UiModeProvider><JobDrawerProvider><TourProvider><FeedbackProvider>
          <Routes><Route path="/getting-started" element={<GettingStartedPage />} /><Route path="*" element={<div data-testid="elsewhere" />} /></Routes>
        </FeedbackProvider></TourProvider></JobDrawerProvider></UiModeProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('getting started (0.10.0)', () => {
  it('shows the 7-station journey with done state from the server and a detail card on hover', async () => {
    mount('/getting-started')
    const journey = await screen.findByTestId('gs-journey')
    expect(within(journey).getAllByRole('button')).toHaveLength(7)
    // mock data has hosts, images and labs → the first stations are done
    await waitFor(() => expect(screen.getByText(/already done on this server/).textContent).toMatch(/[3-7]\/7/))
    fireEvent.mouseEnter(screen.getByTestId('gs-step-validate'))
    expect(screen.getByTestId('gs-detail').textContent).toContain('Validate')
    expect(screen.getByTestId('gs-detail').textContent).toContain('How, in three moves')
    expect(screen.getAllByTestId('gs-menu-item').length).toBeGreaterThan(10)
    fireEvent.click(screen.getByTestId('gs-done'))
    expect(localStorage.getItem('avdlab.gettingStarted.done')).toBe('1')
  })
})

describe('feedback mailto (0.10.0)', () => {
  it('prefills recipient, subject and page context', () => {
    const href = feedbackMailto('dietrich@arista.com', { kind: 'bug', title: 'Deploy hangs', body: 'it hangs', page: '/labs/1/runtime' },
      { version: '0.10.0', user: 'admin', userAgent: 'UA', href: 'https://avdlab.example/labs/1/runtime', context: ['Labs › evpn-lab › Runtime'] })
    expect(href.startsWith('mailto:dietrich%40arista.com?subject=')).toBe(true)
    const u = new URL(href)
    expect(decodeURIComponent(u.searchParams.get('subject')!)).toBe('[avdlab] bug: Deploy hangs')
    const body = decodeURIComponent(u.searchParams.get('body')!)
    expect(body).toContain('page: /labs/1/runtime')
    expect(body).toContain('where: Labs › evpn-lab › Runtime')
    expect(body).toContain('version: 0.10.0')
    expect(body).toContain('user: admin')
  })
})
