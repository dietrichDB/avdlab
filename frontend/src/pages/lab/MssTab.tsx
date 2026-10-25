import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { labsApi, labKnobValues } from '../../api/labs'
import { mssApi } from '../../api/phase4'
import { trafficApi } from '../../api/traffic'
import { isTerminal } from '../../api/jobs'
import { ApiError } from '../../api/client'
import type {
  Lab, MssAction, MssApplication, MssCase, MssCaseResult, MssPolicy, MssProtocol, MssSegment, MssSummary, MssVerifyResult, MssVrf, Segmentation,
} from '../../api/types'
import { useJobDrawer } from '../../components/JobDrawer'
import { Badge, ErrorBox, Field, JobStatusBadge, Spinner } from '../../components/ui'
import { ListInput } from '../../components/ListInput'
import { YamlEditor } from '../../components/YamlEditor'
import { useJobResult } from '../../hooks/useJobResult'
import { cls, fmtAgo, fmtDate } from '../../lib/format'
import { isRunning } from '../../lib/labActions'
import {
  BUILTIN_APP, BUILTIN_POLICIES, FORWARDING_SEGMENTS, PROTOCOLS, VERDICT_CLASS, normalizeSegmentation, policyMatrix, policyVerdict, segmentationIssues,
  segmentationKnob,
} from '../../lib/mss'
import { parseYaml, toYaml } from '../../lib/yaml'

type Matrix = MssSummary['matrix']

const rm = <T,>(xs: T[], i: number) => xs.filter((_, j) => j !== i)
const put = <T,>(xs: T[], i: number, v: T) => xs.map((x, j) => (j === i ? v : x))

export function FoundationBadge() {
  return <Badge className="bg-amber-100 text-amber-800 dark:bg-amber-900/60 dark:text-amber-200">foundation — MSS design to be refined</Badge>
}

// ---------------------------------------------------------------- matrix
export function MssMatrix({ matrix, policies }: { matrix: Matrix; policies: MssPolicy[] }) {
  const vrfs = Object.keys(matrix)
  if (!vrfs.length) return <div className="p-3 text-center text-sm text-zinc-500">No VRFs with segments.</div>
  return (
    <div className="space-y-4" data-testid="mss-matrix">
      {vrfs.map((vrf) => {
        const segs = Object.keys(matrix[vrf])
        return (
          <div key={vrf} className="overflow-x-auto" data-testid={`mss-matrix-${vrf}`}>
            <div className="mb-1 text-sm font-medium">VRF <span className="mono">{vrf}</span></div>
            {!segs.length ? <div className="text-xs text-zinc-500">no segments</div> : (
              <table className="table w-auto">
                <thead>
                  <tr><th className="text-xs text-zinc-500">src ↓ / dst →</th>{segs.map((d) => <th key={d} className="mono">{d}</th>)}</tr>
                </thead>
                <tbody>
                  {segs.map((src) => (
                    <tr key={src}>
                      <th className="mono text-left">{src}</th>
                      {segs.map((dst) => {
                        const p = matrix[vrf][src]?.[dst] ?? null
                        const v = policyVerdict({ policies }, p)
                        return (
                          <td key={dst} data-testid={`mss-cell-${vrf}-${src}-${dst}`} data-verdict={v ?? 'none'}
                            className={cls('mono whitespace-nowrap text-center text-xs', VERDICT_CLASS[v ?? 'none'])} title={v ? `${p}: ${v}` : 'unspecified'}>
                            {p ?? '—'}
                          </td>
                        )
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )
      })}
      <div className="flex flex-wrap gap-2 text-[11px]">
        {(['forward', 'drop', 'mixed', 'none'] as const).map((k) => <span key={k} className={cls('rounded px-1.5 py-0.5', VERDICT_CLASS[k])}>{k === 'none' ? 'unspecified' : k}</span>)}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------- editors
function ApplicationsEditor({ apps, onChange }: { apps: MssApplication[]; onChange: (a: MssApplication[]) => void }) {
  return (
    <section className="card p-3" data-testid="mss-apps">
      <div className="mb-2 flex items-center"><span className="h2 mr-auto">Applications</span>
        <button type="button" className="btn-secondary btn-sm" onClick={() => onChange([...apps, { name: '', protocol: 'tcp', ports: null }])}>Add application</button></div>
      {!apps.length ? <div className="text-xs text-zinc-500">None. <span className="mono">{BUILTIN_APP}</span> is always available.</div> : (
        <table className="table">
          <thead><tr><th>Name</th><th>Protocol</th><th>Ports</th><th /></tr></thead>
          <tbody>
            {apps.map((a, i) => (
              <tr key={i}>
                <td><input className="input mono" aria-label={`application ${i + 1} name`} value={a.name} onChange={(e) => onChange(put(apps, i, { ...a, name: e.target.value }))} /></td>
                <td>
                  <select className="input" aria-label={`application ${i + 1} protocol`} value={a.protocol} onChange={(e) => onChange(put(apps, i, { ...a, protocol: e.target.value as MssProtocol }))}>
                    {PROTOCOLS.map((p) => <option key={p} value={p}>{p}</option>)}
                  </select>
                </td>
                <td><input className="input mono" aria-label={`application ${i + 1} ports`} value={a.ports ?? ''} placeholder={a.protocol === 'tcp' || a.protocol === 'udp' ? '80,443' : '—'}
                  disabled={a.protocol === 'icmp' || a.protocol === 'any'} onChange={(e) => onChange(put(apps, i, { ...a, ports: e.target.value || null }))} /></td>
                <td><button type="button" className="btn-secondary btn-sm" aria-label={`remove application ${a.name || i + 1}`} onClick={() => onChange(rm(apps, i))}>×</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}

function PoliciesEditor({ policies, apps, onChange }: { policies: MssPolicy[]; apps: string[]; onChange: (p: MssPolicy[]) => void }) {
  const appChoices = [...apps, BUILTIN_APP]
  return (
    <section className="card space-y-2 p-3" data-testid="mss-policies">
      <div className="flex items-center"><span className="h2 mr-auto">Policies</span>
        <button type="button" className="btn-secondary btn-sm" onClick={() => onChange([...policies, { name: '', rules: [] }])}>Add policy</button></div>
      <div className="text-[11px] text-zinc-500">Rules are evaluated in order; traffic matching no rule is dropped. Built-ins: {Object.keys(BUILTIN_POLICIES).map((p) => <span key={p} className="mono mr-1">{p}</span>)}</div>
      {policies.map((p, i) => (
        <div key={i} className="rounded border border-zinc-200 p-2 dark:border-zinc-800" data-testid="mss-policy">
          <div className="mb-1 flex gap-1">
            <input className="input mono" aria-label={`policy ${i + 1} name`} value={p.name} placeholder="policy name" onChange={(e) => onChange(put(policies, i, { ...p, name: e.target.value }))} />
            <button type="button" className="btn-secondary btn-sm" onClick={() => onChange(put(policies, i, { ...p, rules: [...p.rules, { application: appChoices[0], action: 'forward', log: false }] }))}>Add rule</button>
            <button type="button" className="btn-secondary btn-sm" aria-label={`remove policy ${p.name || i + 1}`} onClick={() => onChange(rm(policies, i))}>×</button>
          </div>
          {p.rules.length > 0 && (
            <table className="table">
              <thead><tr><th>#</th><th>Application</th><th>Action</th><th>Log</th><th /></tr></thead>
              <tbody>
                {p.rules.map((r, j) => (
                  <tr key={j}>
                    <td className="text-zinc-500">{(j + 1) * 10}</td>
                    <td>
                      <select className="input mono" aria-label={`policy ${p.name} rule ${j + 1} application`} value={r.application}
                        onChange={(e) => onChange(put(policies, i, { ...p, rules: put(p.rules, j, { ...r, application: e.target.value }) }))}>
                        {!appChoices.includes(r.application) && <option value={r.application}>{r.application || '—'}</option>}
                        {appChoices.map((a) => <option key={a} value={a}>{a}</option>)}
                      </select>
                    </td>
                    <td>
                      <select className="input" aria-label={`policy ${p.name} rule ${j + 1} action`} value={r.action}
                        onChange={(e) => onChange(put(policies, i, { ...p, rules: put(p.rules, j, { ...r, action: e.target.value as MssAction }) }))}>
                        <option value="forward">forward</option><option value="drop">drop</option>
                      </select>
                    </td>
                    <td><input type="checkbox" aria-label={`policy ${p.name} rule ${j + 1} log`} checked={r.log} onChange={(e) => onChange(put(policies, i, { ...p, rules: put(p.rules, j, { ...r, log: e.target.checked }) }))} /></td>
                    <td><button type="button" className="btn-secondary btn-sm" aria-label={`remove rule ${j + 1}`} onClick={() => onChange(put(policies, i, { ...p, rules: rm(p.rules, j) }))}>×</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      ))}
    </section>
  )
}

function SegmentEditor({ seg, others, policies, onChange, onRemove }: {
  seg: MssSegment; others: string[]; policies: string[]; onChange: (s: MssSegment) => void; onRemove: () => void
}) {
  const fromChoices = [...others, FORWARDING_SEGMENTS]
  const polChoices = [...policies, ...Object.keys(BUILTIN_POLICIES)]
  return (
    <div className="rounded border border-zinc-200 p-2 dark:border-zinc-800" data-testid="mss-segment">
      <div className="grid gap-2 sm:grid-cols-[10rem_1fr_1fr_8rem_auto]">
        <Field label="Segment"><input className="input mono" aria-label="segment name" value={seg.name} onChange={(e) => onChange({ ...seg, name: e.target.value })} /></Field>
        <Field label="Prefixes"><ListInput aria-label={`segment ${seg.name} prefixes`} value={seg.prefixes} placeholder="10.10.11.0/24" onChange={(prefixes) => onChange({ ...seg, prefixes })} /></Field>
        <Field label="Interfaces"><ListInput aria-label={`segment ${seg.name} interfaces`} value={seg.interfaces} placeholder="Vlan11" onChange={(interfaces) => onChange({ ...seg, interfaces })} /></Field>
        <Field label="Fallback">
          <select className="input" aria-label={`segment ${seg.name} fallback`} value={seg.fallback ?? ''} onChange={(e) => onChange({ ...seg, fallback: (e.target.value || null) as MssAction | null })}>
            <option value="">—</option><option value="forward">forward</option><option value="drop">drop</option>
          </select>
        </Field>
        <button type="button" className="btn-secondary btn-sm self-end" aria-label={`remove segment ${seg.name}`} onClick={onRemove}>×</button>
      </div>
      <div className="mt-2">
        <span className="label">Policies (from → policy)</span>
        <div className="space-y-1">
          {seg.policies.map((p, i) => (
            <div key={i} className="flex items-center gap-1">
              <select className="input mono" aria-label={`segment ${seg.name} policy ${i + 1} from`} value={p.from} onChange={(e) => onChange({ ...seg, policies: put(seg.policies, i, { ...p, from: e.target.value }) })}>
                {!fromChoices.includes(p.from) && <option value={p.from}>{p.from || '—'}</option>}
                {fromChoices.map((f) => <option key={f} value={f}>{f}</option>)}
              </select>
              <span className="text-zinc-400">→</span>
              <select className="input mono" aria-label={`segment ${seg.name} policy ${i + 1} policy`} value={p.policy} onChange={(e) => onChange({ ...seg, policies: put(seg.policies, i, { ...p, policy: e.target.value }) })}>
                {!polChoices.includes(p.policy) && <option value={p.policy}>{p.policy || '—'}</option>}
                {polChoices.map((f) => <option key={f} value={f}>{f}</option>)}
              </select>
              <button type="button" className="btn-secondary btn-sm" aria-label={`remove segment ${seg.name} policy ${i + 1}`} onClick={() => onChange({ ...seg, policies: rm(seg.policies, i) })}>×</button>
            </div>
          ))}
          <button type="button" className="btn-secondary btn-sm" onClick={() => onChange({ ...seg, policies: [...seg.policies, { from: fromChoices[0], policy: polChoices[0] }] })}>Add from → policy</button>
        </div>
      </div>
    </div>
  )
}

function VrfsEditor({ vrfs, policies, onChange }: { vrfs: MssVrf[]; policies: string[]; onChange: (v: MssVrf[]) => void }) {
  return (
    <section className="card space-y-2 p-3" data-testid="mss-vrfs">
      <div className="flex items-center"><span className="h2 mr-auto">VRFs &amp; segments</span>
        <button type="button" className="btn-secondary btn-sm" onClick={() => onChange([...vrfs, { name: '', segments: [] }])}>Add VRF</button></div>
      <div className="text-[11px] text-zinc-500">A destination segment picks the policy for traffic from each source segment (<span className="mono">{FORWARDING_SEGMENTS}</span> = any other), else its fallback.</div>
      {vrfs.map((v, i) => (
        <div key={i} className="space-y-2 rounded border border-zinc-200 p-2 dark:border-zinc-800" data-testid="mss-vrf">
          <div className="flex gap-1">
            <input className="input mono max-w-xs" aria-label={`vrf ${i + 1} name`} value={v.name} placeholder="VRF10" onChange={(e) => onChange(put(vrfs, i, { ...v, name: e.target.value }))} />
            <button type="button" className="btn-secondary btn-sm" onClick={() => onChange(put(vrfs, i, { ...v, segments: [...v.segments, { name: '', prefixes: [], interfaces: [], policies: [], fallback: null }] }))}>Add segment</button>
            <button type="button" className="btn-secondary btn-sm" aria-label={`remove vrf ${v.name || i + 1}`} onClick={() => onChange(rm(vrfs, i))}>×</button>
          </div>
          {v.segments.map((s, j) => (
            <SegmentEditor key={j} seg={s} others={v.segments.filter((_, k) => k !== j).map((x) => x.name).filter(Boolean)} policies={policies}
              onChange={(ns) => onChange(put(vrfs, i, { ...v, segments: put(v.segments, j, ns) }))}
              onRemove={() => onChange(put(vrfs, i, { ...v, segments: rm(v.segments, j) }))} />
          ))}
        </div>
      ))}
    </section>
  )
}

// ---------------------------------------------------------------- verify
export function MssCasesTable({ result }: { result: MssVerifyResult }) {
  return (
    <div data-testid="mss-cases">
      <div className="mb-1 flex gap-3 text-xs" data-testid="mss-verify-summary">
        <span className="text-emerald-600 dark:text-emerald-400">{result.passed} passed</span>
        <span className={result.failed ? 'text-red-600 dark:text-red-400' : 'text-zinc-500'}>{result.failed} failed</span>
      </div>
      <table className="table">
        <thead><tr><th>Src</th><th>Dst</th><th>Application</th><th>Expect</th><th>Observed</th><th>Result</th><th>Detail</th></tr></thead>
        <tbody>
          {result.cases.map((c: MssCaseResult, i) => (
            <tr key={i} data-testid="mss-case-row" className={c.pass ? '' : 'bg-red-50/60 dark:bg-red-950/20'}>
              <td className="mono">{c.src}</td>
              <td className="mono">{c.dst}{c.target_ip ? <span className="text-zinc-500"> ({c.target_ip})</span> : null}</td>
              <td className="mono">{c.application}</td>
              <td>{c.expect}</td>
              <td className={c.observed === 'error' ? 'text-amber-600 dark:text-amber-400' : ''}>{c.observed}</td>
              <td>{c.pass
                ? <Badge className="bg-emerald-100 text-emerald-800 dark:bg-emerald-900/60 dark:text-emerald-200">pass</Badge>
                : <Badge className="bg-red-100 text-red-800 dark:bg-red-900/60 dark:text-red-200">fail</Badge>}</td>
              <td className="mono text-xs text-zinc-500">{c.detail ?? ''}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

const asVerifyResult = (r: unknown): MssVerifyResult | null => {
  const o = r as MssVerifyResult | null
  return o && Array.isArray(o.cases) ? o : null
}

function VerifyPanel({ lab, summary, apps }: { lab: Lab; summary: MssSummary | undefined; apps: string[] }) {
  const qc = useQueryClient()
  const drawer = useJobDrawer()
  const clients = useQuery({ queryKey: ['labs', lab.id, 'clients'], queryFn: () => trafficApi.clients(lab.id), retry: false })
  const names = (clients.data ?? []).map((c) => c.name)
  const [mode, setMode] = useState<'auto' | 'explicit'>('auto')
  const [cases, setCases] = useState<MssCase[]>([])
  const [jobId, setJobId] = useState<string | null>(null)
  const job = useJobResult(jobId)
  const done = !!job.data && isTerminal(job.data.status)
  useEffect(() => { if (done) qc.invalidateQueries({ queryKey: ['labs', lab.id, 'mss'] }) }, [done, qc, lab.id])
  const appChoices = [...apps, BUILTIN_APP]
  const verify = useMutation({
    mutationFn: () => drawer.track(mssApi.verify(lab.id, { cases: mode === 'auto' ? null : cases })),
    onSuccess: (r) => setJobId(r.job_id),
  })
  const deployed = isRunning(lab.state)
  const casesOk = mode === 'auto' || (cases.length > 0 && cases.every((c) => c.src && c.dst && c.src !== c.dst && c.application))
  const result = done ? asVerifyResult(job.data?.result) : null
  const last = summary?.last_verify
  const newCase = (): MssCase => ({ src: names[0] ?? '', dst: names[1] ?? '', application: appChoices[0], expect: 'allow' })

  return (
    <section className="card space-y-3 p-3" data-testid="mss-verify">
      <div className="flex flex-wrap items-center gap-2">
        <span className="h2 mr-auto">Verify</span>
        <label className="flex items-center gap-1 text-sm"><input type="radio" name="mss-mode" checked={mode === 'auto'} onChange={() => setMode('auto')} /> auto-derive</label>
        <label className="flex items-center gap-1 text-sm"><input type="radio" name="mss-mode" checked={mode === 'explicit'} onChange={() => { setMode('explicit'); if (!cases.length) setCases([newCase()]) }} /> explicit cases</label>
        <button className="btn-primary btn-sm" data-testid="mss-verify-run" disabled={!deployed || !casesOk || verify.isPending || (!!jobId && !done)}
          title={!deployed ? 'deploy the lab first' : undefined} onClick={() => verify.mutate()}>{verify.isPending ? 'Starting…' : 'Verify'}</button>
      </div>
      <div className="text-[11px] text-zinc-500">{mode === 'auto'
        ? 'Every client pair × every application whose segments are known from the clients’ VLAN prefixes; expectation from the policy matrix.'
        : 'Probe per case: icmp → ping, tcp → nc -z, udp → iperf3; pass when the observed result matches the expectation.'}</div>
      {mode === 'explicit' && (
        <div className="space-y-1" data-testid="mss-case-editor">
          {cases.map((c, i) => (
            <div key={i} className="flex flex-wrap items-center gap-1">
              {(['src', 'dst'] as const).map((k) => (
                <select key={k} className="input w-36 mono" aria-label={`case ${i + 1} ${k}`} value={c[k]} onChange={(e) => setCases(put(cases, i, { ...c, [k]: e.target.value }))}>
                  {!names.includes(c[k]) && <option value={c[k]}>{c[k] || '—'}</option>}
                  {names.map((n) => <option key={n} value={n}>{n}</option>)}
                </select>
              ))}
              <select className="input w-40 mono" aria-label={`case ${i + 1} application`} value={c.application} onChange={(e) => setCases(put(cases, i, { ...c, application: e.target.value }))}>
                {appChoices.map((a) => <option key={a} value={a}>{a}</option>)}
              </select>
              <select className="input w-24" aria-label={`case ${i + 1} expect`} value={c.expect} onChange={(e) => setCases(put(cases, i, { ...c, expect: e.target.value as MssCase['expect'] }))}>
                <option value="allow">allow</option><option value="deny">deny</option>
              </select>
              <button type="button" className="btn-secondary btn-sm" aria-label={`remove case ${i + 1}`} onClick={() => setCases(rm(cases, i))}>×</button>
            </div>
          ))}
          <button type="button" className="btn-secondary btn-sm" onClick={() => setCases([...cases, newCase()])}>Add case</button>
          {!names.length && <div className="text-xs text-amber-600">No clients: build the lab (clients come from connected endpoints).</div>}
        </div>
      )}
      <ErrorBox error={verify.error} />
      {jobId && job.data && !done && <div className="flex items-center gap-2 text-xs text-zinc-500"><JobStatusBadge status={job.data.status} /> probing…</div>}
      {job.data?.status === 'failed' && <ErrorBox error={job.data.error ?? 'verify failed'} />}
      {result && <MssCasesTable result={result} />}
      {last && !result && (
        <div className="text-sm" data-testid="mss-last-verify">
          Last verify <span title={fmtDate(last.created_at)}>{fmtAgo(last.created_at)}</span>:{' '}
          <span className="text-emerald-600 dark:text-emerald-400">{last.passed ?? 0} passed</span>,{' '}
          <span className={last.failed ? 'text-red-600 dark:text-red-400' : ''}>{last.failed ?? 0} failed</span>
          {last.job_id && <> · <button className="link mono" onClick={() => drawer.openJob(last.job_id!)}>{last.job_id.slice(0, 8)}</button></>}
        </div>
      )}
    </section>
  )
}

// ---------------------------------------------------------------- tab
export default function MssTab({ lab }: { lab: Lab }) {
  const qc = useQueryClient()
  const mss = useQuery({ queryKey: ['labs', lab.id, 'mss'], queryFn: () => mssApi.get(lab.id) })
  const stored = useMemo(() => normalizeSegmentation(labKnobValues(lab).segmentation), [lab])
  const [draft, setDraft] = useState<Segmentation>(stored)
  const [raw, setRaw] = useState(false)
  const [rawText, setRawText] = useState('')
  const [rawErr, setRawErr] = useState<string | null>(null)
  useEffect(() => setDraft(stored), [stored])

  const dirty = JSON.stringify(segmentationKnob(draft)) !== JSON.stringify(segmentationKnob(stored))
  const issues = segmentationIssues(draft)
  const save = useMutation({
    mutationFn: () => labsApi.update(lab.id, { knobs: { segmentation: segmentationKnob(draft) } }),
    onSuccess: (l) => {
      qc.setQueryData(['labs', lab.id], l)
      qc.invalidateQueries({ queryKey: ['labs', lab.id, 'mss'] })
      qc.invalidateQueries({ queryKey: ['labs'] })
    },
  })
  const violations = save.error instanceof ApiError && save.error.status === 422 ? save.error.violations : []
  const matrix = !dirty && mss.data ? mss.data.matrix : policyMatrix(draft)
  const toggleRaw = () => {
    if (!raw) { setRawText(toYaml(segmentationKnob(draft))); setRawErr(null) }
    setRaw(!raw)
  }
  const appNames = draft.applications.map((a) => a.name).filter(Boolean)
  const polNames = draft.policies.map((p) => p.name).filter(Boolean)

  return (
    <div className="space-y-4" data-testid="mss-tab">
      <section className="card flex flex-wrap items-center gap-3 p-3">
        <span className="h2">Segmentation (MSS)</span>
        <FoundationBadge />
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" name="segmentation.enabled" checked={draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} />
          <span>enabled <span className="mono text-xs text-zinc-500">router segment-security</span></span>
        </label>
        <span className="mr-auto" />
        <button className="btn-secondary btn-sm" data-testid="mss-raw-toggle" onClick={toggleRaw}>{raw ? 'Form editor' : 'Raw YAML'}</button>
        {dirty && <span className="text-xs text-zinc-500">unsaved changes</span>}
        <button className="btn-secondary btn-sm" disabled={!dirty} onClick={() => { setDraft(stored); if (raw) setRawText(toYaml(segmentationKnob(stored))) }}>Reset</button>
        <button className="btn-primary btn-sm" data-testid="mss-save" disabled={!dirty || !!rawErr || save.isPending} onClick={() => save.mutate()}>{save.isPending ? 'Saving…' : 'Save'}</button>
      </section>
      {issues.length > 0 && (
        <ul className="list-disc rounded border border-amber-300 bg-amber-50 px-6 py-2 text-xs text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-100" data-testid="mss-issues">
          {issues.map((m, i) => <li key={i}>{m}</li>)}
        </ul>
      )}
      {violations.length > 0 && (
        <ul className="list-disc rounded border border-red-300 bg-red-50 px-6 py-2 text-xs text-red-800 dark:border-red-800 dark:bg-red-950/40 dark:text-red-200">
          {violations.map((v, i) => <li key={i}>{v.path && <span className="mono mr-1">{v.path}:</span>}{v.message}</li>)}
        </ul>
      )}
      {save.error && !violations.length && <ErrorBox error={save.error} />}

      <div className="grid gap-4 xl:grid-cols-[1fr_28rem]">
        <div className="min-w-0 space-y-4">
          {raw ? (
            <section className="card p-3" data-testid="mss-raw">
              <Field label="knobs.segmentation" error={rawErr}>
                <YamlEditor id="mss-raw-yaml" value={rawText} height="420px" onChange={(t) => {
                  setRawText(t)
                  const r = parseYaml(t)
                  if (r.ok) { setRawErr(null); setDraft(normalizeSegmentation(r.value)) } else setRawErr(r.error)
                }} />
              </Field>
            </section>
          ) : (<>
            <ApplicationsEditor apps={draft.applications} onChange={(applications) => setDraft({ ...draft, applications })} />
            <PoliciesEditor policies={draft.policies} apps={appNames} onChange={(policies) => setDraft({ ...draft, policies })} />
            <VrfsEditor vrfs={draft.vrfs} policies={polNames} onChange={(vrfs) => setDraft({ ...draft, vrfs })} />
          </>)}
        </div>
        <div className="min-w-0 space-y-4">
          <section className="card overflow-x-auto p-3">
            <div className="mb-2 flex items-center gap-2"><span className="h2 mr-auto">Policy matrix</span>{dirty && <Badge className="bg-zinc-200 dark:bg-zinc-700">preview (unsaved)</Badge>}</div>
            <ErrorBox error={mss.error} />
            {mss.isLoading ? <Spinner /> : <MssMatrix matrix={matrix} policies={draft.policies} />}
          </section>
          <section className="card p-3" data-testid="mss-rendered-on">
            <div className="h2 mb-1">Rendered on</div>
            {mss.data?.rendered_on.length
              ? <div className="flex flex-wrap gap-1">{mss.data.rendered_on.map((h) => <span key={h} className="mono rounded bg-zinc-100 px-1.5 py-0.5 text-xs dark:bg-zinc-800">{h}</span>)}</div>
              : <div className="text-xs text-zinc-500">No node carries <span className="mono">router segment-security</span> yet: enable, save and build (only L3 leafs that carry the VRFs get it).</div>}
          </section>
          <VerifyPanel lab={lab} summary={mss.data} apps={appNames} />
        </div>
      </div>
    </div>
  )
}
