import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import type { ReactElement } from 'react'
import { setupServer } from 'msw/node'
import { http, HttpResponse } from 'msw'
import { handlers } from '../mocks/handlers'
import { authDb, db, reset as resetDb, resetAuth } from '../mocks/data'
import { AuthProvider } from '../hooks/useAuth'
import { JobDrawerProvider } from '../components/JobDrawer'
import { Layout, crumbsFor, visibleNav } from '../components/Layout'
import { IntroVideo, probeVideo } from '../components/IntroVideo'
import { Hint } from '../components/ui'
import DashboardPage, { nextSteps } from '../pages/Dashboard'
import LabDetailPage from '../pages/lab/LabDetail'
import { UI_MODE_KEY, UiModeProvider, getUiMode } from '../lib/uiMode'
import type { Host, Image, Lab } from '../api/types'

const server = setupServer(...handlers)
const origin = globalThis.location?.origin ?? 'http://localhost'
beforeAll(() => {
  server.listen({ onUnhandledRequest: 'bypass' })
  const f = globalThis.fetch
  globalThis.fetch = ((u: RequestInfo | URL, init?: RequestInit) => f(typeof u === 'string' && u.startsWith('/') ? origin + u : u, init)) as typeof fetch
})
afterAll(() => server.close())
beforeEach(() => {
  resetDb(); resetAuth(); db.authed = true; authDb.currentId = 'usr-admin'; localStorage.clear()
  // the intro video exists unless a test says otherwise
  server.use(http.head('*/intro/*', () => new HttpResponse(null, { status: 200, headers: { 'content-type': 'video/mp4' } })))
})

function mount(path: string, routes: { path: string; element: ReactElement }[], withAuth = false) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const tree = <Routes>{routes.map((r) => <Route key={r.path} path={r.path} element={r.element} />)}</Routes>
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <UiModeProvider>
          <JobDrawerProvider>{withAuth ? <AuthProvider>{tree}</AuthProvider> : tree}</JobDrawerProvider>
        </UiModeProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}
const mountShell = (path = '/') =>
  mount(path, [{ path: '/', element: <Layout /> }, { path: '/labs', element: <Layout /> }], true)
const lab = (name: string) => db.labs.find((l) => l.name === name)!

describe('UI mode', () => {
  it('defaults to beginner; the toggle persists and filters the navigation', async () => {
    expect(getUiMode()).toBe('beginner')
    mountShell()
    const toggle = await screen.findByTestId('mode-toggle')
    expect(toggle).toHaveAttribute('data-mode', 'beginner')
    // admin sees the Admin group including Admin settings; beginner hides NetBox, Devcontainer, Jobs
    expect(await screen.findByTestId('nav-admin-settings')).toHaveAttribute('href', '/admin/settings')
    expect(screen.getByTestId('nav-hosts')).toBeInTheDocument()
    expect(screen.queryByTestId('nav-netbox')).toBeNull()
    expect(screen.queryByTestId('nav-devcontainer')).toBeNull()
    expect(screen.queryByTestId('nav-jobs')).toBeNull()
    for (const g of ['overview', 'provisioning', 'learn', 'admin']) expect(screen.getByTestId(`nav-group-${g}`)).toBeInTheDocument()

    fireEvent.click(within(toggle).getByRole('button', { name: 'Advanced' }))
    expect(localStorage.getItem(UI_MODE_KEY)).toBe('advanced')
    expect(screen.getByTestId('nav-netbox')).toBeInTheDocument()
    expect(screen.getByTestId('nav-devcontainer')).toBeInTheDocument()
    expect(screen.getByTestId('nav-jobs')).toBeInTheDocument()

    fireEvent.click(within(toggle).getByRole('button', { name: 'Beginner' }))
    expect(localStorage.getItem(UI_MODE_KEY)).toBe('beginner')
    expect(screen.queryByTestId('nav-netbox')).toBeNull()
  })

  it('a stored advanced mode survives a reload', async () => {
    localStorage.setItem(UI_MODE_KEY, 'advanced')
    mountShell()
    expect(await screen.findByTestId('mode-toggle')).toHaveAttribute('data-mode', 'advanced')
    expect(screen.getByTestId('nav-jobs')).toBeInTheDocument()
  })

  it('non-admins get no Admin settings / Users entries; the user menu links admin settings for admins', async () => {
    expect(visibleNav('operator', false).flatMap((g) => g.items.map((i) => i.to))).not.toContain('/admin/settings')
    expect(visibleNav('admin', false).flatMap((g) => g.items.map((i) => i.to))).toEqual(expect.arrayContaining(['/users', '/admin/settings', '/jobs', '/netbox']))
    mountShell()
    const menu = await screen.findByTestId('user-menu')
    fireEvent.click(await within(menu).findByRole('button', { name: /admin/ }))
    expect(screen.getByRole('menuitem', { name: 'Admin settings' })).toHaveAttribute('href', '/admin/settings')
  })

  it('top bar shows breadcrumbs and the GitHub button from /meta', async () => {
    mountShell('/labs')
    expect(await screen.findByTestId('topbar-github')).toHaveAttribute('href', 'https://github.com/example/avdlab')
    expect(screen.getByTestId('breadcrumbs')).toHaveTextContent(/Provisioning.*Labs/)
    expect(crumbsFor('/labs/abc/topology', { lab: 'dc1-lab' }).map((c) => c.label)).toEqual(['Provisioning', 'Labs', 'dc1-lab', 'Topology'])
    expect(crumbsFor('/admin/settings').map((c) => c.label)).toEqual(['Admin', 'Admin settings'])
    expect(crumbsFor('/').map((c) => c.label)).toEqual(['Overview', 'Dashboard'])
  })

  it('lab tabs: beginner hides MSS/NetBox/Git, "Show advanced" switches mode', async () => {
    const l = lab('dc1-lab')
    mount(`/labs/${l.id}/overview`, [{ path: '/labs/:id/:tab', element: <LabDetailPage /> }])
    await screen.findByRole('tab', { name: 'Overview' })
    for (const t of ['Settings', 'Build', 'Topology', 'Runtime', 'Validation', 'CloudVision', 'Clients', 'Devices', 'Jobs']) {
      expect(screen.getByRole('tab', { name: t })).toBeInTheDocument()
    }
    expect(screen.queryByRole('tab', { name: 'MSS' })).toBeNull()
    expect(screen.queryByRole('tab', { name: 'NetBox' })).toBeNull()
    expect(screen.queryByRole('tab', { name: 'Git & VS Code' })).toBeNull()
    fireEvent.click(screen.getByTestId('show-advanced'))
    expect(await screen.findByRole('tab', { name: 'MSS' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Git & VS Code' })).toBeInTheDocument()
    expect(localStorage.getItem(UI_MODE_KEY)).toBe('advanced')
    expect(screen.queryByTestId('show-advanced')).toBeNull()
  })

  it('an advanced tab opened by URL stays visible in beginner mode', async () => {
    const l = lab('dc1-lab')
    mount(`/labs/${l.id}/git`, [{ path: '/labs/:id/:tab', element: <LabDetailPage /> }])
    expect(await screen.findByRole('tab', { name: 'Git & VS Code' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.queryByRole('tab', { name: 'MSS' })).toBeNull()
  })

  it('<Hint> only renders in beginner mode', () => {
    const { unmount } = mount('/', [{ path: '/', element: <Hint>do this first</Hint> }])
    expect(screen.getByTestId('hint')).toHaveTextContent('do this first')
    unmount()
    localStorage.setItem(UI_MODE_KEY, 'advanced')
    mount('/', [{ path: '/', element: <Hint>do this first</Hint> }])
    expect(screen.queryByTestId('hint')).toBeNull()
  })
})

describe('dashboard', () => {
  it('beginner: hero, buttons, video, stat tiles and next steps — no tables', async () => {
    mount('/', [{ path: '/', element: <DashboardPage /> }])
    expect(await screen.findByTestId('hero')).toHaveTextContent('Build, deploy and validate Arista AVD labs')
    expect(screen.getByTestId('hero-learn')).toHaveAttribute('href', '/learn')
    expect(screen.getByTestId('hero-new-lab')).toHaveAttribute('href', '/labs/new')
    expect(await screen.findByTestId('hero-github')).toHaveAttribute('href', 'https://github.com/example/avdlab')
    const video = await screen.findByTestId('intro-video')
    expect(video).toHaveAttribute('src', '/intro/avdlab-intro.mp4')
    expect(video).toHaveAttribute('poster', '/intro/poster.jpg')
    await waitFor(() => expect(screen.getByTestId('stat-labs-value')).toHaveTextContent(String(db.labs.length)))
    expect(screen.getByTestId('stat-hosts')).toHaveAttribute('href', '/hosts')
    expect(screen.getByTestId('stat-images')).toBeInTheDocument()
    expect(screen.getByTestId('stat-running')).toBeInTheDocument()
    const steps = await screen.findAllByTestId('next-step')
    expect(steps.map((s) => s.getAttribute('data-step'))).toEqual(['host', 'image', 'lab', 'deploy', 'validate'])
    expect(steps[0]).toHaveAttribute('data-done', db.hosts.length ? 'true' : 'false')
    expect(screen.queryByTestId('dashboard-advanced')).toBeNull()
    expect(screen.queryByText('Recent jobs')).toBeNull()
  })

  it('advanced: adds the hosts/labs tables and recent jobs', async () => {
    localStorage.setItem(UI_MODE_KEY, 'advanced')
    mount('/', [{ path: '/', element: <DashboardPage /> }])
    expect(await screen.findByTestId('dashboard-advanced')).toBeInTheDocument()
    expect(await screen.findByTestId('dashboard-labs')).toHaveTextContent('dc1-lab')
    expect(await screen.findByTestId('dashboard-hosts')).toBeInTheDocument()
    expect(screen.getByText('Recent jobs')).toBeInTheDocument()
    expect(screen.getByTestId('hero')).toBeInTheDocument()
  })

  it('next steps follow what exists', () => {
    const h = { id: 'h', name: 'h' } as Host
    const i = { id: 'i' } as Image
    const lab = (state: string) => ({ id: 'l', name: 'my-lab', state }) as Lab
    const first = (steps: ReturnType<typeof nextSteps>) => steps.find((s) => !s.done)
    expect(first(nextSteps([], [], []))?.label).toBe('Add a lab host')
    expect(first(nextSteps([h], [], []))?.label).toBe('Download a cEOS image')
    expect(first(nextSteps([h], [i], []))?.label).toBe('Create your first lab')
    expect(first(nextSteps([h], [i], [lab('built')]))).toMatchObject({ label: 'Deploy my-lab', to: '/labs/l/build' })
    expect(first(nextSteps([h], [i], [lab('deployed')]))).toMatchObject({ label: 'Validate my-lab', to: '/labs/l/validation' })
    expect(first(nextSteps([h], [i], [lab('validated')]))).toBeUndefined()
  })
})

describe('IntroVideo', () => {
  it('shows the fallback when the URL is empty', () => {
    mount('/', [{ path: '/', element: <IntroVideo src="" /> }])
    expect(screen.getByTestId('intro-video-fallback')).toHaveTextContent(/isn't available/)
    expect(screen.queryByTestId('intro-video')).toBeNull()
  })

  it('shows the fallback when the file 404s (HEAD) or the SPA answers with HTML', async () => {
    server.use(http.head('*/intro/missing.mp4', () => new HttpResponse(null, { status: 404 })))
    mount('/', [{ path: '/', element: <IntroVideo src="/intro/missing.mp4" /> }])
    expect(await screen.findByTestId('intro-video-fallback')).toBeInTheDocument()
    const html = (async () => new Response('<html>', { status: 200, headers: { 'content-type': 'text/html' } })) as unknown as typeof fetch
    expect(await probeVideo('/x.mp4', html)).toBe('missing')
    const boom = (async () => { throw new TypeError('network') }) as unknown as typeof fetch
    expect(await probeVideo('/x.mp4', boom)).toBe('ok') // let <video onError> decide
  })

  it('falls back when the <video> element errors', async () => {
    mount('/', [{ path: '/', element: <IntroVideo src="/intro/avdlab-intro.mp4" /> }])
    const v = await screen.findByTestId('intro-video')
    fireEvent.error(v)
    expect(screen.getByTestId('intro-video-fallback')).toBeInTheDocument()
  })
})
