import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { labsApi } from '../api/labs'
import type { Lab } from '../api/types'
import { useJobDrawer } from './JobDrawer'
import { ErrorBox, Hint } from './ui'
import { fmtAgo } from '../lib/format'

/**
 * 0.10.0: after a Build on a running lab, push only the devices whose intended config changed (eos_config replace over
 * eAPI, no container restart) instead of a full Redeploy.
 */
export default function PushConfigsCard({ lab, compact = false }: { lab: Lab; compact?: boolean }) {
  const drawer = useJobDrawer()
  const qc = useQueryClient()
  const plan = useQuery({ queryKey: ['labs', lab.id, 'push-plan'], queryFn: () => labsApi.pushPlan(lab.id), staleTime: 5_000 })
  const push = useMutation({
    mutationFn: (hosts?: string[]) => drawer.track(labsApi.push(lab.id, hosts)),
    onSuccess: () => setTimeout(() => qc.invalidateQueries({ queryKey: ['labs', lab.id, 'push-plan'] }), 2000),
  })
  const p = plan.data
  if (!p || !p.available) return null
  const pending = [...p.changed, ...p.unknown]
  const sinceTs = p.since ? new Date(p.since * 1000).toISOString() : null
  return (
    <section className="card space-y-2 p-3" data-testid="push-configs">
      <div className="flex flex-wrap items-center gap-2">
        <span className="h2 mr-auto">Push config changes</span>
        {pending.length ? <span className="badge bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-200" data-testid="push-count">{pending.length} of {p.total} devices changed</span>
          : <span className="badge bg-emerald-100 text-emerald-800 dark:bg-emerald-900/60 dark:text-emerald-200" data-testid="push-count">in sync</span>}
      </div>
      {!compact && <Hint className="mb-0">Changed a knob or a file and rebuilt? You don't need a full Redeploy: this sends the new intended config to the changed switches only, as a config replace over eAPI — a few seconds, no reboot, the fabric keeps running. Validate afterwards.</Hint>}
      <div className="text-xs text-cv-muted">
        {sinceTs ? <>last deploy/push {fmtAgo(sinceTs)} · </> : null}
        {p.changed.length > 0 && <span>changed: <span className="mono">{p.changed.join(', ')}</span></span>}
        {p.unknown.length > 0 && <span>{p.changed.length ? ' · ' : ''}not tracked yet (deployed before this version): <span className="mono">{p.unknown.join(', ')}</span></span>}
        {!pending.length && <span>every device runs its intended config</span>}
      </div>
      <ErrorBox error={push.error} />
      <div className="flex flex-wrap gap-2">
        <button className="btn-primary btn-sm" disabled={!pending.length || push.isPending} onClick={() => push.mutate(undefined)} data-testid="push-changed">
          {push.isPending ? 'Starting…' : pending.length ? `Push ${pending.length} changed` : 'Nothing to push'}
        </button>
        <button className="btn-secondary btn-sm" disabled={push.isPending || !p.total} onClick={() => push.mutate([...p.changed, ...p.unchanged, ...p.unknown])} data-testid="push-all" title="config replace on every device (still no restart)">Push all {p.total}</button>
      </div>
    </section>
  )
}
