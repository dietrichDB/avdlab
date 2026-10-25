import { useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useGlossary, useLearningMode } from '../../hooks/useLearn'
import { findTerm, pitfallText } from '../../lib/learn'
import type { KnobLearn } from '../../api/phase6'

/**
 * A glossary term: dotted underline + hover card (short, long, related terms, lesson and doc links).
 * `<Term>VTEP</Term>` or `<Term k="MLAG">MLAG pair</Term>`. Plain text when learning mode is off or the term is unknown.
 */
export function Term({ k, children }: { k?: string; children?: ReactNode }) {
  const [on] = useLearningMode()
  const g = useGlossary(on)
  const [open, setOpen] = useState(false)
  const name = k ?? (typeof children === 'string' ? children : '')
  const t = on ? findTerm(g.data, name) : undefined
  if (!t) return <>{children ?? k}</>
  return (
    <span className="relative inline" onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}
      onFocus={() => setOpen(true)} onBlur={() => setOpen(false)}>
      <span tabIndex={0} data-testid="term" data-term={t.term}
        className="cursor-help underline decoration-cyan-500 decoration-dotted underline-offset-2">{children ?? t.term}</span>
      {open && (
        <span role="tooltip" data-testid="term-card"
          className="absolute left-0 top-full z-50 mt-1 block w-72 rounded-md border border-cv-border bg-cv-surface p-2 text-left text-xs font-normal normal-case tracking-normal text-zinc-700 shadow-lg dark:text-zinc-200">
          <span className="block font-semibold text-zinc-900 dark:text-zinc-100">{t.term}{t.aliases.length > 0 && <span className="ml-1 font-normal text-zinc-500">({t.aliases.slice(0, 3).join(', ')})</span>}</span>
          <span className="mt-1 block">{t.short}</span>
          {t.long && <span className="mt-1 block text-zinc-500 dark:text-zinc-400">{t.long}</span>}
          {t.see.length > 0 && <span className="mt-1 block text-zinc-500">see: {t.see.join(', ')}</span>}
          <span className="mt-1 flex gap-3">
            {t.lesson && <Link className="link" to={`/learn/lessons/${t.lesson}`}>lesson →</Link>}
            {t.doc_url && <a className="link" href={t.doc_url} target="_blank" rel="noreferrer">AVD docs</a>}
            <Link className="link" to={`/learn/glossary?q=${encodeURIComponent(t.term)}`}>glossary</Link>
          </span>
        </span>
      )}
    </span>
  )
}

/** Knob `(i)` popover from `knob.learn`: summary, AVD keys it writes, "renders as" CLI, pitfalls, doc link. */
export function KnobInfo({ learn, knobKey }: { learn?: KnobLearn | null; knobKey: string }) {
  const [on] = useLearningMode()
  const [open, setOpen] = useState(false)
  if (!on || !learn || !(learn.summary || learn.avd_keys?.length || learn.renders)) return null
  return (
    <span className="relative ml-1 inline-block align-middle">
      <button type="button" aria-label={`about ${knobKey}`} aria-expanded={open} data-testid={`knob-info-${knobKey}`}
        className="inline-flex h-4 w-4 items-center justify-center rounded-full border border-cyan-500 text-[10px] font-semibold italic leading-none text-cyan-600 hover:bg-cyan-50 dark:text-cyan-300 dark:hover:bg-cyan-950"
        onClick={(e) => { e.preventDefault(); e.stopPropagation(); setOpen((o) => !o) }}>i</button>
      {open && (
        <span data-testid="knob-learn" role="dialog" aria-label={`${knobKey} help`}
          className="absolute left-0 top-full z-50 mt-1 block w-80 space-y-1.5 rounded-md border border-cv-border bg-cv-surface p-2.5 text-left text-xs font-normal text-zinc-700 shadow-lg dark:text-zinc-200"
          onClick={(e) => e.stopPropagation()}>
          <span className="flex items-start gap-2">
            <span className="mono flex-1 font-semibold">{knobKey}</span>
            <button type="button" aria-label="close" className="text-zinc-400 hover:text-zinc-800" onClick={(e) => { e.preventDefault(); setOpen(false) }}>×</button>
          </span>
          {learn.summary && <span className="block">{learn.summary}</span>}
          {!!learn.avd_keys?.length && (
            <span className="block">
              <span className="block text-[10px] font-semibold uppercase tracking-wide text-zinc-500">writes AVD keys</span>
              <span className="mt-0.5 flex flex-wrap gap-1">
                {learn.avd_keys.map((k) => (
                  <Link key={k} to={`/learn/schema?key=${encodeURIComponent(k.replace(/<node_type>/g, 'l3leaf'))}`} data-testid="avd-key"
                    className="mono rounded bg-cyan-50 px-1 text-cyan-800 hover:underline dark:bg-cyan-950/60 dark:text-cyan-200">{k}</Link>
                ))}
              </span>
            </span>
          )}
          {learn.renders && (
            <span className="block">
              <span className="block text-[10px] font-semibold uppercase tracking-wide text-zinc-500">renders as</span>
              <code className="mono mt-0.5 block whitespace-pre-wrap rounded bg-zinc-100 p-1.5 dark:bg-zinc-800" data-testid="knob-renders">{learn.renders}</code>
            </span>
          )}
          {!!learn.pitfalls?.length && (
            <span className="block">
              <span className="block text-[10px] font-semibold uppercase tracking-wide text-amber-600">pitfalls</span>
              <ul className="list-disc pl-4">{learn.pitfalls.map((p, i) => <li key={i}>{pitfallText(p)}</li>)}</ul>
            </span>
          )}
          {learn.doc_url && <a className="link block" href={learn.doc_url} target="_blank" rel="noreferrer">AVD documentation →</a>}
        </span>
      )}
    </span>
  )
}
