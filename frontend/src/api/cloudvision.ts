import { api } from './client'
import type { CVDeployParams, CVStatus, CVTarget, CVTargetIn, CVTestResult, JobRef } from './types'

export const cvApi = {
  targets: () => api.get<CVTarget[]>('/cv/targets'),
  target: (id: string) => api.get<CVTarget>(`/cv/targets/${id}`),
  createTarget: (body: CVTargetIn) => api.post<CVTarget>('/cv/targets', body),
  updateTarget: (id: string, body: Partial<CVTargetIn>) => api.patch<CVTarget>(`/cv/targets/${id}`, body),
  removeTarget: (id: string) => api.delete<void>(`/cv/targets/${id}`),
  testTarget: (id: string) => api.post<CVTestResult>(`/cv/targets/${id}/test`),
  // lab-scoped
  status: (labId: string) => api.get<CVStatus>(`/labs/${labId}/cv/status`),
  onboard: (labId: string) => api.post<JobRef>(`/labs/${labId}/cv/onboard`),
  deploy: (labId: string, body: CVDeployParams) => api.post<JobRef>(`/labs/${labId}/cv/deploy`, body),
}
