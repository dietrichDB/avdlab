import { useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { usersApi } from '../api/auth'
import { requestsApi } from '../api/admin'
import type { AccountRequest, UserAccount, UserRole, UserWithTemp } from '../api/types'
import { useAuth } from '../hooks/useAuth'
import { Badge, ConfirmButton, CopyButton, Empty, ErrorBox, Field, Modal, PageHeader, Spinner } from '../components/ui'
import { fmtAgo } from '../lib/format'
import { errorText } from './Account'

const ROLES: UserRole[] = ['admin', 'operator', 'viewer']
export const ROLE_TONE: Record<UserRole, string> = {
  admin: 'bg-violet-100 text-violet-800 dark:bg-violet-900/60 dark:text-violet-200',
  operator: 'bg-blue-100 text-blue-800 dark:bg-blue-900/60 dark:text-blue-200',
  viewer: 'bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300',
}

function TempPassword({ result, onClose }: { result: UserWithTemp | null; onClose: () => void }) {
  return (
    <Modal open={!!result?.temporary_password} onClose={onClose} title="Temporary password">
      {result?.temporary_password && (
        <div className="space-y-3" data-testid="temp-password">
          <p className="text-sm">Give this password to <b>{result.username}</b>. It is shown only once; they must change it at first sign-in.</p>
          <div className="flex items-center gap-2">
            <code className="mono flex-1 select-all rounded bg-zinc-100 px-2 py-1 text-sm dark:bg-zinc-800" data-testid="temp-password-value">{result.temporary_password}</code>
            <CopyButton text={result.temporary_password} />
          </div>
          <button className="btn-primary" onClick={onClose}>Done</button>
        </div>
      )}
    </Modal>
  )
}

function CreateUserDialog({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: (u: UserWithTemp) => void }) {
  const [username, setUsername] = useState('')
  const [role, setRole] = useState<UserRole>('operator')
  const [password, setPassword] = useState('')
  const m = useMutation({
    mutationFn: () => usersApi.create({ username: username.trim(), role, ...(password ? { password } : { send_temp: true }) }),
    onSuccess: (u) => { setUsername(''); setPassword(''); onCreated(u) },
  })
  const submit = (e: FormEvent) => { e.preventDefault(); m.mutate() }
  return (
    <Modal open={open} onClose={onClose} title="New user">
      <form className="space-y-3" onSubmit={submit} data-testid="create-user-form">
        <Field label="Username"><input className="input" value={username} onChange={(e) => setUsername(e.target.value)} autoFocus data-testid="new-username" /></Field>
        <Field label="Role">
          <select className="input" value={role} onChange={(e) => setRole(e.target.value as UserRole)} data-testid="new-role">
            {ROLES.map((r) => <option key={r}>{r}</option>)}
          </select>
        </Field>
        <Field label="Password" help="leave empty to generate a 20-character temporary password (must be changed at first sign-in)">
          <input className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
        </Field>
        <ErrorBox error={errorText(m.error)} />
        <div className="flex gap-2">
          <button className="btn-primary" disabled={!username.trim() || m.isPending} data-testid="create-user-submit">Create</button>
          <button type="button" className="btn-secondary" onClick={onClose}>Cancel</button>
        </div>
      </form>
    </Modal>
  )
}

/** v0.9.0 (CONTRACT-8 A): public account requests waiting for an admin */
function PendingRequests({ onApproved, onChanged }: { onApproved: (u: UserWithTemp) => void; onChanged: () => void }) {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: ['account-requests'], queryFn: () => requestsApi.list('pending') })
  const [roles, setRoles] = useState<Record<string, UserRole>>({})
  const [reasons, setReasons] = useState<Record<string, string>>({})
  const refresh = () => { qc.invalidateQueries({ queryKey: ['account-requests'] }); onChanged() }
  const approve = useMutation({
    mutationFn: (r: AccountRequest) => requestsApi.approve(r.id, roles[r.id] ?? 'viewer'),
    onSuccess: (res) => { onApproved({ ...res.user, temporary_password: res.temporary_password }); refresh() },
  })
  const reject = useMutation({
    mutationFn: (r: AccountRequest) => requestsApi.reject(r.id, reasons[r.id]),
    onSuccess: refresh,
  })
  const rows = q.data ?? []
  return (
    <section className="card p-4" data-testid="pending-requests">
      <div className="mb-2 flex items-center gap-2">
        <div className="h2">Pending requests</div>
        {rows.length > 0 && <Badge className="bg-amber-100 text-amber-800 dark:bg-amber-900/60 dark:text-amber-200">{rows.length}</Badge>}
        <span className="text-xs text-zinc-500">account requests from the public sign-up form</span>
      </div>
      <ErrorBox error={q.error ?? errorText(approve.error) ?? errorText(reject.error)} />
      {q.isLoading ? <Spinner /> : !rows.length ? <p className="text-xs text-zinc-500">No pending requests.</p> : (
        <div className="overflow-x-auto">
          <table className="table">
            <thead><tr><th>Username</th><th>Name / e-mail</th><th>Reason</th><th>Requested</th><th>Role</th><th /></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id} data-testid={`request-row-${r.username}`}>
                  <td className="font-medium">{r.username}</td>
                  <td className="text-xs">{r.full_name || '—'}<div className="text-zinc-500"><a className="link" href={`mailto:${r.email}`}>{r.email}</a></div></td>
                  <td className="max-w-xs whitespace-pre-wrap text-xs text-zinc-600 dark:text-zinc-400">{r.reason || '—'}</td>
                  <td className="whitespace-nowrap text-xs text-zinc-500" title={`${r.created_at ?? ''} from ${r.ip ?? '?'}`}>{r.created_at ? fmtAgo(r.created_at) : '—'}</td>
                  <td>
                    <select className="input w-28 py-0 text-xs" value={roles[r.id] ?? 'viewer'} onChange={(e) => setRoles({ ...roles, [r.id]: e.target.value as UserRole })}
                      data-testid={`request-role-${r.username}`}>
                      {ROLES.map((x) => <option key={x}>{x}</option>)}
                    </select>
                  </td>
                  <td className="whitespace-nowrap space-x-1">
                    <button className="btn-primary btn-sm" disabled={approve.isPending} onClick={() => approve.mutate(r)} data-testid={`request-approve-${r.username}`}>Approve</button>
                    <input className="input inline-block w-36 py-0 text-xs" placeholder="reason (optional)" value={reasons[r.id] ?? ''}
                      onChange={(e) => setReasons({ ...reasons, [r.id]: e.target.value })} data-testid={`request-reason-${r.username}`} />
                    <ConfirmButton onConfirm={() => reject.mutate(r)} confirmText="Reject?">Reject</ConfirmButton>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

export default function UsersPage() {
  const qc = useQueryClient()
  const { user: me, role } = useAuth()
  const users = useQuery({ queryKey: ['users'], queryFn: usersApi.list, enabled: role === 'admin' })
  const audit = useQuery({ queryKey: ['users-audit'], queryFn: () => usersApi.audit(100), enabled: role === 'admin' })
  const [creating, setCreating] = useState(false)
  const [temp, setTemp] = useState<UserWithTemp | null>(null)
  const done = () => { qc.invalidateQueries({ queryKey: ['users'] }); qc.invalidateQueries({ queryKey: ['users-audit'] }) }
  const act = useMutation({
    mutationFn: async ({ kind, u, role: r, disabled }: { kind: 'role' | 'disable' | 'reset' | 'mfa' | 'unlock' | 'delete'; u: UserAccount; role?: UserRole; disabled?: boolean }) => {
      switch (kind) {
        case 'role': return usersApi.patch(u.id, { role: r })
        case 'disable': return usersApi.patch(u.id, { disabled })
        case 'reset': return usersApi.resetPassword(u.id)
        case 'mfa': return usersApi.resetMfa(u.id)
        case 'unlock': return usersApi.unlock(u.id)
        case 'delete': return usersApi.remove(u.id)
      }
    },
    onSuccess: (r) => {
      if (r && typeof r === 'object' && 'temporary_password' in r && r.temporary_password) setTemp(r as UserWithTemp)
      done()
    },
  })

  if (role !== 'admin') return <ErrorBox error="Only admins can manage users." />
  return (
    <div className="space-y-4">
      <PageHeader title="Users" sub="accounts, roles (admin · operator · viewer) and two-factor authentication"
        actions={<>
          <Link className="btn-secondary" to="/admin/settings" data-testid="admin-settings-link">Admin settings</Link>
          <button className="btn-primary" onClick={() => setCreating(true)} data-testid="new-user">New user</button>
        </>} />
      <PendingRequests onApproved={setTemp} onChanged={done} />
      <ErrorBox error={users.error ?? errorText(act.error)} />
      {users.isLoading ? <Spinner /> : !users.data?.length ? <Empty>No users.</Empty> : (
        <div className="card overflow-x-auto">
          <table className="table" data-testid="users-table">
            <thead><tr><th>Username</th><th>Role</th><th>MFA</th><th>Disabled</th><th>Locked</th><th>Last login</th><th /></tr></thead>
            <tbody>
              {users.data.map((u) => (
                <tr key={u.id} data-testid={`user-row-${u.username}`}>
                  <td className="font-medium">
                    {u.username}{u.username === me && <span className="ml-1 text-xs text-zinc-400">(you)</span>}
                    {u.must_change_password && <Badge className="ml-1 bg-amber-100 text-amber-800 dark:bg-amber-900/60 dark:text-amber-200">must change pw</Badge>}
                  </td>
                  <td>
                    <select className="input w-28 py-0 text-xs" value={u.role} onChange={(e) => act.mutate({ kind: 'role', u, role: e.target.value as UserRole })} data-testid={`user-role-${u.username}`}>
                      {ROLES.map((r) => <option key={r}>{r}</option>)}
                    </select>
                  </td>
                  <td>{u.mfa_enabled ? <Badge className="bg-emerald-100 text-emerald-800 dark:bg-emerald-900/60 dark:text-emerald-200">on</Badge> : <span className="text-xs text-zinc-400">off</span>}</td>
                  <td>
                    <input type="checkbox" checked={u.disabled} onChange={(e) => act.mutate({ kind: 'disable', u, disabled: e.target.checked })} aria-label={`disable ${u.username}`} data-testid={`user-disabled-${u.username}`} />
                  </td>
                  <td className="text-xs">{u.locked ? <span className="text-red-600 dark:text-red-400" title={u.locked_until ?? ''}>locked</span> : <span className="text-zinc-400">—</span>}</td>
                  <td className="whitespace-nowrap text-xs text-zinc-500" title={u.last_login_at ?? ''}>{u.last_login_at ? fmtAgo(u.last_login_at) : 'never'}</td>
                  <td className="whitespace-nowrap space-x-1">
                    <ConfirmButton className="btn-secondary btn-sm" onConfirm={() => act.mutate({ kind: 'reset', u })} confirmText="Reset?">Reset password</ConfirmButton>
                    {u.mfa_enabled && <ConfirmButton className="btn-secondary btn-sm" onConfirm={() => act.mutate({ kind: 'mfa', u })} confirmText="Clear MFA?">Reset MFA</ConfirmButton>}
                    {u.locked && <button className="btn-secondary btn-sm" onClick={() => act.mutate({ kind: 'unlock', u })}>Unlock</button>}
                    {u.username !== me && <ConfirmButton onConfirm={() => act.mutate({ kind: 'delete', u })} confirmText="Delete?">Delete</ConfirmButton>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <section>
        <h2 className="mb-2 font-semibold">Audit log <span className="text-xs font-normal text-zinc-500">last 100 sign-in and account events</span></h2>
        <ErrorBox error={audit.error} />
        {audit.isLoading ? <Spinner /> : !audit.data?.length ? <Empty>No events yet.</Empty> : (
          <div className="card overflow-x-auto">
            <table className="table" data-testid="audit-table">
              <thead><tr><th>When</th><th>User</th><th>Event</th><th>IP</th><th>Detail</th></tr></thead>
              <tbody>
                {audit.data.map((a) => (
                  <tr key={a.id}>
                    <td className="whitespace-nowrap text-xs text-zinc-500" title={a.at ?? ''}>{a.at ? fmtAgo(a.at) : '—'}</td>
                    <td>{a.username || '—'}</td>
                    <td className="mono text-xs">{a.event}</td>
                    <td className="mono text-xs text-zinc-500" title={a.user_agent ?? ''}>{a.ip ?? '—'}</td>
                    <td className="text-xs text-zinc-500">{a.detail ?? ''}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <CreateUserDialog open={creating} onClose={() => setCreating(false)} onCreated={(u) => { setCreating(false); setTemp(u); done() }} />
      <TempPassword result={temp} onClose={() => setTemp(null)} />
    </div>
  )
}
