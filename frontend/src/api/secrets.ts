import { api } from './client'
import type { Secret, SecretKind } from './types'

export const secretsApi = {
  list: () => api.get<Secret[]>('/secrets'),
  create: (body: { name: string; kind: SecretKind; value: string; meta?: Record<string, unknown> }) =>
    api.post<Secret>('/secrets', body),
  remove: (id: string) => api.delete<void>(`/secrets/${id}`),
}
