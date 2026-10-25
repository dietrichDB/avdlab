import { api } from './client'
import type { Job } from './types'

export const jobsApi = {
  list: (params: { lab_id?: string; host_id?: string; limit?: number } = {}) => {
    const q = new URLSearchParams()
    if (params.lab_id) q.set('lab_id', params.lab_id)
    if (params.host_id) q.set('host_id', params.host_id)
    if (params.limit) q.set('limit', String(params.limit))
    const qs = q.toString()
    return api.get<Job[]>(`/jobs${qs ? `?${qs}` : ''}`)
  },
  get: (id: string) => api.get<Job>(`/jobs/${id}`),
  log: (id: string) => api.get<{ log: string }>(`/jobs/${id}/log`),
  /** kill a queued/running job: `{cancelled: true, was_running}`; finished/unknown → `{cancelled: false}` */
  cancel: (id: string) => api.post<{ cancelled: boolean; was_running?: boolean }>(`/jobs/${id}/cancel`),
  types: () => api.get<string[]>('/jobs/types'),
}

export const TERMINAL: ReadonlySet<string> = new Set(['succeeded', 'failed', 'cancelled'])
export const isTerminal = (status?: string | null) => !!status && TERMINAL.has(status)
