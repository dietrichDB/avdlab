import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { topologyApi } from '../../api/topology'
import { topologyEditApi } from '../../api/phase5'
import { labsApi } from '../../api/labs'
import { ApiError } from '../../api/client'
import type { EditOp, EditRejection, EditResult, Lab, Topology } from '../../api/types'
import { useJobDrawer } from '../../components/JobDrawer'
import { TopologyGraph, TopologyLegend, type Positions } from '../../components/TopologyGraph'
import { AddEndpointForm, NodeInspector, PendingOps, UplinkPopover } from '../../components/TopologyEditor'
import { Empty, ErrorBox, Hint, Spinner } from '../../components/ui'
import { AddTestHostForm, ClientActions } from '../../components/TestHosts'
import { KnobInfo, Term } from '../../components/learn/Term'
import { trafficApi } from '../../api/traffic'
import { isRunning } from '../../lib/labActions'
import { roleOf, toFlow } from '../../lib/topology'
import { editErrorText, editRejection, retargetUplink, uplinksOf } from '../../lib/topologyEdit'
import DesignerPanel, { DesignWithAiButton } from '../../components/ai/DesignerPanel'

type Popover = { node: string; index: number; switch: string; at?: { x: number; y: number } }

export default function TopologyTab({ lab }: { lab: Lab }) {
  const drawer = useJobDrawer()
  const qc = useQueryClient()
  const topo = useQuery({ queryKey: ['topology', lab.id], queryFn: () => topologyApi.get(lab.id) })
  const regen = useMutation({ mutationFn: () => drawer.track(topologyApi.regenerate(lab.id)) })
  const [filter, setFilter] = useState('')
  const [editing, setEditing] = useState(false)
  const [selected, setSelected] = useState<string | null>(null)
  const [testHost, setTestHost] = useState<{ switch?: string; port?: string } | null>(null)
  const [aiOpen, setAiOpen] = useState(false)
  const canHotAdd = isRunning(lab.state) && lab.target !== 'act'
  const roles = useMemo(() => new Map((topo.data?.nodes ?? []).map((n) => [n.name, roleOf(n)])), [topo.data])
  const links = (topo.data?.links ?? []).filter((l) => !filter || `${l.a} ${l.b} ${l.a_if} ${l.b_if}`.toLowerCase().includes(filter.toLowerCase()))
  const missing = topo.error instanceof ApiError && (topo.error.status === 404 || topo.error.status === 409)

  if (topo.isLoading) return <Spinner />
  return (
    <div className="space-y-3">
      <Hint className="mb-0" title="The fabric at a glance.">Spines, leaves and their links as AVD sees them — build the lab first, then click a node to see its role and config.</Hint>
      <div className="flex flex-wrap items-center gap-3">
        <TopologyLegend />
        <span className="flex-1" />
        {topo.data && <span className="text-xs text-zinc-500">{topo.data.nodes.length} nodes · {topo.data.links.length} links{topo.data.mgmt_subnet ? ` · mgmt ${topo.data.mgmt_subnet}` : ''}</span>}
        {topo.data?.nodes.length ? (
          <button className="btn-secondary btn-sm" data-tour="add-test-host" data-testid="add-test-host" disabled={!canHotAdd}
            title={canHotAdd ? 'plug a linux client into a free switch port without redeploying the fabric' : 'deploy the lab (containerlab) first'}
            onClick={() => setTestHost((t) => (t ? null : {}))}>Add test host</button>
        ) : null}
        {topo.data?.nodes.length ? (
          <label className="inline-flex items-center gap-1.5 text-xs font-medium" data-tour="edit-mode">
            <input type="checkbox" checked={editing} onChange={(e) => setEditing(e.target.checked)} data-testid="edit-toggle" /> Edit mode
          </label>
        ) : null}
        <DesignWithAiButton onClick={() => setAiOpen(true)} />
        <button className="btn-secondary btn-sm" onClick={() => regen.mutate()} disabled={regen.isPending}>Regenerate</button>
      </div>
      <DesignerPanel open={aiOpen} onClose={() => setAiOpen(false)} labId={lab.id} />
      <ErrorBox error={regen.error ?? (!missing && topo.error)} />
      {missing || !topo.data?.nodes.length ? <Empty>No topology yet — build the lab to generate the containerlab topology.</Empty> : (
        <>
          {testHost && <AddTestHostForm key={`${testHost.switch}-${testHost.port}`} lab={lab} initial={testHost} onCancel={() => setTestHost(null)} onDone={() => setTestHost(null)} />}
          <Editor lab={lab} topo={topo.data} editing={editing} selected={selected} setSelected={setSelected}
            onFreePort={canHotAdd ? (sw, port) => setTestHost({ switch: sw, port }) : undefined}
            onApplied={(g) => qc.setQueryData(['topology', lab.id], (old: Topology | undefined) => ({ ...g, layout: g.layout ?? old?.layout }))} />
          <section className="card overflow-x-auto">
            <div className="flex items-center gap-2 px-3 pt-2">
              <span className="h2 flex-1">Links <span className="text-xs font-normal text-zinc-500">(<Term k="P2P link">P2P</Term> uplinks, <Term k="peer-link">MLAG peer-links</Term>, endpoints — each a veth pair in <Term k="containerlab">containerlab</Term>)</span></span>
              <input className="input w-48 py-0.5 text-xs" placeholder="filter" value={filter} onChange={(e) => setFilter(e.target.value)} />
            </div>
            <table className="table" data-testid="link-table">
              <thead><tr><th>A</th><th>A interface</th><th>B</th><th>B interface</th><th>clab ports</th><th>Type</th></tr></thead>
              <tbody>
                {links.map((l, i) => (
                  <tr key={i}>
                    <td className="mono font-medium">{l.a} <span className="text-zinc-400">{roles.get(l.a)}</span></td>
                    <td className="mono">{l.a_if}</td>
                    <td className="mono font-medium">{l.b} <span className="text-zinc-400">{roles.get(l.b)}</span></td>
                    <td className="mono">{l.b_if}</td>
                    <td className="mono text-zinc-500">{l.a_port && l.b_port ? `${l.a_port} ↔ ${l.b_port}` : '—'}</td>
                    <td className="text-xs">{l.type ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        </>
      )}
    </div>
  )
}

function Editor({ lab, topo, editing, selected, setSelected, onApplied, onFreePort }: {
  lab: Lab; topo: Topology; editing: boolean; selected: string | null; setSelected: (n: string | null) => void; onApplied: (g: Topology) => void
  onFreePort?: (sw: string, port: string) => void
}) {
  const clients = useQuery({ queryKey: ['labs', lab.id, 'clients'], queryFn: () => trafficApi.clients(lab.id), enabled: !!selected, retry: false })
  const templates = useQuery({ queryKey: ['labs', 'templates'], queryFn: labsApi.templates, staleTime: 60_000 })
  const knobLearn = (key: string) => templates.data?.find((t) => t.name === lab.template)?.knobs.find((k) => k.key === key)?.learn
  const selClient = selected ? clients.data?.find((c) => c.name === selected) : undefined
  const qc = useQueryClient()
  const drawer = useJobDrawer()
  const layoutQ = useQuery({ queryKey: ['topology', lab.id, 'layout'], queryFn: () => topologyEditApi.layout(lab.id), enabled: topo.layout === undefined })
  const ports = useQuery({ queryKey: ['topology', lab.id, 'ports'], queryFn: () => topologyEditApi.ports(lab.id), enabled: editing || !!selected })
  const saved = topo.layout ?? layoutQ.data ?? null

  // ---- layout
  const [draft, setDraft] = useState<Positions | null>(null)
  const [locked, setLocked] = useState<boolean | null>(null)
  const [resetKey, setResetKey] = useState(0)
  const isLocked = locked ?? saved?.locked ?? false
  const layoutDirty = draft !== null || (locked !== null && locked !== (saved?.locked ?? false))
  const saveLayout = useMutation({
    mutationFn: () => topologyEditApi.saveLayout(lab.id, {
      positions: draft ?? saved?.positions ?? autoPositions(topo), locked: isLocked,
    }),
    onSuccess: (l) => {
      qc.setQueryData(['topology', lab.id, 'layout'], l)
      qc.setQueryData(['topology', lab.id], (old: Topology | undefined) => (old ? { ...old, layout: l } : old))
      setDraft(null); setLocked(null)
    },
  })
  const autoLayout = () => { setDraft(autoPositions(topo)); setResetKey((k) => k + 1) }

  // ---- ops
  const [ops, setOpsRaw] = useState<EditOp[]>([])
  const [preview, setPreview] = useState<EditResult | null>(null)
  const [rejection, setRejection] = useState<EditRejection | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [applied, setApplied] = useState<EditResult | null>(null)
  const [popover, setPopover] = useState<Popover | null>(null)
  const [menu, setMenu] = useState<{ name: string; at: { x: number; y: number } } | null>(null)
  const [showAdd, setShowAdd] = useState(false)
  const setOps = (f: (o: EditOp[]) => EditOp[]) => { setOpsRaw(f); setPreview(null); setRejection(null); setError(null) }
  const addOp = (o: EditOp) => { setOps((x) => [...x, o]); setApplied(null) }
  const fail = (e: unknown) => { const r = editRejection(e); setRejection(r); setError(r ? null : editErrorText(e)) }

  const dryRun = useMutation({
    mutationFn: () => topologyEditApi.edit(lab.id, ops, true),
    onMutate: () => { setRejection(null); setError(null) },
    onSuccess: (r) => setPreview(r),
    onError: fail,
  })
  const apply = useMutation({
    mutationFn: () => topologyEditApi.edit(lab.id, ops, false),
    onSuccess: (r) => {
      onApplied(r.graph)
      qc.invalidateQueries({ queryKey: ['topology', lab.id, 'ports'] })
      qc.invalidateQueries({ queryKey: ['labs', lab.id] })
      setOpsRaw([]); setPreview(null); setApplied(r)
    },
    onError: (e) => { setPreview(null); fail(e) },
  })
  const build = useMutation({ mutationFn: () => drawer.track(labsApi.build(lab.id)), onSuccess: () => setApplied(null) })

  const uplinkLinks = useMemo(() => {
    const s = new Set<number>()
    for (const n of topo.nodes) for (const u of uplinksOf(topo, n.name)) s.add(Number(u.edgeKey))
    return s
  }, [topo])
  const spines = topo.nodes.filter((n) => roleOf(n) === 'spine').length
  const l2 = Number((lab.knobs ?? {}).l2leaf_per_pair ?? 0)
  const [spineN, setSpineN] = useState<string>('')
  const [l2N, setL2N] = useState<string>('')

  return (
    <div className="space-y-3">
      {editing && (
        <div className="card flex flex-wrap items-center gap-2 px-3 py-2 text-xs" data-testid="edit-toolbar">
          <button className="btn-secondary btn-sm" onClick={() => setShowAdd((v) => !v)} data-testid="tb-add-server" data-tour="add-endpoint">Add server</button>
          <KnobInfo learn={knobLearn('servers')} knobKey="servers" />
          <span className="h-4 border-l border-zinc-300 dark:border-zinc-700" />
          <button className="btn-secondary btn-sm" onClick={() => addOp({ op: 'add_leaf_pair' })} data-testid="tb-add-pair">+ leaf pair</button>
          <button className="btn-secondary btn-sm" onClick={() => addOp({ op: 'remove_last_leaf_pair' })} data-testid="tb-remove-pair">− last leaf pair</button>
          <KnobInfo learn={knobLearn('leaf_pairs')} knobKey="leaf_pairs" />
          <span className="inline-flex items-center gap-1">L2 leafs/pair
            <input className="input w-12 py-0 text-xs" type="number" min={0} max={4} placeholder={String(l2)} value={l2N} onChange={(e) => setL2N(e.target.value)} data-testid="tb-l2-n" />
            <button className="btn-secondary btn-sm" disabled={l2N === ''} onClick={() => { addOp({ op: 'set_l2leaf_per_pair', n: Number(l2N) }); setL2N('') }} data-testid="tb-l2-set">set</button>
            <KnobInfo learn={knobLearn('l2leaf_per_pair')} knobKey="l2leaf_per_pair" />
          </span>
          <span className="inline-flex items-center gap-1">Spines
            <input className="input w-12 py-0 text-xs" type="number" min={1} max={8} placeholder={String(spines)} value={spineN} onChange={(e) => setSpineN(e.target.value)} data-testid="tb-spine-n" />
            <button className="btn-secondary btn-sm" disabled={spineN === ''} onClick={() => { addOp({ op: 'set_spine_count', n: Number(spineN) }); setSpineN('') }} data-testid="tb-spine-set">set</button>
            <KnobInfo learn={knobLearn('spine_count')} knobKey="spine_count" />
          </span>
          <span className="flex-1" />
          <label className="inline-flex items-center gap-1"><input type="checkbox" checked={isLocked} onChange={(e) => setLocked(e.target.checked)} data-testid="tb-lock" /> lock layout</label>
          <button className="btn-secondary btn-sm" onClick={autoLayout} data-testid="tb-auto-layout">Auto-layout</button>
          <button className="btn-primary btn-sm" onClick={() => saveLayout.mutate()} disabled={!layoutDirty || saveLayout.isPending} data-testid="tb-save-layout">
            {saveLayout.isPending ? 'Saving…' : 'Save layout'}
          </button>
        </div>
      )}
      <ErrorBox error={saveLayout.error} />
      {editing && <div className="text-[11px] text-zinc-500">Drag nodes to arrange · drag the switch end of a blue uplink onto another switch to move it · right-click a server to remove · click a node to inspect.</div>}
      {applied && (
        <div className="flex items-center gap-2 rounded border border-blue-300 bg-blue-50 px-3 py-2 text-sm text-blue-900 dark:border-blue-800 dark:bg-blue-950/40 dark:text-blue-100" data-testid="applied-banner">
          <span className="flex-1">Changes applied and committed ({applied.changed_files.length} file{applied.changed_files.length === 1 ? '' : 's'}). Build to regenerate configs.</span>
          <button className="btn-primary btn-sm" onClick={() => build.mutate()} disabled={build.isPending} data-testid="applied-build">Build now</button>
          <button className="btn-secondary btn-sm" onClick={() => setApplied(null)}>Later</button>
        </div>
      )}
      <ErrorBox error={build.error} />
      {showAdd && editing && <AddEndpointForm topo={topo} ports={ports.data} ops={ops} onAdd={(o) => addOp(o)} onCancel={() => setShowAdd(false)} />}
      <PendingOps ops={ops} preview={preview} rejection={rejection} error={error} busy={dryRun.isPending || apply.isPending}
        onRemove={(i) => setOps((x) => x.filter((_, j) => j !== i))}
        onDiscard={() => setOps(() => [])}
        onPreview={() => dryRun.mutate()} onApply={() => apply.mutate()} />
      <div className={selected ? 'grid gap-3 lg:grid-cols-[1fr_18rem]' : ''}>
        <div data-tour="topology-graph">
        <TopologyGraph topology={topo} className="h-[55vh]" positions={draft ?? saved?.positions ?? null} resetKey={resetKey}
          editable={editing} draggable={editing && !isLocked} reconnectableLinks={uplinkLinks} selected={selected}
          onPositionsChange={(p) => setDraft(p)}
          onNodeClick={(n) => { setSelected(n); setMenu(null) }}
          onNodeContextMenu={editing ? (name, at) => { if (roles(topo, name) === 'host') setMenu({ name, at }); else setSelected(name) } : undefined}
          onEdgeRetarget={(link, s, t, at) => {
            const r = retargetUplink(topo, link, s, t)
            if (r) setPopover({ ...r, at })
          }}>
          {menu && (
            <div className="card absolute z-10 py-1 text-xs shadow-lg" style={{ left: menu.at.x, top: menu.at.y }} data-testid="node-menu">
              <button className="block w-full px-3 py-1 text-left hover:bg-zinc-100 dark:hover:bg-zinc-800" onClick={() => { setSelected(menu.name); setMenu(null) }}>Inspect</button>
              <button className="block w-full px-3 py-1 text-left text-red-600 hover:bg-zinc-100 dark:hover:bg-zinc-800" data-testid="node-menu-remove"
                onClick={() => { addOp({ op: 'remove_endpoint', name: menu.name }); setMenu(null) }}>Remove {menu.name}</button>
            </div>
          )}
          {popover && (
            <UplinkPopover topo={topo} ports={ports.data} ops={ops} node={popover.node} index={popover.index} initialSwitch={popover.switch} at={popover.at}
              onAdd={(o) => { addOp(o); setPopover(null) }} onCancel={() => setPopover(null)} />
          )}
        </TopologyGraph>
        </div>
        {selected && (
          <NodeInspector topo={topo} name={selected} ports={ports.data} editing={editing} onClose={() => setSelected(null)}
            onMoveUplink={(index, sw) => setPopover({ node: selected, index, switch: sw })}
            onRemoveEndpoint={(name) => { addOp({ op: 'remove_endpoint', name }); setSelected(null) }}
            onFreePort={onFreePort}>
            {selClient && (
              <div className="border-t border-zinc-200 pt-2 dark:border-zinc-800">
                <div className="h2 mb-1">Test host</div>
                <ClientActions lab={lab} client={selClient} clients={clients.data ?? []} />
              </div>
            )}
          </NodeInspector>
        )}
      </div>
    </div>
  )
}

function roles(topo: Topology, name: string) {
  const n = topo.nodes.find((x) => x.name === name)
  return n ? roleOf(n) : 'other'
}

function autoPositions(topo: Topology): Positions {
  return Object.fromEntries(toFlow(topo).nodes.map((n) => [n.id, n.position]))
}
