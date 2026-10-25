import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { setupServer } from 'msw/node'
import { handlers } from '../mocks/handlers'
import { db, reset as resetDb } from '../mocks/data'
import { JobDrawerProvider } from '../components/JobDrawer'
import { TourProvider } from '../components/learn/Tour'
import ImagesPage from '../pages/Images'
import NewLabPage from '../pages/NewLab'
import { imageLabel } from '../components/ImagePicker'

const server = setupServer(...handlers)
const origin = globalThis.location?.origin ?? 'http://localhost'
beforeAll(() => {
  server.listen({ onUnhandledRequest: 'bypass' })
  const f = globalThis.fetch
  globalThis.fetch = ((u: RequestInfo | URL, init?: RequestInit) => f(typeof u === 'string' && u.startsWith('/') ? origin + u : u, init)) as typeof fetch
  db.authed = true
})
afterAll(() => server.close())
beforeEach(() => { resetDb(); db.authed = true; db.badToken = false; localStorage.clear() })

function mount(path: string, element: JSX.Element, route: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <JobDrawerProvider><TourProvider>
          <Routes><Route path={route} element={element} /><Route path="*" element={<div data-testid="elsewhere" />} /></Routes>
        </TourProvider></JobDrawerProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('arista.com token (0.7.1)', () => {
  it('offers to replace a rejected token and recovers the catalogue', async () => {
    db.secrets.push({ id: 's-tok', name: 'arista_token', kind: 'arista_token', meta: {}, created_at: new Date().toISOString() })
    db.badToken = true
    mount('/images', <ImagesPage />, '/images')
    // 502 from the catalogue → explicit "Replace token" affordance (previously a dead end)
    const bad = await screen.findByTestId('bad-token')
    expect(bad.textContent).toContain('rejected')
    fireEvent.click(within(bad).getByText('Replace token'))
    const form = await screen.findByTestId('token-form')
    expect(form.textContent).toContain('replaces the saved one')
    fireEvent.change(form.querySelector('input')!, { target: { value: 'good-token-123' } })
    fireEvent.click(within(form).getByText('Replace token'))
    await waitFor(() => expect(screen.getByTestId('catalog')).toBeInTheDocument())
    expect(db.secrets.filter((s) => s.name === 'arista_token')).toHaveLength(1) // replaced in place
    expect(db.badToken).toBe(false)
  })

  it('token button opens the form even when the catalogue works, with Remove', async () => {
    db.secrets.push({ id: 's-tok', name: 'arista_token', kind: 'arista_token', meta: {}, created_at: new Date().toISOString() })
    mount('/images', <ImagesPage />, '/images')
    await screen.findByTestId('catalog')
    const btn = screen.getByTestId('token-button')
    expect(btn.textContent).toBe('Token…')
    fireEvent.click(btn)
    const form = await screen.findByTestId('token-form')
    expect(within(form).getByText('Remove')).toBeInTheDocument()
    fireEvent.click(within(form).getByText('Cancel'))
    await waitFor(() => expect(screen.queryByTestId('token-form')).toBeNull())
  })

  it('shows the set-token form when none is configured', async () => {
    mount('/images', <ImagesPage />, '/images')
    const form = await screen.findByTestId('token-form')
    expect(within(form).getByText('Save token')).toBeInTheDocument()
    expect(screen.getByTestId('token-button').textContent).toBe('Set token')
  })
})

describe('image dropdown on New lab (0.7.1)', () => {
  it('lists downloaded images and sets version + arch from the choice', async () => {
    mount('/labs/new', <NewLabPage />, '/labs/new')
    fireEvent.click(within(await screen.findByTestId('template-single-dc-l3ls')).getByText('Use'))
    fireEvent.click(await screen.findByText('Next'))
    const picker = (await screen.findByTestId('image-picker')) as HTMLSelectElement
    await waitFor(() => expect(picker.options.length).toBeGreaterThan(3))
    const labels = [...picker.options].map((o) => o.textContent)
    expect(labels).toContain(imageLabel(db.images[0], { 'host-lab1': 'lab1' }))
    expect(labels.some((l) => l?.includes('cEOSarm 4.34.2F · arm64'))).toBe(true)
    expect(labels[labels.length - 1]).toContain('Other…')
    // the mock template defaults ceos_version → preselected when it matches a downloaded image
    fireEvent.change(picker, { target: { value: 'img-3' } })
    await waitFor(() => expect(picker.value).toBe('img-3'))
    // "Other…" reveals a free-text version
    fireEvent.change(picker, { target: { value: '__custom__' } })
    const txt = (await screen.findByTestId('image-version')) as HTMLInputElement
    fireEvent.change(txt, { target: { value: '4.30.1F' } })
    expect(txt.value).toBe('4.30.1F')
    expect(picker.value).toBe('__custom__')
  })
})
