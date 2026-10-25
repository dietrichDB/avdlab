import { api } from './client'
import type { JobRef, Topology } from './types'

export const topologyApi = {
  get: (labId: string) => api.get<Topology>(`/topology/${labId}`),
  regenerate: (labId: string) => api.post<JobRef>(`/topology/${labId}/regenerate`),
}
