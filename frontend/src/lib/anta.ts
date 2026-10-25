import { ANTA_STATUSES, type AntaResult, type AntaStatus, type ValidationTotals } from '../api/types'

export interface ResultFilter {
  /** statuses to show; empty = all */
  statuses: AntaStatus[]
  /** device name; '' = all */
  device: string
  /** free text over test, categories, messages, custom_field */
  text?: string
}

export function filterResults(rows: AntaResult[], f: ResultFilter): AntaResult[] {
  const st = new Set(f.statuses)
  const q = (f.text ?? '').trim().toLowerCase()
  return rows.filter((r) => {
    if (st.size && !st.has(r.result)) return false
    if (f.device && r.name !== f.device) return false
    if (q) {
      const hay = [r.test, r.description, ...(r.categories ?? []), ...(r.messages ?? []), r.custom_field ?? ''].join(' ').toLowerCase()
      if (!hay.includes(q)) return false
    }
    return true
  })
}

export function emptyTotals(): ValidationTotals {
  return { success: 0, failure: 0, error: 0, skipped: 0, unset: 0 }
}

export function countByStatus(rows: AntaResult[]): ValidationTotals {
  const t = emptyTotals()
  for (const r of rows) if (r.result in t) t[r.result]++
  return t
}

export function devicesOf(rows: AntaResult[]): string[] {
  return [...new Set(rows.map((r) => r.name))].sort()
}

/** failures/errors first, then by device and test */
const rank: Record<AntaStatus, number> = { error: 0, failure: 1, unset: 2, skipped: 3, success: 4 }
export function sortResults(rows: AntaResult[]): AntaResult[] {
  return [...rows].sort((a, b) => (rank[a.result] ?? 9) - (rank[b.result] ?? 9) || a.name.localeCompare(b.name) || a.test.localeCompare(b.test))
}

export const statusColours: Record<AntaStatus, string> = {
  success: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/60 dark:text-emerald-200',
  failure: 'bg-red-100 text-red-800 dark:bg-red-900/60 dark:text-red-200',
  error: 'bg-fuchsia-100 text-fuchsia-800 dark:bg-fuchsia-900/60 dark:text-fuchsia-200',
  skipped: 'bg-zinc-200 text-zinc-700 dark:bg-zinc-700 dark:text-zinc-200',
  unset: 'bg-amber-100 text-amber-800 dark:bg-amber-900/60 dark:text-amber-200',
}

export { ANTA_STATUSES }
