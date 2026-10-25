import { useParams, Link } from 'react-router-dom'
import { useJobStream } from '../hooks/useJobStream'
import { LogPane } from '../components/LogPane'
import { JobStatusBadge, KV, PageHeader } from '../components/ui'
import { fmtDate, duration } from '../lib/format'
import { KillJobButton } from '../components/KillJobButton'

export default function JobPage() {
  const { id = '' } = useParams()
  const { lines, job, connected } = useJobStream(id)
  return (
    <div className="flex h-[calc(100vh-2rem)] flex-col">
      <PageHeader
        title={<span className="flex items-center gap-2">{job?.type ?? 'Job'} <JobStatusBadge status={job?.status ?? 'queued'} /></span>}
        sub={<span className="mono">{id}</span>}
        actions={<KillJobButton jobId={id} status={job?.status} className="btn-danger" />}
      />
      <div className="mb-3 grid gap-4 md:grid-cols-2">
        <KV items={[
          ['Lab', job?.lab_id ? <Link className="link" to={`/labs/${job.lab_id}`}>{job.lab_id}</Link> : '—'],
          ['Host', job?.host_id ?? '—'],
          ['Created', fmtDate(job?.created_at)],
          ['Started', fmtDate(job?.started_at)],
          ['Ended', fmtDate(job?.ended_at)],
          ['Duration', duration(job?.started_at, job?.ended_at)],
          ['Stream', connected ? 'connected' : 'closed'],
        ]} />
        <div className="space-y-2 text-xs">
          {job?.error && <div className="rounded border border-red-300 bg-red-50 p-2 text-red-800 dark:border-red-800 dark:bg-red-950/40 dark:text-red-200">{job.error}</div>}
          {job?.params && Object.keys(job.params).length > 0 && <pre className="mono card p-2">params: {JSON.stringify(job.params, null, 2)}</pre>}
          {job?.result && Object.keys(job.result).length > 0 && <pre className="mono card max-h-40 overflow-auto p-2">result: {JSON.stringify(job.result, null, 2)}</pre>}
          {job?.artefacts && job.artefacts.length > 0 && (
            <div className="card p-2">
              <div className="h2 mb-1">Artefacts</div>
              <ul className="mono">{job.artefacts.map((a, i) => <li key={i}>{a.name}: {a.path}</li>)}</ul>
            </div>
          )}
        </div>
      </div>
      <LogPane lines={lines} className="min-h-0 flex-1 rounded" />
    </div>
  )
}
