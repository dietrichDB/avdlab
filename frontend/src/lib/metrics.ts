import type { DiskUsage, HostMetrics } from '../api/types'

export const WINDOW_MS = 2 * 3600_000
export const GB = 1024 ** 3
const MB = 1024 ** 2
const r1 = (n: number) => Math.round(n * 10) / 10
const r2 = (n: number) => Math.round(n * 100) / 100

export interface HostRow {
  t: number
  cpu: number | null
  load1: number | null
  load5: number | null
  load15: number | null
  memUsed: number | null
  memAvail: number | null
  memTotal: number | null
  /** disk used % keyed by mount */
  [disk: `disk:${string}`]: number | null
}

/** points → chart rows (GB for memory, % for disk), restricted to the 2 h before the newest point */
export function hostRows(points: HostMetrics['points'], windowMs = WINDOW_MS): HostRow[] {
  const rows = points
    .map((p) => {
      const row: HostRow = {
        t: new Date(p.at).getTime(),
        cpu: p.cpu_pct == null ? null : r1(p.cpu_pct),
        load1: p.load1, load5: p.load5, load15: p.load15,
        memUsed: p.mem_used == null ? null : r2(p.mem_used / GB),
        memAvail: p.mem_available == null ? null : r2(p.mem_available / GB),
        memTotal: p.mem_total == null ? null : r2(p.mem_total / GB),
      }
      for (const [mount, d] of Object.entries(p.disk ?? {})) row[`disk:${mount}`] = d.size ? r1((d.used / d.size) * 100) : null
      return row
    })
    .filter((r) => !Number.isNaN(r.t))
    .sort((a, b) => a.t - b.t)
  if (!rows.length) return rows
  const end = rows[rows.length - 1].t
  return rows.filter((r) => r.t >= end - windowMs)
}

export function diskMounts(points: HostMetrics['points']): string[] {
  const s = new Set<string>()
  points.forEach((p) => Object.keys(p.disk ?? {}).forEach((m) => s.add(m)))
  return [...s].sort()
}

/** latest disk usage per mount (for the summary under the chart) */
export function latestDisk(points: HostMetrics['points']) {
  const last = [...points].sort((a, b) => a.at.localeCompare(b.at)).slice(-1)[0]
  return Object.entries(last?.disk ?? ({} as Record<string, DiskUsage>)).map(([mount, d]) => ({ mount, ...d, pct: d.size ? (d.used / d.size) * 100 : 0 }))
}

/** top-N containers by their latest memory use */
export function topContainers(containers: HostMetrics['containers'], n = 10): string[] {
  return Object.entries(containers ?? {})
    .map(([name, pts]) => {
      const last = [...pts].sort((a, b) => a.at.localeCompare(b.at)).slice(-1)[0]
      return { name, mem: last?.mem_used ?? 0 }
    })
    .sort((a, b) => b.mem - a.mem || a.name.localeCompare(b.name))
    .slice(0, n)
    .map((x) => x.name)
}

/** container series merged on timestamp → rows {t, [name]: MB} for a multi-line chart */
export function containerRows(containers: HostMetrics['containers'], names: string[], windowMs = WINDOW_MS): Record<string, number>[] {
  const byT = new Map<number, Record<string, number>>()
  for (const name of names) {
    for (const p of containers[name] ?? []) {
      const t = new Date(p.at).getTime()
      if (Number.isNaN(t) || p.mem_used == null) continue
      const row = byT.get(t) ?? { t }
      row[name] = r1(p.mem_used / MB)
      byT.set(t, row)
    }
  }
  const rows = [...byT.values()].sort((a, b) => a.t - b.t)
  if (!rows.length) return rows
  const end = rows[rows.length - 1].t
  return rows.filter((r) => r.t >= end - windowMs)
}

export const fmtTime = (t: number) => new Date(t).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })

/** short container label: clab-<lab>-<node> → <node> */
export const shortContainer = (name: string) => name.replace(/^clab-/, '')
