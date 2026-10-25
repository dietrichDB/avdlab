import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { gitApi } from '../../api/git'
import { secretsApi } from '../../api/secrets'
import type { GitStatus, Lab, OpenLinks, Secret } from '../../api/types'
import { useJobDrawer } from '../../components/JobDrawer'
import { Badge, ConfirmButton, CopyButton, ErrorBox, Field, KV, Spinner } from '../../components/ui'
import { fmtAgo, fmtDate } from '../../lib/format'
import { gitConfigBody, gitFormFrom, gitPanelState, tokenUser, type GitForm } from '../../lib/git'

const TONE: Record<string, string> = {
  ok: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/60 dark:text-emerald-200',
  warn: 'bg-amber-100 text-amber-800 dark:bg-amber-900/60 dark:text-amber-200',
  error: 'bg-red-100 text-red-800 dark:bg-red-900/60 dark:text-red-200',
  muted: 'bg-zinc-200 text-zinc-700 dark:bg-zinc-700 dark:text-zinc-200',
}

/** remote form: url / branch / auth kind; the token is write-only, the key is a stored ssh_key secret */
export function GitRemoteForm({ status, secrets, busy, onSubmit, onRemove }: {
  status: GitStatus | undefined; secrets: Secret[]; busy?: boolean
  onSubmit: (f: GitForm) => void; onRemove?: () => void
}) {
  const [f, setF] = useState<GitForm>(() => gitFormFrom(status))
  useEffect(() => setF(gitFormFrom(status)), [status])
  const keys = secrets.filter((s) => s.kind === 'ssh_key')
  const storedToken = !!status?.has_secret && status?.auth_kind === 'token'
  const tokenMissing = f.auth_kind === 'token' && !f.token && !storedToken
  const keyMissing = f.auth_kind === 'ssh_key' && !f.secret_id && !(status?.has_secret && status.auth_kind === 'ssh_key')
  return (
    <form className="space-y-3" data-testid="git-form" onSubmit={(e) => { e.preventDefault(); onSubmit(f) }}>
      <div className="grid gap-2 sm:grid-cols-[1fr_8rem]">
        <Field label="Remote URL" help="https://github.com/org/repo.git or git@host:org/repo.git: no credentials in the URL">
          <input className="input mono" name="git.url" required value={f.url} onChange={(e) => setF({ ...f, url: e.target.value })} placeholder="https://github.com/me/dc1-lab.git" spellCheck={false} />
        </Field>
        <Field label="Branch"><input className="input mono" name="git.branch" value={f.branch} onChange={(e) => setF({ ...f, branch: e.target.value })} /></Field>
      </div>
      <Field label="Authentication">
        <select className="input" name="git.auth_kind" value={f.auth_kind} onChange={(e) => setF({ ...f, auth_kind: e.target.value as GitForm['auth_kind'] })}>
          <option value="none">none (public / local)</option>
          <option value="token">access token (HTTPS)</option>
          <option value="ssh_key">SSH key</option>
        </select>
      </Field>
      {f.auth_kind === 'token' && (
        <Field label="Access token" help={<>write-only; sent as <span className="mono">{tokenUser(f.url)}</span> at run time only.{storedToken ? ' Leave empty to keep the stored token.' : ''}</>}>
          <input className="input mono" type="password" name="git.token" autoComplete="off" value={f.token}
            placeholder={storedToken ? '•••••••• stored' : 'ghp_… / glpat-…'} onChange={(e) => setF({ ...f, token: e.target.value })} />
        </Field>
      )}
      {f.auth_kind === 'ssh_key' && (
        <Field label="SSH key secret" help={keys.length ? 'the public key must be a deploy key with write access' : 'no ssh_key secrets yet: generate one on the Hosts page'}>
          <select className="input" name="git.secret_id" value={f.secret_id} onChange={(e) => setF({ ...f, secret_id: e.target.value })}>
            <option value="">{status?.has_secret && status.auth_kind === 'ssh_key' ? '— keep current key —' : '— choose —'}</option>
            {keys.map((k) => <option key={k.id} value={k.id}>{k.name}</option>)}
          </select>
        </Field>
      )}
      <div className="flex items-center gap-2">
        <button className="btn-primary btn-sm" data-testid="git-save" disabled={busy || !f.url.trim() || tokenMissing || keyMissing}>{status?.configured ? 'Update remote' : 'Set remote'}</button>
        {status?.configured && onRemove && <ConfirmButton className="btn-secondary btn-sm" confirmText="Remove remote?" onConfirm={onRemove}>Remove</ConfirmButton>}
      </div>
    </form>
  )
}

export function GitStatusSummary({ status }: { status: GitStatus | undefined }) {
  const st = gitPanelState(status)
  if (!st.configured) return <div className="text-sm text-zinc-500" data-testid="git-status">No remote configured. Every change is already committed locally.</div>
  const ls = status?.last_sync
  return (
    <div data-testid="git-status">
      <KV items={[
        ['Remote', <span className="mono break-all">{status?.url}</span>],
        ['Branch', <span className="mono">{status?.branch ?? 'main'}</span>],
        ['Auth', <span>{status?.auth_kind ?? 'none'}{status?.auth_kind && status.auth_kind !== 'none' && (status.has_secret ? <span className="ml-1 text-emerald-600">✓ stored</span> : <span className="ml-1 text-amber-600">missing credential</span>)}</span>],
        ['Sync', <Badge className={TONE[st.tone]}><span data-testid="git-sync-label">{st.syncLabel}</span></Badge>],
        ['Last sync', ls ? <span title={ls.message}>
          <span className={ls.ok ? '' : 'text-red-600 dark:text-red-400'}>{ls.direction} {ls.ok ? 'ok' : 'failed'}</span>
          <span className="ml-1 text-zinc-500" title={fmtDate(ls.at)}>{fmtAgo(ls.at)}</span>
          {ls.message && <div className="mono text-[11px] text-zinc-500">{ls.message}</div>}
        </span> : <span className="text-zinc-400">never</span>],
      ]} />
      <div className="mt-1 text-[11px] text-zinc-500">ahead/behind are from the last fetch (push/pull refreshes them).</div>
    </div>
  )
}

export function OpenLinksPanel({ links }: { links: OpenLinks }) {
  const rows: [string, string, string | null][] = [
    ['Lab folder (Remote-SSH)', links.remote_ssh, links.remote_ssh],
    ['Topology file', links.remote_ssh_topology, links.remote_ssh_topology],
    ['Dev container (clone in volume)', links.devcontainer ?? '', links.devcontainer],
  ]
  return (
    <div className="space-y-3" data-testid="open-links">
      <ul className="space-y-2">
        {rows.filter(([, , href]) => !!href).map(([label, text, href]) => (
          <li key={label} className="flex flex-wrap items-center gap-2">
            <a className="btn-primary btn-sm" href={href!}>Open {label}</a>
            <CopyButton text={text} label="Copy link" />
            <span className="mono min-w-0 flex-1 truncate text-[11px] text-zinc-500" title={text}>{text}</span>
          </li>
        ))}
      </ul>
      <KV items={[
        ['SSH', <span className="flex items-center gap-2"><span className="mono">{links.ssh_command}</span><CopyButton text={links.ssh_command} /></span>],
        ['Topology path', <span className="flex items-center gap-2"><span className="mono break-all">{links.clab_topology_path}</span><CopyButton text={links.clab_topology_path} /></span>],
      ]} />
      {links.hints.length > 0 && (
        <ul className="list-disc space-y-0.5 pl-5 text-xs text-zinc-600 dark:text-zinc-400">
          {links.hints.map((h, i) => <li key={i}>{h}</li>)}
        </ul>
      )}
    </div>
  )
}

export default function GitTab({ lab }: { lab: Lab }) {
  const qc = useQueryClient()
  const drawer = useJobDrawer()
  const status = useQuery({ queryKey: ['labs', lab.id, 'git'], queryFn: () => gitApi.status(lab.id) })
  const secrets = useQuery({ queryKey: ['secrets'], queryFn: secretsApi.list })
  const links = useQuery({ queryKey: ['labs', lab.id, 'open-links'], queryFn: () => gitApi.openLinks(lab.id), retry: false })
  const refresh = () => qc.invalidateQueries({ queryKey: ['labs', lab.id, 'git'] })
  const save = useMutation({ mutationFn: (f: GitForm) => gitApi.configure(lab.id, gitConfigBody(f)), onSuccess: () => { refresh(); qc.invalidateQueries({ queryKey: ['secrets'] }); qc.invalidateQueries({ queryKey: ['labs', lab.id, 'open-links'] }) } })
  const remove = useMutation({ mutationFn: () => gitApi.remove(lab.id), onSuccess: refresh })
  const [force, setForce] = useState(false)
  const push = useMutation({ mutationFn: () => drawer.track(gitApi.push(lab.id, force)) })
  const pull = useMutation({ mutationFn: () => drawer.track(gitApi.pull(lab.id)) })
  const sync = useMutation({ mutationFn: () => drawer.track(gitApi.sync(lab.id)) })
  const st = gitPanelState(status.data)

  if (status.isLoading) return <Spinner label="git status…" />
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <section className="card space-y-3 p-3">
        <div className="flex items-center gap-2">
          <span className="h2 mr-auto">Git remote</span>
          <label className="flex items-center gap-1 text-xs" title="git push --force"><input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} disabled={!st.canPush} /> force</label>
          <button className="btn-secondary btn-sm" data-testid="git-pull" disabled={!st.canPull || pull.isPending} onClick={() => pull.mutate()} title="git pull --ff-only, then re-validate">Pull</button>
          <button className="btn-primary btn-sm" data-testid="git-push" disabled={!st.canPush || push.isPending} onClick={() => push.mutate()}>Push</button>
        </div>
        <ErrorBox error={status.error ?? push.error ?? pull.error} />
        <GitStatusSummary status={status.data} />
        <hr className="border-zinc-200 dark:border-zinc-800" />
        <GitRemoteForm status={status.data} secrets={secrets.data ?? []} busy={save.isPending} onSubmit={(f) => save.mutate(f)} onRemove={() => remove.mutate()} />
        <ErrorBox error={save.error ?? remove.error} />
        {save.isSuccess && <div className="text-xs text-emerald-600">Remote saved.</div>}
      </section>

      <section className="card space-y-3 p-3">
        <div className="flex items-center gap-2">
          <span className="h2 mr-auto">Open in VS Code</span>
          <button className="btn-secondary btn-sm" data-testid="sync-files" disabled={!lab.host_id || sync.isPending} title={lab.host_id ? 'rsync the lab directory to the host without deploying' : 'assign a host in Settings'} onClick={() => sync.mutate()}>
            {sync.isPending ? 'Starting…' : 'Sync files to host'}
          </button>
        </div>
        <ErrorBox error={sync.error} />
        {links.isLoading ? <Spinner /> : links.data ? <OpenLinksPanel links={links.data} /> : (
          <div className="text-sm text-zinc-500">{lab.host_id ? <ErrorBox error={links.error} /> : 'Assign a host in Settings to get Remote-SSH links.'}</div>
        )}
      </section>
    </div>
  )
}
