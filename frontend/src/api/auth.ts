import { api } from './client'
import type {
  AuditEntry, AuthMe, LoginResult, MfaSetup, PasswordPolicy, RecoveryCodes, UserAccount, UserCreateIn, UserPatchIn, UserWithTemp,
} from './types'

export const authApi = {
  me: () => api.get<AuthMe>('/auth/me', { silent401: true }),
  login: (username: string, password: string) => api.post<LoginResult>('/auth/login', { username, password }, { silent401: true }),
  verifyMfa: (code: string) => api.post<AuthMe>('/auth/mfa/verify', { code }, { silent401: true }),
  logout: () => api.post<{ ok: boolean }>('/auth/logout'),
  policy: () => api.get<PasswordPolicy>('/auth/policy', { silent401: true }),
  changePassword: (current_password: string, new_password: string) => api.post<AuthMe>('/auth/password', { current_password, new_password }),
  mfaSetup: () => api.post<MfaSetup>('/auth/mfa/setup'),
  mfaEnable: (code: string) => api.post<{ mfa_enabled: boolean } & RecoveryCodes>('/auth/mfa/enable', { code }),
  mfaDisable: (password: string, code: string) => api.post<{ mfa_enabled: boolean }>('/auth/mfa/disable', { password, code }),
  regenerateRecoveryCodes: (password: string) => api.post<RecoveryCodes>('/auth/mfa/recovery-codes', { password }),
}

export const usersApi = {
  list: () => api.get<UserAccount[]>('/users'),
  create: (body: UserCreateIn) => api.post<UserWithTemp>('/users', body),
  patch: (id: string, body: UserPatchIn) => api.patch<UserAccount>(`/users/${id}`, body),
  remove: (id: string) => api.delete<{ ok: boolean }>(`/users/${id}`),
  resetPassword: (id: string) => api.post<UserWithTemp>(`/users/${id}/reset-password`),
  resetMfa: (id: string) => api.post<UserAccount>(`/users/${id}/mfa/reset`),
  unlock: (id: string) => api.post<UserAccount>(`/users/${id}/unlock`),
  audit: (limit = 100) => api.get<AuditEntry[]>(`/users/audit?limit=${limit}`),
}
