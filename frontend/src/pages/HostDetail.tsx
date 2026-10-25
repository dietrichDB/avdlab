import { lazy, Suspense, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { hostsApi } from '../api/hosts'
import type { ContainerKillResult } from '../api/types'
import { normArch } from '../api/images'
import PlatformBadge from '../components/PlatformBadge'
import { ContainerActionDialog, type ContainerAction } from '../components/ContainerActionDialog'
import { TerminalPanel } from '../components/TerminalPanel'
import { Badge, ErrorBox, PageHeader, Spinner } from '../components/ui'
import { cls, fmtAgo, fmtBytes, fmtDate } from '../lib/format'

const HostCharts = lazy(() => import('../components/HostCharts'))

function containerStateColour(state: string) {
  const s = state.toLowerCase()
  if (s === 'running') return 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/60 dark:text-emerald-200'
  if (s === 'exited' || s === 'dead') return 'bg-red-100 text-red-800 dark:bg-red-900/60 dark:text-red-200'
  return 'bg-amber-100 text-amber-800 dark:bg-amber-900/60 dark:text-amber-200'
}

export default function HostDetailPage() {
  const { id = '' } = useParams()
  const [term, setTerm] = useState(false)
  const [procFilter, setProcFilter] = useState('')
  const [cAction, setCAction] = useState<ContainerAction | null>(null)
  const [cResult, setCResult] = useState<ContainerKillResult | null>(null)
  const host = useQuery({ queryKey: ['hosts', id], queryFn: () => hostsApi.get(id) })
  const caps = useQuery({ queryKey: ['hosts', id, 'caps'], queryFn: () => hostsApi.capabilities(id) })
  const metrics = useQuery({ queryKey: ['hosts', id, 'metrics'], queryFn: () => hostsApi.metrics(id), refetchInterval: 20_000, refetchIntervalInBackground: false })
  const procs = useQuery({ queryKey: ['hosts', id, 'processes'], queryFn: () => hostsApi.processes(id), refetchInterval: 30_000 })
  const containers = useQuery({ queryKey: ['hosts', id, 'containers'], queryFn: () => hostsApi.containers(id), refetchInterval: 30_000 })

  if (host.isLoading) return <Spinner />
  if (!host.data) return <ErrorBox error={host.error ?? 'host not found'} />
  const h = host.data
  const c = caps.data?.capabilities
  const arch = normArch(c?.os?.arch)
  const procRows = (procs.data?.processes ?? []).filter((p) => !procFilter || p.command.toLowerCase().includes(procFilter.toLowerCase()) || p.user.includes(procFilter))
  return (
    <div className="space-y-4">
      <PageHeader
        title={<span className="flex items-center gap-2">{h.name}{arch && <Badge className="bg-zinc-200 dark:bg-zinc-700">{arch}</Badge>}<PlatformBadge caps={c} /></span>}
        sub={<><Link className="link" to="/hosts">Hosts</Link> · <span className="mono">{h.ssh_user}@{h.address}:{h.ssh_port}</span>{h.summary?.os ? ` · ${h.summary.os}` : ''}{c?.cpu?.nproc ? ` · ${c.cpu.nproc} CPUs` : ''}{c?.mem?.total_gb ? ` · ${c.mem.total_gb} GB` : ''}</>}
        actions={<>
          {metrics.data && (
            <span className="text-xs text-zinc-500" data-testid="metrics-sampled">
              <span className={cls('mr-1 inline-block h-2 w-2 rounded-full', metrics.data.watched ? 'bg-emerald-500' : 'bg-zinc-400')} title={metrics.data.watched ? 'sampler watching this host' : 'not watched'} />
              sampled {metrics.data.sampled_at ? <span title={fmtDate(metrics.data.sampled_at)}>{fmtAgo(metrics.data.sampled_at)}</span> : 'never'}
            </span>
          )}
          <button className="btn-primary" data-testid="host-terminal" onClick={() => setTerm(true)}>Terminal</button>
        </>}
      />
      <ErrorBox error={metrics.error} />
      {metrics.isLoading ? <Spinner label="Loading metrics…" /> : metrics.data && (
        <Suspense fallback={<Spinner label="Loading charts…" />}><HostCharts metrics={metrics.data} /></Suspense>
      )}

      <div className="grid gap-4 xl:grid-cols-2">
        <section className="card overflow-x-auto p-2">
          <div className="mb-2 flex items-center gap-2 px-1">
            <span className="h2 mr-auto">Processes</span>
            {procs.data && <span className="text-xs text-zinc-500">{fmtAgo(procs.data.at)} · every 30 s</span>}
            <input className="input w-40 py-0.5 text-xs" placeholder="filter" value={procFilter} onChange={(e) => setProcFilter(e.target.value)} />
          </div>
          <ErrorBox error={procs.error} />
          {procs.isLoading ? <Spinner /> : (
            <table className="table" data-testid="processes">
              <thead><tr><th>PID</th><th>User</th><th className="text-right">CPU %</th><th className="text-right">Mem %</th><th className="text-right">RSS</th><th>Command</th></tr></thead>
              <tbody>
                {procRows.map((p) => (
                  <tr key={p.pid}>
                    <td className="mono">{p.pid}</td>
                    <td className="mono">{p.user}</td>
                    <td className="mono text-right">{p.cpu_pct.toFixed(1)}</td>
                    <td className="mono text-right">{p.mem_pct.toFixed(1)}</td>
                    <td className="mono text-right">{fmtBytes(p.rss * 1024)}</td>
                    <td className="mono max-w-[28rem] truncate" title={p.command}>{p.command}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
        <section className="card overflow-x-auto p-2">
          <div className="mb-2 flex items-center gap-2 px-1">
            <span className="h2 mr-auto">Containers</span>
            {containers.data && <span className="text-xs text-zinc-500">{containers.data.length} · {containers.data.filter((x) => x.state === 'running').length} running</span>}
          </div>
          <ErrorBox error={containers.error} />
          {cResult && (
            <div className="mb-2 px-1 text-xs" data-testid="container-result">
              <span className={cResult.removed ? 'text-emerald-600' : 'text-red-600'}>
                docker {cResult.action === 'rm' ? 'rm -f' : 'stop'} {cResult.name}: {cResult.removed ? 'done' : `failed (rc=${cResult.returncode})`}
              </span>
              {cResult.output && <span className="mono ml-2 text-zinc-500">{cResult.output}</span>}
            </div>
          )}
          {containers.isLoading ? <Spinner /> : (
            <table className="table" data-testid="containers">
              <thead><tr><th>Name</th><th>Image</th><th>State</th><th>Status</th><th>Lab</th><th></th></tr></thead>
              <tbody>
                {(containers.data ?? []).map((x) => (
                  <tr key={x.name}>
                    <td className="mono">{x.name}</td>
                    <td className="mono text-zinc-500">{x.image}</td>
                    <td><Badge className={containerStateColour(x.state)}>{x.state}</Badge></td>
                    <td className="text-xs text-zinc-500">{x.status}</td>
                    <td className="mono">{x.labels?.['avdlab.lab'] ?? '—'}</td>
                    <td className="whitespace-nowrap">
                      {x.state === 'running' && (
                        <button className="btn-secondary btn-sm" onClick={() => setCAction({ container: x, force: false })}>Stop</button>
                      )}{' '}
                      <button className="btn-danger btn-sm" onClick={() => setCAction({ container: x, force: true })}>Kill</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>
      <ContainerActionDialog hostId={h.id} action={cAction} onClose={() => setCAction(null)} onDone={setCResult} />
      {term && <TerminalPanel title={`${h.name}: host shell`} path={hostsApi.terminalPath(h.id)} onClose={() => setTerm(false)} />}
    </div>
  )
}
