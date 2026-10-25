import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { labsApi } from '../api/labs'
import type { Lab, PsirtResult } from '../api/types'
import { useJobDrawer } from './JobDrawer'
import { ErrorBox, Hint } from './ui'
import { Term } from './learn/Term'
import { cls, fmtAgo } from '../lib/format'

const RESULTS: PsirtResult[] = ['affected', 'inconclusive', 'mitigated', 'not affected', 'error', 'skipped']
const TONE: Record<string, string> = {
  affected: 'bg-red-100 text-red-800 dark:bg-red-900/60 dark:text-red-200',
  inconclusive: 'bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-200',
  mitigated: 'bg-sky-100 text-sky-800 dark:bg-sky-900/50 dark:text-sky-200',
  'not affected': 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/60 dark:text-emerald-200',
  error: 'bg-zinc-200 text-zinc-800 dark:bg-zinc-700 dark:text-zinc-200',
  skipped: 'bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300',
}
const SEV: Record<string, string> = { critical: 'text-red-700 dark:text-red-300', high: 'text-orange-700 dark:text-orange-300', medium: 'text-amber-700 dark:text-amber-300', low: 'text-zinc-600', none: 'text-zinc-500', unknown: 'text-zinc-500' }
const ICON: Record<string, string> = { affected: '🛑', inconclusive: '❓', mitigated: '🛡️', 'not affected': '✅', error: '❗', skipped: '⏭️' }

/** Validation tab: run `anta psirt` (ANTA 1.10 preview) against the running lab and show the advisory assessment. */
export default function PsirtCard({ lab }: { lab: Lab }) {
  const drawer = useJobDrawer()
  const qc = useQueryClient()
  const st = useQuery({ queryKey: ['labs', lab.id, 'psirt'], queryFn: () => labsApi.psirt(lab.id) })
  const [tests, setTests] = useState('')
  const [open, setOpen] = useState<string | null>(null)
  const run = useMutation({
    mutationFn: () => drawer.track(labsApi.runPsirt(lab.id, tests.split(/[\s,]+/).map((t) => t.trim().toUpperCase()).filter(Boolean))),
    onSuccess: () => setTimeout(() => qc.invalidateQueries({ queryKey: ['labs', lab.id, 'psirt'] }), 3000),
  })
  const last = st.data?.last ?? null
  const available = !!st.data?.available
  return (
    <section className="card space-y-3 p-3" data-testid="psirt-card">
      <div className="flex flex-wrap items-center gap-2">
        <span className="h2 mr-auto">Security advisories <span className="text-xs font-normal text-cv-muted">(<Term k="ANTA">ANTA</Term> PSIRT · preview)</span></span>
        {last && <span className="text-xs text-cv-muted" title={new Date(last.at * 1000).toLocaleString()}>last run {fmtAgo(new Date(last.at * 1000).toISOString())} · {last.devices.length} devices · {last.duration}s</span>}
      </div>
      <Hint className="mb-0" title="What this shows.">ANTA ships one test per published Arista security advisory (SA). <code>anta psirt</code> checks every running switch against all of them and reports, per device and advisory, whether it is <b>affected</b>, <b>mitigated</b> by a configured control, <b>not affected</b> (fixed release, feature not used) or <b>inconclusive</b>, with the remediation plan. cEOS-lab reports its EOS version like hardware, so the lab is a faithful demo.</Hint>
      <div className="flex flex-wrap items-end gap-2">
        <label className="block min-w-[14rem] flex-1 text-sm">
          <span className="label">Advisories <span className="font-normal text-cv-muted">(optional: e.g. <span className="mono">SA117 SA140</span>; empty = all built-in)</span></span>
          <input className="input mono" value={tests} onChange={(e) => setTests(e.target.value)} placeholder="all advisories" data-testid="psirt-tests" />
        </label>
        <button className="btn-primary" disabled={!available || run.isPending} onClick={() => run.mutate()} data-testid="psirt-run" title={available ? 'runs anta psirt in the agent container' : 'deploy the lab first'}>
          {run.isPending ? 'Starting…' : 'Run advisory scan'}
        </button>
        {last?.has_md && <a className="btn-secondary" href={`/api/labs/${lab.id}/psirt/report.md`} target="_blank" rel="noreferrer" data-testid="psirt-md">Markdown report ↗</a>}
      </div>
      <ErrorBox error={run.error ?? st.error} />
      {!available && <div className="text-xs text-cv-muted">Deploy the lab to scan it.</div>}
      {last && (
        <>
          <div className="flex flex-wrap gap-1" data-testid="psirt-summary">
            {RESULTS.map((r) => (
              <span key={r} className={cls('rounded px-2 py-0.5 text-xs font-semibold', TONE[r], !last.summary[r] && 'opacity-40')}>{ICON[r]} {r} {last.summary[r] ?? 0}</span>
            ))}
            <span className="ml-auto text-xs text-cv-muted">device × advisory</span>
          </div>
          <div className="overflow-x-auto">
            <table className="table" data-testid="psirt-table">
              <thead><tr><th>Advisory</th><th>Severity</th><th>Device</th><th>Result</th><th></th></tr></thead>
              <tbody>
                {last.assessments.map((a) => {
                  const key = `${a.device}|${a.advisory_id || a.test}`
                  const rows = last.rows.filter((r) => r.device === a.device && (r.advisory_id || r.test) === (a.advisory_id || a.test))
                  return [
                    <tr key={key} data-testid="psirt-row" data-result={a.result}>
                      <td><a className="link mono" href={a.url} target="_blank" rel="noreferrer">{a.advisory_id || a.test}</a><div className="text-xs text-cv-muted">{a.title}</div></td>
                      <td className={cls('font-medium', SEV[a.severity] ?? '')}>{a.severity}</td>
                      <td className="mono">{a.device}</td>
                      <td><span className={cls('rounded px-1.5 py-0.5 text-xs font-semibold', TONE[a.result] ?? TONE.error)}>{ICON[a.result] ?? ''} {a.result}</span></td>
                      <td>{rows.some((r) => r.vulnerability_messages || r.remediation) && <button type="button" className="link text-xs" onClick={() => setOpen(open === key ? null : key)}>{open === key ? 'hide' : 'details'}</button>}</td>
                    </tr>,
                    open === key ? (
                      <tr key={key + '-d'} data-testid="psirt-detail"><td colSpan={5} className="bg-cv-surface-2 text-xs">
                        {rows.map((r, i) => (
                          <div key={i} className="mb-2">
                            {r.vulnerability_id && <div className="font-semibold">{r.vulnerability_id} <span className={cls('font-normal', SEV[r.vulnerability_severity] ?? '')}>{r.vulnerability_severity}</span> — <span className={cls('rounded px-1 font-semibold', TONE[r.vulnerability_result] ?? '')}>{r.vulnerability_result}</span></div>}
                            {r.vulnerability_messages && <pre className="whitespace-pre-wrap font-mono">{r.vulnerability_messages}</pre>}
                            {r.remediation && <div><span className="font-semibold">Remediation:</span> <span className="whitespace-pre-wrap">{r.remediation}</span></div>}
                          </div>
                        ))}
                      </td></tr>
                    ) : null,
                  ]
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  )
}
