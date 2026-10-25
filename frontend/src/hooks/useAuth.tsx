import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { authApi } from '../api/auth'
import { ApiError, setUnauthorizedHandler } from '../api/client'
import type { AuthMe, UserRole } from '../api/types'

export interface AuthState {
  user: string | null
  role: UserRole | null
  mfaEnabled: boolean
  mustChangePassword: boolean
  loading: boolean
  /** step 1: `{ mfaRequired: true }` means a second step (`verifyMfa`) is needed */
  login: (u: string, p: string) => Promise<{ mfaRequired: boolean }>
  /** step 2: TOTP or recovery code; throws `MfaRestartError` when the pending sign-in is gone (too many codes) */
  verifyMfa: (code: string) => Promise<void>
  logout: () => Promise<void>
  refresh: () => Promise<void>
  /** apply a fresh `/auth/me`-shaped payload (password change, MFA enable) */
  apply: (m: AuthMe | null) => void
}

export class MfaRestartError extends Error {}

/** "account locked until 2026-10-08T12:34:56+00:00" → "Account locked until 14:34" */
export function lockoutMessage(detail: unknown): string {
  const s = typeof detail === 'string' ? detail : ''
  const iso = /\d{4}-\d{2}-\d{2}T[\d:.]+(?:Z|[+-]\d{2}:\d{2})?/.exec(s)?.[0]
  if (!iso) return 'Account locked — try again later or ask an admin to unlock it'
  const t = new Date(iso)
  const when = Number.isNaN(t.getTime()) ? iso : t.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  return `Account locked until ${when} (too many failed sign-ins) — or ask an admin to unlock it`
}

export const AuthCtx = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<AuthMe | null>(null)
  const [loading, setLoading] = useState(true)
  const navigate = useNavigate()
  const location = useLocation()
  const qc = useQueryClient()

  const refresh = useCallback(async () => {
    try {
      setMe(await authApi.me())
    } catch {
      setMe(null)
    }
  }, [])

  useEffect(() => {
    refresh().finally(() => setLoading(false))
  }, [refresh])

  useEffect(() => {
    setUnauthorizedHandler(() => {
      setMe(null)
      qc.clear()
      if (location.pathname !== '/login') navigate('/login', { replace: true, state: { from: location.pathname } })
    })
  }, [navigate, location.pathname, qc])

  const login = useCallback(async (u: string, p: string) => {
    try {
      const m = await authApi.login(u, p)
      if ('mfa_required' in m && m.mfa_required) return { mfaRequired: true }
      setMe(m as AuthMe)
      return { mfaRequired: false }
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) throw new Error('Invalid username or password')
      if (e instanceof ApiError && e.status === 423) throw new Error(lockoutMessage(e.detail))
      throw e
    }
  }, [])

  const verifyMfa = useCallback(async (code: string) => {
    try {
      setMe(await authApi.verifyMfa(code))
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) throw new MfaRestartError(typeof e.detail === 'string' ? e.detail : 'Sign in again')
      if (e instanceof ApiError && e.status === 400) throw new Error('Invalid code')
      throw e
    }
  }, [])

  const logout = useCallback(async () => {
    await authApi.logout().catch(() => undefined)
    setMe(null)
    qc.clear()
    navigate('/login', { replace: true })
  }, [navigate, qc])

  const value = useMemo<AuthState>(() => ({
    user: me?.user ?? null,
    role: me?.role ?? null,
    mfaEnabled: !!me?.mfa_enabled,
    mustChangePassword: !!me?.must_change_password,
    loading, login, verifyMfa, logout, refresh, apply: setMe,
  }), [me, loading, login, verifyMfa, logout, refresh])
  return <AuthCtx.Provider value={value}>{children}</AuthCtx.Provider>
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthCtx)
  if (!ctx) throw new Error('useAuth outside AuthProvider')
  return ctx
}
