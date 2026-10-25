import { useEffect, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { labsApi } from '../../api/labs'
import { jobsApi } from '../../api/jobs'
import type { BuildResult, Lab } from '../../api/types'
import PushConfigsCard from '../../components/PushConfigsCard'
import { useJobDrawer } from '../../components/JobDrawer'
import { DiffViewer } from '../../components/DiffViewer'
import { ErrorBox, Hint, JobStatusBadge, KV, Spinner } from '../../components/ui'
import { duration, fmtAgo } from '../../lib/format'
import { Term } from '../../components/learn/Term'

export default function BuildTab({ lab }: { lab: Lab }) {
  const drawer = useJobDrawer()
  const builds = useQuery({
    queryKey: ['jobs', { lab_id: lab.id, type: 'lab.build' }],
    queryFn: async () => (await jobsApi.list({ lab_id: lab.id, limit: 50 })).filter((j) => j.type === 'lab.build'),
  })
  const last = lab.last_build_job ?? builds.data?.[0] ?? null
  const result = (last?.result ?? {}) as BuildResult
  const history = useQuery({ queryKey: ['labs', lab.id, 'history'], queryFn: () => labsApi.history(lab.id) })
  const [a, setA] = useState('')
  const [b, setB] = useState('')
  useEffect(() => {
    const h = history.data
    if (h && h.length && !b) { setB(h[0].sha); setA(h[1]?.sha ?? h[0].sha) }
  }, [history.data, b])
  const diff = useQuery({
    queryKey: ['labs', lab.id, 'diff', a, b],
    queryFn: async () => {
      const r = await labsApi.diff(lab.id, a, b)
      return typeof r === 'string' ? r : r.diff
    },
    enabled: !!a && !!b && a !== b,
  })
  const build = useMutation({ mutationFn: () => drawer.track(labsApi.build(lab.id)) })

  return (
    <div className="space-y-4">
      <Hint className="mb-0" title="Build runs AVD.">It turns the lab's YAML inventory into EOS configs and documentation — press <b>Build</b>, then review the generated files before you deploy.</Hint>
      {lab.target !== 'act' && <PushConfigsCard lab={lab} compact />}
      <div className="grid gap-4 lg:grid-cols-[22rem_1fr]">
        <section className="card p-3">
          <div className="mb-2 flex items-center justify-between">
            <span className="h2">Last build</span>
            <span className="text-[11px] text-zinc-500"><Term k="eos_designs">eos_designs</Term> → <Term k="structured config">structured config</Term> → <Term k="eos_cli_config_gen">eos_cli_config_gen</Term></span>
            <button className="btn-primary btn-sm" onClick={() => build.mutate()} disabled={build.isPending}>Build now</button>
          </div>
          <ErrorBox error={build.error} />
          {!last ? <div className="text-sm text-zinc-500">No builds yet.</div> : (
            <>
              <KV items={[
                ['Status', <JobStatusBadge status={last.status} />],
                ['Job', <button className="link mono" onClick={() => drawer.openJob(last.id)}>{last.id.slice(0, 10)}</button>],
                ['When', fmtAgo(last.created_at)],
                ['Duration', duration(last.started_at, last.ended_at)],
                ['Hosts', result.hosts?.length ?? '—'],
                ['ANTA tests', result.anta_tests ? Object.values(result.anta_tests).reduce((x, y) => x + y, 0) : '—'],
              ]} />
              {last.error && <pre className="mono mt-2 whitespace-pre-wrap rounded bg-red-50 p-2 text-red-800 dark:bg-red-950/40 dark:text-red-200">{last.error}</pre>}
              {!!result.warnings?.length && (
                <div className="mt-2">
                  <div className="h2 mb-1">Warnings</div>
                  <ul className="list-disc pl-4 text-xs text-amber-700 dark:text-amber-300">{result.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>
                </div>
              )}
              <div className="mt-2">
                <div className="h2 mb-1">Changed files ({result.changed_files?.length ?? 0})</div>
                <ul className="mono max-h-64 overflow-auto" data-testid="changed-files">
                  {(result.changed_files ?? []).map((f) => <li key={f}><Link className="link" to={`/labs/${lab.id}/files`}>{f}</Link></li>)}
                </ul>
              </div>
            </>
          )}
          {builds.data && builds.data.length > 1 && (
            <div className="mt-3">
              <div className="h2 mb-1">Previous builds</div>
              <ul className="space-y-0.5 text-xs">
                {builds.data.slice(0, 10).map((j) => (
                  <li key={j.id} className="flex items-center gap-2">
                    <JobStatusBadge status={j.status} />
                    <button className="link mono" onClick={() => drawer.openJob(j.id)}>{j.id.slice(0, 8)}</button>
                    <span className="text-zinc-500">{fmtAgo(j.created_at)}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </section>
        <section className="card flex min-w-0 flex-col p-3">
          <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
            <span className="h2 mr-2">Diff</span>
            <RevSelect label="from" value={a} onChange={setA} revs={history.data ?? []} />
            <RevSelect label="to" value={b} onChange={setB} revs={history.data ?? []} />
            <button className="btn-secondary btn-sm" onClick={() => { const t = a; setA(b); setB(t) }}>swap</button>
          </div>
          <ErrorBox error={history.error ?? diff.error} />
          {history.isLoading || diff.isLoading ? <Spinner /> : a === b ? <div className="text-sm text-zinc-500">Pick two different revisions.</div> : <DiffViewer diff={diff.data ?? ''} className="max-h-[65vh]" />}
        </section>
      </div>
      <section className="card overflow-x-auto">
        <div className="h2 px-3 pt-2">History</div>
        <table className="table">
          <thead><tr><th>Rev</th><th>Message</th><th>Author</th><th>Date</th><th>Files</th><th></th></tr></thead>
          <tbody>
            {(history.data ?? []).map((r, i, all) => (
              <tr key={r.sha}>
                <td className="mono">{r.short}</td>
                <td>{r.message}</td>
                <td className="text-xs">{r.author}</td>
                <td className="text-zinc-500">{fmtAgo(r.date)}</td>
                <td className="text-xs text-zinc-500">{r.files?.length ?? ''}</td>
                <td>{all[i + 1] && <button className="btn-secondary btn-sm" onClick={() => { setA(all[i + 1].sha); setB(r.sha) }}>diff</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  )
}

function RevSelect({ label, value, onChange, revs }: { label: string; value: string; onChange: (v: string) => void; revs: { sha: string; short: string; message: string }[] }) {
  return (
    <label className="flex items-center gap-1">
      <span className="text-zinc-500">{label}</span>
      <select className="input w-64 py-0.5 text-xs" value={value} onChange={(e) => onChange(e.target.value)}>
        {revs.map((r) => <option key={r.sha} value={r.sha}>{r.short} {r.message.slice(0, 40)}</option>)}
      </select>
    </label>
  )
}
