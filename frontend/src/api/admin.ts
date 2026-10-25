import { api } from './client'
import type {
  AccountRequest, AccountRequestStatus, AppSettings, AppSettingsIn, ApproveResult, RegisterIn, RegisterResult, TestMailResult, UserRole,
} from './types'

/** CONTRACT-8 A — public account requests (no session needed) */
export const registrationApi = {
  register: (body: RegisterIn) => api.post<RegisterResult>('/register', body, { silent401: true }),
  status: (id: string) => api.get<{ status: AccountRequestStatus }>(`/register/status/${encodeURIComponent(id)}`, { silent401: true }),
}

/** CONTRACT-8 A — admin: pending account requests (Users page) */
export const requestsApi = {
  list: (status: 'pending' | 'all' = 'pending') => api.get<AccountRequest[]>(`/users/requests?status=${status}`),
  approve: (id: string, role: UserRole) => api.post<ApproveResult>(`/users/requests/${id}/approve`, { role }),
  reject: (id: string, reason?: string) => api.post<AccountRequest & { mail_queued: boolean }>(`/users/requests/${id}/reject`, { reason: reason || null }),
}

/** CONTRACT-8 A — admin settings */
export const adminApi = {
  settings: () => api.get<AppSettings>('/admin/settings'),
  saveSettings: (body: AppSettingsIn) => api.put<AppSettings>('/admin/settings', body),
  testMail: () => api.post<TestMailResult>('/admin/settings/test-mail'),
}
