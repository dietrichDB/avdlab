import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { hostsApi } from '../api/hosts'
import { labsApi } from '../api/labs'
import { jobsApi } from '../api/jobs'
import { imagesApi } from '../api/images'
import type { Host, Image, Lab } from '../api/types'
import { JobsTable } from '../components/JobsTable'
import { Hero } from '../components/Hero'
import { IntroVideo } from '../components/IntroVideo'
import HostHealth from '../components/HostHealth'
import { Icon, type IconName } from '../components/Brand'
import { LabStateBadge, Empty, Spinner } from '../components/ui'
import { useMeta } from '../hooks/useMeta'
import { useUiMode } from '../lib/uiMode'
import { isRunning } from '../lib/labActions'
import { cls, fmtAgo } from '../lib/format'

const DAY = 24 * 3600_000
/** "online" = probed successfully within the last day (the API has no live heartbeat). */
export const hostOnline = (h: Host, now = Date.now()) => !!h.last_probe_at && now - new Date(h.last_probe_at).getTime() < DAY

export interface NextStep { id: string; label: string; detail: string; to: string; done: boolean }

/** CONTRACT-8 B §3: the getting-started checklist, driven by what exists. */
export function nextSteps(hosts: Host[], images: Image[], labs: Lab[]): NextStep[] {
  const deployed = labs.find((l) => isRunning(l.state))
  const validated = labs.find((l) => l.state === 'validated')
  const firstLab = deployed ?? labs[0]
  return [
    { id: 'host', label: 'Add a lab host', detail: 'A Linux machine with Docker that runs containerlab', to: '/hosts', done: hosts.length > 0 },
    { id: 'image', label: 'Download a cEOS image', detail: 'Fetch cEOS-lab from arista.com with your token', to: '/images', done: images.length > 0 },
    { id: 'lab', label: 'Create your first lab', detail: 'Pick a template, AVD generates the configs', to: '/labs/new', done: labs.length > 0 },
    { id: 'deploy', label: firstLab ? `Deploy ${firstLab.name}` : 'Deploy a lab', detail: 'Build the configs and start the switches', to: firstLab ? `/labs/${firstLab.id}/build` : '/labs', done: !!deployed },
    { id: 'validate', label: deployed ? `Validate ${deployed.name}` : 'Validate with ANTA', detail: 'Run the ANTA test catalogue against the fabric', to: deployed ? `/labs/${deployed.id}/validation` : '/labs', done: !!validated },
  ]
}

export default function DashboardPage() {
  const { advanced } = useUiMode()
  const meta = useMeta()
  const hosts = useQuery({ queryKey: ['hosts'], queryFn: hostsApi.list })
  const labs = useQuery({ queryKey: ['labs'], queryFn: labsApi.list })
  const images = useQuery({ queryKey: ['images'], queryFn: imagesApi.list })
  const jobs = useQuery({ queryKey: ['jobs', { limit: 15 }], queryFn: () => jobsApi.list({ limit: 15 }), refetchInterval: 5000, enabled: advanced })

  const h = hosts.data ?? []
  const l = labs.data ?? []
  const im = images.data ?? []
  const loaded = !hosts.isLoading && !labs.isLoading && !images.isLoading
  const m = meta.data

  return (
    <div className="mx-auto max-w-7xl space-y-5" data-testid="dashboard">
      <Hero title={m?.app_title || 'avdlab'} githubUrl={m?.github_url} />

      <div className="grid gap-5 xl:grid-cols-5">
        <div className="xl:col-span-3">
          {/* until /meta answers we don't know the URL — don't flash the fallback */}
          {meta.isLoading ? <div className="card aspect-video animate-pulse" /> : <IntroVideo src={m?.intro_video_url ?? null} />}
        </div>
        <div className="space-y-5 xl:col-span-2">
          <div className="grid grid-cols-2 gap-3" data-testid="stat-tiles">
            <StatTile id="labs" icon="labs" label="Labs" value={labs.isLoading ? '…' : l.length} to="/labs" />
            <StatTile id="running" icon="play" label="Running labs" value={labs.isLoading ? '…' : l.filter((x) => isRunning(x.state)).length} to="/labs" tone="emerald" />
            <StatTile id="hosts" icon="server" label="Hosts online" value={hosts.isLoading ? '…' : h.filter((x) => hostOnline(x)).length} sub={hosts.isLoading ? undefined : `of ${h.length}`} to="/hosts" />
            <StatTile id="images" icon="image" label="Images" value={images.isLoading ? '…' : im.length} to="/images" />
          </div>
          <section className="card" data-testid="next-steps">
            <div className="card-header"><span className="text-[13px] font-semibold">Next steps</span><span className="text-xs text-cv-muted">{loaded ? `${nextSteps(h, im, l).filter((s) => s.done).length}/5 done` : ''}</span></div>
            {!loaded ? <Spinner /> : <NextSteps steps={nextSteps(h, im, l)} />}
          </section>
        </div>
      </div>

      <section className="card overflow-hidden" data-testid="dashboard-host-health">
        <div className="card-header"><span className="h2">Lab hosts</span><span className="text-xs text-cv-muted">CPU · memory · disk, sampled every 20 s while this page is open</span><Link to="/hosts" className="link text-xs">manage</Link></div>
        <HostHealth />
      </section>
      {advanced && (
        <div className="space-y-5" data-testid="dashboard-advanced">
          <div className="flex items-center gap-3 pt-2"><span className="h2">Details</span><span className="h-px flex-1 bg-cv-border" /></div>
          <div className="grid gap-5 lg:grid-cols-2">
            <section className="card overflow-hidden">
              <div className="card-header"><span className="h2">Hosts</span><Link to="/hosts" className="link text-xs">manage</Link></div>
              {hosts.isLoading ? <Spinner /> : !h.length ? <div className="p-6 text-center text-[13px] text-cv-muted">No hosts. <Link className="link" to="/hosts">Add one</Link>.</div> : (
                <table className="table" data-testid="dashboard-hosts">
                  <thead><tr><th>Name</th><th>Address</th><th>Docker</th><th>clab</th><th>Probed</th></tr></thead>
                  <tbody>
                    {h.map((x) => (
                      <tr key={x.id}>
                        <td className="font-medium">{x.name}</td>
                        <td className="mono">{x.ssh_user}@{x.address}:{x.ssh_port}</td>
                        <td className="mono">{x.summary?.docker ? String(x.summary.docker) : <span className="text-cv-muted">—</span>}</td>
                        <td className="mono">{x.summary?.containerlab ? String(x.summary.containerlab) : <span className="text-cv-muted">—</span>}</td>
                        <td className="text-cv-muted">{fmtAgo(x.last_probe_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </section>
            <section className="card overflow-hidden">
              <div className="card-header"><span className="h2">Labs</span><Link to="/labs" className="link text-xs">all</Link></div>
              {labs.isLoading ? <Spinner /> : !l.length ? <div className="p-6 text-center text-[13px] text-cv-muted">No labs yet.</div> : (
                <table className="table" data-testid="dashboard-labs">
                  <thead><tr><th>Name</th><th>Template</th><th>State</th><th>cEOS</th><th>Updated</th></tr></thead>
                  <tbody>
                    {l.map((x) => (
                      <tr key={x.id}>
                        <td><Link className="link font-medium" to={`/labs/${x.id}`}>{x.name}</Link></td>
                        <td className="mono">{x.template}</td>
                        <td><LabStateBadge state={x.state} /></td>
                        <td className="mono">{x.ceos_version ?? '—'}</td>
                        <td className="text-cv-muted">{fmtAgo(x.updated_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </section>
          </div>
          <section data-testid="dashboard-jobs">
            <div className="mb-2 flex items-center justify-between">
              <span className="h2">Recent jobs</span>
              <Link to="/jobs" className="link text-xs">all</Link>
            </div>
            {jobs.isLoading ? <Spinner /> : jobs.data?.length ? <JobsTable jobs={jobs.data} /> : <Empty>No jobs yet.</Empty>}
          </section>
        </div>
      )}
    </div>
  )
}

const TONE = {
  blue: 'bg-cv-primary/10 text-cv-primary',
  emerald: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
}

function StatTile({ id, icon, label, value, sub, to, tone = 'blue' }: {
  id: string; icon: IconName; label: string; value: number | string; sub?: string; to: string; tone?: keyof typeof TONE
}) {
  return (
    <Link to={to} className="card group flex items-center gap-3 px-4 py-3 transition-shadow hover:shadow-md" data-testid={`stat-${id}`}>
      <span className={cls('flex h-9 w-9 shrink-0 items-center justify-center rounded-md', TONE[tone])}><Icon name={icon} className="h-[18px] w-[18px]" /></span>
      <span className="min-w-0">
        <span className="block text-[11px] font-medium uppercase tracking-wider text-cv-muted">{label}</span>
        <span className="flex items-baseline gap-1">
          <span className="text-xl font-semibold tabular-nums" data-testid={`stat-${id}-value`}>{value}</span>
          {sub && <span className="text-xs text-cv-muted">{sub}</span>}
        </span>
      </span>
    </Link>
  )
}

function NextSteps({ steps }: { steps: NextStep[] }) {
  const next = steps.find((s) => !s.done)?.id
  return (
    <ol className="divide-y divide-cv-border">
      {steps.map((s, i) => (
        <li key={s.id}>
          <Link to={s.to} className={cls('group flex items-center gap-3 px-4 py-2.5 hover:bg-cv-surface-2', s.id === next && 'bg-cv-primary-soft/60')}
            data-testid="next-step" data-step={s.id} data-done={s.done ? 'true' : 'false'}>
            <span className={cls('flex h-6 w-6 shrink-0 items-center justify-center rounded-full border text-[11px] font-semibold',
              s.done ? 'border-emerald-500 bg-emerald-500 text-white' : s.id === next ? 'border-cv-primary text-cv-primary' : 'border-cv-border-strong text-cv-muted')}>
              {s.done ? <Icon name="check" className="h-3.5 w-3.5" /> : i + 1}
            </span>
            <span className="min-w-0 flex-1">
              <span className={cls('block text-[13px] font-medium', s.done && 'text-cv-muted line-through decoration-cv-muted/50')}>{s.label}</span>
              {!s.done && <span className="block truncate text-xs text-cv-muted">{s.detail}</span>}
            </span>
            {s.id === next && <span className="text-xs font-medium text-cv-primary-text">Next</span>}
            <Icon name="chevron" className="h-4 w-4 text-cv-muted opacity-0 transition-opacity group-hover:opacity-100" />
          </Link>
        </li>
      ))}
    </ol>
  )
}
