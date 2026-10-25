import type { NetBoxSyncResult } from '../api/types'

/** netbox/sync.py object kinds, in dependency order */
export const NETBOX_KINDS = ['tag', 'manufacturer', 'platform', 'device_type', 'device_role', 'site', 'location', 'rack', 'device',
  'interface', 'vlan', 'vrf', 'prefix', 'ip_address', 'cable'] as const

export interface SyncRow { type: string; created: number; updated: number; deleted: number; unchanged: number }

/** One row per object type seen in any of the counters, canonical order first. */
export function syncRows(r: NetBoxSyncResult): SyncRow[] {
  const types = new Set([...Object.keys(r.created ?? {}), ...Object.keys(r.updated ?? {}), ...Object.keys(r.deleted ?? {}), ...Object.keys(r.unchanged ?? {})])
  const order = (t: string) => { const i = (NETBOX_KINDS as readonly string[]).indexOf(t); return i < 0 ? 999 : i }
  return [...types].sort((a, b) => order(a) - order(b) || a.localeCompare(b)).map((type) => ({
    type, created: r.created?.[type] ?? 0, updated: r.updated?.[type] ?? 0, deleted: r.deleted?.[type] ?? 0, unchanged: r.unchanged?.[type] ?? 0,
  }))
}

export function syncTotals(r: NetBoxSyncResult): Omit<SyncRow, 'type'> {
  const sum = (o?: Record<string, number>) => Object.values(o ?? {}).reduce((a, b) => a + b, 0)
  return { created: sum(r.created), updated: sum(r.updated), deleted: sum(r.deleted), unchanged: sum(r.unchanged) }
}
