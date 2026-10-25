import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { labsApi } from '../api/labs'
import { learnApi, type LessonSummary } from '../api/phase6'
import { useGlossary, useLearningMode, useTours } from '../hooks/useLearn'
import { SchemaBrowser } from '../components/learn/SchemaBrowser'
import { useTourOptional } from '../components/learn/Tour'
import { ErrorBox, PageHeader, Spinner, Tabs } from '../components/ui'
import { LEARN_LAB_KEY, LEVELS, byLevel, filterGlossary, getLS, progressPct, setLS } from '../lib/learn'
import { cls } from '../lib/format'

const SECTIONS = [
  { id: 'lessons', label: 'Lessons' },
  { id: 'glossary', label: 'Glossary' },
  { id: 'schema', label: 'Schema browser' },
  { id: 'tours', label: 'Tours' },
] as const
type Section = (typeof SECTIONS)[number]['id']
const LEVEL_TEXT: Record<string, string> = { intro: 'Intro', core: 'Core', advanced: 'Advanced' }

/** The lab lessons, checks and tours act on (persisted). */
export function useLearnLab(): [string | null, (id: string | null) => void] {
  const [lab, setLab] = useState<string | null>(() => getLS(LEARN_LAB_KEY))
  return [lab, (id) => { setLS(LEARN_LAB_KEY, id); setLab(id) }]
}

export function LearnLabPicker({ value, onChange }: { value: string | null; onChange: (id: string | null) => void }) {
  const labs = useQuery({ queryKey: ['labs'], queryFn: labsApi.list })
  const list = labs.data ?? []
  const eff = value && list.some((l) => l.id === value) ? value : ''
  return (
    <label className="inline-flex items-center gap-1.5 text-xs">
      <span className="text-zinc-500">practice lab</span>
      <select className="input w-auto py-0.5 text-xs" value={eff} onChange={(e) => onChange(e.target.value || null)} data-testid="learn-lab">
        <option value="">— pick a lab —</option>
        {list.map((l) => <option key={l.id} value={l.id}>{l.name} ({l.state})</option>)}
      </select>
    </label>
  )
}

export default function LearnPage() {
  const { section = 'lessons' } = useParams()
  const navigate = useNavigate()
  const [on, setOn] = useLearningMode()
  const [lab, setLab] = useLearnLab()
  const current = (SECTIONS.some((s) => s.id === section) ? section : 'lessons') as Section
  return (
    <div className="max-w-6xl">
      <PageHeader title="Learn AVD" sub="Learn while you build: lessons with checks against your lab, the AVD glossary and schema, guided tours"
        actions={<>
          <LearnLabPicker value={lab} onChange={setLab} />
          <label className="inline-flex cursor-pointer items-center gap-1.5 rounded-md border border-cv-hint-border bg-cv-hint-bg px-2.5 py-1.5 text-xs font-medium text-cv-hint-text">
            <input type="checkbox" checked={on} onChange={(e) => setOn(e.target.checked)} data-testid="learn-mode-hub" /> Learning mode
          </label>
        </>} />
      <p className="-mt-2 mb-4 text-[13px] text-cv-muted">Learning mode adds (i) help to every knob, click-to-explain to config viewers and hover cards to glossary terms across the app.</p>
      <Tabs tabs={[...SECTIONS]} value={current} onChange={(s) => navigate(`/learn/${s}`)} />
      {current === 'lessons' && <LessonsHub />}
      {current === 'glossary' && <GlossaryView />}
      {current === 'schema' && <SchemaSection />}
      {current === 'tours' && <ToursLauncher labId={lab} />}
    </div>
  )
}

function ProgressBar({ pct, className }: { pct: number; className?: string }) {
  return (
    <div className={cls('h-1.5 w-full overflow-hidden rounded bg-zinc-200 dark:bg-zinc-800', className)} role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
      <div className={cls('h-full rounded', pct >= 100 ? 'bg-emerald-500' : 'bg-cyan-500')} style={{ width: `${pct}%` }} />
    </div>
  )
}
export { ProgressBar }

export function LessonsHub() {
  const lessons = useQuery({ queryKey: ['learn', 'lessons'], queryFn: learnApi.lessons })
  const groups = useMemo(() => byLevel(lessons.data ?? []), [lessons.data])
  if (lessons.isLoading) return <Spinner />
  const all = lessons.data ?? []
  const done = all.filter((l) => l.progress?.completed).length
  return (
    <div className="space-y-4" data-testid="lessons-hub">
      <ErrorBox error={lessons.error} />
      <div className="card flex items-center gap-3 p-3 text-sm">
        <span className="font-medium" data-testid="lessons-completed">{done} / {all.length} lessons completed</span>
        <ProgressBar pct={all.length ? Math.round((100 * done) / all.length) : 0} className="max-w-md flex-1" />
      </div>
      {LEVELS.filter((lv) => groups[lv].length).map((lv) => (
        <section key={lv} data-testid={`level-${lv}`}>
          <div className="h2 mb-2">{LEVEL_TEXT[lv]}</div>
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {groups[lv].map((l) => <LessonCard key={l.id} l={l} />)}
          </div>
        </section>
      ))}
    </div>
  )
}

function LessonCard({ l }: { l: LessonSummary }) {
  const pct = progressPct(l.progress)
  return (
    <Link to={`/learn/lessons/${l.id}`} className="card block p-3 hover:border-blue-500" data-testid="lesson-card">
      <div className="flex items-start gap-2">
        <span className="flex-1 font-semibold">{l.title}</span>
        {l.progress?.completed && <span className="text-emerald-600" aria-label="completed">✓</span>}
      </div>
      {l.summary && <p className="mt-1 line-clamp-2 text-xs text-zinc-600 dark:text-zinc-400">{l.summary}</p>}
      <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-zinc-500">
        <span>{l.minutes} min</span>
        {l.requires.map((r) => <span key={r} className="rounded bg-zinc-100 px-1 dark:bg-zinc-800">needs {r}</span>)}
        <span>{l.tries} tries · {l.checkpoints.length} checks · {l.quizzes} quiz</span>
      </div>
      <div className="mt-2 flex items-center gap-2 text-[11px]">
        <ProgressBar pct={pct} />
        <span className="w-16 text-right text-zinc-500" data-testid="lesson-progress">{l.progress?.passed.length ?? 0}/{l.progress?.total ?? l.checkpoints.length}</span>
      </div>
    </Link>
  )
}

function GlossaryView() {
  const [params, setParams] = useSearchParams()
  const qtext = params.get('q') ?? ''
  const g = useGlossary()
  const list = filterGlossary(g.data ?? [], qtext)
  return (
    <div className="space-y-3" data-testid="glossary">
      <input className="input max-w-sm" placeholder="filter terms" value={qtext} onChange={(e) => setParams(e.target.value ? { q: e.target.value } : {}, { replace: true })} data-testid="glossary-filter" />
      {g.isLoading ? <Spinner /> : <ErrorBox error={g.error} />}
      <dl className="grid gap-2 md:grid-cols-2">
        {list.map((t) => (
          <div key={t.term} className="card p-3 text-sm" id={`term-${t.term}`} data-testid="glossary-term">
            <dt className="font-semibold">{t.term}{t.aliases.length > 0 && <span className="ml-1 text-xs font-normal text-zinc-500">({t.aliases.join(', ')})</span>}</dt>
            <dd className="mt-1 space-y-1">
              <p>{t.short}</p>
              {t.long && <p className="text-xs text-zinc-600 dark:text-zinc-400">{t.long}</p>}
              <div className="flex flex-wrap gap-2 text-xs">
                {t.see.map((s) => <button key={s} className="link" onClick={() => setParams({ q: s }, { replace: true })}>{s}</button>)}
                {t.lesson && <Link className="link" to={`/learn/lessons/${t.lesson}`}>lesson →</Link>}
                {t.doc_url && <a className="link" href={t.doc_url} target="_blank" rel="noreferrer">AVD docs</a>}
              </div>
            </dd>
          </div>
        ))}
      </dl>
      {g.data && !list.length && <div className="text-sm text-zinc-500">No term matches “{qtext}”.</div>}
    </div>
  )
}

function SchemaSection() {
  const [params] = useSearchParams()
  const key = params.get('key') ?? ''
  const schema = params.get('schema') === 'eos_cli_config_gen' ? 'eos_cli_config_gen' : 'eos_designs'
  return <SchemaBrowser initialKey={key} initialSchema={schema} />
}

function ToursLauncher({ labId }: { labId: string | null }) {
  const tours = useTours()
  const tour = useTourOptional()
  if (tours.isLoading) return <Spinner />
  return (
    <div className="grid gap-3 md:grid-cols-2" data-testid="tours-launcher">
      <ErrorBox error={tours.error} />
      {Object.entries(tours.data ?? {}).map(([id, t]) => (
        <div key={id} className="card flex flex-col gap-2 p-3">
          <div className="font-semibold">{t.title}</div>
          <div className="text-xs text-zinc-500">{t.steps.length} steps · starts at <span className="mono">{t.route}</span></div>
          <ol className="list-decimal pl-5 text-xs text-zinc-600 dark:text-zinc-400">{t.steps.map((s, i) => <li key={i}>{s.title}</li>)}</ol>
          <div className="mt-auto"><button className="btn-primary btn-sm" disabled={!tour} onClick={() => tour?.start(id, t.route.includes(':id') ? labId : null)} data-testid={`tour-start-${id}`}>Start tour</button></div>
        </div>
      ))}
    </div>
  )
}
