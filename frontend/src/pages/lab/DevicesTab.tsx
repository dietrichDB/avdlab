import { useState } from 'react'
import { ACT_SNAPSHOT_REASON } from '../../lib/act'
import { useMutation, useQuery } from '@tanstack/react-query'
import { labsApi } from '../../api/labs'
import { trafficApi } from '../../api/traffic'
import { cmdSlug, nodesApi } from '../../api/nodes'
import { ApiError } from '../../api/client'
import type { ConfigDiff, Lab, Snapshot } from '../../api/types'
import { useJobDrawer } from '../../components/JobDrawer'
import { DiffViewer } from '../../components/DiffViewer'
import { TerminalPanel } from '../../components/TerminalPanel'
import { Badge, CopyButton, ErrorBox, Spinner } from '../../components/ui'
import { cls, fmtAgo, fmtDate } from '../../lib/format'
import { isRunning } from '../../lib/labActions'
import { ConfigViewer } from '../../components/learn/ConfigExplain'
import { useLearningMode } from '../../hooks/useLearn'

interface NodeItem { name: string; kind: 'eos' | 'linux'; role?: string; mgmt?: string | null }

/** intended vs running: "identical" badge, or the unified diff with +/- colouring */
export function ConfigDiffView({ diff }: { diff: ConfigDiff }) {
  if (diff.identical || !diff.diff.trim()) {
    return <div data-testid="config-identical" className="rounded border border-emerald-300 bg-emerald-50 px-3 py-2 text-sm text-emerald-800 dark:border-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-200">Running config matches the intended config.</div>
  }
  const lines = diff.diff.split('\n')
  const added = lines.filter((l) => l.startsWith('+') && !l.startsWith('+++')).length
  const removed = lines.filter((l) => l.startsWith('-') && !l.startsWith('---')).length
  return (
    <div className="space-y-1">
      <div className="flex gap-3 text-xs" data-testid="config-diff-summary">
        <span className="text-amber-700 dark:text-amber-300">drift</span>
        <span className="text-emerald-700 dark:text-emerald-300">+{added} running only</span>
        <span className="text-red-700 dark:text-red-300">−{removed} intended only</span>
      </div>
      <DiffViewer diff={diff.diff} className="max-h-[60vh] text-xs" />
    </div>
  )
}

function notDeployedMsg(e: unknown) {
  return e instanceof ApiError && e.status === 409 ? 'Lab is not deployed: running configs are fetched live from the nodes.' : null
}

function ConfigPanel({ lab, node }: { lab: Lab; node: string }) {
  const [mode, setMode] = useState<'running' | 'diff'>('running')
  const [learning] = useLearningMode()
  const running = useQuery({ queryKey: ['labs', lab.id, 'running', node], queryFn: () => nodesApi.runningConfig(lab.id, node), enabled: mode === 'running', staleTime: 30_000 })
  const diff = useQuery({ queryKey: ['labs', lab.id, 'config-diff', node], queryFn: () => nodesApi.configDiff(lab.id, node), enabled: mode === 'diff', staleTime: 30_000 })
  const q = mode === 'running' ? running : diff
  return (
    <section className="card p-2">
      <div className="mb-2 flex flex-wrap items-center gap-2 px-1">
        <span className="h2">Config</span>
        <div className="flex overflow-hidden rounded border border-zinc-300 text-xs dark:border-zinc-700" role="group">
          <button className={cls('px-2 py-0.5', mode === 'running' ? 'bg-blue-600 text-white' : 'hover:bg-zinc-100 dark:hover:bg-zinc-800')} aria-pressed={mode === 'running'} onClick={() => setMode('running')}>Running</button>
          <button className={cls('px-2 py-0.5', mode === 'diff' ? 'bg-blue-600 text-white' : 'hover:bg-zinc-100 dark:hover:bg-zinc-800')} aria-pressed={mode === 'diff'} data-testid="diff-toggle" onClick={() => setMode('diff')}>Diff vs intended</button>
        </div>
        <span className="flex-1" />
        {mode === 'running' && running.data && <span className="text-xs text-zinc-500" title={fmtDate(running.data.fetched_at)}>fetched {fmtAgo(running.data.fetched_at)}</span>}
        {mode === 'running' && running.data && <CopyButton text={running.data.config} />}
        <button className="btn-secondary btn-sm" disabled={q.isFetching} onClick={() => q.refetch()}>{q.isFetching ? 'Fetching…' : 'Refresh'}</button>
      </div>
      {q.isLoading ? <Spinner label={`show running-config on ${node}…`} />
        : q.error ? (notDeployedMsg(q.error) ? <div className="p-3 text-sm text-zinc-500">{notDeployedMsg(q.error)}</div> : <ErrorBox error={q.error} />)
          : mode === 'running' && running.data
            ? (learning
              ? <ConfigViewer labId={lab.id} node={node} text={running.data.config} byText testId="running-config" />
              : <pre className="mono max-h-[60vh] overflow-auto rounded border border-zinc-200 bg-zinc-50 p-2 dark:border-zinc-800 dark:bg-zinc-950" data-testid="running-config">{running.data.config}</pre>)
            : mode === 'diff' && diff.data ? <ConfigDiffView diff={diff.data} /> : null}
    </section>
  )
}

function SnapshotBrowser({ lab, nodes }: { lab: Lab; nodes: string[] }) {
  const drawer = useJobDrawer()
  const snaps = useQuery({ queryKey: ['labs', lab.id, 'snapshots'], queryFn: () => nodesApi.snapshots(lab.id) })
  const take = useMutation({ mutationFn: () => drawer.track(nodesApi.snapshot(lab.id)) })
  const [sel, setSel] = useState<Snapshot | null>(null)
  const [node, setNode] = useState('')
  const [cmd, setCmd] = useState('')
  const effNode = node || nodes[0] || ''
  const effCmd = cmd || sel?.commands[0] || ''
  const file = useQuery({
    queryKey: ['labs', lab.id, 'snapshots', sel?.id, effNode, effCmd],
    queryFn: () => nodesApi.snapshotFile(lab.id, sel!.id, effNode, cmdSlug(effCmd)),
    enabled: !!sel && !!effNode && !!effCmd,
  })
  return (
    <section className="card p-2" data-testid="snapshots">
      <div className="mb-2 flex items-center gap-2 px-1">
        <span className="h2 mr-auto">Snapshots</span>
        <button className="btn-primary btn-sm" data-testid="snapshot-take" disabled={lab.target === 'act' || !isRunning(lab.state) || take.isPending} title={lab.target === 'act' ? ACT_SNAPSHOT_REASON : isRunning(lab.state) ? 'run the show commands on every cEOS node and commit the output' : 'lab is not deployed'} onClick={() => take.mutate()}>
          {take.isPending ? 'Starting…' : 'Snapshot'}
        </button>
      </div>
      <ErrorBox error={take.error ?? snaps.error} />
      {!snaps.data?.length ? <div className="p-3 text-center text-sm text-zinc-500">No snapshots yet.</div> : (
        <div className="grid gap-2 lg:grid-cols-[14rem_1fr]">
          <ul className="max-h-[50vh] overflow-auto text-sm">
            {snaps.data.map((s) => (
              <li key={s.id}>
                <button className={cls('w-full rounded px-2 py-1 text-left', sel?.id === s.id ? 'bg-blue-100 dark:bg-blue-900/40' : 'hover:bg-zinc-100 dark:hover:bg-zinc-800')} onClick={() => { setSel(s); setCmd('') }}>
                  <div className="mono">{s.id}</div>
                  <div className="text-[11px] text-zinc-500">{fmtAgo(s.created_at)} · {s.nodes} nodes · {s.commands.length} cmds</div>
                </button>
              </li>
            ))}
          </ul>
          <div className="min-w-0">
            {!sel ? <div className="p-3 text-sm text-zinc-500">Pick a snapshot.</div> : (<>
              <div className="mb-2 flex flex-wrap gap-2">
                <select className="input w-auto py-0.5 text-xs" aria-label="snapshot node" value={effNode} onChange={(e) => setNode(e.target.value)}>
                  {nodes.map((n) => <option key={n}>{n}</option>)}
                </select>
                <select className="input w-auto py-0.5 text-xs" aria-label="snapshot command" value={effCmd} onChange={(e) => setCmd(e.target.value)}>
                  {sel.commands.map((c) => <option key={c}>{c}</option>)}
                </select>
              </div>
              {file.isLoading ? <Spinner /> : file.error ? <ErrorBox error={file.error} /> : <pre className="mono max-h-[50vh] overflow-auto rounded border border-zinc-200 bg-zinc-50 p-2 dark:border-zinc-800 dark:bg-zinc-950" data-testid="snapshot-text">{file.data}</pre>}
            </>)}
          </div>
        </div>
      )}
    </section>
  )
}

export default function DevicesTab({ lab }: { lab: Lab }) {
  const devices = useQuery({ queryKey: ['labs', lab.id, 'devices'], queryFn: () => labsApi.devices(lab.id) })
  const clients = useQuery({ queryKey: ['labs', lab.id, 'clients'], queryFn: () => trafficApi.clients(lab.id), retry: false })
  const [selected, setSelected] = useState<string | null>(null)
  const [term, setTerm] = useState<NodeItem | null>(null)
  const eos: NodeItem[] = (devices.data ?? []).map((d) => ({ name: d.hostname, kind: 'eos', role: d.type, mgmt: d.mgmt_ip }))
  const linux: NodeItem[] = (clients.data ?? []).map((c) => ({ name: c.name, kind: 'linux', role: 'client', mgmt: c.mgmt_ip }))
  const nodes = [...eos, ...linux]
  const cur = nodes.find((n) => n.name === selected) ?? eos[0] ?? null
  const deployed = isRunning(lab.state)

  if (devices.isLoading) return <Spinner />
  return (
    <div className="space-y-4">
      <div className="grid gap-4 lg:grid-cols-[16rem_1fr]">
        <section className="card p-1" data-testid="device-list">
          <div className="h2 px-2 py-1">Nodes</div>
          <ErrorBox error={devices.error instanceof ApiError && devices.error.status === 404 ? null : devices.error} />
          {!nodes.length && <div className="p-3 text-sm text-zinc-500">No devices: build the lab first.</div>}
          <ul>
            {nodes.map((n) => (
              <li key={n.name}>
                <div className={cls('flex items-center gap-1 rounded px-2 py-1', cur?.name === n.name ? 'bg-blue-100 dark:bg-blue-900/40' : 'hover:bg-zinc-100 dark:hover:bg-zinc-800')}>
                  <button className="min-w-0 flex-1 text-left" onClick={() => setSelected(n.name)} data-testid="device-item">
                    <div className="mono truncate font-medium">{n.name}</div>
                    <div className="text-[11px] text-zinc-500">{n.role}{n.mgmt ? ` · ${n.mgmt}` : ''}</div>
                  </button>
                  <Badge className={n.kind === 'eos' ? 'bg-blue-100 text-blue-800 dark:bg-blue-900/60 dark:text-blue-200' : 'bg-zinc-200 dark:bg-zinc-700'}>{n.kind}</Badge>
                  <button className="btn-secondary btn-sm" title={deployed ? 'open a terminal' : 'lab is not deployed'} disabled={!deployed} aria-label={`terminal ${n.name}`} onClick={() => setTerm(n)}>&gt;_</button>
                </div>
              </li>
            ))}
          </ul>
        </section>
        <div className="min-w-0 space-y-4">
          {cur && (
            <div className="flex items-center gap-2">
              <span className="mono text-base font-semibold">{cur.name}</span>
              <span className="flex-1" />
              <button className="btn-secondary btn-sm" data-testid="open-terminal" disabled={!deployed} onClick={() => setTerm(cur)}>Terminal</button>
            </div>
          )}
          {cur?.kind === 'eos' && <ConfigPanel key={cur.name} lab={lab} node={cur.name} />}
          {cur?.kind === 'linux' && <div className="card p-3 text-sm text-zinc-500">Linux client: open a bash terminal, or see its plan in the Clients tab.</div>}
          <SnapshotBrowser lab={lab} nodes={eos.map((n) => n.name)} />
        </div>
      </div>
      {term && (
        <TerminalPanel
          key={term.name}
          title={`${lab.slug} / ${term.name}`}
          path={(shell) => nodesApi.terminalPath(lab.id, term.name, shell)}
          shells={term.kind === 'eos' ? ['cli', 'bash'] : ['bash']}
          defaultShell={term.kind === 'eos' ? 'cli' : 'bash'}
          onClose={() => setTerm(null)}
        />
      )}
    </div>
  )
}
