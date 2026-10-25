import { useState, type FormEvent } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { authApi } from '../api/auth'
import { ApiError } from '../api/client'
import type { PasswordPolicy } from '../api/types'
import { useAuth } from '../hooks/useAuth'
import { Badge, CopyButton, ErrorBox, Field, PageHeader } from '../components/ui'
import { cls } from '../lib/format'
import { AiLinkSection } from '../components/ai/AiLinkForm'

const DEFAULT_POLICY: PasswordPolicy = { min_length: 12, classes_required: 3, max_repeat: 3 }

export interface PolicyCheck { id: string; label: string; ok: boolean }

/** Client-side mirror of `core/passwords.py` (the server is authoritative and also checks a deny-list). */
export function policyChecks(pw: string, username: string, policy: PasswordPolicy = DEFAULT_POLICY): PolicyCheck[] {
  const classes = [/[a-z]/, /[A-Z]/, /[0-9]/, /[^A-Za-z0-9]/].filter((rx) => rx.test(pw)).length
  const u = username.trim().toLowerCase()
  const runRx = new RegExp(`(.)\\1{${policy.max_repeat},}`)
  return [
    { id: 'length', label: `at least ${policy.min_length} characters`, ok: pw.length >= policy.min_length },
    { id: 'classes', label: `${policy.classes_required} of: lowercase, uppercase, digit, symbol`, ok: classes >= policy.classes_required },
    { id: 'username', label: 'does not contain your username', ok: !pw || u.length < 3 || !pw.toLowerCase().includes(u) },
    { id: 'repeat', label: `no more than ${policy.max_repeat} identical characters in a row`, ok: !!pw && !runRx.test(pw) },
  ]
}

/** ApiError with a `{message, violations}` 422 body → readable text. */
export function errorText(e: unknown): string | null {
  if (!e) return null
  if (e instanceof ApiError) {
    const v = e.violations.map((x) => x.message)
    if (e.status === 422 && v.length) return v.join('; ')
  }
  return e instanceof Error ? e.message : String(e)
}

export function PolicyChecklist({ password, username, policy }: { password: string; username: string; policy?: PasswordPolicy }) {
  return (
    <ul className="space-y-0.5 text-xs" data-testid="policy-checklist">
      {policyChecks(password, username, policy).map((c) => (
        <li key={c.id} data-testid={`policy-${c.id}`} data-ok={c.ok ? '1' : '0'} className={c.ok ? 'text-emerald-700 dark:text-emerald-400' : 'text-zinc-500'}>
          {c.ok ? '✓' : '○'} {c.label}
        </li>
      ))}
      <li className="text-zinc-400">not a commonly used password (checked by the server)</li>
    </ul>
  )
}

/** Change-password form — used on /account and as the forced first-login screen. */
export function ChangePasswordForm({ onDone, submitLabel = 'Change password' }: { onDone?: () => void; submitLabel?: string }) {
  const { user, apply } = useAuth()
  const policy = useQuery({ queryKey: ['auth-policy'], queryFn: authApi.policy, staleTime: Infinity })
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [done, setDone] = useState(false)
  const checks = policyChecks(next, user ?? '', policy.data)
  const valid = checks.every((c) => c.ok) && next === confirm && !!current
  const m = useMutation({
    mutationFn: () => authApi.changePassword(current, next),
    onSuccess: (me) => {
      setCurrent(''); setNext(''); setConfirm(''); setDone(true)
      apply(me)
      onDone?.()
    },
  })
  const submit = (e: FormEvent) => { e.preventDefault(); setDone(false); if (valid) m.mutate() }
  return (
    <form onSubmit={submit} className="space-y-3" data-testid="change-password-form">
      <Field label="Current password">
        <input className="input" type="password" value={current} onChange={(e) => setCurrent(e.target.value)} autoComplete="current-password" data-testid="pw-current" />
      </Field>
      <Field label="New password">
        <input className="input" type="password" value={next} onChange={(e) => setNext(e.target.value)} autoComplete="new-password" data-testid="pw-new" />
      </Field>
      <PolicyChecklist password={next} username={user ?? ''} policy={policy.data} />
      <Field label="Repeat new password" error={confirm && confirm !== next ? 'passwords do not match' : undefined}>
        <input className="input" type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="new-password" data-testid="pw-confirm" />
      </Field>
      <ErrorBox error={errorText(m.error)} />
      {done && <div className="text-sm text-emerald-700 dark:text-emerald-400">Password changed — other sessions were signed out.</div>}
      <button className="btn-primary" disabled={!valid || m.isPending} data-testid="pw-submit">{m.isPending ? 'Saving…' : submitLabel}</button>
    </form>
  )
}

export function RecoveryCodesPanel({ codes }: { codes: string[] }) {
  const text = codes.join('\n') + '\n'
  return (
    <div className="space-y-2 rounded border border-amber-300 bg-amber-50 p-3 dark:border-amber-800 dark:bg-amber-950/30" data-testid="recovery-codes">
      <div className="text-sm font-medium">Recovery codes — shown only once</div>
      <div className="text-xs text-zinc-600 dark:text-zinc-400">Each code signs you in once if you lose your phone. Store them somewhere safe.</div>
      <ol className="mono grid grid-cols-2 gap-x-6 gap-y-0.5 text-sm">
        {codes.map((c) => <li key={c}>{c}</li>)}
      </ol>
      <div className="flex gap-2">
        <CopyButton text={text} label="Copy codes" />
        <a className="btn-secondary btn-sm" download="avdlab-recovery-codes.txt" href={`data:text/plain;charset=utf-8,${encodeURIComponent(text)}`}>Download</a>
      </div>
    </div>
  )
}

function MfaSection() {
  const { mfaEnabled, refresh } = useAuth()
  const [code, setCode] = useState('')
  const [password, setPassword] = useState('')
  const [regenPw, setRegenPw] = useState('')
  const [codes, setCodes] = useState<string[] | null>(null)
  const setup = useMutation({ mutationFn: authApi.mfaSetup, onSuccess: () => setCodes(null) })
  const enable = useMutation({
    mutationFn: () => authApi.mfaEnable(code),
    onSuccess: (r) => { setCodes(r.recovery_codes); setCode(''); setup.reset(); refresh() },
  })
  const disable = useMutation({
    mutationFn: () => authApi.mfaDisable(password, code),
    onSuccess: () => { setCode(''); setPassword(''); setCodes(null); refresh() },
  })
  const regen = useMutation({
    mutationFn: () => authApi.regenerateRecoveryCodes(regenPw),
    onSuccess: (r) => { setCodes(r.recovery_codes); setRegenPw('') },
  })

  return (
    <section className="card space-y-3 p-4" data-testid="mfa-section">
      <div className="flex items-center gap-2">
        <h2 className="font-semibold">Two-factor authentication</h2>
        <Badge className={mfaEnabled ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/60 dark:text-emerald-200' : 'bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300'}>
          {mfaEnabled ? 'enabled' : 'off'}
        </Badge>
      </div>
      {codes && <RecoveryCodesPanel codes={codes} />}
      {!mfaEnabled && !setup.data && (
        <div className="space-y-2">
          <p className="text-sm text-zinc-600 dark:text-zinc-400">Use an authenticator app (Microsoft/Google Authenticator, 1Password, …) for a 6-digit code at sign-in.</p>
          <button className="btn-primary" onClick={() => setup.mutate()} disabled={setup.isPending} data-testid="mfa-setup">Set up</button>
        </div>
      )}
      {!mfaEnabled && setup.data && (
        <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); enable.mutate() }}>
          <p className="text-sm">Scan the QR code with your authenticator app, then enter the 6-digit code it shows.</p>
          {/* SVG generated by our own backend (qrcode lib), never user-supplied */}
          <div className="w-48 rounded bg-white p-2" data-testid="mfa-qr" dangerouslySetInnerHTML={{ __html: setup.data.qr_svg }} />
          <div className="text-xs text-zinc-500">Can’t scan? Enter this key: <span className="mono select-all text-zinc-800 dark:text-zinc-200" data-testid="mfa-secret">{setup.data.secret}</span></div>
          <Field label="Code from the app">
            <input className="input w-32" inputMode="numeric" maxLength={6} value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))} data-testid="mfa-enable-code" />
          </Field>
          <ErrorBox error={enable.error} />
          <div className="flex gap-2">
            <button className="btn-primary" disabled={code.length !== 6 || enable.isPending} data-testid="mfa-enable">Enable</button>
            <button type="button" className="btn-secondary" onClick={() => setup.reset()}>Cancel</button>
          </div>
        </form>
      )}
      {mfaEnabled && (
        <div className="grid gap-4 md:grid-cols-2">
          <form className="space-y-2" onSubmit={(e) => { e.preventDefault(); disable.mutate() }}>
            <div className="text-sm font-medium">Disable</div>
            <Field label="Password"><input className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} data-testid="mfa-password" /></Field>
            <Field label="Code (app or recovery code)"><input className="input" value={code} onChange={(e) => setCode(e.target.value)} data-testid="mfa-disable-code" /></Field>
            <ErrorBox error={disable.error} />
            <button className="btn-danger" disabled={!password || !code || disable.isPending} data-testid="mfa-disable">Disable MFA</button>
          </form>
          <form className="space-y-2" onSubmit={(e) => { e.preventDefault(); regen.mutate() }}>
            <div className="text-sm font-medium">Recovery codes</div>
            <p className="text-xs text-zinc-500">Generating new codes invalidates all previous ones.</p>
            <Field label="Password"><input className="input" type="password" value={regenPw} onChange={(e) => setRegenPw(e.target.value)} data-testid="mfa-regen-password" /></Field>
            <ErrorBox error={regen.error} />
            <button className="btn-secondary" disabled={!regenPw || regen.isPending} data-testid="mfa-regenerate">Regenerate recovery codes</button>
          </form>
        </div>
      )}
    </section>
  )
}

export default function AccountPage() {
  const { user, role } = useAuth()
  return (
    <div className="max-w-3xl space-y-4">
      <PageHeader title="Account" sub={<span>signed in as <b>{user}</b> · role <span className={cls('mono')}>{role}</span></span>} />
      <section className="card max-w-md p-4">
        <h2 className="mb-2 font-semibold">Change password</h2>
        <ChangePasswordForm />
      </section>
      <MfaSection />
      <AiLinkSection />
    </div>
  )
}
