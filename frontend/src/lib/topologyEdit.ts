import type { EditOp, EditRejection, EndpointAdapter, Topology, TopologyPorts, Violation } from '../api/types'
import { ApiError, extractViolations } from '../api/client'
import { roleOf, shortIf, type Role } from './topology'

/** Body of POST /topology/{id}/edit for the pending ops list. */
export function editPayload(ops: EditOp[], dry_run: boolean): { ops: EditOp[]; dry_run: boolean } {
  return { ops: ops.map((o) => ({ ...o })), dry_run }
}

/** One-line human description of an op (pending ops list). */
export function describeOp(o: EditOp): string {
  switch (o.op) {
    case 'set_uplink': return `${o.node}: uplink #${o.index + 1} → ${o.switch} ${shortIf(o.switch_port)}`
    case 'add_endpoint': return `add ${o.type.replace(/s$/, '')} ${o.name} (${o.adapters.map((a) => a.switches.map((s, i) => `${s}:${shortIf(a.switch_ports[i] ?? '?')}`).join(' + ')).join(', ')})`
    case 'remove_endpoint': return `remove endpoint ${o.name}`
    case 'update_endpoint': return `update endpoint ${o.name}`
    case 'add_leaf_pair': return '+ leaf pair'
    case 'remove_last_leaf_pair': return '− last leaf pair'
    case 'set_l2leaf_per_pair': return `L2 leafs per pair = ${o.n}`
    case 'set_spine_count': return `spine count = ${o.n}`
  }
}

/** Switch ports claimed by pending ops (so the pickers don't offer them twice). */
export function reservedPorts(ops: EditOp[]): Map<string, Set<string>> {
  const out = new Map<string, Set<string>>()
  const add = (sw: string, p: string) => (out.get(sw) ?? out.set(sw, new Set()).get(sw)!).add(p)
  for (const o of ops) {
    if (o.op === 'set_uplink') add(o.switch, o.switch_port)
    if (o.op === 'add_endpoint' || o.op === 'update_endpoint') for (const a of o.adapters) a.switches.forEach((s, i) => a.switch_ports[i] && add(s, a.switch_ports[i]))
  }
  return out
}

/** Free ports of a node offered by a picker: the server's free list minus ports reserved by pending ops. */
export function freePortsFor(ports: TopologyPorts | undefined, node: string, ops: EditOp[] = []): string[] {
  const free = ports?.[node]?.free ?? []
  const taken = reservedPorts(ops).get(node)
  return free.filter((p) => !taken?.has(p))
}

export interface Uplink { index: number; node: string; node_if: string; switch: string; switch_if: string; edgeKey: string }

const UPLINK_PARENT: Partial<Record<Role, Role[]>> = { l3leaf: ['spine'], l2leaf: ['l3leaf'], spine: [] }

/**
 * Uplinks of a node in `uplink_switches` order: links towards its parent layer (leaf → spine, l2leaf → l3leaf),
 * sorted by the node's own interface (AVD assigns uplink_interfaces in order).
 */
export function uplinksOf(topo: Topology, node: string): Uplink[] {
  const roles = new Map(topo.nodes.map((n) => [n.name, roleOf(n)]))
  const parents = UPLINK_PARENT[roles.get(node) ?? 'other'] ?? []
  const out: Omit<Uplink, 'index'>[] = []
  topo.links.forEach((l, i) => {
    const key = `${i}`
    if (l.a === node && parents.includes(roles.get(l.b)!)) out.push({ node, node_if: l.a_if, switch: l.b, switch_if: l.b_if, edgeKey: key })
    else if (l.b === node && parents.includes(roles.get(l.a)!)) out.push({ node, node_if: l.b_if, switch: l.a, switch_if: l.a_if, edgeKey: key })
  })
  return out
    .sort((x, y) => x.node_if.localeCompare(y.node_if, undefined, { numeric: true }))
    .map((u, index) => ({ ...u, index }))
}

/** Candidate switches an uplink of `node` may be moved to (same parent layer). */
export function uplinkTargets(topo: Topology, node: string): string[] {
  const role = roleOf(topo.nodes.find((n) => n.name === node) ?? { name: node, kind: '' })
  const parents = UPLINK_PARENT[role] ?? []
  return topo.nodes.filter((n) => parents.includes(roleOf(n))).map((n) => n.name).sort((a, b) => a.localeCompare(b, undefined, { numeric: true }))
}

/** For a retargeted edge (drag of one end onto another node) → the uplink it is and the new switch, or null when not an uplink move. */
export function retargetUplink(topo: Topology, linkIndex: number, newSource: string, newTarget: string): { node: string; index: number; switch: string } | null {
  const l = topo.links[linkIndex]
  if (!l) return null
  for (const node of [l.a, l.b]) {
    const up = uplinksOf(topo, node).find((u) => u.edgeKey === String(linkIndex))
    if (!up) continue
    // the node end must stay; the other end moved to a new switch of the same layer
    const other = newSource === node ? newTarget : newTarget === node ? newSource : null
    if (!other || other === up.switch) return null
    if (!uplinkTargets(topo, node).includes(other)) return null
    return { node, index: up.index, switch: other }
  }
  return null
}

export interface LeafGroup { label: string; switches: string[] }

/** Leaf pairs (from mlag_peer links, else name suffix a/b) and single leafs, for the Add-server form. */
export function leafGroups(topo: Topology): LeafGroup[] {
  const leaves = topo.nodes.filter((n) => { const r = roleOf(n); return r === 'l3leaf' || r === 'l2leaf' }).map((n) => n.name)
  const sort = (xs: string[]) => [...xs].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }))
  const pairs = new Map<string, string[]>()
  for (const l of topo.links) {
    if (l.type !== 'mlag_peer') continue
    const k = sort([l.a, l.b]).join('|')
    pairs.set(k, sort([l.a, l.b]))
  }
  if (!pairs.size) {
    const by = new Map<string, string[]>()
    for (const n of leaves) { const m = /^(.*\d)([ab])$/.exec(n); if (m) (by.get(m[1]) ?? by.set(m[1], []).get(m[1])!).push(n) }
    for (const v of by.values()) if (v.length === 2) pairs.set(sort(v).join('|'), sort(v))
  }
  const out: LeafGroup[] = [...pairs.values()].sort((a, b) => a[0].localeCompare(b[0], undefined, { numeric: true })).map((p) => ({ label: `${p[0]} + ${p[1]}`, switches: p }))
  for (const n of sort(leaves)) out.push({ label: n, switches: [n] })
  return out
}

export interface EndpointForm {
  name: string
  type: string
  switches: string[]
  switch_ports: string[]
  endpoint_ports: string[]
  mode: 'access' | 'trunk'
  vlans: string
  native_vlan: string
  port_channel: boolean
}

/** Add-server form → add_endpoint op. */
export function endpointOp(f: EndpointForm): Extract<EditOp, { op: 'add_endpoint' }> {
  const a: EndpointAdapter = {
    switches: [...f.switches],
    switch_ports: f.switches.map((_, i) => f.switch_ports[i]),
    endpoint_ports: f.switches.map((_, i) => f.endpoint_ports[i] || `eth${i + 1}`),
    mode: f.mode,
    vlans: f.vlans.trim(),
  }
  if (f.mode === 'trunk' && f.native_vlan.trim()) a.native_vlan = Number(f.native_vlan)
  if (f.port_channel && f.switches.length > 1) a.port_channel = { mode: 'active' }
  return { op: 'add_endpoint', name: f.name.trim(), type: f.type, adapters: [a] }
}

export function endpointFormErrors(f: EndpointForm, existing: string[]): string[] {
  const e: string[] = []
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(f.name.trim())) e.push('name: letters, digits, - _ . only')
  else if (existing.includes(f.name.trim())) e.push(`name: ${f.name.trim()} already exists`)
  if (!f.switches.length) e.push('pick a leaf pair or leaf')
  f.switches.forEach((s, i) => { if (!f.switch_ports[i]) e.push(`pick a port on ${s}`) })
  if (!/^\d+([-,]\d+)*$/.test(f.vlans.trim())) e.push('vlans: e.g. 11 or 11-12 or 11,13')
  if (f.mode === 'access' && /[-,]/.test(f.vlans.trim())) e.push('access mode takes a single VLAN')
  return e
}

/** A 422 from /edit → message, violations and the failing op index. */
export function editRejection(err: unknown): EditRejection | null {
  if (!(err instanceof ApiError) || err.status !== 422) return null
  const d = (err.detail ?? {}) as Partial<EditRejection> & { detail?: unknown }
  const violations: Violation[] = d.violations ? extractViolations(d.violations) : extractViolations(err.detail)
  return { message: typeof d.message === 'string' ? d.message : 'validation failed', violations, op_index: typeof d.op_index === 'number' ? d.op_index : null }
}

/** Error text for any other failure (409 for custom labs carries a hint). */
export function editErrorText(err: unknown): string {
  if (err instanceof ApiError) {
    const d = err.detail as { message?: string; hint?: string } | string | null
    if (typeof d === 'string') return d
    if (d && typeof d === 'object') return [d.message, d.hint].filter(Boolean).join(' — ') || err.message
  }
  return err instanceof Error ? err.message : String(err)
}
