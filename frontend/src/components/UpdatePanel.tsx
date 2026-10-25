import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { updateApi, waitForNewVersion, type UpdateStatus } from '../api/phase6'
import { ApiError } from '../api/client'
import { useJobDrawer } from './JobDrawer'
import { ErrorBox, KV, Spinner } from './ui'
import { Markdown } from '../lib/markdown'
import { fmtAgo, fmtDate } from '../lib/format'

export function useUpdateStatus() {
  return useQuery({ queryKey: ['meta', 'update'], queryFn: updateApi.status, staleTime: 10 * 60_000, retry: false })
}

/** Small "update" pill next to the version badge when a newer release is known. */
export function UpdatePill() {
  const u = useUpdateStatus()
  if (!u.data?.available) return null
  return (
    <Link to="/about#update" data-testid="update-pill" title={`avdlab ${u.data.latest} is available`}
      className="ml-1 inline-flex items-center rounded-full bg-emerald-100 px-1.5 text-[10px] font-semibold text-emerald-800 hover:bg-emerald-200 dark:bg-emerald-900/60 dark:text-emerald-200">
      update {u.data.latest}
    </Link>
  )
}

const MODE_TEXT: Record<string, string> = {
  source: 'git pull + docker compose up -d --build in the deploy directory',
  image: 'pull the release image and recreate the api container',
  manual: 'manual: follow the release notes',
}

function conflictText(e: unknown): string | null {
  if (!(e instanceof ApiError) || e.status !== 409) return null
  const d = e.detail as { message?: string; reason?: string } | string
  return typeof d === 'string' ? d : [d?.message, d?.reason].filter(Boolean).join(': ')
}

/**
 * CONTRACT-6 §1 update panel (About + Changelog): current / latest / notes / mode, Check now, Update now (confirm → job
 * drawer). After the job starts the API container is recreated: poll /api/meta until the new version answers, then reload.
 */
export function UpdatePanel({ reload = () => window.location.reload(), pollMs = 3000 }: { reload?: () => void; pollMs?: number }) {
  const qc = useQueryClient()
  const drawer = useJobDrawer()
  const u = useUpdateStatus()
  const [confirm, setConfirm] = useState(false)
  const [phase, setPhase] = useState<null | 'waiting' | 'down' | 'done' | 'timeout'>(null)
  const [newVersion, setNewVersion] = useState<string | null>(null)
  const check = useMutation({ mutationFn: updateApi.check, onSuccess: (s) => qc.setQueryData(['meta', 'update'], s) })
  const apply = useMutation({
    mutationFn: () => updateApi.apply({ to: u.data?.latest ?? undefined }),
    onSuccess: (r) => {
      setConfirm(false)
      if (r.job_id) drawer.openJob(r.job_id)
      setPhase('waiting')
      const s = u.data as UpdateStatus
      waitForNewVersion(s.current, s.latest, { interval: pollMs, onTick: (t) => setPhase(t === 'down' ? 'down' : t === 'new' ? 'done' : 'waiting') })
        .then((v) => { setNewVersion(v); setPhase('done'); qc.invalidateQueries({ queryKey: ['meta'] }); setTimeout(reload, Math.min(1500, pollMs * 5)) })
        .catch(() => setPhase('timeout'))
    },
  })
  const s = u.data
  if (u.isLoading) return <section className="card p-4" id="update"><Spinner label="Checking for updates…" /></section>
  return (
    <section className="card space-y-3 p-4" id="update" data-testid="update-panel">
      <div className="flex flex-wrap items-center gap-2">
        <span className="h2 flex-1">Updates</span>
        {s?.available && <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-xs font-semibold text-emerald-800 dark:bg-emerald-900/60 dark:text-emerald-200" data-testid="update-available">update available</span>}
        {s && !s.available && s.latest && !s.error && <span className="text-xs text-emerald-600 dark:text-emerald-400" data-testid="update-uptodate">up to date</span>}
        <button className="btn-secondary btn-sm" onClick={() => check.mutate()} disabled={check.isPending || phase !== null} data-testid="update-check">{check.isPending ? 'Checking…' : 'Check now'}</button>
        {s?.available && s.can_self_update && !confirm && phase === null && (
          <button className="btn-primary btn-sm" onClick={() => setConfirm(true)} data-testid="update-now">Update now</button>
        )}
      </div>
      <ErrorBox error={u.error ?? check.error} />
      {s && (
        <>
          {s.hint && <div className="rounded border border-zinc-300 bg-zinc-50 px-3 py-2 text-xs text-zinc-600 dark:border-zinc-700 dark:bg-zinc-800/50 dark:text-zinc-300" data-testid="update-hint">{s.hint}</div>}
          {s.error && <div className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-100" data-testid="update-error">Last check failed: {s.error}</div>}
          <KV items={[
            ['running', <span className="mono" data-testid="update-current">v{s.current}</span>],
            ['latest', s.latest ? <span className="mono" data-testid="update-latest">v{s.latest}{s.url && <a className="link ml-2 font-sans text-xs" href={s.url} target="_blank" rel="noreferrer">release</a>}</span> : '—'],
            ['published', s.published_at ? fmtDate(s.published_at) : '—'],
            ['checked', s.checked_at ? <span title={fmtDate(s.checked_at)}>{fmtAgo(s.checked_at)}</span> : 'never'],
            ['mode', <span data-testid="update-mode"><span className="mono">{s.mode}</span> <span className="text-xs text-zinc-500">— {MODE_TEXT[s.mode] ?? ''}</span></span>],
            ['self-update', s.can_self_update ? <span className="text-emerald-600 dark:text-emerald-400">possible</span> : <span className="text-zinc-500" data-testid="update-reason">not possible{s.reason ? `: ${s.reason}` : ''}</span>],
            ...(s.last_update ? [['last update', <span data-testid="update-last">{s.last_update.from} → {s.last_update.to}{s.last_update.at ? ` · ${fmtAgo(s.last_update.at)}` : ''} · {s.last_update.ok === null ? 'pending' : s.last_update.ok ? 'ok' : 'failed'}</span>] as [string, JSX.Element]] : []),
          ]} />
          {s.available && s.notes && (
            <details open className="rounded border border-zinc-200 p-2 dark:border-zinc-800" data-testid="update-notes">
              <summary className="cursor-pointer text-xs font-medium">Release notes v{s.latest}</summary>
              <Markdown source={s.notes} className="md mt-2 space-y-1 text-sm" />
            </details>
          )}
          {confirm && (
            <div className="rounded border border-blue-300 bg-blue-50 p-3 text-sm dark:border-blue-800 dark:bg-blue-950/40" data-testid="update-confirm">
              <p>Update avdlab v{s.current} → v{s.latest} ({s.mode} mode)? The API restarts; running jobs are refused while it updates and this page reloads when the new version answers.</p>
              <div className="mt-2 flex gap-2">
                <button className="btn-primary btn-sm" onClick={() => apply.mutate()} disabled={apply.isPending} data-testid="update-confirm-yes">{apply.isPending ? 'Starting…' : 'Update'}</button>
                <button className="btn-secondary btn-sm" onClick={() => setConfirm(false)}>Cancel</button>
              </div>
            </div>
          )}
          {apply.error && <ErrorBox error={conflictText(apply.error) ?? apply.error} />}
          {phase && (
            <div className="rounded border border-zinc-300 px-3 py-2 text-sm dark:border-zinc-700" data-testid="update-phase">
              {phase === 'waiting' && <span className="flex items-center gap-2"><span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-zinc-400 border-t-transparent" />Updating: waiting for the API to restart…</span>}
              {phase === 'down' && <span className="flex items-center gap-2"><span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-amber-500 border-t-transparent" />API is restarting…</span>}
              {phase === 'done' && <span className="text-emerald-700 dark:text-emerald-300">avdlab {newVersion ? `v${newVersion} ` : ''}is up — reloading…</span>}
              {phase === 'timeout' && <span className="text-red-700 dark:text-red-300">The new version did not answer in time. Check the update log on the host (data/update.log).</span>}
            </div>
          )}
        </>
      )}
    </section>
  )
}
