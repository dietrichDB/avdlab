import { Link } from 'react-router-dom'
import type { Job } from '../api/types'
import { useJobDrawer } from './JobDrawer'
import { KillJobButton } from './KillJobButton'
import { JobStatusBadge, Empty } from './ui'
import { duration, fmtAgo } from '../lib/format'

export function JobsTable({ jobs, showTarget = true }: { jobs: Job[]; showTarget?: boolean }) {
  const drawer = useJobDrawer()
  if (!jobs.length) return <Empty>No jobs yet.</Empty>
  return (
    <div className="card overflow-x-auto">
      <table className="table">
        <thead>
          <tr>
            <th>Status</th><th>Type</th><th>ID</th>{showTarget && <th>Target</th>}<th>Created</th><th>Duration</th><th>Error</th><th></th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((j) => (
            <tr key={j.id}>
              <td><JobStatusBadge status={j.status} /></td>
              <td className="font-medium">{j.type}</td>
              <td className="mono"><Link className="link" to={`/jobs/${j.id}`}>{j.id.slice(0, 10)}</Link></td>
              {showTarget && (
                <td className="mono text-xs">
                  {j.lab_id && <Link className="link" to={`/labs/${j.lab_id}`}>lab {j.lab_id.slice(0, 8)}</Link>}
                  {j.host_id && <span>host {j.host_id.slice(0, 8)}</span>}
                </td>
              )}
              <td className="text-zinc-500" title={j.created_at}>{fmtAgo(j.created_at)}</td>
              <td>{duration(j.started_at, j.ended_at)}</td>
              <td className="max-w-xs truncate text-red-600 dark:text-red-400" title={j.error ?? ''}>{j.error}</td>
              <td className="whitespace-nowrap">
                <button className="btn-secondary btn-sm" onClick={() => drawer.openJob(j.id)}>Log</button>{' '}
                <KillJobButton jobId={j.id} status={j.status} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
