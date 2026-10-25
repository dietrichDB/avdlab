import { useMemo, useState, type ReactNode } from 'react'
import type { EditOp, EditRejection, EditResult, Topology, TopologyPorts } from '../api/types'
import { roleOf, shortIf } from '../lib/topology'
import {
  describeOp, endpointFormErrors, endpointOp, freePortsFor, leafGroups, uplinksOf, uplinkTargets, type EndpointForm,
} from '../lib/topologyEdit'
import { KV } from './ui'
import { cls } from '../lib/format'

// ---------------------------------------------------------------- port picker
/** Lists only the free ports of `node` (server free list minus pending reservations). */
export function PortPicker({ node, ports, ops, value, onChange, testId }: {
  node: string; ports: TopologyPorts | undefined; ops: EditOp[]; value: string; onChange: (p: string) => void; testId?: string
}) {
  const free = freePortsFor(ports, node, ops)
  return (
    <select className="input py-0.5 text-xs" value={value} onChange={(e) => onChange(e.target.value)} data-testid={testId ?? `port-picker-${node}`}>
      <option value="">{free.length ? 'free port…' : 'no free ports'}</option>
      {free.map((p) => <option key={p} value={p}>{p}</option>)}
    </select>
  )
}

/** Popover after dragging an uplink end onto another switch (or "Move" in the inspector): pick switch + free port → set_uplink. */
export function UplinkPopover({ topo, ports, ops, node, index, initialSwitch, at, onAdd, onCancel }: {
  topo: Topology; ports: TopologyPorts | undefined; ops: EditOp[]; node: string; index: number; initialSwitch: string
  at?: { x: number; y: number }; onAdd: (op: EditOp) => void; onCancel: () => void
}) {
  const [sw, setSw] = useState(initialSwitch)
  const [port, setPort] = useState('')
  const targets = uplinkTargets(topo, node)
  const current = uplinksOf(topo, node)[index]
  return (
    <div className="card absolute z-10 w-64 space-y-2 p-3 text-xs shadow-lg" style={at ? { left: Math.max(8, at.x - 128), top: Math.max(8, at.y + 8) } : { right: 8, top: 8 }}
      data-testid="uplink-popover" onPointerDown={(e) => e.stopPropagation()}>
      <div className="font-medium">Move {node} uplink #{index + 1}</div>
      {current && <div className="text-zinc-500">now {shortIf(current.node_if)} → {current.switch} {shortIf(current.switch_if)}</div>}
      <label className="block">
        <span className="label">Switch</span>
        <select className="input py-0.5 text-xs" value={sw} onChange={(e) => { setSw(e.target.value); setPort('') }} data-testid="uplink-switch">
          {targets.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
      </label>
      <label className="block">
        <span className="label">Switch port</span>
        <PortPicker node={sw} ports={ports} ops={ops} value={port} onChange={setPort} testId="uplink-port" />
      </label>
      <div className="flex justify-end gap-2">
        <button type="button" className="btn-secondary btn-sm" onClick={onCancel}>Cancel</button>
        <button type="button" className="btn-primary btn-sm" disabled={!sw || !port} data-testid="uplink-add"
          onClick={() => onAdd({ op: 'set_uplink', node, index, switch: sw, switch_port: port })}>Add change</button>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------- add server
const emptyForm = (): EndpointForm => ({ name: '', type: 'servers', switches: [], switch_ports: [], endpoint_ports: ['eth1', 'eth2'], mode: 'trunk', vlans: '', native_vlan: '', port_channel: true })

export function AddEndpointForm({ topo, ports, ops, onAdd, onCancel }: {
  topo: Topology; ports: TopologyPorts | undefined; ops: EditOp[]; onAdd: (op: EditOp) => void; onCancel: () => void
}) {
  const [f, setF] = useState<EndpointForm>(emptyForm)
  const [touched, setTouched] = useState(false)
  const groups = useMemo(() => leafGroups(topo), [topo])
  const existing = useMemo(() => [
    ...topo.nodes.map((n) => n.name),
    ...ops.flatMap((o) => (o.op === 'add_endpoint' ? [o.name] : [])),
  ], [topo, ops])
  const errors = endpointFormErrors(f, existing)
  const set = (p: Partial<EndpointForm>) => setF((x) => ({ ...x, ...p }))
  const group = groups.find((g) => g.switches.join('|') === f.switches.join('|'))

  return (
    <form className="card space-y-2 p-3 text-xs" data-testid="add-endpoint-form"
      onSubmit={(e) => { e.preventDefault(); setTouched(true); if (!errors.length) { onAdd(endpointOp(f)); setF(emptyForm()); setTouched(false) } }}>
      <div className="font-medium">Add server</div>
      <div className="grid grid-cols-2 gap-2">
        <label className="block"><span className="label">Name</span>
          <input className="input py-0.5 text-xs" value={f.name} onChange={(e) => set({ name: e.target.value })} data-testid="ep-name" /></label>
        <label className="block"><span className="label">Type</span>
          <select className="input py-0.5 text-xs" value={f.type} onChange={(e) => set({ type: e.target.value })} data-testid="ep-type">
            {['servers', 'firewalls', 'routers', 'load_balancers', 'storage_arrays', 'generic_devices'].map((t) => <option key={t}>{t}</option>)}
          </select></label>
      </div>
      <label className="block"><span className="label">Attach to</span>
        <select className="input py-0.5 text-xs" value={group?.label ?? ''} data-testid="ep-attach"
          onChange={(e) => {
            const g = groups.find((x) => x.label === e.target.value)
            const sw = g?.switches ?? []
            set({ switches: sw, switch_ports: sw.map(() => ''), endpoint_ports: sw.map((_, i) => f.endpoint_ports[i] || `eth${i + 1}`), port_channel: sw.length > 1 })
          }}>
          <option value="">leaf pair or single leaf…</option>
          {groups.map((g) => <option key={g.label} value={g.label}>{g.switches.length > 1 ? `pair ${g.label}` : g.label}</option>)}
        </select></label>
      {f.switches.map((s, i) => (
        <div key={s} className="grid grid-cols-[1fr_1fr_5rem] items-end gap-2">
          <span className="mono pb-1">{s}</span>
          <label className="block"><span className="label">Switch port</span>
            <PortPicker node={s} ports={ports} ops={ops} value={f.switch_ports[i] ?? ''}
              onChange={(p) => set({ switch_ports: f.switch_ports.map((x, j) => (j === i ? p : x)) })} /></label>
          <label className="block"><span className="label">NIC</span>
            <input className="input py-0.5 text-xs" value={f.endpoint_ports[i] ?? ''} onChange={(e) => set({ endpoint_ports: f.switches.map((_, j) => (j === i ? e.target.value : f.endpoint_ports[j] ?? '')) })} /></label>
        </div>
      ))}
      <div className="grid grid-cols-3 gap-2">
        <label className="block"><span className="label">Mode</span>
          <select className="input py-0.5 text-xs" value={f.mode} onChange={(e) => set({ mode: e.target.value as EndpointForm['mode'] })} data-testid="ep-mode">
            <option value="trunk">trunk</option><option value="access">access</option>
          </select></label>
        <label className="block"><span className="label">VLANs</span>
          <input className="input py-0.5 text-xs" value={f.vlans} placeholder="11-12" onChange={(e) => set({ vlans: e.target.value })} data-testid="ep-vlans" /></label>
        {f.mode === 'trunk' && <label className="block"><span className="label">Native VLAN</span>
          <input className="input py-0.5 text-xs" value={f.native_vlan} onChange={(e) => set({ native_vlan: e.target.value.replace(/\D/g, '') })} /></label>}
      </div>
      {f.switches.length > 1 && (
        <label className="flex items-center gap-1.5"><input type="checkbox" checked={f.port_channel} onChange={(e) => set({ port_channel: e.target.checked })} data-testid="ep-pc" /> Port-channel (LACP active, MLAG)</label>
      )}
      {touched && errors.length > 0 && <ul className="list-disc pl-4 text-red-600 dark:text-red-400" data-testid="ep-errors">{errors.map((e) => <li key={e}>{e}</li>)}</ul>}
      <div className="flex justify-end gap-2">
        <button type="button" className="btn-secondary btn-sm" onClick={onCancel}>Close</button>
        <button className="btn-primary btn-sm" data-testid="ep-add">Add change</button>
      </div>
    </form>
  )
}

// ---------------------------------------------------------------- pending ops
export function PendingOps({ ops, onRemove, onDiscard, onPreview, onApply, preview, rejection, error, busy }: {
  ops: EditOp[]; onRemove: (i: number) => void; onDiscard: () => void; onPreview: () => void; onApply: () => void
  preview: EditResult | null; rejection: EditRejection | null; error: string | null; busy: boolean
}) {
  if (!ops.length && !error) return null
  return (
    <section className="card space-y-2 p-3 text-xs" data-testid="pending-ops">
      <div className="flex items-center gap-2">
        <span className="h2 flex-1">Pending changes ({ops.length})</span>
        <button className="btn-secondary btn-sm" onClick={onDiscard} disabled={busy} data-testid="ops-discard">Discard</button>
        {preview
          ? <button className="btn-primary btn-sm" onClick={onApply} disabled={busy || !ops.length} data-testid="ops-apply">{busy ? 'Applying…' : 'Apply'}</button>
          : <button className="btn-primary btn-sm" onClick={onPreview} disabled={busy || !ops.length} data-testid="ops-preview">{busy ? 'Checking…' : 'Preview (dry run)'}</button>}
      </div>
      <ol className="space-y-0.5">
        {ops.map((o, i) => (
          <li key={i} className={cls('flex items-center gap-2 rounded px-1.5 py-0.5', rejection?.op_index === i ? 'bg-red-50 text-red-800 dark:bg-red-950/40 dark:text-red-200' : 'bg-zinc-50 dark:bg-zinc-800/50')}
            data-testid={`op-${i}`}>
            <span className="w-4 text-zinc-400">{i + 1}.</span>
            <span className="mono flex-1">{describeOp(o)}</span>
            <button className="text-zinc-400 hover:text-red-600" onClick={() => onRemove(i)} aria-label={`remove change ${i + 1}`} disabled={busy}>×</button>
          </li>
        ))}
      </ol>
      {rejection && (
        <div className="rounded border border-red-300 bg-red-50 px-2 py-1.5 text-red-800 dark:border-red-800 dark:bg-red-950/40 dark:text-red-200" data-testid="ops-violations">
          <div className="font-medium">{rejection.message}{rejection.op_index !== null ? ` (change ${rejection.op_index + 1})` : ''}</div>
          <ul className="mono list-disc pl-4">{rejection.violations.map((v, i) => <li key={i}>{v.path ? `${v.path}: ` : ''}{v.message}</li>)}</ul>
        </div>
      )}
      {error && <div className="rounded border border-red-300 bg-red-50 px-2 py-1.5 text-red-800 dark:border-red-800 dark:bg-red-950/40 dark:text-red-200" data-testid="ops-error">{error}</div>}
      {preview && (
        <div className="rounded border border-emerald-300 bg-emerald-50 px-2 py-1.5 text-emerald-900 dark:border-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-100" data-testid="ops-dryrun">
          <div className="font-medium">Dry run OK — {preview.graph.nodes.length} nodes, {preview.graph.links.length} links after apply. Files that will change:</div>
          <ul className="mono list-disc pl-4">{preview.changed_files.map((f) => <li key={f}>{f}</li>)}</ul>
          {preview.warnings.length > 0 && <ul className="list-disc pl-4 text-amber-700 dark:text-amber-300">{preview.warnings.map((w) => <li key={w}>{w}</li>)}</ul>}
        </div>
      )}
    </section>
  )
}

// ---------------------------------------------------------------- inspector
export function NodeInspector({ topo, name, ports, editing, onClose, onMoveUplink, onRemoveEndpoint, onFreePort, children }: {
  topo: Topology; name: string; ports: TopologyPorts | undefined; editing: boolean; onClose: () => void
  onMoveUplink: (index: number, sw: string) => void; onRemoveEndpoint: (name: string) => void
  /** CONTRACT-6 §4: click a free port of a switch → add a test host there */
  onFreePort?: (sw: string, port: string) => void
  children?: ReactNode
}) {
  const n = topo.nodes.find((x) => x.name === name)
  if (!n) return null
  const role = roleOf(n)
  const p = ports?.[name]
  const links = topo.links.filter((l) => l.a === name || l.b === name)
  const ups = uplinksOf(topo, name)
  return (
    <aside className="card space-y-3 p-3 text-xs" data-testid="node-inspector" data-tour="node-inspector">
      <div className="flex items-center gap-2">
        <span className="flex-1 text-sm font-semibold">{name}</span>
        <button className="text-zinc-500 hover:text-zinc-900 dark:hover:text-zinc-100" onClick={onClose} aria-label="Close inspector">×</button>
      </div>
      <KV items={[['role', role], ['kind', n.kind], ['mgmt', n.mgmt_ip ?? '—'], ['serial', n.serial ?? '—'], ['image', <span className="mono break-all">{n.image ?? '—'}</span>]]} />
      {ups.length > 0 && (
        <div>
          <div className="h2 mb-1">Uplinks</div>
          <ul className="space-y-0.5">{ups.map((u) => (
            <li key={u.index} className="flex items-center gap-2">
              <span className="mono flex-1">{shortIf(u.node_if)} → {u.switch} {shortIf(u.switch_if)}</span>
              {editing && <button className="link" onClick={() => onMoveUplink(u.index, u.switch)} data-testid={`move-uplink-${u.index}`}>move…</button>}
            </li>
          ))}</ul>
        </div>
      )}
      <div>
        <div className="h2 mb-1">Links ({links.length})</div>
        <ul className="mono space-y-0.5">{links.map((l, i) => {
          const [mine, peer, peerIf] = l.a === name ? [l.a_if, l.b, l.b_if] : [l.b_if, l.a, l.a_if]
          return <li key={i}>{shortIf(mine)} ↔ {peer} {shortIf(peerIf)}</li>
        })}</ul>
      </div>
      {p && (
        <div data-testid="inspector-ports">
          <div className="h2 mb-1">Ports — {p.used.length} used / {p.free.length} free</div>
          <div className="flex flex-wrap gap-1">
            {p.used.map((x) => <span key={x} className="rounded bg-blue-100 px-1 text-blue-800 dark:bg-blue-900/60 dark:text-blue-200">{shortIf(x)}</span>)}
            {p.free.map((x) => (onFreePort && role !== 'host'
              ? <button key={x} type="button" className="rounded bg-zinc-100 px-1 text-zinc-600 hover:bg-cyan-100 hover:text-cyan-900 dark:bg-zinc-800 dark:hover:bg-cyan-900/60" title={`add a test host on ${x}`} onClick={() => onFreePort(name, x)} data-testid={`free-port-${x}`}>{shortIf(x)}</button>
              : <span key={x} className="rounded bg-zinc-100 px-1 text-zinc-500 dark:bg-zinc-800">{shortIf(x)}</span>))}
          </div>
          {onFreePort && role !== 'host' && p.free.length > 0 && <div className="mt-1 text-[10px] text-zinc-500">click a free port to plug in a test host</div>}
        </div>
      )}
      {children}
      {editing && role === 'host' && (
        <button className="btn-danger btn-sm" onClick={() => onRemoveEndpoint(name)} data-testid="inspector-remove">Remove {name}</button>
      )}
    </aside>
  )
}
