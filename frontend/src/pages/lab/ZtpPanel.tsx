import { useQuery } from '@tanstack/react-query'
import { jobsApi } from '../../api/jobs'
import { ztpApi } from '../../api/phase4'
import type { Lab, ZtpNode } from '../../api/types'
import { Badge, ErrorBox, Spinner } from '../../components/ui'
import { cls, fmtAgo, fmtDate } from '../../lib/format'
import { ZTP_STEPS, ztpComplete, ztpSteps, type ZtpStepState } from '../../lib/provisioning'

const STEP_CLASS: Record<ZtpStepState, string> = {
  done: 'border-emerald-500 bg-emerald-50 text-emerald-800 dark:border-emerald-600 dark:bg-emerald-950/40 dark:text-emerald-200',
  current: 'border-amber-400 bg-amber-50 text-amber-800 dark:border-amber-600 dark:bg-amber-950/40 dark:text-amber-200',
  pending: 'border-zinc-200 text-zinc-400 dark:border-zinc-700 dark:text-zinc-500',
}
const MARK: Record<ZtpStepState, string> = { done: '✓', current: '…', pending: '·' }

/** DHCP ✓ → bootfile ✓ → eAPI ✓ for one node */
export function ZtpTimeline({ node, active }: { node: ZtpNode; active?: boolean }) {
  const steps = ztpSteps(node)
  return (
    <div className="flex items-center gap-1" data-testid={`ztp-timeline-${node.name}`}>
      {ZTP_STEPS.map((s, i) => {
        const st = steps[s.id]
        return (
          <span key={s.id} className="flex items-center gap-1">
            {i > 0 && <span className={cls('h-px w-4', st === 'pending' ? 'bg-zinc-300 dark:bg-zinc-700' : 'bg-emerald-400')} />}
            <span data-testid={`ztp-step-${s.id}`} data-state={st} aria-label={`${s.label} ${st}`}
              className={cls('inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[11px] font-medium', STEP_CLASS[st], st === 'current' && active && 'animate-pulse')}>
              {s.label} <span>{MARK[st]}</span>
            </span>
          </span>
        )
      })}
    </div>
  )
}

export function ZtpNodesTable({ nodes, active }: { nodes: ZtpNode[]; active?: boolean }) {
  const done = nodes.filter(ztpComplete).length
  return (
    <div>
      <div className="mb-1 flex gap-3 px-2 text-xs text-zinc-500 dark:text-zinc-400" data-testid="ztp-summary">
        <span>{nodes.length} nodes</span>
        <span>DHCP {nodes.filter((n) => n.lease).length}/{nodes.length}</span>
        <span>bootfile {nodes.filter((n) => n.bootfile_fetched).length}/{nodes.length}</span>
        <span className={done < nodes.length ? 'text-amber-600 dark:text-amber-400' : ''}>eAPI {done}/{nodes.length}</span>
      </div>
      <table className="table" data-testid="ztp-table">
        <thead><tr><th>Node</th><th>MAC</th><th>IP</th><th>Bootfile</th><th>Progress</th></tr></thead>
        <tbody>
          {nodes.map((n) => (
            <tr key={n.name} data-testid="ztp-row">
              <td className="mono">{n.name}</td>
              <td className="mono text-zinc-500">{n.mac ?? '—'}</td>
              <td className="mono">{n.ip ?? '—'}</td>
              <td className="mono text-zinc-500">{n.bootfile ?? '—'}</td>
              <td><ZtpTimeline node={n} active={active} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** Runtime tab panel for provisioning.mode = ztp; polls every 10 s while a deploy runs (or nodes are still provisioning). */
export function ZtpPanel({ lab }: { lab: Lab }) {
  const jobs = useQuery({
    queryKey: ['jobs', { lab_id: lab.id, limit: 10 }],
    queryFn: () => jobsApi.list({ lab_id: lab.id, limit: 10 }),
    refetchInterval: 10_000,
  })
  const deploying = (jobs.data ?? []).some((j) => j.type === 'lab.deploy' && (j.status === 'queued' || j.status === 'running'))
  const ztp = useQuery({
    queryKey: ['labs', lab.id, 'ztp'],
    queryFn: () => ztpApi.status(lab.id),
    refetchInterval: (q) => {
      const nodes = q.state.data?.nodes ?? []
      return deploying || (nodes.length > 0 && nodes.some((n) => !ztpComplete(n)) && (lab.state === 'deployed' || lab.state === 'validated')) ? 10_000 : false
    },
  })
  const d = ztp.data
  const at = d?.at == null ? null : typeof d.at === 'number' ? new Date(d.at * 1000).toISOString() : d.at
  return (
    <section className="card min-w-0 overflow-x-auto p-2" data-testid="ztp-panel">
      <div className="mb-2 flex flex-wrap items-center gap-2 px-1">
        <span className="h2">ZTP</span>
        {d?.source && <Badge className="bg-sky-100 text-sky-800 dark:bg-sky-900/60 dark:text-sky-200">{d.source}</Badge>}
        {d?.server_ip && <span className="mono text-xs text-zinc-500">server {d.server_ip}</span>}
        {deploying && <Badge className="bg-blue-100 text-blue-800 dark:bg-blue-900/60 dark:text-blue-200">deploying · polling 10 s</Badge>}
        <span className="mr-auto" />
        {at && <span className="text-xs text-zinc-500" title={fmtDate(at)}>{d?.fresh ? 'read' : 'last poll'} {fmtAgo(at)}</span>}
        {ztp.isFetching && <span className="text-xs text-zinc-500">reading leases…</span>}
        <button className="btn-secondary btn-sm" onClick={() => ztp.refetch()} disabled={ztp.isFetching} title="GET /ztp (leases + HTTP log, ≤15 s)">Refresh</button>
      </div>
      <ErrorBox error={ztp.error} />
      {d?.error && <div className="mb-2 rounded border border-amber-300 bg-amber-50 px-2 py-1 text-xs text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-100">{d.error}</div>}
      {ztp.isLoading ? <Spinner label="ZTP status…" /> : !d?.nodes.length
        ? <div className="p-4 text-center text-sm text-zinc-500 dark:text-zinc-400">No ZTP data yet: build the lab in ZTP mode and deploy it. The <span className="mono">ztp</span> node then serves DHCP (option 67) and the bootfiles.</div>
        : <ZtpNodesTable nodes={d.nodes} active={deploying} />}
    </section>
  )
}
