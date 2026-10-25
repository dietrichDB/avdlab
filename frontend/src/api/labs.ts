import { api } from './client'
import type { PsirtStatus, PushPlan,
  AntaResult, DeployParams, Device, FileContent, FileEntry, JobRef, Lab, LabCreateIn, LabDiff, LabPatchIn, LabStatus,
  Revision, Template, ValidateParams, ValidationRun,
} from './types'

const enc = (p: string) => p.split('/').map(encodeURIComponent).join('/')

export const labsApi = {
  templates: () => api.get<Template[]>('/labs/templates'),
  list: () => api.get<Lab[]>('/labs'),
  get: (id: string) => api.get<Lab>(`/labs/${id}`),
  create: (body: LabCreateIn) => api.post<Lab>('/labs', body),
  update: (id: string, body: LabPatchIn) => api.patch<Lab>(`/labs/${id}`, body),
  remove: (id: string) => api.delete<void>(`/labs/${id}`),
  files: (id: string) => api.get<FileEntry[]>(`/labs/${id}/files`),
  file: (id: string, path: string) => api.get<FileContent | string>(`/labs/${id}/files/${enc(path)}`),
  saveFile: (id: string, path: string, content: string) => api.put<unknown>(`/labs/${id}/files/${enc(path)}`, { content }),
  build: (id: string) => api.post<JobRef>(`/labs/${id}/build`),
  history: (id: string) => api.get<Revision[]>(`/labs/${id}/history`),
  diff: (id: string, a: string, b: string) =>
    api.get<LabDiff | string>(`/labs/${id}/diff?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`),
  devices: (id: string) => api.get<Device[]>(`/labs/${id}/devices`),
  // runtime (CONTRACT-2b)
  deploy: (id: string, body: Partial<DeployParams> = {}) => api.post<JobRef>(`/labs/${id}/deploy`, body),
  /** 0.10.0 incremental push: which devices' intended config changed since they last got it */
  pushPlan: (id: string) => api.get<PushPlan>(`/labs/${id}/push/plan`),
  push: (id: string, hosts?: string[]) => api.post<JobRef>(`/labs/${id}/push`, hosts ? { hosts } : {}),
  destroy: (id: string) => api.post<JobRef>(`/labs/${id}/destroy`),
  status: (id: string) => api.get<LabStatus>(`/labs/${id}/status`),
  refreshStatus: (id: string) => api.post<JobRef>(`/labs/${id}/status/refresh`),
  validate: (id: string, body: Partial<ValidateParams> = {}) => api.post<JobRef>(`/labs/${id}/validate`, body),
  /** 0.10.0: ANTA security advisories (anta psirt, preview) */
  psirt: (id: string) => api.get<PsirtStatus>(`/labs/${id}/psirt`),
  runPsirt: (id: string, tests: string[] = []) => api.post<JobRef>(`/labs/${id}/psirt`, { tests }),
  validations: (id: string) => api.get<ValidationRun[]>(`/labs/${id}/validations`),
  reportUrl: (id: string, runId: string, format: 'json' | 'md' | 'csv') =>
    `/api/labs/${id}/validations/${runId}/report?format=${format}`,
  /** parsed anta_report.json; tolerates the file being served as text */
  reportJson: async (id: string, runId: string): Promise<AntaResult[]> => {
    const r = await api.get<AntaResult[] | string>(`/labs/${id}/validations/${runId}/report?format=json`)
    return typeof r === 'string' ? (JSON.parse(r) as AntaResult[]) : r
  },
  reportText: async (id: string, runId: string, format: 'md' | 'csv'): Promise<string> => {
    const r = await api.get<unknown>(`/labs/${id}/validations/${runId}/report?format=${format}`, { headers: { Accept: 'text/plain, */*' } })
    return typeof r === 'string' ? r : JSON.stringify(r)
  },
}

/** GET /labs/{id} carries knob values as `knobs` (contract); older shapes keep them under `settings.knobs`. */
export function labKnobValues(lab: Lab): Record<string, unknown> {
  if (lab.knobs) return lab.knobs
  const s = (lab.settings ?? {}) as Record<string, unknown>
  if (s.knobs && typeof s.knobs === 'object') return s.knobs as Record<string, unknown>
  return s
}

export function labCustomKnobs(lab: Lab): string[] {
  if (lab.custom_knobs) return lab.custom_knobs
  const c = (lab.settings as Record<string, unknown> | undefined)?.custom_knobs
  return Array.isArray(c) ? c.map(String) : []
}
