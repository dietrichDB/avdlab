import { api } from './client'
import type {
  ActInfo, JobRef, MssSummary, MssVerifyParams, NetBoxStatus, NetBoxSyncParams, NetBoxTarget, NetBoxTargetIn, NetBoxTestResult, ZtpStatus,
} from './types'

/** CONTRACT-4 A — GET /labs/{id}/ztp */
export const ztpApi = {
  status: (labId: string) => api.get<ZtpStatus>(`/labs/${labId}/ztp`),
}

/** CONTRACT-4 B — NetBox targets + lab sync */
export const netboxApi = {
  targets: () => api.get<NetBoxTarget[]>('/netbox/targets'),
  target: (id: string) => api.get<NetBoxTarget>(`/netbox/targets/${id}`),
  createTarget: (body: NetBoxTargetIn) => api.post<NetBoxTarget>('/netbox/targets', body),
  updateTarget: (id: string, body: Partial<NetBoxTargetIn>) => api.patch<NetBoxTarget>(`/netbox/targets/${id}`, body),
  removeTarget: (id: string) => api.delete<void>(`/netbox/targets/${id}`),
  testTarget: (id: string) => api.post<NetBoxTestResult>(`/netbox/targets/${id}/test`),
  status: (labId: string) => api.get<NetBoxStatus>(`/labs/${labId}/netbox/status`),
  sync: (labId: string, body: NetBoxSyncParams) => api.post<JobRef>(`/labs/${labId}/netbox/sync`, body),
}

/** CONTRACT-4 C — MSS foundation */
export const mssApi = {
  get: (labId: string) => api.get<MssSummary>(`/labs/${labId}/mss`),
  verify: (labId: string, body: MssVerifyParams) => api.post<JobRef>(`/labs/${labId}/mss/verify`, body),
}

/** CONTRACT-4 D — Arista Cloud Test */
export const actApi = {
  info: (labId: string) => api.get<ActInfo>(`/labs/${labId}/act`),
  prepare: (labId: string) => api.post<ActInfo>(`/labs/${labId}/act/prepare`),
  topologyUrl: (labId: string) => `/api/labs/${labId}/act/topology`,
  topologyText: async (labId: string): Promise<string> => {
    const r = await api.get<unknown>(`/labs/${labId}/act/topology`, { headers: { Accept: 'application/x-yaml, text/plain, */*' } })
    return typeof r === 'string' ? r : JSON.stringify(r)
  },
}
