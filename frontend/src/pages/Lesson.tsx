import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api/client'
import { labsApi } from '../api/labs'
import { learnApi, type CheckResult, type Lesson, type LessonBlock } from '../api/phase6'
import type { JobRef, Lab } from '../api/types'
import { useJobDrawer } from '../components/JobDrawer'
import { ErrorBox, Spinner } from '../components/ui'
import { Markdown } from '../lib/markdown'
import { cls } from '../lib/format'
import { mergeKnob, tryPlan } from '../lib/learn'
import { LearnLabPicker, ProgressBar, useLearnLab } from './Learn'

const STATE_RANK: Record<string, number> = { draft: 0, destroyed: 1, built: 1, deployed: 2, validated: 3 }
const NEED_RANK: Record<string, number> = { built: 1, deployed: 2, validated: 3 }

/** Lessons runner: markdown + :::try (in-app actions) + :::check (server checkpoints) + :::quiz (instant feedback). */
export default function LessonPage() {
  const { id = '' } = useParams()
  const q = useQuery({ queryKey: ['learn', 'lesson', id], queryFn: () => learnApi.lesson(id) })
  const [labId, setLabId] = useLearnLab()
  const lab = useQuery({ queryKey: ['labs', labId], queryFn: () => labsApi.get(labId!), enabled: !!labId, retry: false })
  if (q.isLoading) return <Spinner />
  if (!q.data) return <ErrorBox error={q.error ?? 'lesson not found'} />
  return <LessonView key={id} lesson={q.data} labId={labId} setLabId={setLabId} lab={lab.data ?? null} />
}

export function LessonView({ lesson: l, labId, setLabId, lab }: { lesson: Lesson; labId: string | null; setLabId: (id: string | null) => void; lab: Lab | null }) {
  const [passed, setPassed] = useState<Set<string>>(() => new Set(l.passed))
  const total = l.checkpoints.length
  const done = l.checkpoints.filter((c) => passed.has(c)).length
  const pct = total ? Math.round((100 * done) / total) : 0
  const missing = lab ? l.requires.filter((r) => (STATE_RANK[lab.state] ?? 0) < NEED_RANK[r]) : []
  return (
    <div className="max-w-3xl space-y-4" data-testid="lesson">
      <div className="text-xs"><Link className="link" to="/learn/lessons">← Lessons</Link></div>
      <header className="space-y-2">
        <h1 className="h1">{l.title}</h1>
        <div className="flex flex-wrap items-center gap-2 text-xs text-zinc-500">
          <span className="rounded bg-zinc-100 px-1.5 py-0.5 dark:bg-zinc-800">{l.level}</span>
          <span>{l.minutes} min</span>
          {l.requires.map((r) => <span key={r}>needs a {r} lab</span>)}
          <span className="flex-1" />
          <LearnLabPicker value={labId} onChange={setLabId} />
        </div>
        <div className="flex items-center gap-2 text-xs">
          <ProgressBar pct={pct} className="flex-1" />
          <span data-testid="lesson-progress-count">{done}/{total} checks passed</span>
        </div>
        {!labId && <div className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-100">Pick a practice lab (top right) — try buttons and checks act on it.</div>}
        {missing.length > 0 && <div className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-100" data-testid="lesson-requires">This lesson needs a {missing.join(' + ')} lab; {lab?.name} is {lab?.state}.</div>}
      </header>
      <article className="space-y-3">
        {l.blocks.map((b, i) => <Block key={b.kind === 'markdown' ? `md-${i}` : `${b.kind}-${b.id}`} lesson={l} b={b} labId={labId} lab={lab}
          passed={b.kind === 'check' && passed.has(b.id)} onPassed={(cid) => setPassed((s) => new Set(s).add(cid))} />)}
      </article>
      <nav className="flex items-center justify-between border-t border-zinc-200 pt-3 text-sm dark:border-zinc-800">
        {l.prev ? <Link className="btn-secondary btn-sm" to={`/learn/lessons/${l.prev}`} data-testid="lesson-prev">← Previous</Link> : <span />}
        {l.next ? <Link className="btn-primary btn-sm" to={`/learn/lessons/${l.next}`} data-testid="lesson-next">Next lesson →</Link> : <Link className="btn-secondary btn-sm" to="/learn">Back to Learn</Link>}
      </nav>
    </div>
  )
}

function Block({ lesson, b, labId, lab, passed, onPassed }: { lesson: Lesson; b: LessonBlock; labId: string | null; lab: Lab | null; passed: boolean; onPassed: (id: string) => void }) {
  switch (b.kind) {
    case 'markdown': return <Markdown source={b.text} className="md space-y-2 text-sm leading-relaxed" />
    case 'try': return <TryButton b={b} labId={labId} lab={lab} />
    case 'check': return <CheckBlock lessonId={lesson.id} b={b} labId={labId} passedEarlier={passed} onPassed={onPassed} />
    case 'quiz': return <QuizBlock b={b} />
  }
}

function TryButton({ b, labId, lab }: { b: Extract<LessonBlock, { kind: 'try' }>; labId: string | null; lab: Lab | null }) {
  const navigate = useNavigate()
  const drawer = useJobDrawer()
  const qc = useQueryClient()
  const [msg, setMsg] = useState<string | null>(null)
  const plan = tryPlan(b, labId)
  const run = useMutation({
    mutationFn: async () => {
      if (plan.kind === 'navigate') { navigate(plan.to); return }
      if (plan.kind === 'request') {
        const r = await api.post<JobRef>(plan.path, {})
        if (r?.job_id) drawer.openJob(r.job_id)
        return
      }
      if (plan.kind === 'knob' && labId) {
        const knobs = mergeKnob((lab?.knobs ?? {}) as Record<string, unknown>, plan.knob, plan.value)
        await labsApi.update(labId, { knobs })
        qc.invalidateQueries({ queryKey: ['labs', labId] })
        setMsg(`knob ${plan.knob} updated — rebuild to see the effect`)
      }
    },
  })
  const icon = { open_file: '📄', open_tab: '↗', run_job: '▶', set_knob: '⚙' }[b.action]
  return (
    <div className="flex flex-wrap items-center gap-2 rounded border border-cyan-200 bg-cyan-50/60 px-3 py-2 text-sm dark:border-cyan-900 dark:bg-cyan-950/30" data-testid="lesson-try">
      <span className="text-[10px] font-semibold uppercase tracking-wide text-cyan-700 dark:text-cyan-300">try</span>
      <button className="btn-secondary btn-sm" disabled={plan.kind === 'need-lab' || run.isPending} onClick={() => run.mutate()} data-testid={`try-${b.id}`}>
        <span aria-hidden className="mr-1">{icon}</span>{b.label}
      </button>
      {plan.kind === 'need-lab' && <span className="text-xs text-zinc-500">pick a practice lab first</span>}
      {msg && <span className="text-xs text-emerald-700 dark:text-emerald-300">{msg}</span>}
      <ErrorBox error={run.error} />
    </div>
  )
}

function CheckBlock({ lessonId, b, labId, passedEarlier, onPassed }: { lessonId: string; b: Extract<LessonBlock, { kind: 'check' }>; labId: string | null; passedEarlier: boolean; onPassed: (id: string) => void }) {
  const qc = useQueryClient()
  const [res, setRes] = useState<CheckResult | null>(null)
  const check = useMutation({
    mutationFn: () => learnApi.check(lessonId, { lab_id: labId, checkpoint_id: b.id }),
    onSuccess: (r) => {
      setRes(r)
      if (r.passed) { onPassed(b.id); qc.invalidateQueries({ queryKey: ['learn', 'lessons'] }) }
    },
  })
  const ok = res ? res.passed : passedEarlier ? true : null
  return (
    <div className={cls('rounded border px-3 py-2 text-sm', ok === true ? 'border-emerald-300 bg-emerald-50 dark:border-emerald-800 dark:bg-emerald-950/30' : ok === false ? 'border-red-300 bg-red-50 dark:border-red-800 dark:bg-red-950/30' : 'border-zinc-300 dark:border-zinc-700')} data-testid="lesson-check">
      <div className="flex flex-wrap items-center gap-2">
        <span className={cls('w-5 text-center text-base font-bold', ok === true ? 'text-emerald-600' : ok === false ? 'text-red-600' : 'text-zinc-400')} data-testid="check-mark">{ok === true ? '✓' : ok === false ? '✗' : '○'}</span>
        <span className="flex-1">{b.label}</span>
        <button className="btn-secondary btn-sm" disabled={check.isPending} onClick={() => check.mutate()} data-testid={`check-${b.id}`}>{check.isPending ? 'Checking…' : res || passedEarlier ? 'Check again' : 'Check'}</button>
      </div>
      {res && <div className="mono mt-1 pl-7 text-xs text-zinc-600 dark:text-zinc-300" data-testid="check-detail">{res.detail}</div>}
      {res && !res.passed && res.hint && <div className="mt-1 pl-7 text-xs text-amber-800 dark:text-amber-200" data-testid="check-hint">Hint: {res.hint}</div>}
      {!res && passedEarlier && <div className="mt-1 pl-7 text-xs text-emerald-700 dark:text-emerald-300">passed earlier</div>}
      <ErrorBox error={check.error} />
    </div>
  )
}

function QuizBlock({ b }: { b: Extract<LessonBlock, { kind: 'quiz' }> }) {
  const [pick, setPick] = useState<number | null>(null)
  const right = pick === b.answer
  return (
    <fieldset className="rounded border border-violet-200 px-3 py-2 text-sm dark:border-violet-900" data-testid="lesson-quiz">
      <legend className="px-1 text-[10px] font-semibold uppercase tracking-wide text-violet-700 dark:text-violet-300">quiz</legend>
      <p className="font-medium">{b.question}</p>
      <ul className="mt-2 space-y-1">
        {b.options.map((o, i) => (
          <li key={i}>
            <button type="button" onClick={() => setPick(i)} data-testid={`quiz-${b.id}-${i}`} aria-pressed={pick === i}
              className={cls('w-full rounded border px-2 py-1 text-left',
                pick === null ? 'border-zinc-200 hover:bg-zinc-50 dark:border-zinc-700 dark:hover:bg-zinc-800'
                  : i === b.answer ? 'border-emerald-400 bg-emerald-50 dark:border-emerald-700 dark:bg-emerald-950/40'
                    : i === pick ? 'border-red-400 bg-red-50 dark:border-red-700 dark:bg-red-950/40' : 'border-zinc-200 opacity-70 dark:border-zinc-700')}>
              {o}
            </button>
          </li>
        ))}
      </ul>
      {pick !== null && (
        <div className={cls('mt-2 text-xs', right ? 'text-emerald-700 dark:text-emerald-300' : 'text-red-700 dark:text-red-300')} data-testid="quiz-feedback">
          <span className="font-semibold">{right ? 'Correct.' : 'Not quite.'}</span> <span className="text-zinc-700 dark:text-zinc-300">{b.explanation}</span>
        </div>
      )}
    </fieldset>
  )
}
