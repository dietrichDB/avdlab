import { useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { registrationApi } from '../api/admin'
import type { AccountRequestStatus, RegisterResult } from '../api/types'
import { useMeta } from '../hooks/useMeta'
import { Logo } from '../components/Brand'
import { ErrorBox, Field, Spinner } from '../components/ui'
import { errorText } from './Account'

const USERNAME_RE = /^[a-z0-9._-]{3,32}$/
const EMAIL_RE = /^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$/
const STORE_KEY = 'avdlab.accountRequest'

function GitHubButton({ url }: { url: string }) {
  return (
    <a className="btn-secondary btn-sm" href={url} target="_blank" rel="noreferrer" data-testid="register-github">
      <svg viewBox="0 0 16 16" className="h-4 w-4" fill="currentColor" aria-hidden="true">
        <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z" />
      </svg>
      GitHub
    </a>
  )
}

function loadStored(): RegisterResult | null {
  try {
    const raw = localStorage.getItem(STORE_KEY)
    return raw ? (JSON.parse(raw) as RegisterResult) : null
  } catch {
    return null
  }
}

const STATUS_TEXT: Record<AccountRequestStatus, string> = {
  pending: 'waiting for an administrator',
  approved: 'approved — check your e-mail for the temporary password, or ask the administrator',
  rejected: 'not approved',
}

function Submitted({ req, supportEmail, onNew }: { req: RegisterResult; supportEmail?: string | null; onNew: () => void }) {
  const status = useQuery({
    queryKey: ['register-status', req.id],
    queryFn: () => registrationApi.status(req.id),
    refetchInterval: (q) => (q.state.data?.status === 'pending' ? 30_000 : false),
    retry: false,
  })
  const st = status.data?.status ?? req.status
  return (
    <div className="space-y-3" data-testid="register-success">
      <div className="font-semibold">Request sent</div>
      <p className="text-sm text-zinc-600 dark:text-zinc-400">
        Thanks! An administrator reviews your request. You'll receive an e-mail with your sign-in details once it is
        approved — or the administrator will contact you directly.
      </p>
      <div className="text-sm">
        Status: <b data-testid="register-status">{st}</b> <span className="text-xs text-zinc-500">({STATUS_TEXT[st]})</span>
      </div>
      {supportEmail && (
        <p className="text-xs text-zinc-500">Questions? <a className="link" href={`mailto:${supportEmail}`}>{supportEmail}</a></p>
      )}
      <div className="flex items-center justify-between text-xs">
        <Link className="link" to="/login">Back to sign in</Link>
        <button type="button" className="link" onClick={onNew}>Send another request</button>
      </div>
    </div>
  )
}

export default function RegisterPage() {
  const meta = useMeta()
  const m = meta.data
  const [done, setDone] = useState<RegisterResult | null>(loadStored)
  const [username, setUsername] = useState('')
  const [email, setEmail] = useState('')
  const [fullName, setFullName] = useState('')
  const [reason, setReason] = useState('')
  const reg = useMutation({
    mutationFn: () => registrationApi.register({ username: username.trim().toLowerCase(), email: email.trim(), full_name: fullName.trim(), reason: reason.trim() }),
    onSuccess: (r) => {
      setDone(r)
      try { localStorage.setItem(STORE_KEY, JSON.stringify(r)) } catch { /* storage unavailable */ }
    },
  })
  const name = username.trim().toLowerCase()
  const nameErr = name && !USERNAME_RE.test(name) ? '3–32 characters: lowercase letters, digits, . _ -' : null
  const emailErr = email.trim() && !EMAIL_RE.test(email.trim()) ? 'not a valid e-mail address' : null
  const valid = !!name && !nameErr && !!email.trim() && !emailErr
  const submit = (e: FormEvent) => { e.preventDefault(); if (valid) reg.mutate() }
  const reset = () => {
    setDone(null)
    reg.reset()
    try { localStorage.removeItem(STORE_KEY) } catch { /* ignore */ }
  }

  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <div className="card w-full max-w-md space-y-4 p-5" data-testid="register-page">
        <div className="space-y-2">
          <div className="flex items-center justify-between gap-2">
            <Logo className="h-10" />
            {m?.github_url && <GitHubButton url={m.github_url} />}
          </div>
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            {m?.app_title || 'avdlab'} builds, deploys and validates Arista AVD labs on containerlab — and teaches AVD while you do it.
            Request an account and an administrator will set it up for you.
          </p>
        </div>
        {meta.isLoading ? <Spinner /> : done ? (
          <Submitted req={done} supportEmail={m?.support_email} onNew={reset} />
        ) : m && m.registration_enabled === false ? (
          <div className="space-y-2" data-testid="register-disabled">
            <p className="text-sm">Account requests are currently disabled on this server.</p>
            {m.support_email && <p className="text-xs text-zinc-500">Contact <a className="link" href={`mailto:${m.support_email}`}>{m.support_email}</a> for access.</p>}
            <Link className="link text-xs" to="/login">Back to sign in</Link>
          </div>
        ) : (
          <form className="space-y-3" onSubmit={submit} data-testid="register-form">
            <Field label="Username" error={nameErr} help={!nameErr ? 'lowercase letters, digits, . _ - (3–32)' : undefined}>
              <input className="input" value={username} onChange={(e) => setUsername(e.target.value)} autoFocus autoComplete="username" data-testid="register-username" />
            </Field>
            <Field label="E-mail" error={emailErr}>
              <input className="input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" data-testid="register-email" />
            </Field>
            <Field label="Full name">
              <input className="input" value={fullName} onChange={(e) => setFullName(e.target.value)} autoComplete="name" data-testid="register-fullname" />
            </Field>
            <Field label="Why do you need access?" help="e.g. the course, customer or project — helps the administrator decide">
              <textarea className="input min-h-[72px]" value={reason} onChange={(e) => setReason(e.target.value)} maxLength={2000} data-testid="register-reason" />
            </Field>
            <ErrorBox error={errorText(reg.error)} />
            <button className="btn-primary w-full justify-center" disabled={!valid || reg.isPending} data-testid="register-submit">
              {reg.isPending ? 'Sending…' : 'Request account'}
            </button>
            <div className="flex items-center justify-between text-xs">
              <Link className="link" to="/login">Already have an account? Sign in</Link>
              {m?.support_email && <a className="link" href={`mailto:${m.support_email}`} data-testid="register-support">{m.support_email}</a>}
            </div>
          </form>
        )}
      </div>
    </div>
  )
}
