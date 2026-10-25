import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { aiApi } from '../../api/ai'
import { ApiError } from '../../api/client'
import type { AiApplyOut, AiChatTurn, AiDesignOut, AiTopologyOp } from '../../api/types'
import { DiffViewer } from '../DiffViewer'
import { ErrorBox, Hint, Spinner } from '../ui'
import { cls } from '../../lib/format'

export function SparkleIcon({ className = 'h-4 w-4' }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={className} fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9L12 3z" />
      <path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8L19 15z" />
    </svg>
  )
}

export function DesignWithAiButton({ onClick, className = 'btn-secondary btn-sm' }: { onClick: () => void; className?: string }) {
  return (
    <button type="button" className={cls(className, 'inline-flex items-center gap-1.5')} onClick={onClick} data-testid="ai-open"
      title="describe the lab you want; the AI proposes validated AVD inputs">
      <SparkleIcon className="h-3.5 w-3.5 text-violet-600 dark:text-violet-400" /> Design with AI
    </button>
  )
}

/** compact one-line rendering of a knob value (old → new) */
export function fmtValue(v: unknown, max = 140): string {
  if (v === undefined) return '—'
  const s = typeof v === 'string' ? v : JSON.stringify(v)
  return s.length > max ? s.slice(0, max - 1) + '…' : s
}

export function opLabel(op: AiTopologyOp): string {
  const o = op as Record<string, unknown>
  const adapters = Array.isArray(o.adapters) ? (o.adapters as { switches?: string[]; switch_ports?: string[] }[]) : []
  const ports = adapters.flatMap((a) => (a.switches ?? []).map((sw, i) => `${sw}:${a.switch_ports?.[i] ?? '?'}`))
  switch (op.op) {
    case 'add_endpoint': return `add ${String(o.type ?? 'server').replace(/s$/, '')} ${o.name}${ports.length ? ` → ${ports.join(', ')}` : ''}`
    case 'update_endpoint': return `update ${o.name}${ports.length ? ` → ${ports.join(', ')}` : ''}`
    case 'remove_endpoint': return `remove ${o.name}`
    case 'set_uplink': return `${o.node} uplink ${o.index} → ${o.switch}:${o.switch_port}`
    case 'set_spine_count': return `spines = ${o.n}`
    case 'set_l2leaf_per_pair': return `L2 leafs per pair = ${o.n}`
    default: return op.op.replace(/_/g, ' ')
  }
}

type Msg =
  | { id: number; role: 'user'; content: string }
  | { id: number; role: 'assistant'; out: AiDesignOut; status: 'open' | 'applied' | 'discarded' | 'used' }

let seq = 0

export interface DesignerPanelProps {
  open: boolean
  onClose: () => void
  /** lab mode: proposals are applied to this lab */
  labId?: string
  /** New-lab mode: proposals fill the wizard's knob form */
  template?: string
  currentKnobs?: Record<string, unknown>
  onUseKnobs?: (knobs: Record<string, unknown>) => void
  onApplied?: (res: AiApplyOut) => void
}

/** "Design with AI" side panel: chat → validated proposal card → Apply / Refine / Discard. */
export default function DesignerPanel({ open, onClose, labId, template, currentKnobs, onUseKnobs, onApplied }: DesignerPanelProps) {
  const qc = useQueryClient()
  const link = useQuery({ queryKey: ['ai-link'], queryFn: aiApi.link, enabled: open })
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [prompt, setPrompt] = useState('')
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const endRef = useRef<HTMLDivElement>(null)
  useEffect(() => { endRef.current?.scrollIntoView?.({ block: 'end' }) }, [msgs])

  const history = (): AiChatTurn[] => msgs.map((m): AiChatTurn => (m.role === 'user'
    ? { role: 'user', content: m.content }
    : { role: 'assistant', content: m.out.assistant_message || m.out.summary }))

  const design = useMutation({
    mutationFn: ({ text, turns }: { text: string; turns: AiChatTurn[] }) =>
      aiApi.design({ ...(labId ? { lab_id: labId } : { template, current_knobs: currentKnobs }), prompt: text, history: turns }),
    onSuccess: (out) => setMsgs((m) => [...m, { id: ++seq, role: 'assistant', out, status: 'open' }]),
  })
  const apply = useMutation({
    mutationFn: ({ out }: { id: number; out: AiDesignOut }) => aiApi.apply({ lab_id: labId!, knobs: out.knobs, topology_ops: out.topology_ops }),
    onSuccess: (res, { id }) => {
      setMsgs((m) => m.map((x) => (x.id === id && x.role === 'assistant' ? { ...x, status: 'applied' } : x)))
      if (res.lab) qc.setQueryData(['labs', labId], res.lab)
      qc.invalidateQueries({ queryKey: ['labs'] })
      qc.invalidateQueries({ queryKey: ['topology', labId] })
      onApplied?.(res)
    },
  })

  const send = (e?: FormEvent) => {
    e?.preventDefault()
    const text = prompt.trim()
    if (!text || design.isPending) return
    const turns = history() // the turns *before* this prompt
    setMsgs((m) => [...m, { id: ++seq, role: 'user', content: text }])
    setPrompt('')
    design.mutate({ text, turns })
  }
  const setStatus = (id: number, status: 'discarded' | 'used') => setMsgs((m) => m.map((x) => (x.id === id && x.role === 'assistant' ? { ...x, status } : x)))
  const refine = () => inputRef.current?.focus()

  if (!open) return null
  const noLink = (link.data && !link.data.effective) || (design.error instanceof ApiError && design.error.status === 409
    && (design.error.detail as { code?: string } | null)?.code === 'no_ai_link')
  const modelLabel = link.data?.effective === 'user' ? `${link.data.link?.provider} · ${link.data.link?.model}`
    : link.data?.effective === 'shared' ? `shared · ${link.data.shared.model}` : null

  return (
    <aside className="fixed inset-y-0 right-0 z-40 flex w-full max-w-xl flex-col border-l border-zinc-200 bg-white shadow-xl dark:border-zinc-800 dark:bg-zinc-900"
      data-testid="ai-panel" aria-label="Design with AI">
      <header className="flex items-center gap-2 border-b border-zinc-200 px-4 py-3 dark:border-zinc-800">
        <SparkleIcon className="h-5 w-5 text-violet-600 dark:text-violet-400" />
        <div className="flex-1">
          <div className="font-semibold">Design with AI</div>
          <div className="text-[11px] text-zinc-500">{labId ? 'proposes knob values and topology operations for this lab' : 'fills the knob form of the new lab'}{modelLabel ? ` · ${modelLabel}` : ''}</div>
        </div>
        <button type="button" className="btn-secondary btn-sm" onClick={onClose} data-testid="ai-close" aria-label="close">✕</button>
      </header>

      <div className="flex-1 space-y-3 overflow-y-auto p-4">
        <Hint className="mb-0" title="The AI only proposes AVD inputs.">
          It turns your request into knob values and topology operations; avdlab validates them with pyavd and shows the diff.
          Nothing changes until you press {labId ? 'Apply' : 'Use these knobs'} — and every applied change is a git commit you can revert.
        </Hint>
        {link.isLoading && <Spinner />}
        {noLink ? (
          <div className="rounded border border-violet-300 bg-violet-50 p-3 text-sm dark:border-violet-800 dark:bg-violet-950/30" data-testid="ai-cta">
            <div className="font-medium">Link your Claude / OpenAI / local model</div>
            <p className="mt-1 text-xs text-zinc-600 dark:text-zinc-400">
              The designer uses a model you bring: an Anthropic or OpenAI API key, or a local Ollama / LM Studio / vLLM server.
              Your key is stored encrypted and never shown again.
            </p>
            <Link className="btn-primary btn-sm mt-2 inline-block" to="/account" onClick={onClose} data-testid="ai-cta-link">Link a model on your Account page</Link>
          </div>
        ) : (
          <>
            {msgs.length === 0 && (
              <div className="text-xs text-zinc-500" data-testid="ai-empty">
                Describe what you want, for example: <span className="italic">“3 spines, 4 leaf pairs with MLAG, 2 servers per pair, tenant ACME with VLANs 110/120, CloudVision cvaas”</span>.
              </div>
            )}
            {msgs.map((m) => m.role === 'user'
              ? <div key={m.id} className="ml-auto max-w-[85%] whitespace-pre-wrap rounded-lg bg-blue-600 px-3 py-2 text-sm text-white" data-testid="ai-msg-user">{m.content}</div>
              : <AssistantMessage key={m.id} m={m} labMode={!!labId}
                  applying={apply.isPending && apply.variables?.id === m.id}
                  applyError={apply.variables?.id === m.id ? apply.error : null}
                  onApply={() => apply.mutate({ id: m.id, out: m.out })}
                  onUse={() => { onUseKnobs?.(m.out.knobs); setStatus(m.id, 'used') }}
                  onRefine={refine} onDiscard={() => setStatus(m.id, 'discarded')} />)}
            {design.isPending && <div className="flex items-center gap-2 text-xs text-zinc-500" data-testid="ai-thinking"><Spinner /> thinking and validating…</div>}
            {design.error && !noLink && <ErrorBox error={design.error} />}
          </>
        )}
        <div ref={endRef} />
      </div>

      {!noLink && (
        <form className="border-t border-zinc-200 p-3 dark:border-zinc-800" onSubmit={send}>
          <textarea ref={inputRef} className="input min-h-[64px] w-full resize-y text-sm" value={prompt} onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) send() }}
            placeholder={msgs.length ? 'refine: “use 3 leaf pairs instead”, “add VLAN 130 to ACME” …' : 'what should the lab look like?'} data-testid="ai-prompt" />
          <div className="mt-2 flex items-center gap-2">
            <span className="flex-1 text-[11px] text-zinc-400">Ctrl/⌘+Enter to send · lab data (knobs, topology summary — never secrets) goes to the configured provider</span>
            <button className="btn-primary btn-sm" disabled={!prompt.trim() || design.isPending} data-testid="ai-send">Send</button>
          </div>
        </form>
      )}
    </aside>
  )
}

function AssistantMessage({ m, labMode, applying, applyError, onApply, onUse, onRefine, onDiscard }: {
  m: Extract<Msg, { role: 'assistant' }>
  labMode: boolean
  applying: boolean
  applyError: unknown
  onApply: () => void
  onUse: () => void
  onRefine: () => void
  onDiscard: () => void
}) {
  const { out } = m
  if (out.kind === 'question') {
    return <div className="max-w-[85%] whitespace-pre-wrap rounded-lg bg-zinc-100 px-3 py-2 text-sm dark:bg-zinc-800" data-testid="ai-question">{out.question ?? out.summary}</div>
  }
  if (out.kind === 'text') {
    return <div className="max-w-[85%] whitespace-pre-wrap rounded-lg bg-zinc-100 px-3 py-2 text-sm dark:bg-zinc-800" data-testid="ai-text">{out.text ?? out.summary}</div>
  }
  return <ProposalCard out={out} status={m.status} labMode={labMode} applying={applying} applyError={applyError}
    onApply={onApply} onUse={onUse} onRefine={onRefine} onDiscard={onDiscard} />
}

export function ProposalCard({ out, status, labMode, applying, applyError, onApply, onUse, onRefine, onDiscard }: {
  out: AiDesignOut
  status: 'open' | 'applied' | 'discarded' | 'used'
  labMode: boolean
  applying: boolean
  applyError: unknown
  onApply: () => void
  onUse: () => void
  onRefine: () => void
  onDiscard: () => void
}) {
  const [showDiff, setShowDiff] = useState(false)
  const dry = out.dry_run
  const valid = !!dry?.valid
  const nothing = valid && !out.knob_changes.length && !out.topology_ops.length
  return (
    <div className={cls('card space-y-2 p-3 text-sm', status === 'discarded' && 'opacity-50')} data-testid="ai-proposal">
      <div className="flex items-start gap-2">
        <SparkleIcon className="mt-0.5 h-4 w-4 shrink-0 text-violet-600 dark:text-violet-400" />
        <div className="flex-1 whitespace-pre-wrap" data-testid="ai-summary">{out.summary}</div>
        <span className={cls('rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase', valid ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/60 dark:text-emerald-200' : 'bg-red-100 text-red-800 dark:bg-red-900/60 dark:text-red-200')}
          data-testid="ai-valid">{valid ? 'validated' : 'invalid'}</span>
      </div>
      {out.repaired && <div className="text-[11px] text-zinc-500">the first proposal failed validation; the model fixed it</div>}
      {out.assumptions.length > 0 && (
        <div>
          <div className="text-xs font-medium text-zinc-500">Assumptions</div>
          <ul className="list-disc pl-5 text-xs" data-testid="ai-assumptions">{out.assumptions.map((a, i) => <li key={i}>{a}</li>)}</ul>
        </div>
      )}
      {out.knob_changes.length > 0 && (
        <div>
          <div className="text-xs font-medium text-zinc-500">Knob changes</div>
          <ul className="mono space-y-0.5 text-xs" data-testid="ai-knob-changes">
            {out.knob_changes.map((c) => (
              <li key={c.key} data-testid={`ai-knob-${c.key}`}>
                <span className="font-semibold">{c.key}</span>: <span className="text-red-700 line-through decoration-red-300 dark:text-red-300">{fmtValue(c.old)}</span>
                {' → '}<span className="text-emerald-700 dark:text-emerald-300">{fmtValue(c.new)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {out.topology_ops.length > 0 && (
        <div>
          <div className="text-xs font-medium text-zinc-500">Topology operations</div>
          <ol className="mono list-decimal pl-5 text-xs" data-testid="ai-ops">{out.topology_ops.map((o, i) => <li key={i}>{opLabel(o)}</li>)}</ol>
        </div>
      )}
      {dry && (
        <div className="space-y-1 border-t border-zinc-200 pt-2 dark:border-zinc-800" data-testid="ai-dry-run">
          {!valid && (
            <ul className="list-disc rounded border border-red-300 bg-red-50 px-5 py-1.5 text-xs text-red-800 dark:border-red-800 dark:bg-red-950/40 dark:text-red-200" data-testid="ai-violations">
              {dry.violations.map((v, i) => <li key={i}>{v.path && <span className="mono mr-1">{v.path}:</span>}{v.message}</li>)}
            </ul>
          )}
          {valid && (
            <div className="text-xs text-zinc-600 dark:text-zinc-400">
              pyavd validation passed{dry.changed_files.length ? ` · ${dry.changed_files.length} file(s) change` : ''}
              {dry.graph ? ` · ${dry.graph.nodes.length} nodes, ${dry.graph.links.length} links` : dry.nodes ? ` · ${dry.nodes} switches` : ''}
              {dry.changed_files.length > 0 && <button type="button" className="link ml-2" onClick={() => setShowDiff((s) => !s)} data-testid="ai-diff-toggle">{showDiff ? 'hide diff' : 'show diff'}</button>}
            </div>
          )}
          {valid && dry.changed_files.length > 0 && <div className="mono text-[11px] text-zinc-500" data-testid="ai-changed-files">{dry.changed_files.join(' · ')}</div>}
          {(dry.warnings ?? []).map((w, i) => <div key={i} className="text-[11px] text-amber-700 dark:text-amber-400">{w}</div>)}
          {showDiff && <DiffViewer diff={dry.diff} className="max-h-80 text-[11px]" />}
        </div>
      )}
      <ErrorBox error={applyError} />
      {status === 'open' ? (
        <div className="flex flex-wrap gap-2 pt-1">
          {labMode
            ? <button type="button" className="btn-primary btn-sm" disabled={!valid || nothing || applying} onClick={onApply} data-testid="ai-apply">{applying ? 'Applying…' : 'Apply'}</button>
            : <button type="button" className="btn-primary btn-sm" disabled={!valid || nothing} onClick={onUse} data-testid="ai-use-knobs">Use these knobs</button>}
          <button type="button" className="btn-secondary btn-sm" onClick={onRefine} data-testid="ai-refine">Refine</button>
          <button type="button" className="btn-secondary btn-sm" onClick={onDiscard} data-testid="ai-discard">Discard</button>
        </div>
      ) : (
        <div className="text-xs font-medium text-zinc-500" data-testid="ai-status">
          {status === 'applied' ? 'Applied — committed to the lab’s git. Build the lab to render the configs.' : status === 'used' ? 'Knobs copied into the form — review them, then continue.' : 'Discarded.'}
        </div>
      )}
    </div>
  )
}

