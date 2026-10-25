import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { labsApi } from '../../api/labs'
import { netboxApi } from '../../api/phase4'
import { isTerminal } from '../../api/jobs'
import type { Lab, NetBoxSyncParams, NetBoxSyncResult } from '../../api/types'
import { useJobDrawer } from '../../components/JobDrawer'
import { Badge, ErrorBox, Field, JobStatusBadge, KV, Spinner } from '../../components/ui'
import { useJobResult } from '../../hooks/useJobResult'
import { fmtAgo, fmtDate } from '../../lib/format'
import { syncRows, syncTotals } from '../../lib/netbox'
import { NetBoxTestConnection } from '../NetBox'

const BUILT = new Set(['built', 'deployed', 'validated', 'destroyed'])

/** created / updated / deleted / unchanged per object type + skipped list */
export function NetBoxSyncResultView({ result }: { result: NetBoxSyncResult }) {
  const rows = syncRows(result)
  const tot = syncTotals(result)
  return (
    <div className="space-y-3" data-testid="netbox-last-sync">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        {result.dry_run && <Badge className="bg-amber-100 text-amber-800 dark:bg-amber-900/60 dark:text-amber-200">dry run — nothing written</Badge>}
        <span data-testid="netbox-totals" className="flex flex-wrap gap-3 text-xs text-zinc-600 dark:text-zinc-300">
          <span><b className="text-emerald-600 dark:text-emerald-400">{tot.created}</b> created</span>
          <span><b className="text-blue-600 dark:text-blue-400">{tot.updated}</b> updated</span>
          <span><b className="text-red-600 dark:text-red-400">{tot.deleted}</b> deleted</span>
          <span><b>{tot.unchanged}</b> unchanged</span>
          <span><b className={result.skipped.length ? 'text-amber-600 dark:text-amber-400' : ''}>{result.skipped.length}</b> skipped</span>
        </span>
        <span className="mr-auto" />
        {result.at && <span className="text-xs text-zinc-500" title={fmtDate(result.at)}>{fmtAgo(result.at)}</span>}
        {result.netbox_url && <a className="btn-secondary btn-sm" data-testid="netbox-open" href={result.netbox_url} target="_blank" rel="noreferrer">Open in NetBox ↗</a>}
      </div>
      {rows.length === 0 ? <div className="text-sm text-zinc-500">No objects.</div> : (
        <table className="table" data-testid="netbox-counts">
          <thead><tr><th>Type</th><th className="text-right">Created</th><th className="text-right">Updated</th><th className="text-right">Deleted</th><th className="text-right">Unchanged</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.type} data-testid={`netbox-count-${r.type}`}>
                <td className="mono">{r.type}</td>
                <td className="text-right tabular-nums">{r.created || <span className="text-zinc-400">0</span>}</td>
                <td className="text-right tabular-nums">{r.updated || <span className="text-zinc-400">0</span>}</td>
                <td className="text-right tabular-nums">{r.deleted || <span className="text-zinc-400">0</span>}</td>
                <td className="text-right tabular-nums text-zinc-500">{r.unchanged}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {result.skipped.length > 0 && (
        <div>
          <div className="label">Skipped</div>
          <ul className="space-y-1 text-xs" data-testid="netbox-skipped">
            {result.skipped.map((s, i) => <li key={i} className="rounded border border-amber-300 bg-amber-50 px-2 py-1 text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-100">{s}</li>)}
          </ul>
        </div>
      )}
    </div>
  )
}

export default function NetBoxTab({ lab }: { lab: Lab }) {
  const qc = useQueryClient()
  const drawer = useJobDrawer()
  const targets = useQuery({ queryKey: ['netbox', 'targets'], queryFn: netboxApi.targets })
  const status = useQuery({ queryKey: ['labs', lab.id, 'netbox'], queryFn: () => netboxApi.status(lab.id) })
  const [opts, setOpts] = useState<NetBoxSyncParams>({ delete_stale: false, dry_run: false, include_clients: true })
  const [jobId, setJobId] = useState<string | null>(null)
  const job = useJobResult(jobId)
  const bind = useMutation({
    mutationFn: (id: string) => labsApi.update(lab.id, { netbox_target_id: id || null }),
    onSuccess: (l) => {
      qc.setQueryData(['labs', lab.id], l)
      qc.invalidateQueries({ queryKey: ['labs', lab.id, 'netbox'] })
      qc.invalidateQueries({ queryKey: ['netbox', 'targets'] })
    },
  })
  const sync = useMutation({
    mutationFn: () => drawer.track(netboxApi.sync(lab.id, opts)),
    onSuccess: (r) => setJobId(r.job_id),
  })
  const done = !!job.data && isTerminal(job.data.status)
  useEffect(() => { if (done) qc.invalidateQueries({ queryKey: ['labs', lab.id, 'netbox'] }) }, [done, qc, lab.id])

  const target = status.data?.target ?? null
  const boundId = lab.netbox_target_id ?? target?.id ?? ''
  const reason = !target ? 'pick a NetBox target first' : !target.has_token ? `target ${target.name} has no API token` : !BUILT.has(lab.state) ? `build the lab first (state: ${lab.state})` : undefined
  const flag = (k: keyof NetBoxSyncParams, label: React.ReactNode) => (
    <label className="flex items-center gap-2 text-sm">
      <input type="checkbox" name={k} checked={opts[k]} onChange={(e) => setOpts({ ...opts, [k]: e.target.checked })} />
      <span>{label}</span>
    </label>
  )

  if (status.isLoading) return <Spinner label="NetBox status…" />
  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_22rem]">
      <section className="card min-w-0 overflow-x-auto p-3">
        <div className="h2 mb-2">Last sync</div>
        <ErrorBox error={status.error} />
        {jobId && job.data && !done && <div className="mb-2 flex items-center gap-2 text-xs text-zinc-500"><JobStatusBadge status={job.data.status} /> syncing…</div>}
        {job.data?.status === 'failed' && <div className="mb-2"><ErrorBox error={job.data.error ?? 'sync failed'} /></div>}
        {status.data?.last_sync
          ? <NetBoxSyncResultView result={status.data.last_sync} />
          : <div className="p-4 text-center text-sm text-zinc-500 dark:text-zinc-400">Not synced yet. Sync creates/updates the lab&apos;s devices, interfaces, VLANs, VRFs, prefixes, IPs and cables, all tagged <span className="mono">avdlab:{lab.slug}</span>.</div>}
      </section>

      <div className="space-y-4">
        <section className="card space-y-3 p-3" data-testid="netbox-target-select">
          <div className="h2">Target</div>
          <Field label="NetBox target" help={targets.data?.length === 0 ? <>no targets yet: add one on the <Link className="link" to="/netbox">NetBox</Link> page</> : undefined}>
            <select className="input" name="netbox_target_id" value={boundId} disabled={bind.isPending} onChange={(e) => bind.mutate(e.target.value)}>
              <option value="">— none —</option>
              {(targets.data ?? []).map((t) => <option key={t.id} value={t.id}>{t.name} ({t.url})</option>)}
            </select>
          </Field>
          <ErrorBox error={bind.error} />
          {target && (<>
            <KV items={[
              ['URL', <a className="link mono" href={target.url} target="_blank" rel="noreferrer">{target.url}</a>],
              ['Token', target.has_token ? 'set' : <span className="text-red-600 dark:text-red-400">missing</span>],
              ['Certs', target.verify_certs ? 'verify' : <span className="text-amber-600">no verify</span>],
            ]} />
            <NetBoxTestConnection target={target} />
          </>)}
        </section>

        <section className="card space-y-2 p-3" data-testid="netbox-sync-form">
          <div className="h2">Sync</div>
          <div className="text-xs text-zinc-500">Idempotent: looks objects up by name/serial, creates or updates them.</div>
          {flag('include_clients', 'include clients (as servers)')}
          {flag('delete_stale', <>delete stale objects tagged <span className="mono">avdlab:{lab.slug}</span></>)}
          {flag('dry_run', 'dry run (count only, write nothing)')}
          <ErrorBox error={sync.error} />
          <button className="btn-primary w-full justify-center" data-testid="netbox-sync" disabled={!!reason || sync.isPending || (!!jobId && !done)} title={reason} onClick={() => sync.mutate()}>
            {sync.isPending ? 'Starting…' : opts.dry_run ? 'Dry-run sync' : 'Sync to NetBox'}
          </button>
          {reason && <div className="text-xs text-zinc-500">{reason}</div>}
        </section>
      </div>
    </div>
  )
}
