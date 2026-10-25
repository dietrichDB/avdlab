import type { ActKnob, Lab } from '../api/types'

export const ACT_DEFAULT: ActKnob = { os_version: '4.33.1F', username: 'cvpadmin', password: 'cvp123!', internet_access: true, tools_server_ip: '' }

export function normalizeAct(v: unknown): ActKnob {
  const o = (v && typeof v === 'object' && !Array.isArray(v) ? v : {}) as Record<string, unknown>
  const str = (k: keyof ActKnob) => (typeof o[k] === 'string' ? (o[k] as string) : o[k] == null ? (ACT_DEFAULT[k] as string) : String(o[k]))
  return {
    os_version: str('os_version'),
    username: str('username'),
    password: str('password'),
    internet_access: typeof o.internet_access === 'boolean' ? o.internet_access : ACT_DEFAULT.internet_access,
    tools_server_ip: o.tools_server_ip == null ? '' : String(o.tools_server_ip),
  }
}

export const isAct = (lab: Pick<Lab, 'target'> | null | undefined) => lab?.target === 'act'

/** backend 409 reasons (executors/act.py TRAFFIC_UNSUPPORTED, lab_routes snapshot) — shown on disabled buttons */
export const ACT_TRAFFIC_REASON = 'traffic tests are not supported on ACT labs in this iteration: clients are ACT generic nodes configured with the scripts in act/clients/ (run them from the tools server)'
export const ACT_SNAPSHOT_REASON = 'snapshots are not supported on ACT labs in this iteration'

export const ACT_ROLE_CLASS: Record<string, string> = {
  switch: 'bg-blue-100 text-blue-800 dark:bg-blue-900/60 dark:text-blue-200',
  'tools-server': 'bg-violet-100 text-violet-800 dark:bg-violet-900/60 dark:text-violet-200',
  client: 'bg-zinc-200 text-zinc-700 dark:bg-zinc-700 dark:text-zinc-200',
}
