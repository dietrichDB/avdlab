import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { jobsApi } from '../api/jobs'
import type { JobStatus } from '../api/types'
import { ConfirmButton } from './ui'

/** Kill is offered only while a job can still be stopped (POST /jobs/{id}/cancel). */
export const isKillable = (status?: JobStatus | string | null) => status === 'queued' || status === 'running'

/** Two-click "Kill" (the second click confirms); refreshes the job/jobs queries so the status badge updates. */
export function KillJobButton({ jobId, status, className = 'btn-danger btn-sm' }: {
  jobId: string; status?: JobStatus | string | null; className?: string
}) {
  const qc = useQueryClient()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  if (!isKillable(status)) return null
  const kill = async () => {
    setBusy(true)
    setError(null)
    try {
      const r = await jobsApi.cancel(jobId)
      if (!r.cancelled) setError('job is no longer running')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['job', jobId] }),
        qc.invalidateQueries({ queryKey: ['jobs'] }),
      ])
    }
  }
  return (
    <span className="inline-flex items-center gap-1">
      <ConfirmButton className={className} onConfirm={kill} confirmText="Kill job?">
        {busy ? 'Killing…' : 'Kill'}
      </ConfirmButton>
      {error && <span className="text-xs text-red-600 dark:text-red-400">{error}</span>}
    </span>
  )
}
