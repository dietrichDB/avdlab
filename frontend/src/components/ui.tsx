import { useEffect, useState, type ReactNode } from 'react'
import { cls } from '../lib/format'
import { useUiMode } from '../lib/uiMode'
import type { JobStatus, LabState } from '../api/types'

// ---- status badges -------------------------------------------------------
const jobColours: Record<JobStatus, string> = {
  queued: 'bg-zinc-200 text-zinc-700 dark:bg-zinc-700 dark:text-zinc-200',
  running: 'bg-blue-100 text-blue-800 dark:bg-blue-900/60 dark:text-blue-200',
  succeeded: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/60 dark:text-emerald-200',
  failed: 'bg-red-100 text-red-800 dark:bg-red-900/60 dark:text-red-200',
  cancelled: 'bg-amber-100 text-amber-800 dark:bg-amber-900/60 dark:text-amber-200',
}

const labColours: Record<LabState, string> = {
  draft: 'bg-zinc-200 text-zinc-700 dark:bg-zinc-700 dark:text-zinc-200',
  built: 'bg-blue-100 text-blue-800 dark:bg-blue-900/60 dark:text-blue-200',
  deployed: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/60 dark:text-emerald-200',
  validated: 'bg-emerald-200 text-emerald-900 dark:bg-emerald-800/60 dark:text-emerald-100',
  destroyed: 'bg-amber-100 text-amber-800 dark:bg-amber-900/60 dark:text-amber-200',
}

export function Badge({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <span className={cls('badge', className)}>
      {children}
    </span>
  )
}

export function JobStatusBadge({ status }: { status: JobStatus | string }) {
  const c = jobColours[status as JobStatus] ?? jobColours.queued
  return (
    <Badge className={c} data-testid="job-status">
      {status === 'running' && <span className="mr-1 inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-current" />}
      {status}
    </Badge>
  )
}

export function LabStateBadge({ state }: { state: LabState | string }) {
  return <Badge className={labColours[state as LabState] ?? labColours.draft}>{state}</Badge>
}

// ---- page bits -----------------------------------------------------------
export function PageHeader({ title, actions, sub }: { title: ReactNode; sub?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
      <div className="min-w-0">
        <h1 className="h1">{title}</h1>
        {sub && <div className="mt-0.5 text-[13px] text-cv-muted">{sub}</div>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="card p-6 text-center text-[13px] text-cv-muted">{children}</div>
}

export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null
  const msg = error instanceof Error ? error.message : String(error)
  return (
    <div className="rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-800 dark:border-red-800 dark:bg-red-950/40 dark:text-red-200">
      {msg}
    </div>
  )
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 p-4 text-[13px] text-cv-muted">
      <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-cv-primary border-t-transparent" />
      {label ?? 'Loading…'}
    </div>
  )
}

export function Field({ label, help, children, error }: { label: ReactNode; help?: ReactNode; error?: ReactNode; children: ReactNode }) {
  return (
    <label className="block">
      <span className="label">{label}</span>
      {children}
      {help && <span className="mt-1 block text-[11px] text-cv-muted">{help}</span>}
      {error && <span className="mt-0.5 block text-[11px] text-red-600 dark:text-red-400">{error}</span>}
    </label>
  )
}

export function KV({ items }: { items: [ReactNode, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
      {items.map(([k, v], i) => (
        <div key={i} className="contents">
          <dt className="text-cv-muted">{k}</dt>
          <dd className="min-w-0 break-words">{v ?? '—'}</dd>
        </div>
      ))}
    </dl>
  )
}

// ---- modal ---------------------------------------------------------------
export function Modal({ open, onClose, title, children, width = 'max-w-lg' }: {
  open: boolean; onClose: () => void; title: ReactNode; children: ReactNode; width?: string
}) {
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])
  if (!open) return null
  return (
    <div className="fixed inset-0 z-40 flex items-start justify-center bg-cv-nav/50 p-4 pt-16 backdrop-blur-[1px]" onMouseDown={onClose} role="dialog" aria-modal>
      <div className={cls('card w-full shadow-xl', width)} onMouseDown={(e) => e.stopPropagation()}>
        <div className="card-header">
          <div className="font-semibold">{title}</div>
          <button className="text-lg leading-none text-cv-muted hover:text-cv-text" onClick={onClose} aria-label="Close">×</button>
        </div>
        <div className="p-4">{children}</div>
      </div>
    </div>
  )
}

export function CopyButton({ text, label = 'Copy' }: { text: string; label?: string }) {
  const [done, setDone] = useState(false)
  return (
    <button
      type="button"
      className="btn-secondary btn-sm"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text)
          setDone(true)
          setTimeout(() => setDone(false), 1500)
        } catch { /* clipboard unavailable */ }
      }}
    >
      {done ? 'Copied' : label}
    </button>
  )
}

export function Tabs<T extends string>({ tabs, value, onChange, extra }: {
  tabs: { id: T; label: ReactNode }[]; value: T; onChange: (t: T) => void
  /** rendered after the last tab, right-aligned (e.g. the beginner-mode "Show advanced" link) */
  extra?: ReactNode
}) {
  return (
    <div className="mb-4 flex flex-wrap items-end gap-x-1 border-b border-cv-border" role="tablist">
      {tabs.map((t) => (
        <button
          key={t.id}
          role="tab"
          aria-selected={t.id === value}
          data-tour={`${t.id}-tab`}
          onClick={() => onChange(t.id)}
          className={cls(
            '-mb-px border-b-2 px-3 py-2 text-[13px] transition-colors',
            t.id === value
              ? 'border-cv-primary font-semibold text-cv-primary-text'
              : 'border-transparent text-cv-muted hover:border-cv-border-strong hover:text-cv-text',
          )}
        >
          {t.label}
        </button>
      ))}
      {extra && <div className="mb-1.5 ml-auto pl-2">{extra}</div>}
    </div>
  )
}

export function ConfirmButton({ onConfirm, children, className = 'btn-danger btn-sm', confirmText = 'Confirm?' }: {
  onConfirm: () => void; children: ReactNode; className?: string; confirmText?: string
}) {
  const [arm, setArm] = useState(false)
  useEffect(() => {
    if (!arm) return
    const t = setTimeout(() => setArm(false), 3000)
    return () => clearTimeout(t)
  }, [arm])
  return (
    <button type="button" className={className} onClick={() => (arm ? (setArm(false), onConfirm()) : setArm(true))}>
      {arm ? confirmText : children}
    </button>
  )
}

// ---- beginner hints (CONTRACT-8 B §4) -----------------------------------------------------------------------------
/** Light-blue ⓘ callout that only renders in beginner mode: what the page does and what to do first. */
export function Hint({ children, title, className }: { children: ReactNode; title?: ReactNode; className?: string }) {
  const { beginner } = useUiMode()
  if (!beginner) return null
  return (
    <div className={cls('hint mb-4', className)} data-testid="hint" role="note">
      <svg viewBox="0 0 24 24" className="mt-px h-4 w-4 shrink-0" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" aria-hidden>
        <circle cx="12" cy="12" r="9" /><path d="M12 16v-4M12 8h.01" />
      </svg>
      <div className="min-w-0">
        {title && <span className="font-semibold">{title} </span>}
        {children}
      </div>
    </div>
  )
}
