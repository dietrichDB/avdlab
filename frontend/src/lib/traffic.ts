import type { IperfFlow, IperfResultRow, LabClient, PingResult, PingResultRow, TrafficRun } from '../api/types'

export type LossLevel = 'ok' | 'partial' | 'down' | 'none'

/** green = 0 % loss, amber = some loss, red = 100 % (or unreachable), grey = not measured */
export function lossLevel(loss: number | null | undefined): LossLevel {
  if (loss === null || loss === undefined || Number.isNaN(loss)) return 'none'
  if (loss <= 0) return 'ok'
  if (loss >= 100) return 'down'
  return 'partial'
}

export const LOSS_CLASSES: Record<LossLevel, string> = {
  ok: 'bg-emerald-100 text-emerald-900 dark:bg-emerald-900/60 dark:text-emerald-100',
  partial: 'bg-amber-100 text-amber-900 dark:bg-amber-900/60 dark:text-amber-100',
  down: 'bg-red-100 text-red-900 dark:bg-red-900/60 dark:text-red-100',
  none: 'bg-zinc-100 text-zinc-400 dark:bg-zinc-800 dark:text-zinc-500',
}

/** Row/column names of a ping matrix: union of src keys and dst keys, sorted. */
export function matrixNames(matrix: PingResult['matrix']): string[] {
  const s = new Set<string>()
  for (const [src, row] of Object.entries(matrix ?? {})) {
    s.add(src)
    for (const dst of Object.keys(row ?? {})) s.add(dst)
  }
  return [...s].sort()
}

/** Builds the matrix from the flat result rows when the backend omitted it. */
export function matrixFromResults(rows: PingResultRow[]): PingResult['matrix'] {
  const m: PingResult['matrix'] = {}
  for (const r of rows) (m[r.src] ??= {})[r.dst] = r.loss_pct
  return m
}

/** Clients that can be ping/iperf endpoints (have at least one IP). */
export const clientsWithIp = (clients: LabClient[]) => clients.filter((c) => c.vlans.some((v) => !!v.ip))

export function asPingResult(raw: unknown): PingResult | null {
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Partial<PingResult>
  if (!Array.isArray(o.results) && !o.matrix) return null
  const results = Array.isArray(o.results) ? o.results : []
  return { results, matrix: o.matrix && Object.keys(o.matrix).length ? o.matrix : matrixFromResults(results) }
}

export function asIperfResults(raw: unknown): IperfResultRow[] | null {
  if (Array.isArray(raw)) return raw as IperfResultRow[]
  if (raw && typeof raw === 'object') {
    const o = raw as { results?: unknown; flows?: unknown }
    if (Array.isArray(o.results)) return o.results as IperfResultRow[]
    if (Array.isArray(o.flows)) return o.flows as IperfResultRow[]
  }
  return null
}

export function runSummary(run: TrafficRun): string {
  const p = run.params ?? {}
  if (run.kind === 'mss') {
    const cases = p.cases as unknown[] | null | undefined
    return cases?.length ? `MSS verify · ${cases.length} case(s)` : 'MSS verify · auto-derived'
  }
  if (run.kind === 'ping') {
    const pairs = p.pairs as unknown[] | null | undefined
    return `${pairs?.length ? `${pairs.length} pair(s)` : 'full mesh'} · count ${p.count ?? 3}`
  }
  const flows = (p.flows as IperfFlow[] | undefined) ?? []
  return flows.map((f) => `${f.src}→${f.dst} ${f.proto}`).join(', ') || 'iperf'
}

export function fmtMbps(bps: number | null | undefined): string {
  if (bps == null || Number.isNaN(bps)) return '—'
  const m = bps / 1e6
  return m >= 1000 ? `${(m / 1000).toFixed(2)} Gbps` : `${m.toFixed(m < 10 ? 2 : 1)} Mbps`
}

export const newFlow = (src = '', dst = ''): IperfFlow => ({ src, dst, proto: 'tcp', bandwidth: null, duration: 10, parallel: 1, reverse: false })
