import { useMemo, useState, type KeyboardEvent } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { labsApi } from '../../api/labs'
import type { AntaResult, AntaStatus, Lab, ValidateParams, ValidationRun, ValidationTotals } from '../../api/types'
import PsirtCard from '../../components/PsirtCard'
import { useJobDrawer } from '../../components/JobDrawer'
import { Badge, Empty, ErrorBox, Hint, Spinner } from '../../components/ui'
import { ANTA_STATUSES, countByStatus, devicesOf, filterResults, sortResults, statusColours } from '../../lib/anta'
import { cls, fmtAgo, fmtDate } from '../../lib/format'
import { isRunning } from '../../lib/labActions'
import { Term } from '../../components/learn/Term'

export const DEFAULT_SKIP_TESTS = ['VerifyNTP', 'VerifyReloadCause']

export function TotalsPills({ totals, compact = false }: { totals: Partial<ValidationTotals>; compact?: boolean }) {
  return (
    <span className="inline-flex flex-wrap gap-1" data-testid="totals">
      {ANTA_STATUSES.filter((s) => (totals[s] ?? 0) > 0 || (!compact && s !== 'unset')).map((s) => (
        <span key={s} className={cls('rounded px-1.5 py-0.5 font-mono text-[11px] font-semibold', statusColours[s], !totals[s] && 'opacity-40')} title={s}>
          {compact ? s[0].toUpperCase() : s} {totals[s] ?? 0}
        </span>
      ))}
    </span>
  )
}

function Chips({ values, onChange, placeholder }: { values: string[]; onChange: (v: string[]) => void; placeholder: string }) {
  const [draft, setDraft] = useState('')
  const add = () => {
    const parts = draft.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean)
    if (parts.length) onChange([...new Set([...values, ...parts])])
    setDraft('')
  }
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); add() }
    else if (e.key === 'Backspace' && !draft && values.length) onChange(values.slice(0, -1))
  }
  return (
    <div className="input flex min-h-[1.9rem] flex-wrap items-center gap-1 py-0.5">
      {values.map((v) => (
        <span key={v} className="inline-flex items-center gap-1 rounded bg-zinc-200 px-1.5 font-mono text-[11px] dark:bg-zinc-700" data-testid="chip">
          {v}
          <button type="button" className="text-zinc-500 hover:text-red-600" aria-label={`remove ${v}`} onClick={() => onChange(values.filter((x) => x !== v))}>×</button>
        </span>
      ))}
      <input
        className="min-w-[8rem] flex-1 bg-transparent text-xs outline-none" placeholder={placeholder}
        value={draft} onChange={(e) => setDraft(e.target.value)} onKeyDown={onKey} onBlur={add}
      />
    </div>
  )
}

/** Filterable ANTA results table (status chips + device select + text). */
export function ValidationResults({ results }: { results: AntaResult[] }) {
  const [statuses, setStatuses] = useState<AntaStatus[]>([])
  const [device, setDevice] = useState('')
  const [text, setText] = useState('')
  const counts = useMemo(() => countByStatus(results), [results])
  const devices = useMemo(() => devicesOf(results), [results])
  const rows = useMemo(() => sortResults(filterResults(results, { statuses, device, text })), [results, statuses, device, text])
  const toggle = (s: AntaStatus) => setStatuses((cur) => (cur.includes(s) ? cur.filter((x) => x !== s) : [...cur, s]))

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-2 px-1 text-xs">
        <div className="flex gap-1" role="group" aria-label="status filter">
          <button type="button" className={cls('rounded border px-1.5 py-0.5', statuses.length === 0 ? 'border-blue-500 text-blue-700 dark:text-blue-300' : 'border-zinc-300 text-zinc-500 dark:border-zinc-700')}
            onClick={() => setStatuses([])}>all {results.length}</button>
          {ANTA_STATUSES.filter((s) => counts[s] > 0).map((s) => (
            <button key={s} type="button" aria-pressed={statuses.includes(s)} data-testid={`filter-${s}`} onClick={() => toggle(s)}
              className={cls('rounded border px-1.5 py-0.5 font-mono font-semibold', statusColours[s],
                statuses.includes(s) ? 'border-current ring-1 ring-current' : 'border-transparent', statuses.length > 0 && !statuses.includes(s) && 'opacity-50')}>
              {s} {counts[s]}
            </button>
          ))}
        </div>
        <select className="input w-44 py-0.5 text-xs" aria-label="device filter" value={device} onChange={(e) => setDevice(e.target.value)}>
          <option value="">all devices ({devices.length})</option>
          {devices.map((d) => <option key={d} value={d}>{d}</option>)}
        </select>
        <input className="input w-56 py-0.5 text-xs" placeholder="filter test / message…" aria-label="text filter" value={text} onChange={(e) => setText(e.target.value)} />
        <span className="ml-auto text-zinc-500" data-testid="result-count">{rows.length} / {results.length}</span>
      </div>
      <table className="table" data-testid="results-table" data-tour="validation-table">
        <thead><tr><th>Device</th><th>Test</th><th>Category</th><th>Result</th><th>Messages</th></tr></thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={`${r.name}-${r.test}-${i}`} data-testid="result-row">
              <td className="mono whitespace-nowrap">{r.name}</td>
              <td>
                <div className="mono">{r.test}</div>
                {r.custom_field && <div className="text-[11px] text-zinc-500 dark:text-zinc-400">{r.custom_field}</div>}
              </td>
              <td className="text-xs text-zinc-500">{(r.categories ?? []).join(', ')}</td>
              <td><Badge className={statusColours[r.result] ?? statusColours.unset}>{r.result}</Badge></td>
              <td className="mono max-w-md whitespace-pre-wrap break-words text-zinc-600 dark:text-zinc-300">{(r.messages ?? []).join('\n')}</td>
            </tr>
          ))}
          {rows.length === 0 && <tr><td colSpan={5} className="py-4 text-center text-sm text-zinc-500">No results match the filters.</td></tr>}
        </tbody>
      </table>
    </div>
  )
}

function RunDetail({ lab, run }: { lab: Lab; run: ValidationRun }) {
  const [raw, setRaw] = useState(false)
  const json = useQuery({ queryKey: ['labs', lab.id, 'validations', run.id, 'json'], queryFn: () => labsApi.reportJson(lab.id, run.id), enabled: !raw, staleTime: Infinity })
  const md = useQuery({ queryKey: ['labs', lab.id, 'validations', run.id, 'md'], queryFn: () => labsApi.reportText(lab.id, run.id, 'md'), enabled: raw, staleTime: Infinity })
  return (
    <section className="card min-w-0 overflow-x-auto p-2">
      <div className="mb-2 flex flex-wrap items-center gap-2 px-1">
        <span className="h2"><Term k="ANTA">ANTA</Term> run</span>
        <span className="mono text-zinc-500">{run.id.slice(0, 8)}</span>
        <span className="text-xs text-zinc-500">{fmtDate(run.created_at)}</span>
        <span data-tour="validation-totals"><TotalsPills totals={run.totals} /></span>
        <div className="ml-auto flex items-center gap-2" data-tour="report-download">
          <label className="flex items-center gap-1 text-xs">
            <input type="checkbox" checked={raw} onChange={(e) => setRaw(e.target.checked)} /> raw Markdown
          </label>
          <a className="btn-secondary btn-sm" href={labsApi.reportUrl(lab.id, run.id, 'csv')} download={`anta_report-${run.id.slice(0, 8)}.csv`}>CSV</a>
          <a className="btn-secondary btn-sm" href={labsApi.reportUrl(lab.id, run.id, 'json')} download={`anta_report-${run.id.slice(0, 8)}.json`}>JSON</a>
        </div>
      </div>
      {raw ? (
        <>
          <ErrorBox error={md.error} />
          {md.isLoading ? <Spinner /> : <pre className="mono max-h-[65vh] overflow-auto whitespace-pre rounded bg-zinc-50 p-2 dark:bg-zinc-950" data-testid="raw-md">{md.data}</pre>}
        </>
      ) : (
        <>
          <ErrorBox error={json.error} />
          {json.isLoading ? <Spinner label="loading anta_report.json…" /> : <ValidationResults results={json.data ?? []} />}
        </>
      )}
    </section>
  )
}

export default function ValidationTab({ lab }: { lab: Lab }) {
  const drawer = useJobDrawer()
  const runs = useQuery({ queryKey: ['labs', lab.id, 'validations'], queryFn: () => labsApi.validations(lab.id) })
  const [params, setParams] = useState<ValidateParams>({ extra_fabric_validation: true, skip_tests: DEFAULT_SKIP_TESTS, user_catalogs: false, tags: [] })
  const [selected, setSelected] = useState<string | null>(null)
  const validate = useMutation({ mutationFn: () => drawer.track(labsApi.validate(lab.id, params)) })
  const sorted = useMemo(() => [...(runs.data ?? [])].sort((a, b) => b.created_at.localeCompare(a.created_at)), [runs.data])
  const current = sorted.find((r) => r.id === selected) ?? sorted[0] ?? null
  const canValidate = isRunning(lab.state)

  return (
    <>
    <Hint title="Validation runs ANTA.">Once the lab is deployed, press <b>Validate</b>: ANTA checks BGP, MLAG, interfaces and more on every switch and lists what passed or failed.</Hint>
    <div className="grid gap-4 lg:grid-cols-[20rem_1fr]">
      <div className="space-y-4">
        <section className="card space-y-2 p-3">
          <div className="h2">Run ANTA</div>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={params.extra_fabric_validation} onChange={(e) => setParams({ ...params, extra_fabric_validation: e.target.checked })} />
            extra fabric validation
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={params.user_catalogs} onChange={(e) => setParams({ ...params, user_catalogs: e.target.checked })} />
            user catalogs <span className="mono text-zinc-500">anta/user_catalogs</span>
          </label>
          <div>
            <span className="label">skip tests</span>
            <Chips values={params.skip_tests} onChange={(v) => setParams({ ...params, skip_tests: v })} placeholder="VerifyXxx + Enter" />
          </div>
          <div>
            <span className="label">tags</span>
            <Chips values={params.tags} onChange={(v) => setParams({ ...params, tags: v })} placeholder="e.g. leaf, spine" />
          </div>
          <ErrorBox error={validate.error} />
          <button className="btn-primary w-full justify-center" disabled={!canValidate || validate.isPending} onClick={() => validate.mutate()}
            title={canValidate ? undefined : 'deploy the lab first'}>
            Validate
          </button>
          {!canValidate && <div className="text-xs text-zinc-500">Lab is {lab.state} — deploy it to run ANTA.</div>}
        </section>

        <section className="card p-2">
          <div className="h2 mb-1 px-1">Runs</div>
          <ErrorBox error={runs.error} />
          {runs.isLoading ? <Spinner /> : sorted.length === 0 ? <div className="px-1 text-sm text-zinc-500">No validation runs yet.</div> : (
            <ul className="space-y-0.5" data-testid="runs">
              {sorted.map((r) => (
                <li key={r.id}>
                  <button type="button" onClick={() => setSelected(r.id)}
                    className={cls('flex w-full flex-col gap-0.5 rounded px-1.5 py-1 text-left', r.id === current?.id ? 'bg-blue-50 dark:bg-blue-900/30' : 'hover:bg-zinc-50 dark:hover:bg-zinc-800/50')}>
                    <span className="flex items-center gap-2 text-xs">
                      <span className="mono">{r.id.slice(0, 8)}</span>
                      <span className="text-zinc-500">{fmtAgo(r.created_at)}</span>
                      {r.job_id && <span role="link" className="link ml-auto mono" onClick={(e) => { e.stopPropagation(); drawer.openJob(r.job_id!) }}>job</span>}
                    </span>
                    <TotalsPills totals={r.totals} compact />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
      {current ? <RunDetail key={current.id} lab={lab} run={current} /> : <Empty>Run a validation to see ANTA results here.</Empty>}
    </div>
    {lab.target !== 'act' && <div className="mt-4"><PsirtCard lab={lab} /></div>}
    </>
  )
}
