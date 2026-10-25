import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { labsApi } from '../../api/labs'
import type { DeployParams, Lab, LabNodeStatus } from '../../api/types'
import PushConfigsCard from '../../components/PushConfigsCard'
import { useJobDrawer } from '../../components/JobDrawer'
import { Badge, ErrorBox, KV, Spinner } from '../../components/ui'
import { cls, fmtAgo } from '../../lib/format'
import { DEPLOYABLE_STATES, isRunning } from '../../lib/labActions'
import { labKnobValues } from '../../api/labs'
import { provisioningMode } from '../../lib/provisioning'
import { ZtpPanel } from './ZtpPanel'

const isCeos = (kind: string) => /ceos|veos/i.test(kind)

function stateColour(state: string) {
  const s = state.toLowerCase()
  if (s === 'running') return 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/60 dark:text-emerald-200'
  if (s === 'exited' || s === 'dead') return 'bg-red-100 text-red-800 dark:bg-red-900/60 dark:text-red-200'
  return 'bg-amber-100 text-amber-800 dark:bg-amber-900/60 dark:text-amber-200'
}

/** containerlab inspect + eAPI probe, one row per node */
export function RuntimeStatusTable({ nodes }: { nodes: LabNodeStatus[] }) {
  const ceos = nodes.filter((n) => isCeos(n.kind))
  const ready = ceos.filter((n) => n.eapi).length
  return (
    <div>
      <div className="mb-1 flex gap-3 px-2 text-xs text-zinc-500 dark:text-zinc-400" data-testid="runtime-summary">
        <span>{nodes.length} nodes</span>
        <span>{nodes.filter((n) => n.state.toLowerCase() === 'running').length} running</span>
        <span className={ready < ceos.length ? 'text-amber-600 dark:text-amber-400' : ''}>eAPI {ready}/{ceos.length}</span>
      </div>
      <table className="table" data-testid="runtime-table">
        <thead><tr><th>Node</th><th>Kind</th><th>State</th><th>Mgmt IP</th><th>Image</th><th className="text-center">eAPI</th></tr></thead>
        <tbody>
          {nodes.map((n) => (
            <tr key={n.name} data-testid="runtime-row">
              <td className="mono">{n.name}</td>
              <td className="mono text-zinc-500">{n.kind}</td>
              <td><Badge className={stateColour(n.state)}>{n.state}</Badge></td>
              <td className="mono">{n.mgmt_ip ?? '—'}</td>
              <td className="mono text-zinc-500">{n.image ?? '—'}</td>
              <td className="text-center" data-testid="eapi">
                {!isCeos(n.kind)
                  ? <span className="text-zinc-400" title="not an EOS node">—</span>
                  : n.eapi
                    ? <span className="font-semibold text-emerald-600 dark:text-emerald-400" title="eAPI reachable" aria-label="eAPI reachable">✓</span>
                    : <span className="font-semibold text-red-600 dark:text-red-400" title="eAPI unreachable" aria-label="eAPI unreachable">✗</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function RuntimeTab({ lab }: { lab: Lab }) {
  const drawer = useJobDrawer()
  const status = useQuery({ queryKey: ['labs', lab.id, 'status'], queryFn: () => labsApi.status(lab.id), staleTime: 15_000 })
  const [opts, setOpts] = useState<DeployParams>({ reconfigure: true, wait_eapi: true, wait_timeout: 600 })
  const deploy = useMutation({ mutationFn: () => drawer.track(labsApi.deploy(lab.id, opts)) })
  const refreshJob = useMutation({ mutationFn: () => drawer.track(labsApi.refreshStatus(lab.id)) })
  const canDeploy = DEPLOYABLE_STATES.has(lab.state) && !!lab.host_id
  const settings = (lab.settings ?? {}) as Record<string, unknown>
  const deployedAt = typeof settings.deployed_at === 'string' ? settings.deployed_at : null
  const ztp = lab.target !== 'act' && provisioningMode(labKnobValues(lab)) === 'ztp'
  const act = lab.target === 'act'

  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_20rem]">
      <div className="min-w-0 space-y-4">
      <section className="card min-w-0 overflow-x-auto p-2">
        <div className="mb-2 flex flex-wrap items-center gap-2 px-1">
          <span className="h2 mr-auto">Status</span>
          {status.isFetching && <span className="text-xs text-zinc-500">probing…</span>}
          <button className="btn-secondary btn-sm" onClick={() => status.refetch()} disabled={status.isFetching} title="GET /status (inline, ≤20 s)">Refresh</button>
          <button className="btn-secondary btn-sm" onClick={() => refreshJob.mutate()} disabled={refreshJob.isPending || !lab.host_id} title="run lab.status as a job">Refresh (job)</button>
        </div>
        <ErrorBox error={status.error ?? refreshJob.error} />
        {status.isLoading ? <Spinner label="containerlab inspect…" /> : !status.data?.deployed ? (
          <div className="p-4 text-center text-sm text-zinc-500 dark:text-zinc-400">
            Not deployed{lab.state === 'destroyed' ? ' (destroyed)' : ''}. {canDeploy ? (act ? 'Use Deploy to push the configs to the ACT lab.' : 'Use Deploy to start the containerlab topology.') : lab.state === 'draft' ? 'Build the lab first.' : ''}
          </div>
        ) : <RuntimeStatusTable nodes={status.data.nodes} />}
      </section>
      {ztp && <ZtpPanel lab={lab} />}
      {!act && <PushConfigsCard lab={lab} />}
      </div>

      <section className="card space-y-3 p-3">
        <div className="h2">Deploy</div>
        <KV items={[
          ['Host', lab.host ? <span>{lab.host.name} <span className="mono text-zinc-500">{lab.host.address}</span></span> : lab.host_id ?? <span className="text-red-600 dark:text-red-400">none</span>],
          ['Target', <span className="mono">{lab.target}</span>],
          ['Deployed', deployedAt ? fmtAgo(deployedAt) : '—'],
        ]} />
        {ztp && <div className="rounded bg-sky-50 px-2 py-1 text-xs text-sky-900 dark:bg-sky-950/40 dark:text-sky-100">ZTP mode: cEOS boots without a startup-config and fetches it from the <span className="mono">ztp</span> node.</div>}
        {act && <div className="rounded bg-violet-50 px-2 py-1 text-xs text-violet-900 dark:bg-violet-950/40 dark:text-violet-100">ACT target: no containerlab; deploy waits for eAPI from the tools server and pushes the configs (see the ACT tab).</div>}
        <label className={cls('flex items-center gap-2 text-sm', act && 'opacity-50')}>
          <input type="checkbox" disabled={act} checked={opts.reconfigure} onChange={(e) => setOpts({ ...opts, reconfigure: e.target.checked })} />
          <span>reconfigure <span className="mono text-zinc-500">--reconfigure</span></span>
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={opts.wait_eapi} onChange={(e) => setOpts({ ...opts, wait_eapi: e.target.checked })} />
          <span>wait for eAPI on all cEOS nodes</span>
        </label>
        <label className={cls('flex items-center gap-2 text-sm', !opts.wait_eapi && 'opacity-50')}>
          <span className="text-zinc-500">timeout</span>
          <input
            type="number" min={60} max={3600} step={30} className="input w-24 py-0.5" disabled={!opts.wait_eapi}
            value={opts.wait_timeout} onChange={(e) => setOpts({ ...opts, wait_timeout: Number(e.target.value) || 600 })}
          />
          <span className="text-zinc-500">s</span>
        </label>
        <ErrorBox error={deploy.error} />
        <button className="btn-primary w-full justify-center" disabled={!canDeploy || deploy.isPending} onClick={() => deploy.mutate()}
          title={!lab.host_id ? 'assign a host in Settings' : !canDeploy ? `not deployable in state ${lab.state}` : undefined}>
          {isRunning(lab.state) ? 'Redeploy' : 'Deploy'}
        </button>
        {!lab.host_id && <div className="text-xs text-red-600 dark:text-red-400">No host assigned — set one in Settings.</div>}
      </section>
    </div>
  )
}
