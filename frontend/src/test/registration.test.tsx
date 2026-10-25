import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import type { ReactElement } from 'react'
import { setupServer } from 'msw/node'
import { handlers } from '../mocks/handlers'
import { authDb, db, regDb, resetAuth, resetRegistration } from '../mocks/data'
import { AuthProvider } from '../hooks/useAuth'
import LoginPage from '../pages/Login'
import RegisterPage from '../pages/Register'
import UsersPage from '../pages/Users'
import AdminSettingsPage from '../pages/AdminSettings'

const server = setupServer(...handlers)
const origin = globalThis.location?.origin ?? 'http://localhost'
beforeAll(() => {
  server.listen({ onUnhandledRequest: 'bypass' })
  const f = globalThis.fetch
  globalThis.fetch = ((u: RequestInfo | URL, init?: RequestInit) => f(typeof u === 'string' && u.startsWith('/') ? origin + u : u, init)) as typeof fetch
})
afterAll(() => server.close())
beforeEach(() => { resetAuth(); resetRegistration(); db.authed = false; localStorage.clear() })

function mount(path: string, routes: { path: string; element: ReactElement }[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <AuthProvider>
          <Routes>
            {routes.map((r) => <Route key={r.path} path={r.path} element={r.element} />)}
          </Routes>
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const asAdmin = () => { db.authed = true; authDb.currentId = 'usr-admin' }

describe('account requests', () => {
  it('login links to the register page; the form submits and shows the pending state', async () => {
    mount('/login', [{ path: '/login', element: <LoginPage /> }, { path: '/register', element: <RegisterPage /> }])
    fireEvent.click(await screen.findByTestId('register-link'))
    await screen.findByTestId('register-form')
    expect(await screen.findByTestId('register-github')).toHaveAttribute('href', 'https://github.com/example/avdlab')
    expect(screen.getByTestId('register-support')).toHaveTextContent('avdlab-support@example.com')
    const submit = screen.getByTestId('register-submit')
    fireEvent.change(screen.getByTestId('register-username'), { target: { value: 'No Spaces' } })
    expect(screen.getByText(/3–32 characters/)).toBeInTheDocument()
    expect(submit).toBeDisabled()
    fireEvent.change(screen.getByTestId('register-username'), { target: { value: 'Grace' } })
    fireEvent.change(screen.getByTestId('register-email'), { target: { value: 'grace@example.com' } })
    fireEvent.change(screen.getByTestId('register-fullname'), { target: { value: 'Grace Hopper' } })
    fireEvent.change(screen.getByTestId('register-reason'), { target: { value: 'UCLL course' } })
    expect(submit).toBeEnabled()
    fireEvent.click(submit)
    await screen.findByTestId('register-success')
    expect(screen.getByTestId('register-status')).toHaveTextContent('pending')
    const req = regDb.requests.find((r) => r.username === 'grace')!
    expect(req).toMatchObject({ email: 'grace@example.com', full_name: 'Grace Hopper', reason: 'UCLL course', status: 'pending' })
  })

  it('shows the server error for a taken username', async () => {
    mount('/register', [{ path: '/register', element: <RegisterPage /> }])
    fireEvent.change(await screen.findByTestId('register-username'), { target: { value: 'ada' } })
    fireEvent.change(screen.getByTestId('register-email'), { target: { value: 'ada2@example.com' } })
    fireEvent.click(screen.getByTestId('register-submit'))
    expect(await screen.findByText(/username ada is not available/)).toBeInTheDocument()
  })

  it('no register link when registration is disabled', async () => {
    regDb.settings.registration_enabled = false
    mount('/login', [{ path: '/login', element: <LoginPage /> }])
    await screen.findByTestId('login-step-password')
    await new Promise((r) => setTimeout(r, 50))
    expect(screen.queryByTestId('register-link')).toBeNull()
  })

  it('admin approves a pending request and sees the temporary password once; reject removes the row', async () => {
    regDb.requests.push({ ...regDb.requests[0], id: 'req-bob', username: 'bob', email: 'bob@example.com' })
    asAdmin()
    mount('/users', [{ path: '/users', element: <UsersPage /> }])
    const row = await screen.findByTestId('request-row-ada')
    expect(within(row).getByText('Ada Lovelace')).toBeInTheDocument()
    expect(screen.getByTestId('admin-settings-link')).toHaveAttribute('href', '/admin/settings')
    fireEvent.change(within(row).getByTestId('request-role-ada'), { target: { value: 'operator' } })
    fireEvent.click(within(row).getByTestId('request-approve-ada'))
    const temp = await screen.findByTestId('temp-password-value')
    expect(temp.textContent).toHaveLength(20)
    const ada = authDb.users.find((u) => u.username === 'ada')!
    expect(ada).toMatchObject({ role: 'operator', must_change_password: true })
    expect(regDb.requests.find((r) => r.id === 'req-ada')!.status).toBe('approved')
    fireEvent.click(screen.getByRole('button', { name: 'Done' }))
    await waitFor(() => expect(screen.queryByTestId('request-row-ada')).toBeNull())
    expect(await screen.findByTestId('user-row-ada')).toBeInTheDocument()

    const bob = screen.getByTestId('request-row-bob')
    fireEvent.change(within(bob).getByTestId('request-reason-bob'), { target: { value: 'unknown applicant' } })
    fireEvent.click(within(bob).getByRole('button', { name: 'Reject' }))
    fireEvent.click(within(bob).getByRole('button', { name: 'Reject?' }))
    await waitFor(() => expect(screen.queryByTestId('request-row-bob')).toBeNull())
    expect(regDb.requests.find((r) => r.id === 'req-bob')).toMatchObject({ status: 'rejected', decision_reason: 'unknown applicant' })
    expect(screen.getByText('No pending requests.')).toBeInTheDocument()
  })
})

describe('admin settings', () => {
  it('saves changed fields, keeps the password write-only and sends a test mail', async () => {
    asAdmin()
    mount('/admin/settings', [{ path: '/admin/settings', element: <AdminSettingsPage /> }])
    await screen.findByTestId('admin-settings')
    expect(screen.getByTestId('settings-save')).toBeDisabled()
    fireEvent.click(screen.getByTestId('test-mail'))
    expect(await screen.findByTestId('test-mail-result')).toHaveTextContent('failed: SMTP is not configured')

    fireEvent.change(screen.getByTestId('set-github_url'), { target: { value: 'https://github.com/acme/avdlab' } })
    fireEvent.change(screen.getByTestId('set-smtp_host'), { target: { value: 'smtp.example.com' } })
    fireEvent.change(screen.getByTestId('set-smtp_port'), { target: { value: '465' } })
    fireEvent.change(screen.getByTestId('set-smtp_password'), { target: { value: 'hunter2-Secret' } })
    fireEvent.click(screen.getByTestId('set-registration_enabled'))
    expect(screen.getByTestId('test-mail')).toBeDisabled()  // save first
    fireEvent.click(screen.getByTestId('settings-save'))
    await screen.findByTestId('settings-saved')
    expect(regDb.settings).toMatchObject({ github_url: 'https://github.com/acme/avdlab', smtp_host: 'smtp.example.com', smtp_port: 465, registration_enabled: false })
    expect(regDb.smtpPassword).toBe('hunter2-Secret')
    expect(regDb.overridden.sort()).toEqual(['github_url', 'registration_enabled', 'smtp_host', 'smtp_port'])
    expect(screen.getByTestId('set-smtp_password')).toHaveValue('')
    expect(screen.getByTestId('set-smtp_password')).toHaveAttribute('placeholder', '•••••••• (set)')

    fireEvent.click(screen.getByTestId('test-mail'))
    await waitFor(() => expect(screen.getByTestId('test-mail-result')).toHaveTextContent('sent to dietrich@arista.com'))
    expect(regDb.mails[regDb.mails.length - 1]).toEqual({ to: 'dietrich@arista.com', subject: '[avdlab] test mail' })
  })

  it('is admin-only', async () => {
    db.authed = true
    authDb.currentId = 'usr-op'
    mount('/admin/settings', [{ path: '/admin/settings', element: <AdminSettingsPage /> }])
    expect(await screen.findByText('Only admins can change the settings.')).toBeInTheDocument()
  })
})
