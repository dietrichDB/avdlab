import { createContext, useCallback, useContext, useState, type ReactNode } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useLocation } from 'react-router-dom'
import { feedbackApi } from '../api/phase5'
import type { FeedbackCreated, FeedbackKind } from '../api/types'
import { useMeta } from '../hooks/useMeta'
import { diagnosticsPreview, feedbackErrors, feedbackMailto, feedbackPayload, type FeedbackForm } from '../lib/feedback'
import { AuthCtx } from '../hooks/useAuth'
import { ErrorBox, Field, Modal } from './ui'

interface Toast { id: number; text: ReactNode; tone: 'ok' | 'error' }
const Ctx = createContext<{ open: () => void; toast: (t: ReactNode, tone?: Toast['tone']) => void }>({ open: () => {}, toast: () => {} })
export const useFeedback = () => useContext(Ctx)

export function FeedbackProvider({ children }: { children: ReactNode }) {
  const [isOpen, setOpen] = useState(false)
  const [toasts, setToasts] = useState<Toast[]>([])
  const toast = useCallback((text: ReactNode, tone: Toast['tone'] = 'ok') => {
    const id = Date.now() + Math.random()
    setToasts((t) => [...t, { id, text, tone }])
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 6000)
  }, [])
  const open = useCallback(() => setOpen(true), [])
  const close = useCallback(() => setOpen(false), [])
  return (
    <Ctx.Provider value={{ open, toast }}>
      {children}
      <button className="fixed bottom-4 right-4 z-30 rounded-full bg-cv-primary px-3 py-1 text-[12px] font-medium text-white opacity-80 shadow-lg hover:opacity-100 hover:bg-cv-primary-hover print:hidden" onClick={open} data-testid="feedback-fab">
        Feedback
      </button>
      {isOpen && <FeedbackModal onClose={close} onSent={(r) => toast(
        <span>Thanks — feedback #{r.id} sent{r.github_issue_url ? <> · <a className="underline" href={r.github_issue_url} target="_blank" rel="noreferrer">GitHub issue</a></> : null}.</span>,
      )} />}
      <div className="fixed bottom-16 right-4 z-50 space-y-2" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} role="status" data-testid="toast"
            className={t.tone === 'ok' ? 'rounded bg-emerald-600 px-3 py-2 text-sm text-white shadow-lg' : 'rounded bg-red-600 px-3 py-2 text-sm text-white shadow-lg'}>{t.text}</div>
        ))}
      </div>
    </Ctx.Provider>
  )
}

export function FeedbackModal({ onClose, onSent }: { onClose: () => void; onSent: (r: FeedbackCreated) => void }) {
  const loc = useLocation()
  const meta = useMeta()
  const qc = useQueryClient()
  const [f, setF] = useState<FeedbackForm>({ kind: 'bug', title: '', body: '', page: loc.pathname + loc.search, include_diagnostics: true })
  const [touched, setTouched] = useState(false)
  const [showDiag, setShowDiag] = useState(false)
  const errors = feedbackErrors(f)
  const send = useMutation({
    mutationFn: () => feedbackApi.create(feedbackPayload(f)),
    onSuccess: (r) => { qc.invalidateQueries({ queryKey: ['feedback'] }); onSent(r); onClose() },
  })
  const set = (p: Partial<FeedbackForm>) => setF((x) => ({ ...x, ...p }))
  const ua = typeof navigator !== 'undefined' ? navigator.userAgent : ''
  const user = useContext(AuthCtx)?.user ?? null  // optional: the modal also renders outside AuthProvider (tests)
  const mailTo = meta.data?.feedback_email ?? meta.data?.support_email ?? null
  // what the user was looking at: the breadcrumb trail (e.g. "Labs › evpn-lab › Topology") and the page title
  const where = () => {
    const crumbs = typeof document !== 'undefined' ? Array.from(document.querySelectorAll('[data-testid=breadcrumbs] a, [data-testid=breadcrumbs] span')).map((e) => e.textContent?.trim()).filter(Boolean) : []
    return [...(crumbs.length ? [crumbs.join(' › ')] : []), ...(typeof document !== 'undefined' && document.title ? [document.title] : [])]
  }
  const mailHref = mailTo ? feedbackMailto(mailTo, f, { version: meta.data?.version, user, userAgent: ua, href: typeof window !== 'undefined' ? window.location.href : undefined, context: where() }) : null
  return (
    <Modal open onClose={onClose} title="Send feedback">
      <form className="space-y-3" data-testid="feedback-form" onSubmit={(e) => { e.preventDefault(); setTouched(true); if (!errors.length) send.mutate() }}>
        <div className="flex gap-3 text-sm" role="radiogroup" aria-label="kind">
          {(['bug', 'suggestion', 'question'] as FeedbackKind[]).map((k) => (
            <label key={k} className="inline-flex items-center gap-1"><input type="radio" name="kind" checked={f.kind === k} onChange={() => set({ kind: k })} data-testid={`fb-kind-${k}`} /> {k}</label>
          ))}
        </div>
        <Field label="Title"><input className="input" value={f.title} onChange={(e) => set({ title: e.target.value })} data-testid="fb-title" autoFocus /></Field>
        <Field label="Description"><textarea className="input min-h-[6rem]" value={f.body} onChange={(e) => set({ body: e.target.value })} data-testid="fb-body"
          placeholder={f.kind === 'bug' ? 'What happened, what did you expect, steps to reproduce…' : ''} /></Field>
        <Field label="Page"><input className="input mono" value={f.page} onChange={(e) => set({ page: e.target.value })} data-testid="fb-page" /></Field>
        <div className="text-sm">
          <label className="inline-flex items-center gap-1.5"><input type="checkbox" checked={f.include_diagnostics} onChange={(e) => set({ include_diagnostics: e.target.checked })} data-testid="fb-diag" /> Include diagnostics</label>
          {f.include_diagnostics && <button type="button" className="link ml-2 text-xs" onClick={() => setShowDiag((v) => !v)} data-testid="fb-diag-toggle">{showDiag ? 'hide' : 'preview what is sent'}</button>}
          {f.include_diagnostics && showDiag && (
            <pre className="mt-1 max-h-48 overflow-auto rounded bg-zinc-100 p-2 font-mono text-[11px] dark:bg-zinc-800" data-testid="fb-diag-preview">{JSON.stringify(diagnosticsPreview(meta.data, ua, f.page), null, 2)}</pre>
          )}
        </div>
        {touched && errors.length > 0 && <ul className="list-disc pl-5 text-xs text-red-600 dark:text-red-400">{errors.map((e) => <li key={e}>{e}</li>)}</ul>}
        <ErrorBox error={send.error} />
        <div className="flex flex-wrap items-center justify-end gap-2">
          {mailHref && (
            <a className="btn-secondary mr-auto" href={mailHref} data-testid="fb-mailto" title={`opens your e-mail client with ${mailTo} and the page context prefilled`}
              onClick={() => setTimeout(onClose, 300)}>✉ E-mail {mailTo}</a>
          )}
          <button type="button" className="btn-secondary" onClick={onClose}>Cancel</button>
          <button className="btn-primary" disabled={send.isPending} data-testid="fb-send">{send.isPending ? 'Sending…' : 'Send'}</button>
        </div>
      </form>
    </Modal>
  )
}
