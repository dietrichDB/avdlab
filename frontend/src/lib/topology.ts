import type { Topology, TopoNode } from '../api/types'

export type Role = 'spine' | 'l3leaf' | 'l2leaf' | 'host' | 'other'

export interface FlowNode {
  id: string
  type: 'device'
  position: { x: number; y: number }
  data: { label: string; role: Role; kind: string; mgmt_ip?: string | null; ifaces: string[] }
}

export interface FlowEdge {
  id: string
  source: string
  target: string
  label: string
  /** link: index into topology.links */
  data: { a_if: string; b_if: string; link: number }
}

export function roleOf(n: TopoNode): Role {
  const r = (n.role ?? '').toLowerCase()
  const name = n.name.toLowerCase()
  if (r === 'spine' || r === 'super_spine' || (!r && /spine/.test(name))) return 'spine'
  if (r === 'l3leaf' || r === 'leaf' || (!r && /leaf/.test(name) && !/leaf\d+c/.test(name))) return 'l3leaf'
  if (r === 'l2leaf' || (!r && /leaf\d+c/.test(name))) return 'l2leaf'
  if (r === 'host' || r === 'server' || r === 'endpoint' || n.kind === 'linux' || (!r && /host|server|client/.test(name))) return 'host'
  if (!r && n.kind === 'ceos') return 'l3leaf'
  return 'other'
}

export const roleColour: Record<Role, { bg: string; border: string }> = {
  spine: { bg: '#dbeafe', border: '#2563eb' },
  l3leaf: { bg: '#d1fae5', border: '#059669' },
  l2leaf: { bg: '#fef3c7', border: '#d97706' },
  host: { bg: '#e4e4e7', border: '#71717a' },
  other: { bg: '#f4f4f5', border: '#a1a1aa' },
}

const ROW: Record<Role, number> = { spine: 0, l3leaf: 1, l2leaf: 2, host: 3, other: 4 }

/** Turns the /topology/{id} JSON into positioned React Flow nodes and edges (layered by role). */
export function toFlow(topo: Topology, opts: { xGap?: number; yGap?: number; positions?: Record<string, { x: number; y: number }> | null } = {}): { nodes: FlowNode[]; edges: FlowEdge[] } {
  const xGap = opts.xGap ?? 200
  const yGap = opts.yGap ?? 150
  const ifaces = new Map<string, Set<string>>()
  for (const l of topo.links) {
    ;(ifaces.get(l.a) ?? ifaces.set(l.a, new Set()).get(l.a)!).add(l.a_if)
    ;(ifaces.get(l.b) ?? ifaces.set(l.b, new Set()).get(l.b)!).add(l.b_if)
  }
  const rows = new Map<number, TopoNode[]>()
  const roles = new Map<string, Role>()
  for (const n of topo.nodes) {
    const role = roleOf(n)
    roles.set(n.name, role)
    const r = ROW[role]
    ;(rows.get(r) ?? rows.set(r, []).get(r)!).push(n)
  }
  const widest = Math.max(1, ...[...rows.values()].map((r) => r.length))
  const nodes: FlowNode[] = []
  for (const [row, list] of [...rows.entries()].sort((a, b) => a[0] - b[0])) {
    const sorted = [...list].sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }))
    const offset = ((widest - sorted.length) * xGap) / 2
    sorted.forEach((n, i) => {
      nodes.push({
        id: n.name,
        type: 'device',
        position: opts.positions?.[n.name] ? { x: opts.positions[n.name].x, y: opts.positions[n.name].y } : { x: offset + i * xGap, y: row * yGap },
        data: { label: n.name, role: roles.get(n.name)!, kind: n.kind, mgmt_ip: n.mgmt_ip, ifaces: [...(ifaces.get(n.name) ?? [])].sort() },
      })
    })
  }
  const known = new Set(nodes.map((n) => n.id))
  const edges: FlowEdge[] = topo.links
    .map((l, link) => ({ l, link }))
    .filter(({ l }) => known.has(l.a) && known.has(l.b))
    .map(({ l, link }, i) => ({
      id: `${l.a}:${l.a_if}--${l.b}:${l.b_if}#${i}`,
      source: l.a,
      target: l.b,
      label: `${shortIf(l.a_if)} ↔ ${shortIf(l.b_if)}`,
      data: { a_if: l.a_if, b_if: l.b_if, link },
    }))
  return { nodes, edges }
}

export function shortIf(i: string): string {
  return i.replace(/^Ethernet/, 'Et').replace(/^Management/, 'Ma').replace(/^Port-Channel/, 'Po')
}
