import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { feedbackApi } from '../api/phase5'
import type { Feedback, FeedbackStatus } from '../api/types'
import { useFeedback } from '../components/Feedback'
import { Badge, ConfirmButton, Empty, ErrorBox, PageHeader, Spinner } from '../components/ui'
import { fmtAgo } from '../lib/format'

const KIND_TONE: Record<string, string> = {
  bug: 'bg-red-100 text-red-800 dark:bg-red-900/60 dark:text-red-200',
  suggestion: 'bg-blue-100 text-blue-800 dark:bg-blue-900/60 dark:text-blue-200',
  question: 'bg-violet-100 text-violet-800 dark:bg-violet-900/60 dark:text-violet-200',
}
const STATUSES: FeedbackStatus[] = ['new', 'triaged', 'done']

export default function FeedbackAdminPage() {
  const qc = useQueryClient()
  const fb = useFeedback()
  const list = useQuery({ queryKey: ['feedback'], queryFn: feedbackApi.list })
  const [status, setStatus] = useState<FeedbackStatus | 'all'>('all')
  const [open, setOpen] = useState<string | number | null>(null)
  const patch = useMutation({
    mutationFn: ({ id, s }: { id: Feedback['id']; s: FeedbackStatus }) => feedbackApi.setStatus(id, s),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['feedback'] }),
  })
  const remove = useMutation({ mutationFn: (id: Feedback['id']) => feedbackApi.remove(id), onSuccess: () => qc.invalidateQueries({ queryKey: ['feedback'] }) })
  const rows = (list.data ?? []).filter((f) => status === 'all' || f.status === status)
  const count = (s: FeedbackStatus) => (list.data ?? []).filter((f) => f.status === s).length

  return (
    <div>
      <PageHeader title="Feedback" sub="bug reports, suggestions and questions sent from the app"
        actions={<button className="btn-primary" onClick={fb.open}>New feedback</button>} />
      <div className="mb-3 flex gap-1 text-xs">
        {(['all', ...STATUSES] as const).map((s) => (
          <button key={s} className={s === status ? 'btn-primary btn-sm' : 'btn-secondary btn-sm'} onClick={() => setStatus(s)}>
            {s}{s !== 'all' ? ` (${count(s)})` : ''}
          </button>
        ))}
      </div>
      <ErrorBox error={list.error ?? patch.error ?? remove.error} />
      {list.isLoading ? <Spinner /> : !rows.length ? <Empty>No feedback{status !== 'all' ? ` with status ${status}` : ''}.</Empty> : (
        <div className="card overflow-x-auto">
          <table className="table" data-testid="feedback-table">
            <thead><tr><th>Kind</th><th>Title</th><th>Page</th><th>Received</th><th>Status</th><th>GitHub</th><th /></tr></thead>
            <tbody>
              {rows.map((f) => (
                <FeedbackRow key={f.id} f={f} expanded={open === f.id} onToggle={() => setOpen(open === f.id ? null : f.id)}
                  onStatus={(s) => patch.mutate({ id: f.id, s })} onDelete={() => remove.mutate(f.id)} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function FeedbackRow({ f, expanded, onToggle, onStatus, onDelete }: {
  f: Feedback; expanded: boolean; onToggle: () => void; onStatus: (s: FeedbackStatus) => void; onDelete: () => void
}) {
  return (
    <>
      <tr data-testid={`feedback-row-${f.id}`}>
        <td><Badge className={KIND_TONE[f.kind] ?? KIND_TONE.question}>{f.kind}</Badge></td>
        <td><button className="text-left font-medium hover:underline" onClick={onToggle}>{f.title}</button></td>
        <td className="mono text-zinc-500">{f.page || '—'}</td>
        <td className="whitespace-nowrap text-xs text-zinc-500" title={f.created_at}>{fmtAgo(f.created_at)}</td>
        <td>
          <select className="input w-24 py-0 text-xs" value={f.status} onChange={(e) => onStatus(e.target.value as FeedbackStatus)} data-testid={`feedback-status-${f.id}`}>
            {STATUSES.map((s) => <option key={s}>{s}</option>)}
          </select>
        </td>
        <td>{f.github_issue_url ? <a className="link text-xs" href={f.github_issue_url} target="_blank" rel="noreferrer">issue ↗</a> : <span className="text-xs text-zinc-400">—</span>}</td>
        <td><ConfirmButton className="btn-secondary btn-sm" onConfirm={onDelete} confirmText="Delete?">Delete</ConfirmButton></td>
      </tr>
      {expanded && (
        <tr><td colSpan={7}><pre className="whitespace-pre-wrap font-sans text-sm">{f.body}</pre></td></tr>
      )}
    </>
  )
}
