import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { labsApi } from '../../api/labs'
import { hostsApi } from '../../api/hosts'
import type { Lab } from '../../api/types'
import { Badge, Empty, ErrorBox, JobStatusBadge, KV, LabStateBadge, Spinner } from '../../components/ui'
import { fmtAgo, fmtDate } from '../../lib/format'
import { ApiError } from '../../api/client'

export default function OverviewTab({ lab }: { lab: Lab }) {
  const devices = useQuery({ queryKey: ['labs', lab.id, 'devices'], queryFn: () => labsApi.devices(lab.id) })
  const host = useQuery({ queryKey: ['hosts', lab.host_id], queryFn: () => hostsApi.get(lab.host_id!), enabled: !!lab.host_id && !lab.host })
  const h = lab.host ?? host.data
  const lb = lab.last_build_job
  const notBuilt = devices.error instanceof ApiError && (devices.error.status === 404 || devices.error.status === 409)
  return (
    <div className="space-y-4">
      <div className="grid gap-4 md:grid-cols-2">
        <section className="card p-3">
          <div className="h2 mb-2">Lab</div>
          <KV items={[
            ['State', <LabStateBadge state={lab.state} />],
            ['Template', <span className="mono">{lab.template}</span>],
            ['cEOS', <span className="mono">{lab.ceos_version ?? '—'}</span>],
            ['Host', h ? <Link className="link" to="/hosts">{h.name} <span className="mono text-zinc-500">{h.address}</span></Link> : 'not assigned'],
            ['Created', fmtDate(lab.created_at)],
            ['Updated', fmtAgo(lab.updated_at)],
          ]} />
        </section>
        <section className="card p-3">
          <div className="h2 mb-2">Last build</div>
          {!lb ? <div className="text-sm text-zinc-500">Never built. Press Build to run pyavd.</div> : (
            <KV items={[
              ['Status', <JobStatusBadge status={lb.status} />],
              ['Job', <Link className="link mono" to={`/jobs/${lb.id}`}>{lb.id.slice(0, 10)}</Link>],
              ['When', fmtAgo(lb.created_at)],
              ['Hosts', Array.isArray(lb.result?.hosts) ? (lb.result.hosts as string[]).length : '—'],
              ['Warnings', Array.isArray(lb.result?.warnings) ? (lb.result.warnings as string[]).length : '—'],
              ['Error', lb.error],
            ]} />
          )}
        </section>
      </div>
      <section>
        <div className="h2 mb-2">Devices</div>
        {devices.isLoading ? <Spinner /> : notBuilt || !devices.data?.length ? <Empty>No structured configs yet — build the lab first.</Empty> : devices.error ? <ErrorBox error={devices.error} /> : (
          <div className="card overflow-x-auto">
            <table className="table" data-testid="devices">
              <thead><tr><th>Hostname</th><th>Type</th><th>Platform</th><th>Mgmt IP</th><th>Serial</th><th>Loopback0</th><th>VTEP</th><th>BGP AS</th><th>Tags</th></tr></thead>
              <tbody>
                {devices.data.map((d) => (
                  <tr key={d.hostname} className={d.is_deployed === false ? 'opacity-50' : ''}>
                    <td className="mono font-medium">{d.hostname}</td>
                    <td>{d.type ?? '—'}</td>
                    <td className="text-xs">{d.platform ?? '—'}</td>
                    <td className="mono">{d.mgmt_ip ?? '—'}</td>
                    <td className="mono">{d.serial ?? '—'}</td>
                    <td className="mono">{d.loopback0 ?? '—'}</td>
                    <td className="mono">{d.vtep ?? '—'}</td>
                    <td className="mono">{d.bgp_as ?? '—'}</td>
                    <td data-testid={`tags-${d.hostname}`}>
                      {Object.entries(d.tags ?? {}).length ? (
                        <span className="flex flex-wrap gap-1">
                          {Object.entries(d.tags ?? {}).map(([k, v]) => (
                            <Badge key={k} className="mono bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-200">{k}:{v}</Badge>
                          ))}
                        </span>
                      ) : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  )
}
