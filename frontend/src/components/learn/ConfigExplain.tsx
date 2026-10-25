import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { learnApi, type ExplainFile, type ExplainLine } from '../../api/phase6'
import { useLearningMode } from '../../hooks/useLearn'
import { cls } from '../../lib/format'
import { isExplainable, parseConfig } from '../../lib/learn'
import { ErrorBox, Spinner } from '../ui'

const fmt = (v: unknown) => (typeof v === 'string' ? v : JSON.stringify(v))

/**
 * Read-only EOS config with learning hooks: hover highlights the line's section, click a line → Explain side panel
 * (`GET /learn/explain`), "Explain this file" → sections with explained/total counts (`POST /learn/explain {all}`).
 * `byText`: the text is not the intended config (running config) — explain by line text instead of line number.
 */
export function ConfigViewer({ labId, node, text, byText = false, initialLine, className, testId = 'config-viewer', height = '60vh' }: {
  labId: string; node: string; text: string; byText?: boolean; initialLine?: string | number | null; className?: string; testId?: string; height?: string
}) {
  const [learning] = useLearningMode()
  const lines = useMemo(() => parseConfig(text), [text])
  const [hover, setHover] = useState<number | null>(null)
  const [sel, setSel] = useState<number | null>(null)
  const [batch, setBatch] = useState(false)
  const box = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (initialLine === undefined || initialLine === null || initialLine === '') return
    const n = typeof initialLine === 'number' || /^\d+$/.test(String(initialLine)) ? Number(initialLine)
      : lines.find((l) => l.text.trim() === String(initialLine).trim())?.no
    if (n) setSel(n)
  }, [initialLine, lines])
  useEffect(() => {
    if (sel) box.current?.querySelector(`[data-line="${sel}"]`)?.scrollIntoView?.({ block: 'nearest' })
  }, [sel])

  const hoverSection = hover ? lines[hover - 1]?.section : null
  const selLine = sel ? lines[sel - 1] : null
  const selSection = selLine?.section ?? null
  const panel = learning && (sel || batch)

  return (
    <div className={cls('grid min-h-0 gap-2', panel ? 'lg:grid-cols-[1fr_22rem]' : '', className)}>
      <div className="flex min-h-0 flex-col gap-1">
        {learning && (
          <div className="flex items-center gap-2 text-[11px] text-zinc-500">
            <span className="flex-1">Click any line to see which AVD input produced it.</span>
            <button className="btn-secondary btn-sm" onClick={() => { setBatch((b) => !b); setSel(null) }} data-testid="explain-file">{batch ? 'Close file view' : 'Explain this file'}</button>
          </div>
        )}
        <div ref={box} style={{ maxHeight: height }} className="mono min-h-0 flex-1 overflow-auto rounded border border-zinc-200 bg-zinc-50 py-1 text-[12px] leading-[1.45] dark:border-zinc-800 dark:bg-zinc-950" data-testid={testId}>
          {lines.map((l) => {
            const inSel = selSection !== null && l.section === selSection
            return (
              <div key={l.no} data-line={l.no}
                className={cls('flex whitespace-pre',
                  sel === l.no ? 'bg-blue-200/70 dark:bg-blue-800/60' : inSel ? 'bg-blue-50 dark:bg-blue-950/40' : hoverSection !== null && l.section === hoverSection ? 'bg-zinc-200/60 dark:bg-zinc-800/60' : '',
                  learning && isExplainable(l) && 'cursor-pointer')}
                onMouseEnter={learning ? () => setHover(l.no) : undefined}
                onMouseLeave={learning ? () => setHover(null) : undefined}
                onClick={learning && isExplainable(l) ? () => { setSel(l.no); setBatch(false) } : undefined}
                data-testid="config-line">
                <span className="w-10 shrink-0 select-none pr-2 text-right text-zinc-400">{l.no}</span>
                <span>{l.text || ' '}</span>
              </div>
            )
          })}
        </div>
      </div>
      {learning && sel && selLine && (
        <ExplainPanel labId={labId} node={node} line={byText ? selLine.text.trim() : sel} text={selLine.text.trim()} onClose={() => setSel(null)}
          onJump={(no) => { if (!byText) setSel(no) }} />
      )}
      {learning && batch && !sel && (
        <ExplainFileView labId={labId} node={node} onPick={(section) => {
          const l = lines.find((x) => x.indent === 0 && x.text.trim() === section)
          if (l) setSel(l.no)
        }} onClose={() => setBatch(false)} />
      )}
    </div>
  )
}

export function ExplainPanel({ labId, node, line, text, onClose, onJump }: {
  labId: string; node: string; line: string | number; text: string; onClose: () => void; onJump?: (no: number) => void
}) {
  const q = useQuery({ queryKey: ['learn', 'explain', labId, node, line], queryFn: () => learnApi.explain(labId, node, line), retry: false, staleTime: 60_000 })
  return (
    <aside className="card max-h-[70vh] space-y-2 overflow-auto p-3 text-xs" data-testid="explain-panel">
      <div className="flex items-start gap-2">
        <span className="h2 flex-1">Explain</span>
        <button aria-label="close explain" className="text-zinc-400 hover:text-zinc-900 dark:hover:text-zinc-100" onClick={onClose}>×</button>
      </div>
      <code className="mono block whitespace-pre-wrap rounded bg-zinc-100 px-1.5 py-1 dark:bg-zinc-800">{text}</code>
      {q.isLoading ? <Spinner label="asking the rules table…" /> : q.error ? <ErrorBox error={q.error} /> : q.data ? <ExplainBody e={q.data} onJump={onJump} /> : null}
    </aside>
  )
}

export function ExplainBody({ e, onJump }: { e: ExplainLine; onJump?: (no: number) => void }) {
  return (
    <div className="space-y-2" data-testid="explain-body">
      {e.section && e.section !== e.line && <div className="text-zinc-500">in <span className="mono">{e.section}</span></div>}
      {e.error && <div className="text-amber-700 dark:text-amber-300">{e.error}</div>}
      {e.why ? <p className="text-sm leading-snug" data-testid="explain-why">{e.why}</p>
        : !e.error && <p className="text-zinc-500" data-testid="explain-unknown">No rule explains this line yet. Look it up in the structured config (intended/structured_configs) or the device documentation.</p>}
      {e.inputs.length > 0 && (
        <div>
          <div className="text-[10px] font-semibold uppercase tracking-wide text-zinc-500">AVD input</div>
          <ul className="space-y-1">
            {e.inputs.map((i, n) => (
              <li key={n} className="rounded border border-zinc-200 p-1.5 dark:border-zinc-800" data-testid="explain-input">
                <Link className="mono link break-all" to={`/learn/schema?key=${encodeURIComponent(i.key.replace(/\[[^\]]*\]/g, ''))}`}>{i.key}</Link>
                <span className="mono ml-1 break-all">= {fmt(i.value)}</span>
                <div className="text-zinc-500">{i.where}{i.knob && <> · knob <span className="mono">{i.knob}</span></>}</div>
              </li>
            ))}
          </ul>
        </div>
      )}
      {e.related_lines.length > 0 && (
        <div>
          <div className="text-[10px] font-semibold uppercase tracking-wide text-zinc-500">related lines</div>
          <ul className="mono space-y-0.5">
            {e.related_lines.slice(0, 12).map((r) => (
              <li key={r.no}><button className="text-left hover:underline" onClick={() => onJump?.(r.no)}><span className="text-zinc-400">{r.no}</span> {r.text.length > 80 ? r.text.slice(0, 80) + '…' : r.text}</button></li>
            ))}
          </ul>
        </div>
      )}
      <div className="flex gap-3">
        {e.doc_url && <a className="link" href={e.doc_url} target="_blank" rel="noreferrer">AVD docs →</a>}
        {e.topic && <span className="text-zinc-400">topic: {e.topic}</span>}
      </div>
    </div>
  )
}

export function ExplainFileView({ labId, node, onPick, onClose }: { labId: string; node: string; onPick: (section: string) => void; onClose: () => void }) {
  const q = useQuery({ queryKey: ['learn', 'explain-file', labId, node], queryFn: () => learnApi.explainFile(labId, node), retry: false, staleTime: 60_000 })
  return (
    <aside className="card max-h-[70vh] space-y-2 overflow-auto p-3 text-xs" data-testid="explain-file-view">
      <div className="flex items-start gap-2">
        <span className="h2 flex-1">{node}: sections</span>
        <button aria-label="close file view" className="text-zinc-400 hover:text-zinc-900" onClick={onClose}>×</button>
      </div>
      {q.isLoading ? <Spinner /> : q.error ? <ErrorBox error={q.error} /> : q.data ? <FileSections f={q.data} onPick={onPick} /> : null}
    </aside>
  )
}

function FileSections({ f, onPick }: { f: ExplainFile; onPick: (s: string) => void }) {
  return (
    <>
      <div className="text-zinc-500" data-testid="explain-file-summary">{f.explained} of {f.total} lines explained · {f.sections.length} sections</div>
      <ul className="space-y-0.5">
        {f.sections.map((s, i) => (
          <li key={i}>
            <button className="flex w-full items-center gap-2 rounded px-1 py-0.5 text-left hover:bg-zinc-100 dark:hover:bg-zinc-800" onClick={() => s.section && onPick(s.section)} data-testid="explain-section">
              <span className="mono min-w-0 flex-1 truncate">{s.section}</span>
              {s.topics.length > 0 && <span className="text-[10px] text-zinc-400">{s.topics.join(', ')}</span>}
              <span className={cls('rounded px-1 font-mono text-[10px]', s.explained === s.lines ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/50 dark:text-emerald-200' : s.explained ? 'bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-200' : 'bg-zinc-100 text-zinc-500 dark:bg-zinc-800')}>
                {s.explained}/{s.lines}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </>
  )
}
