import { api } from './client'
import type { IperfParams, JobRef, LabClient, PingParams, TrafficRun } from './types'

/** CONTRACT-3 §1 — clients & traffic generation */
export const trafficApi = {
  clients: (labId: string) => api.get<LabClient[]>(`/labs/${labId}/clients`),
  ping: (labId: string, body: PingParams) => api.post<JobRef>(`/labs/${labId}/traffic/ping`, body),
  iperf: (labId: string, body: IperfParams) => api.post<JobRef>(`/labs/${labId}/traffic/iperf`, body),
  runs: (labId: string) => api.get<TrafficRun[]>(`/labs/${labId}/traffic/runs`),
  run: (labId: string, runId: string) => api.get<TrafficRun>(`/labs/${labId}/traffic/runs/${runId}`),
}
