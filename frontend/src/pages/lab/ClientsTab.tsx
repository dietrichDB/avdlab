import { useState } from 'react'
import { ACT_TRAFFIC_REASON } from '../../lib/act'
import { MssCasesTable } from './MssTab'
import type { MssVerifyResult } from '../../api/types'
import { useMutation, useQuery } from '@tanstack/react-query'
import { trafficApi } from '../../api/traffic'
import { jobsApi, isTerminal } from '../../api/jobs'
import { ApiError } from '../../api/client'
import type { IperfFlow, IperfResultRow, Lab, LabClient, PingResult, TrafficRun } from '../../api/types'
import { useJobDrawer } from '../../components/JobDrawer'
import { AddTestHostForm, ClientActions } from '../../components/TestHosts'
import { Term } from '../../components/learn/Term'
import { Badge, ErrorBox, Field, JobStatusBadge, Spinner } from '../../components/ui'
import { cls, fmtAgo, fmtDate } from '../../lib/format'
import { isRunning } from '../../lib/labActions'
import {
  LOSS_CLASSES, asIperfResults, asPingResult, clientsWithIp, fmtMbps, lossLevel, matrixNames, newFlow, runSummary,
} from '../../lib/traffic'

function stateBadge(state?: string | null) {
  if (!state) return <span className="text-zinc-400">not deployed</span>
  const s = state.toLowerCase()
  const c = s === 'running' ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/60 dark:text-emerald-200'
    : s === 'exited' || s === 'dead' ? 'bg-red-100 text-red-800 dark:bg-red-900/60 dark:text-red-200'
      : 'bg-amber-100 text-amber-800 dark:bg-amber-900/60 dark:text-amber-200'
  return <Badge className={c}>{state}</Badge>
}

/** One block per client: links, bond, then one row per VLAN (iface / IP / gateway). */
export function ClientPlanTable({ clients }: { clients: LabClient[] }) {
  return (
    <table className="table" data-testid="client-plan">
      <thead><tr><th>Client</th><th>Links</th><th>Bond</th><th>VLAN</th><th>Interface</th><th>IP</th><th>Gateway</th><th>State</th></tr></thead>
      <tbody>
        {clients.map((c) => {
          const vlans = c.vlans.length ? c.vlans : [null]
          return vlans.map((v, i) => (
            <tr key={`${c.name}-${v?.vid ?? 'none'}`} data-testid={i === 0 ? 'client-row' : 'client-vlan-row'} data-client={c.name}>
              {i === 0 && (<>
                <td rowSpan={vlans.length}>
                  <div className="mono font-medium">{c.name}</div>
                  <div className="mono text-[11px] text-zinc-500" title="image">{c.image}</div>
                  {c.mgmt_ip && <div className="mono text-[11px] text-zinc-500" title="mgmt">{c.mgmt_ip}</div>}
                </td>
                <td rowSpan={vlans.length} className="mono">
                  {c.links.map((l) => <div key={l.eth}>{l.eth} → {l.switch}:{l.switch_port}</div>)}
                </td>
                <td rowSpan={vlans.length} className="mono">
                  {c.bond ? <span title="802.3ad">{c.bond.name} <span className="text-zinc-500">({c.bond.members.join(', ')})</span></span> : <span className="text-zinc-400">—</span>}
                </td>
              </>)}
              <td className="mono">{v ? <>{v.vid}{v.vrf && <span className="ml-1 text-zinc-500">{v.vrf}</span>}</> : '—'}</td>
              <td className="mono">{v?.iface ?? '—'}</td>
              <td className="mono">{v?.ip ?? <span className="text-zinc-400" title="L2-only VLAN">L2 only</span>}{v?.default_route && <Badge className="ml-1 bg-blue-100 text-blue-800 dark:bg-blue-900/60 dark:text-blue-200">default</Badge>}</td>
              <td className="mono">{v?.gateway ?? '—'}</td>
              {i === 0 && <td rowSpan={vlans.length}>{stateBadge(c.container_state)}</td>}
            </tr>
          ))
        })}
      </tbody>
    </table>
  )
}

/** src × dst heat table coloured by loss % */
export function PingMatrix({ result }: { result: PingResult }) {
  const names = matrixNames(result.matrix)
  const byPair = new Map(result.results.map((r) => [`${r.src}|${r.dst}`, r]))
  if (!names.length) return <div className="p-3 text-sm text-zinc-500">No results.</div>
  return (
    <div className="overflow-x-auto">
      <table className="text-xs" data-testid="ping-matrix">
        <thead>
          <tr>
            <th className="px-2 py-1 text-left text-zinc-500">src ↓ / dst →</th>
            {names.map((n) => <th key={n} className="mono px-2 py-1 font-medium">{n}</th>)}
          </tr>
        </thead>
        <tbody>
          {names.map((src) => (
            <tr key={src}>
              <th className="mono px-2 py-1 text-left font-medium">{src}</th>
              {names.map((dst) => {
                if (src === dst) return <td key={dst} className="px-2 py-1 text-center text-zinc-300 dark:text-zinc-600">·</td>
                const loss = result.matrix[src]?.[dst]
                const lvl = lossLevel(loss)
                const r = byPair.get(`${src}|${dst}`)
                const title = r ? `${r.target_ip ?? '?'}: ${r.received}/${r.sent} received${r.rtt_avg_ms != null ? `, avg ${r.rtt_avg_ms} ms` : ''}` : undefined
                return (
                  <td key={dst} data-testid="ping-cell" data-src={src} data-dst={dst} data-level={lvl} title={title}
                    className={cls('min-w-[4.5rem] border border-white px-2 py-1 text-center font-semibold dark:border-zinc-900', LOSS_CLASSES[lvl])}>
                    {loss == null ? '—' : `${Math.round(loss)}%`}
                    {r?.rtt_avg_ms != null && lvl !== 'down' && <div className="text-[10px] font-normal opacity-75">{r.rtt_avg_ms.toFixed(2)} ms</div>}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
      <div className="mt-1 flex gap-3 text-[11px] text-zinc-500">
        <span><span className={cls('inline-block h-2 w-3 rounded-sm align-middle', LOSS_CLASSES.ok)} /> 0 % loss</span>
        <span><span className={cls('inline-block h-2 w-3 rounded-sm align-middle', LOSS_CLASSES.partial)} /> partial</span>
        <span><span className={cls('inline-block h-2 w-3 rounded-sm align-middle', LOSS_CLASSES.down)} /> 100 %</span>
      </div>
    </div>
  )
}

export function IperfResultsTable({ rows }: { rows: IperfResultRow[] }) {
  return (
    <table className="table" data-testid="iperf-results">
      <thead><tr><th>Flow</th><th>Proto</th><th className="text-right">Sent</th><th className="text-right">Received</th><th className="text-right">Retrans.</th><th className="text-right">Jitter</th><th className="text-right">Loss</th><th>Status</th></tr></thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i} data-testid="iperf-row">
            <td className="mono">{r.src} → {r.dst}</td>
            <td className="mono">{r.proto}{r.seconds ? <span className="text-zinc-500"> · {r.seconds}s</span> : null}</td>
            <td className="mono text-right">{fmtMbps(r.bits_per_second_sent)}</td>
            <td className="mono text-right">{fmtMbps(r.bits_per_second_received)}</td>
            <td className="mono text-right">{r.retransmits ?? '—'}</td>
            <td className="mono text-right">{r.jitter_ms != null ? `${r.jitter_ms.toFixed(3)} ms` : '—'}</td>
            <td className="mono text-right">{r.lost_percent != null ? `${r.lost_percent.toFixed(2)} %` : '—'}</td>
            <td>{r.ok ? <span className="text-emerald-600 dark:text-emerald-400">ok</span> : <span className="text-red-600 dark:text-red-400" title={r.error ?? undefined}>{r.error ?? 'failed'}</span>}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function FlowEditor({ flows, names, onChange }: { flows: IperfFlow[]; names: string[]; onChange: (f: IperfFlow[]) => void }) {
  const set = (i: number, patch: Partial<IperfFlow>) => onChange(flows.map((f, j) => (j === i ? { ...f, ...patch } : f)))
  return (
    <div className="space-y-1" data-testid="iperf-flows">
      <div className="grid grid-cols-[1fr_1fr_4.5rem_5.5rem_4rem_3.5rem_auto_auto] gap-1 text-[11px] font-medium text-zinc-500">
        <span>src</span><span>dst</span><span>proto</span><span>bandwidth</span><span>secs</span><span>-P</span><span>-R</span><span />
      </div>
      {flows.map((f, i) => (
        <div key={i} className="grid grid-cols-[1fr_1fr_4.5rem_5.5rem_4rem_3.5rem_auto_auto] items-center gap-1" data-testid="iperf-flow">
          <select className="input py-0.5 text-xs" aria-label={`flow ${i + 1} src`} value={f.src} onChange={(e) => set(i, { src: e.target.value })}>
            <option value="">—</option>{names.map((n) => <option key={n}>{n}</option>)}
          </select>
          <select className="input py-0.5 text-xs" aria-label={`flow ${i + 1} dst`} value={f.dst} onChange={(e) => set(i, { dst: e.target.value })}>
            <option value="">—</option>{names.map((n) => <option key={n}>{n}</option>)}
          </select>
          <select className="input py-0.5 text-xs" aria-label={`flow ${i + 1} proto`} value={f.proto} onChange={(e) => set(i, { proto: e.target.value as IperfFlow['proto'] })}>
            <option value="tcp">tcp</option><option value="udp">udp</option>
          </select>
          <input className="input mono py-0.5 text-xs" aria-label={`flow ${i + 1} bandwidth`} placeholder={f.proto === 'udp' ? '100M' : 'unlimited'}
            value={f.bandwidth ?? ''} onChange={(e) => set(i, { bandwidth: e.target.value.trim() || null })} />
          <input className="input py-0.5 text-xs" type="number" min={1} max={300} aria-label={`flow ${i + 1} duration`} value={f.duration} onChange={(e) => set(i, { duration: Number(e.target.value) || 1 })} />
          <input className="input py-0.5 text-xs" type="number" min={1} max={32} aria-label={`flow ${i + 1} parallel`} value={f.parallel} onChange={(e) => set(i, { parallel: Number(e.target.value) || 1 })} />
          <input type="checkbox" aria-label={`flow ${i + 1} reverse`} checked={f.reverse} onChange={(e) => set(i, { reverse: e.target.checked })} />
          <button type="button" className="btn-secondary btn-sm" aria-label={`remove flow ${i + 1}`} disabled={flows.length <= 1} onClick={() => onChange(flows.filter((_, j) => j !== i))}>×</button>
        </div>
      ))}
    </div>
  )
}

/** follows a traffic job until it finishes and hands back its result */
function useJobResult(jobId: string | null) {
  return useQuery({
    queryKey: ['job', jobId],
    queryFn: () => jobsApi.get(jobId!),
    enabled: !!jobId,
    refetchInterval: (q) => (isTerminal(q.state.data?.status) ? false : 2000),
  })
}

function RunView({ run }: { run: TrafficRun }) {
  if (run.kind === 'mss') {
    const r = run.results as MssVerifyResult | null
    return r && Array.isArray(r.cases) ? <MssCasesTable result={r} /> : <div className="text-sm text-zinc-500">No results stored.</div>
  }
  if (run.kind === 'ping') {
    const r = asPingResult(run.results)
    return r ? <PingMatrix result={r} /> : <div className="text-sm text-zinc-500">No results stored.</div>
  }
  const rows = asIperfResults(run.results)
  return rows ? <IperfResultsTable rows={rows} /> : <div className="text-sm text-zinc-500">No results stored.</div>
}

export default function ClientsTab({ lab }: { lab: Lab }) {
  const drawer = useJobDrawer()
  const clients = useQuery({ queryKey: ['labs', lab.id, 'clients'], queryFn: () => trafficApi.clients(lab.id) })
  const runs = useQuery({ queryKey: ['labs', lab.id, 'traffic-runs'], queryFn: () => trafficApi.runs(lab.id) })
  const [pingJob, setPingJob] = useState<string | null>(null)
  const [iperfJob, setIperfJob] = useState<string | null>(null)
  const [count, setCount] = useState(3)
  const [size, setSize] = useState(56)
  const [vlan, setVlan] = useState('')
  const [flows, setFlows] = useState<IperfFlow[]>([])
  const [sequential, setSequential] = useState(true)
  const [openRun, setOpenRun] = useState<string | null>(null)
  const runDetail = useQuery({ queryKey: ['labs', lab.id, 'traffic-runs', openRun], queryFn: () => trafficApi.run(lab.id, openRun!), enabled: !!openRun })

  const ping = useMutation({
    mutationFn: () => drawer.track(trafficApi.ping(lab.id, { pairs: null, count, size, ...(vlan ? { vlan: Number(vlan) } : {}) })),
    onSuccess: (r) => setPingJob(r.job_id),
  })
  const iperf = useMutation({
    mutationFn: () => drawer.track(trafficApi.iperf(lab.id, { flows, sequential })),
    onSuccess: (r) => setIperfJob(r.job_id),
  })
  const pingRes = useJobResult(pingJob)
  const iperfRes = useJobResult(iperfJob)

  const list = clients.data ?? []
  const withIp = clientsWithIp(list)
  const names = withIp.map((c) => c.name)
  const effFlows = flows.length ? flows : [newFlow(names[0] ?? '', names[1] ?? '')]
  const deployed = isRunning(lab.state)
  const act = lab.target === 'act'
  const flowsOk = effFlows.every((f) => f.src && f.dst && f.src !== f.dst)
  const vids = [...new Set(list.flatMap((c) => c.vlans.filter((v) => v.ip).map((v) => v.vid)))].sort((a, b) => a - b)
  const notBuilt = clients.error instanceof ApiError && (clients.error.status === 404 || clients.error.status === 409)
  const pingResult = pingRes.data && isTerminal(pingRes.data.status) ? asPingResult(pingRes.data.result) : null
  const iperfRows = iperfRes.data && isTerminal(iperfRes.data.status) ? asIperfResults(iperfRes.data.result) : null

  if (clients.isLoading) return <Spinner label="Client plan…" />
  return (
    <div className="space-y-4">
      <section className="card overflow-x-auto p-2">
        <div className="mb-2 flex items-center gap-2 px-1">
          <span className="h2 mr-auto">Clients <span className="text-xs font-normal text-zinc-500">(from <Term k="connected endpoints">connected endpoints</Term>)</span></span>
          <span className="text-xs text-zinc-500">{list.length} client(s) · {withIp.length} with IP</span>
        </div>
        {notBuilt ? <div className="p-4 text-center text-sm text-zinc-500">No client plan yet: build the lab (clients are derived from connected endpoints).</div>
          : <><ErrorBox error={clients.error} />{list.length ? <ClientPlanTable clients={list} /> : <div className="p-4 text-center text-sm text-zinc-500">No connected endpoints in this lab.</div>}</>}
      </section>

      {!act && <TestHostsSection lab={lab} clients={list} deployed={deployed} />}

      {act && <div className="rounded border border-violet-300 bg-violet-50 px-3 py-2 text-sm text-violet-900 dark:border-violet-800 dark:bg-violet-950/40 dark:text-violet-100" data-testid="act-traffic-reason">{ACT_TRAFFIC_REASON}</div>}
      {!act && !deployed && list.length > 0 && <div className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-100">Deploy the lab to run traffic tests.</div>}

      <div className="grid gap-4 xl:grid-cols-2">
        <section className="card space-y-3 p-3">
          <div className="flex flex-wrap items-end gap-2">
            <span className="h2 mr-auto self-center">Ping mesh</span>
            <Field label="count"><input className="input w-16 py-0.5 text-xs" type="number" min={1} max={100} value={count} onChange={(e) => setCount(Number(e.target.value) || 1)} /></Field>
            <Field label="size"><input className="input w-20 py-0.5 text-xs" type="number" min={16} max={9000} value={size} onChange={(e) => setSize(Number(e.target.value) || 56)} /></Field>
            <Field label="VLAN">
              <select className="input w-24 py-0.5 text-xs" value={vlan} onChange={(e) => setVlan(e.target.value)} aria-label="ping vlan">
                <option value="">all</option>{vids.map((v) => <option key={v} value={v}>{v}</option>)}
              </select>
            </Field>
            <button className="btn-primary btn-sm" data-testid="ping-mesh" disabled={act || !deployed || withIp.length < 2 || ping.isPending} title={act ? ACT_TRAFFIC_REASON : undefined} onClick={() => ping.mutate()}>
              {ping.isPending ? 'Starting…' : 'Ping mesh'}
            </button>
          </div>
          <ErrorBox error={ping.error} />
          {pingJob && pingRes.data && !isTerminal(pingRes.data.status) && <div className="flex items-center gap-2 text-xs text-zinc-500"><JobStatusBadge status={pingRes.data.status} /> pinging…</div>}
          {pingRes.data?.status === 'failed' && <ErrorBox error={pingRes.data.error ?? 'ping failed'} />}
          {pingResult && <PingMatrix result={pingResult} />}
        </section>

        <section className="card space-y-3 p-3">
          <div className="flex items-center gap-2">
            <span className="h2 mr-auto">iperf3</span>
            <label className="flex items-center gap-1 text-xs"><input type="checkbox" checked={sequential} onChange={(e) => setSequential(e.target.checked)} /> sequential</label>
            <button type="button" className="btn-secondary btn-sm" onClick={() => setFlows([...effFlows, newFlow(names[0] ?? '', names[1] ?? '')])}>Add flow</button>
            <button className="btn-primary btn-sm" data-testid="iperf-run" disabled={act || !deployed || !flowsOk || iperf.isPending} title={act ? ACT_TRAFFIC_REASON : undefined}
              onClick={() => { setFlows(effFlows); iperf.mutate() }}>{iperf.isPending ? 'Starting…' : 'Run'}</button>
          </div>
          <FlowEditor flows={effFlows} names={names} onChange={setFlows} />
          <ErrorBox error={iperf.error} />
          {iperfJob && iperfRes.data && !isTerminal(iperfRes.data.status) && <div className="flex items-center gap-2 text-xs text-zinc-500"><JobStatusBadge status={iperfRes.data.status} /> running flows…</div>}
          {iperfRes.data?.status === 'failed' && <ErrorBox error={iperfRes.data.error ?? 'iperf failed'} />}
          {iperfRows && <div className="overflow-x-auto"><IperfResultsTable rows={iperfRows} /></div>}
        </section>
      </div>

      <section className="card overflow-x-auto p-2">
        <div className="h2 mb-2 px-1">Runs</div>
        <ErrorBox error={runs.error} />
        {!runs.data?.length ? <div className="p-4 text-center text-sm text-zinc-500">No traffic runs yet.</div> : (
          <table className="table" data-testid="traffic-runs">
            <thead><tr><th>When</th><th>Kind</th><th>Parameters</th><th>Job</th><th /></tr></thead>
            <tbody>
              {runs.data.map((r) => (
                <tr key={r.id} className={cls(openRun === r.id && 'bg-blue-50 dark:bg-blue-950/30')}>
                  <td className="whitespace-nowrap text-zinc-500" title={fmtDate(r.created_at)}>{fmtAgo(r.created_at)}</td>
                  <td><Badge className="bg-zinc-200 dark:bg-zinc-700">{r.kind}</Badge></td>
                  <td className="mono">{runSummary(r)}</td>
                  <td>{r.job_id ? <button className="link mono" onClick={() => drawer.openJob(r.job_id!)}>{r.job_id.slice(0, 8)}</button> : '—'}</td>
                  <td><button className="btn-secondary btn-sm" onClick={() => setOpenRun(openRun === r.id ? null : r.id)}>{openRun === r.id ? 'Hide' : 'Show'}</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {openRun && (
          <div className="border-t border-zinc-200 p-2 dark:border-zinc-800" data-testid="traffic-run-detail">
            {runDetail.isLoading ? <Spinner /> : runDetail.data ? <RunView run={runDetail.data} /> : <ErrorBox error={runDetail.error} />}
          </div>
        )}
      </section>
    </div>
  )
}

/** CONTRACT-6 §4: hot-added test hosts with the same quick actions as the topology inspector. */
function TestHostsSection({ lab, clients, deployed }: { lab: Lab; clients: LabClient[]; deployed: boolean }) {
  const [adding, setAdding] = useState(false)
  const [open, setOpen] = useState<string | null>(null)
  const hot = clients.filter((c) => c.hot_added)
  return (
    <section className="card space-y-2 p-3" data-testid="test-hosts">
      <div className="flex items-center gap-2">
        <span className="h2 mr-auto">Test hosts <span className="text-xs font-normal text-zinc-500">hot-added without redeploying the fabric</span></span>
        <button className="btn-secondary btn-sm" disabled={!deployed} title={deployed ? undefined : 'deploy the lab first'} onClick={() => setAdding((a) => !a)} data-testid="clients-add-test-host">Add test host</button>
      </div>
      {adding && <AddTestHostForm lab={lab} onCancel={() => setAdding(false)} onDone={() => setAdding(false)} />}
      {!hot.length ? <div className="p-2 text-center text-sm text-zinc-500">No test hosts yet. Add one here or click a free port of a leaf in the Topology tab.</div> : (
        <ul className="divide-y divide-zinc-200 dark:divide-zinc-800">
          {hot.map((c) => {
            const v = c.vlans.find((x) => x.ip) ?? c.vlans[0]
            return (
              <li key={c.name} className="py-2" data-testid="test-host-row">
                <div className="flex flex-wrap items-center gap-3 text-sm">
                  <span className="mono font-medium">{c.name}</span>
                  <span className="mono text-xs text-zinc-500">{c.links.map((l) => `${l.switch}:${l.switch_port}`).join(', ')}</span>
                  <span className="mono text-xs">{v?.ip ?? '—'}{v?.gateway ? ` via ${v.gateway}` : ''}</span>
                  <span className="text-xs">{c.deployed ? <span className="text-emerald-600 dark:text-emerald-400">deployed</span> : <span className="text-zinc-500">not deployed</span>}</span>
                  <span className="flex-1" />
                  <button className="btn-secondary btn-sm" onClick={() => setOpen(open === c.name ? null : c.name)} data-testid={`test-host-toggle-${c.name}`}>{open === c.name ? 'Hide actions' : 'Actions'}</button>
                </div>
                {open === c.name && <div className="mt-2 rounded border border-zinc-200 p-2 dark:border-zinc-800"><ClientActions lab={lab} client={c} clients={clients} /></div>}
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
