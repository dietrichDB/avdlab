import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { isTerminal } from '../api/jobs'
import type { JobRef } from '../api/types'
import { useJobStream } from '../hooks/useJobStream'
import { KillJobButton } from './KillJobButton'
import { LogPane } from './LogPane'
import { JobStatusBadge } from './ui'
import { duration, fmtDate } from '../lib/format'

interface JobDrawerState {
  jobId: string | null
  open: boolean
  openJob: (id: string) => void
  /** convenience: pass a promise returning {job_id} and the drawer opens when it resolves */
  track: (p: Promise<JobRef>) => Promise<JobRef>
  close: () => void
}

const Ctx = createContext<JobDrawerState | null>(null)

export function JobDrawerProvider({ children }: { children: ReactNode }) {
  const [jobId, setJobId] = useState<string | null>(null)
  const [open, setOpen] = useState(false)
  const [recent, setRecent] = useState<string[]>([])

  const openJob = useCallback((id: string) => {
    setJobId(id)
    setOpen(true)
    setRecent((r) => [id, ...r.filter((x) => x !== id)].slice(0, 8))
  }, [])
  const track = useCallback(async (p: Promise<JobRef>) => {
    const ref = await p
    openJob(ref.job_id)
    return ref
  }, [openJob])
  const close = useCallback(() => setOpen(false), [])

  const value = useMemo(() => ({ jobId, open, openJob, track, close }), [jobId, open, openJob, track, close])
  return (
    <Ctx.Provider value={value}>
      {children}
      {open && jobId && <JobDrawer jobId={jobId} recent={recent} onSelect={openJob} onClose={close} />}
    </Ctx.Provider>
  )
}

export function useJobDrawer(): JobDrawerState {
  const c = useContext(Ctx)
  if (!c) throw new Error('useJobDrawer outside provider')
  return c
}

export function JobDrawer({ jobId, recent = [], onSelect, onClose }: {
  jobId: string; recent?: string[]; onSelect?: (id: string) => void; onClose: () => void
}) {
  const { lines, done, connected, job } = useJobStream(jobId)
  const qc = useQueryClient()
  const status = job?.status ?? (done ? 'succeeded' : 'running')
  const terminal = isTerminal(job?.status)

  // once a job finishes, anything it touched (hosts, labs, images…) may have changed
  useEffect(() => {
    if (terminal) qc.invalidateQueries({ predicate: (q) => q.queryKey[0] !== 'job' })
  }, [terminal, qc])

  return (
    <aside
      data-testid="job-drawer"
      className="fixed inset-y-0 right-0 z-30 flex w-full max-w-2xl flex-col border-l border-zinc-200 bg-white shadow-2xl dark:border-zinc-800 dark:bg-zinc-900"
    >
      <header className="flex items-center gap-2 border-b border-zinc-200 px-3 py-2 dark:border-zinc-800">
        <JobStatusBadge status={status} />
        <div className="min-w-0 flex-1 truncate">
          <span className="font-semibold">{job?.type ?? 'job'}</span>
          <span className="ml-2 mono text-zinc-500">{jobId}</span>
        </div>
        <span className="text-xs text-zinc-500">
          {job?.started_at ? duration(job.started_at, job.ended_at) : job?.created_at ? `queued ${fmtDate(job.created_at)}` : ''}
        </span>
        <span title={connected ? 'stream connected' : 'stream closed'} className={connected ? 'text-emerald-500' : 'text-zinc-400'}>●</span>
        <KillJobButton jobId={jobId} status={job?.status} />
        <Link className="btn-secondary btn-sm" to={`/jobs/${jobId}`} onClick={onClose}>Open</Link>
        <button className="btn-secondary btn-sm" onClick={onClose} aria-label="Close drawer">Close</button>
      </header>
      {recent.length > 1 && (
        <div className="flex gap-1 overflow-x-auto border-b border-zinc-200 px-3 py-1 text-xs dark:border-zinc-800">
          {recent.map((id) => (
            <button
              key={id}
              onClick={() => onSelect?.(id)}
              className={`mono rounded px-1.5 py-0.5 ${id === jobId ? 'bg-blue-100 dark:bg-blue-900/50' : 'hover:bg-zinc-100 dark:hover:bg-zinc-800'}`}
            >
              {id.slice(0, 8)}
            </button>
          ))}
        </div>
      )}
      <LogPane lines={lines} className="min-h-0 flex-1" />
      {(job?.error || (terminal && job?.result && Object.keys(job.result).length > 0)) && (
        <footer className="max-h-48 overflow-auto border-t border-zinc-200 px-3 py-2 text-xs dark:border-zinc-800">
          {job?.error && <div className="mb-1 text-red-600 dark:text-red-400">{job.error}</div>}
          {job?.result && Object.keys(job.result).length > 0 && (
            <pre className="mono whitespace-pre-wrap text-zinc-600 dark:text-zinc-300">{JSON.stringify(job.result, null, 2)}</pre>
          )}
        </footer>
      )}
    </aside>
  )
}
