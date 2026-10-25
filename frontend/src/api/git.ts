import { api } from './client'
import type { GitConfigIn, GitConfigOut, GitImportIn, GitStatus, JobRef, Lab, OpenLinks } from './types'

/** CONTRACT-3 §2 git coupling, §4 import/export, §8 VS Code */
export const gitApi = {
  status: (labId: string) => api.get<GitStatus>(`/labs/${labId}/git`),
  configure: (labId: string, body: GitConfigIn) => api.put<GitConfigOut>(`/labs/${labId}/git`, body),
  remove: (labId: string) => api.delete<void>(`/labs/${labId}/git`),
  push: (labId: string, force = false) => api.post<JobRef>(`/labs/${labId}/git/push`, { force }),
  pull: (labId: string) => api.post<JobRef>(`/labs/${labId}/git/pull`),
  openLinks: (labId: string) => api.get<OpenLinks>(`/labs/${labId}/open-links`),
  sync: (labId: string) => api.post<JobRef>(`/labs/${labId}/sync`),
  importArchive: (file: File, name: string, hostId?: string | null) => {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('name', name)
    if (hostId) fd.append('host_id', hostId)
    return api.post<Lab>('/labs/import', fd)
  },
  importGit: (body: GitImportIn) => api.post<JobRef>('/labs/import/git', body),
  exportUrl: (labId: string) => `/api/labs/${labId}/export`,
}
