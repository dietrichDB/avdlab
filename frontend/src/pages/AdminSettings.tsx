import { useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { adminApi } from '../api/admin'
import type { AppSettings, AppSettingsIn, AppSettingsValues } from '../api/types'
import { useAuth } from '../hooks/useAuth'
import { ErrorBox, Field, PageHeader, Spinner } from '../components/ui'
import { errorText } from './Account'
import { AiSharedSection } from '../components/ai/AiLinkForm'

type Draft = AppSettingsValues

const KEYS: (keyof Draft)[] = [
  'app_title', 'github_url', 'support_email', 'registration_enabled', 'intro_video_url', 'feedback_mail_to', 'feedback_mail_enabled',
  'registration_mail_to', 'smtp_host', 'smtp_port', 'smtp_user', 'smtp_starttls', 'smtp_from',
]

function draftOf(s: AppSettings): Draft {
  const d = {} as Record<string, unknown>
  for (const k of KEYS) d[k] = s[k]
  return d as unknown as Draft
}

function Section({ id, title, sub, children }: { id?: string; title: string; sub?: ReactNode; children: ReactNode }) {
  return (
    <section id={id} className="card scroll-mt-4 space-y-3 p-4">
      <div>
        <div className="h2">{title}</div>
        {sub && <div className="text-xs text-zinc-500 dark:text-zinc-400">{sub}</div>}
      </div>
      {children}
    </section>
  )
}

function Toggle({ label, checked, onChange, testId, help }: { label: string; checked: boolean; onChange: (v: boolean) => void; testId: string; help?: string }) {
  return (
    <label className="flex items-start gap-2 text-sm">
      <input type="checkbox" className="mt-0.5" checked={checked} onChange={(e) => onChange(e.target.checked)} data-testid={testId} />
      <span>{label}{help && <span className="block text-[11px] text-zinc-500 dark:text-zinc-400">{help}</span>}</span>
    </label>
  )
}

export default function AdminSettingsPage() {
  const { role } = useAuth()
  const qc = useQueryClient()
  const q = useQuery({ queryKey: ['admin-settings'], queryFn: adminApi.settings, enabled: role === 'admin' })
  const [draft, setDraft] = useState<Draft | null>(null)
  const [password, setPassword] = useState('')
  const [clearPassword, setClearPassword] = useState(false)
  const [saved, setSaved] = useState(false)

  useEffect(() => { if (q.data && !draft) setDraft(draftOf(q.data)) }, [q.data, draft])

  const save = useMutation({
    mutationFn: () => {
      const body: AppSettingsIn = {}
      const cur = q.data!
      for (const k of KEYS) {
        if (draft![k] !== cur[k]) (body as Record<string, unknown>)[k] = draft![k]
      }
      if (clearPassword) body.smtp_password = ''
      else if (password) body.smtp_password = password
      return adminApi.saveSettings(body)
    },
    onSuccess: (s) => {
      qc.setQueryData(['admin-settings'], s)
      qc.invalidateQueries({ queryKey: ['meta'] })
      setDraft(draftOf(s))
      setPassword('')
      setClearPassword(false)
      setSaved(true)
    },
  })
  const test = useMutation({ mutationFn: adminApi.testMail })

  if (role !== 'admin') return <ErrorBox error="Only admins can change the settings." />
  if (q.isLoading || !draft) return q.error ? <ErrorBox error={q.error} /> : <Spinner />
  const s = q.data!
  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => { setSaved(false); setDraft({ ...draft, [k]: v }) }
  const dirty = KEYS.some((k) => draft[k] !== s[k]) || !!password || clearPassword
  const submit = (e: FormEvent) => { e.preventDefault(); save.mutate() }
  const def = (k: keyof Draft) => {
    const v = s.defaults?.[k]
    return v === undefined || v === '' ? undefined : `default: ${String(v)}`
  }

  return (
    <div className="max-w-3xl space-y-4">
    <form className="space-y-4" onSubmit={submit} data-testid="admin-settings">
      <PageHeader title="Admin settings" sub="stored in the database; unset values fall back to the AVDLAB_* environment"
        actions={<Link className="btn-secondary" to="/users">Users</Link>} />
      <div className="sticky top-0 z-20 flex flex-wrap items-center gap-3 rounded-md border border-cv-border bg-cv-surface/95 px-3 py-2 shadow-sm backdrop-blur">
        <button className="btn-primary" disabled={!dirty || save.isPending} data-testid="settings-save">{save.isPending ? 'Saving…' : 'Save project & e-mail settings'}</button>
        {dirty && <span className="rounded bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-800 dark:bg-amber-900/50 dark:text-amber-200">unsaved changes</span>}
        {saved && !dirty && <span className="text-xs text-emerald-700 dark:text-emerald-400" data-testid="settings-saved">saved</span>}
        <span className="text-xs text-cv-muted">the AI assistant card below has its own Save</span>
      </div>
      <nav className="flex flex-wrap gap-2 text-xs" aria-label="sections" data-testid="settings-nav">
        {[['project', 'Project'], ['email', 'E-mail'], ['ai', 'AI assistant']].map(([id, label]) => (
          <a key={id} href={`#${id}`} className="rounded-full border border-cv-border bg-cv-surface px-3 py-1 text-cv-muted hover:text-cv-text">{label}</a>
        ))}
      </nav>

      <Section id="project" title="Project" sub="public: shown on the dashboard, the top bar, the register and About pages">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="App title" help={def('app_title')}>
            <input className="input" value={draft.app_title} onChange={(e) => set('app_title', e.target.value)} data-testid="set-app_title" />
          </Field>
          <Field label="GitHub URL" help="empty hides the GitHub button">
            <input className="input" value={draft.github_url} onChange={(e) => set('github_url', e.target.value)} placeholder="https://github.com/owner/avdlab" data-testid="set-github_url" />
          </Field>
          <Field label="Support e-mail" help="shown on the register and About pages">
            <input className="input" type="email" value={draft.support_email} onChange={(e) => set('support_email', e.target.value)} data-testid="set-support_email" />
          </Field>
          <Field label="Intro video URL" help="dashboard player; empty hides it">
            <input className="input" value={draft.intro_video_url} onChange={(e) => set('intro_video_url', e.target.value)} data-testid="set-intro_video_url" />
          </Field>
        </div>
        <Toggle label="Registration enabled" help="the sign-in page offers “Request an account”; requests appear on the Users page"
          checked={draft.registration_enabled} onChange={(v) => set('registration_enabled', v)} testId="set-registration_enabled" />
      </Section>

      <Section id="email" title="E-mail" sub={s.smtp_configured ? 'SMTP is configured' : 'SMTP is not configured: no e-mail is sent until an SMTP host is set'}>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="SMTP host" help="empty = no e-mail">
            <input className="input" value={draft.smtp_host} onChange={(e) => set('smtp_host', e.target.value)} placeholder="smtp.example.com" data-testid="set-smtp_host" />
          </Field>
          <Field label="SMTP port" help="587 = STARTTLS, 465 = implicit TLS">
            <input className="input" type="number" min={1} max={65535} value={draft.smtp_port}
              onChange={(e) => set('smtp_port', Number(e.target.value) || 0)} data-testid="set-smtp_port" />
          </Field>
          <Field label="SMTP user" help="empty = no login">
            <input className="input" value={draft.smtp_user} onChange={(e) => set('smtp_user', e.target.value)} autoComplete="off" data-testid="set-smtp_user" />
          </Field>
          <Field label="SMTP password" help={s.has_smtp_password ? 'a password is stored (encrypted) — type to replace it' : 'stored encrypted in the secret store; never shown again'}>
            <input className="input" type="password" value={password} onChange={(e) => { setSaved(false); setPassword(e.target.value); setClearPassword(false) }}
              placeholder={s.has_smtp_password ? '•••••••• (set)' : ''} autoComplete="new-password" data-testid="set-smtp_password" />
            {s.has_smtp_password && (
              <label className="mt-1 flex items-center gap-1 text-[11px] text-zinc-500">
                <input type="checkbox" checked={clearPassword} onChange={(e) => { setSaved(false); setClearPassword(e.target.checked); setPassword('') }} /> clear the stored password
              </label>
            )}
          </Field>
          <Field label="From address" help={`default: ${s.defaults?.smtp_from ?? 'avdlab@<host>'}`}>
            <input className="input" value={draft.smtp_from} onChange={(e) => set('smtp_from', e.target.value)} data-testid="set-smtp_from" />
          </Field>
          <div className="flex items-end pb-2">
            <Toggle label="Use STARTTLS" help="ignored on port 465" checked={draft.smtp_starttls} onChange={(v) => set('smtp_starttls', v)} testId="set-smtp_starttls" />
          </div>
          <Field label="Feedback e-mail to" help="comma-separated; also receives the test mail">
            <input className="input" value={draft.feedback_mail_to} onChange={(e) => set('feedback_mail_to', e.target.value)} data-testid="set-feedback_mail_to" />
          </Field>
          <Field label="Account requests e-mail to" help="empty = the feedback address">
            <input className="input" value={draft.registration_mail_to} onChange={(e) => set('registration_mail_to', e.target.value)} data-testid="set-registration_mail_to" />
          </Field>
        </div>
        <Toggle label="Mail every new feedback entry" checked={draft.feedback_mail_enabled} onChange={(v) => set('feedback_mail_enabled', v)} testId="set-feedback_mail_enabled" />
        <div className="flex flex-wrap items-center gap-2 border-t border-zinc-200 pt-3 dark:border-zinc-800">
          <button type="button" className="btn-secondary" onClick={() => test.mutate()} disabled={test.isPending || dirty} data-testid="test-mail"
            title={dirty ? 'save first' : undefined}>
            {test.isPending ? 'Sending…' : 'Send test mail'}
          </button>
          {test.data && (
            <span className={`text-xs ${test.data.ok ? 'text-emerald-700 dark:text-emerald-400' : 'text-red-600 dark:text-red-400'}`} data-testid="test-mail-result">
              {test.data.ok ? `sent to ${test.data.to}` : `failed: ${test.data.error}`}
            </span>
          )}
          <ErrorBox error={test.error} />
        </div>
      </Section>

      <ErrorBox error={errorText(save.error)} />
    </form>
    {/* v0.10.0: AI designer shared default (own endpoint /admin/ai, saved separately) */}
    <div id="ai" className="scroll-mt-4"><AiSharedSection /></div>
    </div>
  )
}
