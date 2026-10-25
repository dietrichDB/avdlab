import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Navigate, Route, Routes } from 'react-router-dom'
import type { ReactElement } from 'react'
import { setupServer } from 'msw/node'
import { handlers } from '../mocks/handlers'
import { authDb, db, resetAuth } from '../mocks/data'
import { AuthProvider, lockoutMessage, useAuth } from '../hooks/useAuth'
import LoginPage, { ForcedPasswordChange } from '../pages/Login'
import AccountPage, { policyChecks } from '../pages/Account'
import UsersPage from '../pages/Users'
import { UserMenu } from '../components/Layout'

const server = setupServer(...handlers)
const origin = globalThis.location?.origin ?? 'http://localhost'
beforeAll(() => {
  server.listen({ onUnhandledRequest: 'bypass' })
  const f = globalThis.fetch
  globalThis.fetch = ((u: RequestInfo | URL, init?: RequestInit) => f(typeof u === 'string' && u.startsWith('/') ? origin + u : u, init)) as typeof fetch
})
afterAll(() => server.close())
beforeEach(() => { resetAuth(); db.authed = false; localStorage.clear() })

/** mirrors App.tsx RequireAuth */
function Home() {
  const { user, role, loading, mustChangePassword } = useAuth()
  if (loading) return <div>loading</div>
  if (!user) return <Navigate to="/login" replace />
  if (mustChangePassword) return <ForcedPasswordChange />
  return <div data-testid="home">home {user} ({role}) <UserMenu /></div>
}

function mount(path: string, extra?: { path: string; element: ReactElement }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <AuthProvider>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route path="/" element={<Home />} />
            {extra && <Route path={extra.path} element={extra.element} />}
          </Routes>
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function signIn(username: string, password: string) {
  fireEvent.change(screen.getByLabelText('Username'), { target: { value: username } })
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: password } })
  fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
}

describe('password policy checklist', () => {
  it('mirrors the server rules', () => {
    const ok = (pw: string, u = 'alice') => policyChecks(pw, u).filter((c) => !c.ok).map((c) => c.id)
    expect(ok('Blue-Lantern-42x')).toEqual([])
    expect(ok('short')).toEqual(expect.arrayContaining(['length', 'classes']))
    expect(ok('xAliceRocks-2024')).toEqual(['username'])
    expect(ok('Baaaaad-Pass-99')).toEqual(['repeat'])
    expect(lockoutMessage('account locked until 2026-10-08T12:34:56+00:00')).toMatch(/^Account locked until /)
  })
})

describe('login', () => {
  it('two-step login with MFA: wrong code, then a recovery code', async () => {
    mount('/login')
    await screen.findByTestId('login-step-password')
    signIn('mfa-user', 'Secret-Pass-123')
    await screen.findByTestId('login-step-mfa')
    fireEvent.change(screen.getByTestId('mfa-code'), { target: { value: '000000' } })
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }))
    expect(await screen.findByText('Invalid code')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('mfa-toggle-recovery'))
    fireEvent.change(screen.getByTestId('mfa-code'), { target: { value: 'abcde-fghij' } })
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }))
    expect(await screen.findByTestId('home')).toHaveTextContent('home mfa-user (operator)')
    expect(authDb.users.find((u) => u.username === 'mfa-user')!.recovery).toEqual(['klmno-pqrst'])  // single use
  })

  it('shows the lockout time and generic errors', async () => {
    mount('/login')
    await screen.findByTestId('login-step-password')
    signIn('locked', 'whatever')
    expect(await screen.findByText(/Account locked until/)).toBeInTheDocument()
    signIn('olivia', 'nope')
    expect(await screen.findByText('Invalid username or password')).toBeInTheDocument()
  })

  it('forces a password change before anything else', async () => {
    mount('/login')
    await screen.findByTestId('login-step-password')
    signIn('newbie', 'Temp-Pass-0001')
    const form = await screen.findByTestId('forced-password-change')
    expect(screen.queryByTestId('home')).toBeNull()
    fireEvent.change(within(form).getByTestId('pw-current'), { target: { value: 'Temp-Pass-0001' } })
    fireEvent.change(within(form).getByTestId('pw-new'), { target: { value: 'short' } })
    expect(within(form).getByTestId('policy-length')).toHaveAttribute('data-ok', '0')
    expect(within(form).getByTestId('pw-submit')).toBeDisabled()
    fireEvent.change(within(form).getByTestId('pw-new'), { target: { value: 'Brand-New-Pass-9' } })
    fireEvent.change(within(form).getByTestId('pw-confirm'), { target: { value: 'Brand-New-Pass-9' } })
    expect(within(form).getByTestId('policy-length')).toHaveAttribute('data-ok', '1')
    fireEvent.click(within(form).getByTestId('pw-submit'))
    expect(await screen.findByTestId('home')).toHaveTextContent('home newbie (viewer)')
    expect(authDb.users.find((u) => u.username === 'newbie')!.must_change_password).toBe(false)
  })
})

describe('account page', () => {
  it('MFA setup → QR + secret → enable → recovery codes shown once', async () => {
    db.authed = true
    authDb.currentId = 'usr-op'
    mount('/account', { path: '/account', element: <AccountPage /> })
    fireEvent.click(await screen.findByTestId('mfa-setup'))
    expect(await screen.findByTestId('mfa-qr')).toContainHTML('<svg')
    expect(screen.getByTestId('mfa-secret')).toHaveTextContent('JBSWY3DPEHPK3PXP')
    fireEvent.change(screen.getByTestId('mfa-enable-code'), { target: { value: '123456' } })
    fireEvent.click(screen.getByTestId('mfa-enable'))
    const codes = await screen.findByTestId('recovery-codes')
    expect(within(codes).getAllByRole('listitem')).toHaveLength(10)
    expect(within(codes).getByText('Download')).toHaveAttribute('download', 'avdlab-recovery-codes.txt')
    await waitFor(() => expect(within(screen.getByTestId('mfa-section')).getByText('enabled')).toBeInTheDocument())
    expect(screen.getByTestId('mfa-disable')).toBeInTheDocument()
  })
})

describe('users page', () => {
  it('admin creates a user and sees the temporary password once', async () => {
    db.authed = true
    authDb.currentId = 'usr-admin'
    mount('/users', { path: '/users', element: <UsersPage /> })
    await screen.findByTestId('users-table')
    expect(screen.getByTestId('user-row-mfa-user')).toBeInTheDocument()
    expect(screen.getByTestId('audit-table')).toHaveTextContent('login.lockout')
    fireEvent.click(screen.getByTestId('new-user'))
    fireEvent.change(screen.getByTestId('new-username'), { target: { value: 'Carol' } })
    fireEvent.change(screen.getByTestId('new-role'), { target: { value: 'viewer' } })
    fireEvent.click(screen.getByTestId('create-user-submit'))
    const temp = await screen.findByTestId('temp-password-value')
    expect(temp.textContent).toHaveLength(20)
    expect(authDb.users.find((u) => u.username === 'carol')!.must_change_password).toBe(true)
    expect(await screen.findByTestId('user-row-carol')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Done' }))
    expect(screen.queryByTestId('temp-password')).toBeNull()
  })

  it('the user menu shows the role and links to Account', async () => {
    db.authed = true
    authDb.currentId = 'usr-op'
    mount('/')
    expect(await screen.findByTestId('role-badge')).toHaveTextContent('operator')
    fireEvent.click(screen.getByRole('button', { name: /olivia/ }))
    expect(screen.getByRole('menuitem', { name: 'Account' })).toHaveAttribute('href', '/account')
  })
})
