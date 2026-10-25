import { api, request } from './client'
import type { BootstrapIn, ContainerKillResult, DevcontainerIn, GeneratedKey, Host, HostCapabilitiesOut, HostContainer, HostIn, HostMetrics, HostProcesses, JobRef } from './types'

export const hostsApi = {
  list: () => api.get<Host[]>('/hosts'),
  get: (id: string) => api.get<Host>(`/hosts/${id}`),
  create: (body: HostIn) => api.post<Host>('/hosts', body),
  update: (id: string, body: Partial<HostIn>) => api.patch<Host>(`/hosts/${id}`, body),
  remove: (id: string) => api.delete<void>(`/hosts/${id}`),
  generateKey: (body: { name?: string; comment?: string } = {}) => api.post<GeneratedKey>('/hosts/keys/generate', body),
  probe: (id: string) => api.post<JobRef>(`/hosts/${id}/probe`),
  bootstrap: (id: string, body: BootstrapIn) => api.post<JobRef>(`/hosts/${id}/bootstrap`, body),
  verify: (id: string) => api.post<JobRef>(`/hosts/${id}/verify`),
  capabilities: (id: string) => api.get<HostCapabilitiesOut>(`/hosts/${id}/capabilities`),
  // CONTRACT-3 §5 monitoring, §7 terminal
  metrics: (id: string, params: { since?: string; step?: number } = {}) => {
    const q = new URLSearchParams()
    if (params.since) q.set('since', params.since)
    if (params.step) q.set('step', String(params.step))
    const qs = q.toString()
    return api.get<HostMetrics>(`/hosts/${id}/metrics${qs ? `?${qs}` : ''}`)
  },
  processes: (id: string) => api.get<HostProcesses>(`/hosts/${id}/processes`),
  containers: (id: string) => api.get<HostContainer[]>(`/hosts/${id}/containers`),
  /** CONTRACT-3b §3: force → `docker rm -f`, else `docker stop`; containers of a deployed lab need `confirm` = the name */
  killContainer: (id: string, name: string, opts: { force: boolean; confirm?: string }) =>
    request<ContainerKillResult>(`/hosts/${id}/containers/${encodeURIComponent(name)}?force=${opts.force}`, {
      method: 'DELETE', body: opts.confirm ? { confirm: opts.confirm } : undefined,
    }),
  terminalPath: (id: string) => `/hosts/${id}/terminal`,
  devcontainer: async (body: DevcontainerIn): Promise<Blob> => {
    const res = await api.post<Response>('/hosts/devcontainer', body, { raw: true, headers: { Accept: 'application/zip' } })
    return res.blob()
  },
}
