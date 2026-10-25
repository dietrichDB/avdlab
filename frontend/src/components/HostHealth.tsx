import { useQueries, useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { hostsApi } from '../api/hosts'
import type { Host, HostMetricPoint, HostMetrics } from '../api/types'
import { Icon } from './Brand'
import { Spinner } from './ui'
import { cls, fmtAgo, fmtBytes } from '../lib/format'

/** Last point with data, or null. */
export function lastPoint(m?: HostMetrics): HostMetricPoint | null {
  const pts = m?.points ?? []
  for (let i = pts.length - 1; i >= 0; i--) if (pts[i].cpu_pct != null || pts[i].mem_used != null) return pts[i]
  return null
}

export function pct(used?: number | null, total?: number | null): number | null {
  if (used == null || !total) return null
  return Math.max(0, Math.min(100, Math.round((used / total) * 100)))
}

function tone(p: number | null): string {
  if (p == null) return 'bg-zinc-300 dark:bg-zinc-700'
  if (p >= 90) return 'bg-red-500'
  if (p >= 75) return 'bg-amber-500'
  return 'bg-cv-primary'
}

export function Bar({ label, value, text, testId }: { label: string; value: number | null; text: string; testId?: string }) {
  return (
    <div className="min-w-0" data-testid={testId}>
      <div className="flex items-baseline justify-between text-[11px]"><span className="text-cv-muted">{label}</span><span className="mono">{text}</span></div>
      <div className="mt-0.5 h-2 w-full overflow-hidden rounded-full bg-cv-border" role="progressbar" aria-label={label} aria-valuenow={value ?? undefined} aria-valuemin={0} aria-valuemax={100}>
        <div className={cls('h-full rounded-full transition-[width]', tone(value))} style={{ width: `${value ?? 0}%` }} />
      </div>
    </div>
  )
}

/** Dashboard: one row per host with CPU / memory / disk bars and container count from the monitor's latest sample. */
export default function HostHealth() {
  const hosts = useQuery({ queryKey: ['hosts'], queryFn: hostsApi.list })
  const list = hosts.data ?? []
  const metrics = useQueries({
    queries: list.map((h) => ({ queryKey: ['hosts', h.id, 'metrics', 'dash'], queryFn: () => hostsApi.metrics(h.id, { step: 60 }), refetchInterval: 30_000, retry: false, staleTime: 20_000 })),
  })
  if (hosts.isLoading) return <Spinner />
  if (!list.length) return <div className="p-6 text-center text-[13px] text-cv-muted">No hosts yet. <Link className="link" to="/hosts">Add a lab host</Link> to see its load here.</div>
  return (
    <div className="divide-y divide-cv-border" data-testid="host-health">
      {list.map((h, i) => {
        const m = metrics[i]?.data
        const p = lastPoint(m)
        const online = hostOnline(h, m)
        const cpu = p?.cpu_pct == null ? null : Math.round(p.cpu_pct)
        const mem = pct(p?.mem_used, p?.mem_total)
        const disks = Object.entries(p?.disk ?? {})
        const root = disks.find(([k]) => k === '/') ?? disks[0]
        const disk = root ? pct(root[1].used, root[1].size) : null
        const containers = Object.keys(m?.containers ?? {}).length
        return (
          <div key={h.id} className="grid items-center gap-x-4 gap-y-2 px-4 py-3 md:grid-cols-[14rem_1fr_1fr_1fr_6rem]" data-testid="host-health-row" data-host={h.name}>
            <div className="flex min-w-0 items-center gap-2">
              <span className={cls('h-2.5 w-2.5 shrink-0 rounded-full', online ? 'bg-emerald-500' : 'bg-zinc-400')} title={online ? 'online' : 'no recent sample'} />
              <Link to={`/hosts/${h.id}`} className="truncate font-medium hover:underline">{h.name}</Link>
              <span className="truncate text-[11px] text-cv-muted">{h.address}</span>
            </div>
            <Bar label="CPU" value={cpu} text={cpu == null ? '—' : `${cpu}%${p?.load1 != null ? ` · load ${p.load1.toFixed(2)}` : ''}`} testId="bar-cpu" />
            <Bar label="Memory" value={mem} text={p?.mem_used != null && p.mem_total ? `${fmtBytes(p.mem_used)} / ${fmtBytes(p.mem_total)}` : '—'} testId="bar-mem" />
            <Bar label={`Disk ${root ? root[0] : ''}`} value={disk} text={root ? `${fmtBytes(root[1].used)} / ${fmtBytes(root[1].size)}` : '—'} testId="bar-disk" />
            <div className="flex items-center justify-between text-[11px] text-cv-muted md:flex-col md:items-end md:justify-center">
              <span className="inline-flex items-center gap-1"><Icon name="labs" className="h-3 w-3" />{containers} container{containers === 1 ? '' : 's'}</span>
              <span>{m?.sampled_at ? fmtAgo(m.sampled_at) : metrics[i]?.isLoading ? 'sampling…' : 'no sample'}</span>
            </div>
          </div>
        )
      })}
    </div>
  )
}

function hostOnline(h: Host, m?: HostMetrics): boolean {
  if (m?.sampled_at) return Date.now() - new Date(m.sampled_at).getTime() < 5 * 60_000
  return !!h.last_probe_at && Date.now() - new Date(h.last_probe_at).getTime() < 60 * 60_000
}
