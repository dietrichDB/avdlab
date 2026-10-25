import { useState, type FormEvent } from 'react'
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom'
import { MfaRestartError, useAuth } from '../hooks/useAuth'
import { ErrorBox, Field } from '../components/ui'
import { Logo } from '../components/Brand'
import { ChangePasswordForm } from './Account'
import { useMeta } from '../hooks/useMeta'

export default function LoginPage() {
  const { user, login, verifyMfa, loading } = useAuth()
  const meta = useMeta()
  const navigate = useNavigate()
  const loc = useLocation()
  const [step, setStep] = useState<'password' | 'mfa'>('password')
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [code, setCode] = useState('')
  const [recovery, setRecovery] = useState(false)
  const [err, setErr] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  const from = (loc.state as { from?: string } | null)?.from ?? '/'

  if (!loading && user) return <Navigate to={from} replace />

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    try {
      if (step === 'password') {
        const r = await login(username, password)
        if (r.mfaRequired) {
          setStep('mfa')
          setCode('')
          return
        }
      } else {
        await verifyMfa(code)
      }
      navigate(from, { replace: true })
    } catch (e) {
      if (e instanceof MfaRestartError) {
        setStep('password')
        setPassword('')
      }
      setErr(e)
    } finally {
      setBusy(false)
    }
  }

  const codeOk = recovery ? code.replace(/[\s-]/g, '').length === 10 : /^\d{6}$/.test(code)

  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <form onSubmit={submit} className="card w-full max-w-xs space-y-3 p-5" data-testid={`login-step-${step}`}>
        <div className="space-y-1">
          <Logo className="h-10" />
          <div className="text-xs text-zinc-500">AVD lab orchestrator — {step === 'password' ? 'sign in' : 'two-factor authentication'}</div>
        </div>
        {step === 'password' ? (
          <>
            <Field label="Username">
              <input className="input" value={username} onChange={(e) => setUsername(e.target.value)} autoFocus autoComplete="username" />
            </Field>
            <Field label="Password">
              <input className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
            </Field>
          </>
        ) : (
          <>
            {recovery ? (
              <Field label="Recovery code" help="one of the codes you saved when enabling MFA (each works once)">
                <input className="input mono" value={code} onChange={(e) => setCode(e.target.value)} autoFocus autoComplete="off" placeholder="xxxxx-xxxxx" data-testid="mfa-code" />
              </Field>
            ) : (
              <Field label="6-digit code" help="from your authenticator app">
                <input className="input mono tracking-widest" value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
                  autoFocus inputMode="numeric" autoComplete="one-time-code" placeholder="123456" data-testid="mfa-code" />
              </Field>
            )}
            <div className="flex justify-between text-xs">
              <button type="button" className="link" onClick={() => { setRecovery(!recovery); setCode(''); setErr(null) }} data-testid="mfa-toggle-recovery">
                {recovery ? 'Use the authenticator app' : 'Use a recovery code'}
              </button>
              <button type="button" className="link" onClick={() => { setStep('password'); setErr(null) }}>Back</button>
            </div>
          </>
        )}
        <ErrorBox error={err} />
        <button className="btn-primary w-full justify-center" disabled={busy || (step === 'mfa' && !codeOk)}>
          {busy ? 'Signing in…' : step === 'password' ? 'Sign in' : 'Verify'}
        </button>
        {step === 'password' && meta.data?.registration_enabled && <Link className="link block text-center text-xs" to="/register" data-testid="register-link">Request an account</Link>}
      </form>
    </div>
  )
}

/** Shown instead of the app while the account must change its (temporary/default) password. */
export function ForcedPasswordChange() {
  const { user, logout } = useAuth()
  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <div className="card w-full max-w-sm space-y-3 p-5" data-testid="forced-password-change">
        <Logo className="h-10" />
        <div>
          <div className="font-semibold">Choose a new password</div>
          <div className="text-xs text-zinc-500">
            {user} — your password is temporary or does not meet the password policy. Change it before continuing.
          </div>
        </div>
        <ChangePasswordForm submitLabel="Set new password" />
        <button className="link text-xs" onClick={logout}>Sign out</button>
      </div>
    </div>
  )
}
