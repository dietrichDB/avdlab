import { api } from './client'
import type { ConfigDiff, JobRef, RunningConfig, Snapshot } from './types'

const e = encodeURIComponent

export const DEFAULT_SNAPSHOT_COMMANDS = [
  'show running-config', 'show version', 'show lldp neighbors', 'show ip bgp summary', 'show bgp evpn summary', 'show mlag', 'show vxlan vni',
]

/** "show ip bgp summary" → "show-ip-bgp-summary" (directory file name without .txt) */
export const cmdSlug = (cmd: string) => cmd.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')

/** CONTRACT-3 §3 — running configs & snapshots; §7 terminal paths */
export const nodesApi = {
  runningConfig: (labId: string, node: string) => api.get<RunningConfig>(`/labs/${labId}/nodes/${e(node)}/running-config`),
  configDiff: (labId: string, node: string) => api.get<ConfigDiff>(`/labs/${labId}/nodes/${e(node)}/config-diff`),
  snapshot: (labId: string, commands: string[] = DEFAULT_SNAPSHOT_COMMANDS) => api.post<JobRef>(`/labs/${labId}/snapshot`, { commands }),
  snapshots: (labId: string) => api.get<Snapshot[]>(`/labs/${labId}/snapshots`),
  snapshotFile: async (labId: string, snap: string, node: string, slug: string): Promise<string> => {
    const r = await api.get<unknown>(`/labs/${labId}/snapshots/${e(snap)}/${e(node)}/${e(slug)}`, { headers: { Accept: 'text/plain, */*' } })
    if (typeof r === 'string') return r
    const o = r as { content?: string; text?: string }
    return o?.content ?? o?.text ?? JSON.stringify(r, null, 2)
  },
  terminalPath: (labId: string, node: string, shell: 'cli' | 'bash') => `/labs/${labId}/nodes/${e(node)}/terminal?shell=${shell}`,
}
