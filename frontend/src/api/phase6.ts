import { api, ApiError } from './client'
import type { JobRef, Meta } from './types'

// ================================================================ CONTRACT-6 §1 updates
export interface LastUpdate { from: string | null; to: string | null; at: string | null; ok: boolean | null; mode?: string | null; job_id?: string | null }
export interface UpdateStatus {
  current: string
  latest: string | null
  available: boolean
  url: string | null
  notes: string
  published_at: string | null
  image?: string | null
  checked_at: string | null
  error?: string | null
  source?: string | null
  configured?: boolean
  hint?: string | null
  mode: 'source' | 'image' | 'manual' | string
  can_self_update: boolean
  reason: string | null
  last_update: LastUpdate | null
}

export const updateApi = {
  status: () => api.get<UpdateStatus>('/meta/update'),
  check: () => api.post<UpdateStatus>('/meta/update/check'),
  apply: (body: { force?: boolean; to?: string } = {}) => api.post<JobRef>('/meta/update/apply', body),
  log: () => api.get<{ log: string; last_update: LastUpdate | null }>('/meta/update/log'),
}

/**
 * After `app.update` the API container is recreated: poll `/api/meta` until an API answers with a version other than
 * `from` (or equal to `to`). Resolves with the new version; rejects on timeout. `get` / `sleep` are injectable for tests.
 */
export async function waitForNewVersion(from: string, to: string | null, opts: {
  get?: () => Promise<Meta>; sleep?: (ms: number) => Promise<void>; interval?: number; timeout?: number; onTick?: (s: 'down' | 'old' | 'new') => void
} = {}): Promise<string> {
  const get = opts.get ?? (() => api.get<Meta>('/meta', { silent401: true }))
  const sleep = opts.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)))
  const interval = opts.interval ?? 3000
  const deadline = Date.now() + (opts.timeout ?? 15 * 60_000)
  for (;;) {
    try {
      const m = await get()
      if (m.version !== from || (to && m.version === to)) { opts.onTick?.('new'); return m.version }
      opts.onTick?.('old')
    } catch {
      opts.onTick?.('down')
    }
    if (Date.now() > deadline) throw new Error('timed out waiting for the new version')
    await sleep(interval)
  }
}

// ================================================================ CONTRACT-6 §2 template metadata / §3 knob learn
export interface KnobLearn {
  summary?: string | null
  avd_keys?: string[]
  renders?: string | null
  /** strings; YAML may turn "A: b" into {A: b} — see pitfallText */
  pitfalls?: (string | Record<string, unknown>)[]
  doc_url?: string | null
}

// ================================================================ CONTRACT-6 §3 learning
export type SchemaName = 'eos_designs' | 'eos_cli_config_gen'
export interface SchemaChild { name: string; type: string | null; short: string; required?: boolean; deprecated?: boolean }
export interface SchemaEntry {
  schema?: SchemaName
  key: string
  type: string | null
  description: string | null
  default: unknown
  valid_values: unknown[] | null
  min: number | null
  max: number | null
  required: boolean
  deprecation: unknown
  format?: string | null
  pattern?: string | null
  primary_key?: string | null
  keys: SchemaChild[]
  items: { type: string | null; primary_key?: string | null; valid_values?: unknown[] | null; keys?: SchemaChild[] } | null
  doc_url: string | null
  section_url?: string | null
  alias_of?: string | null
}
export interface SchemaHit { schema: SchemaName; key: string; type: string | null; short: string; score: number }

export interface ExplainInput { key: string; value: unknown; where: string; knob?: string }
export interface ExplainLine {
  line: string
  no: number | null
  section: string | null
  parent: string | null
  rule: string | null
  avd_input: ExplainInput | null
  inputs: ExplainInput[]
  why: string | null
  doc_url: string | null
  related_lines: { no: number; text: string; section?: string | null }[]
  topic?: string | null
  error?: string
}
export interface ExplainSection { section: string | null; lines: number; explained: number; topics: string[] }
export interface ExplainFile { host: string; lines: ExplainLine[]; sections: ExplainSection[]; explained: number; total: number }

export type LessonLevel = 'intro' | 'core' | 'advanced'
export interface LessonProgress { passed: string[]; total: number; completed: boolean }
export interface LessonSummary {
  id: string
  title: string
  level: LessonLevel
  minutes: number
  requires: ('built' | 'deployed' | 'validated')[]
  tags: string[]
  order?: number | null
  summary?: string | null
  checkpoints: string[]
  tries: number
  quizzes: number
  progress?: LessonProgress
}
export type TryAction = 'open_file' | 'open_tab' | 'run_job' | 'set_knob'
export type LessonBlock =
  | { kind: 'markdown'; text: string }
  | { kind: 'try'; id: string; label: string; action: TryAction; args: Record<string, unknown>; endpoint?: { method: string; path: string } }
  | { kind: 'check'; id: string; label: string; type: string; args: Record<string, unknown>; hint?: string | null }
  | { kind: 'quiz'; id: string; question: string; options: string[]; answer: number; explanation: string }
export interface Lesson extends LessonSummary { blocks: LessonBlock[]; passed: string[]; prev: string | null; next: string | null }
export interface CheckResult { lesson_id: string; checkpoint_id: string; passed: boolean; detail: string; hint: string | null }
export interface ProgressAll { lessons: Record<string, LessonProgress>; completed: number; total: number; items: { lesson_id: string; checkpoint_id: string; lab_id: string | null; passed_at: string | null }[] }

export interface GlossaryTerm { term: string; short: string; long?: string | null; aliases: string[]; see: string[]; lesson?: string | null; doc_url?: string | null }
export interface TourStep { selector: string; title: string; text: string; placement?: string; lesson?: string }
export interface Tour { title: string; route: string; steps: TourStep[] }
export type Tours = Record<string, Tour>

const q = (o: Record<string, string | number | undefined>) =>
  Object.entries(o).filter(([, v]) => v !== undefined && v !== '').map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`).join('&')

export const learnApi = {
  schema: (name: SchemaName, key = '') => api.get<SchemaEntry>(`/learn/schema/${name}?${q({ key })}`),
  schemaSearch: (text: string, schema: SchemaName | 'all' = 'all', limit = 25) => api.get<SchemaHit[]>(`/learn/schema/search?${q({ q: text, schema, limit })}`),
  explain: (lab_id: string, node: string, line: string | number) => api.get<ExplainLine>(`/learn/explain?${q({ lab_id, node, line: String(line) })}`),
  explainFile: (lab_id: string, node: string) => api.post<ExplainFile>('/learn/explain', { lab_id, node, all: true }),
  lessons: () => api.get<LessonSummary[]>('/learn/lessons'),
  lesson: (id: string) => api.get<Lesson>(`/learn/lessons/${encodeURIComponent(id)}`),
  check: (id: string, body: { lab_id: string | null; checkpoint_id: string }) => api.post<CheckResult>(`/learn/lessons/${encodeURIComponent(id)}/check`, body),
  progress: () => api.get<ProgressAll>('/learn/progress'),
  glossary: () => api.get<GlossaryTerm[]>('/learn/glossary'),
  tours: () => api.get<Tours>('/learn/tours'),
}

/** schema lookup that returns null on 404 (unknown key) */
export async function schemaOrNull(name: SchemaName, key: string): Promise<SchemaEntry | null> {
  try {
    return await learnApi.schema(name, key)
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null
    throw e
  }
}

// ================================================================ CONTRACT-6 §4 hot-added test hosts
export interface TestHostIn {
  name: string
  switch: string
  switch_port: string
  vlans: string
  mode: 'access' | 'trunk'
  ip: string
  image?: string | null
  deploy: boolean
}
export interface TestHostCreated { job_id: string | null; client: string; switch: string; switch_port: string; rebuilt?: string[]; push_hosts?: string[]; commit?: string }
export interface ExecResult { client: string; cmd: string; rc: number; stdout: string; stderr: string }
export interface ClientPing { src: string; dst: string; target_ip: string | null; sent: number; received: number; loss_pct: number; rtt_avg_ms: number | null; ok: boolean; output?: string }
export interface ClientIperf {
  src: string; dst: string; target_ip: string | null; proto: string; ok: boolean; error?: string | null; seconds?: number
  bits_per_second_sent?: number | null; bits_per_second_received?: number | null; retransmits?: number | null
}

const e = encodeURIComponent
export const testHostApi = {
  add: (labId: string, body: TestHostIn) => api.post<TestHostCreated>(`/labs/${labId}/clients`, body),
  remove: (labId: string, name: string) => api.delete<{ job_id: string | null; client: string; push_hosts: string[] }>(`/labs/${labId}/clients/${e(name)}`),
  exec: (labId: string, name: string, cmd: string) => api.post<ExecResult>(`/labs/${labId}/clients/${e(name)}/exec`, { cmd }),
  ping: (labId: string, name: string, dst: string, count = 3) => api.post<ClientPing>(`/labs/${labId}/clients/${e(name)}/ping`, { dst, count }),
  iperf: (labId: string, name: string, dst: string, seconds = 5) => api.post<ClientIperf>(`/labs/${labId}/clients/${e(name)}/iperf`, { dst, seconds }),
}
