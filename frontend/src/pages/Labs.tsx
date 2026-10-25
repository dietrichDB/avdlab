import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { labsApi } from '../api/labs'
import { hostsApi } from '../api/hosts'
import { gitApi } from '../api/git'
import { secretsApi } from '../api/secrets'
import { ApiError } from '../api/client'
import type { Host, Violation } from '../api/types'
import { useJobDrawer } from '../components/JobDrawer'
import { FirstLabOffer } from '../components/learn/FirstLabOffer'
import { Empty, ErrorBox, Field, LabStateBadge, Hint, Modal, PageHeader, Spinner } from '../components/ui'
import { fmtAgo } from '../lib/format'
import { gitConfigBody, type GitForm } from '../lib/git'

function HostSelect({ hosts, value, onChange }: { hosts: Host[]; value: string; onChange: (v: string) => void }) {
  return (
    <Field label="Host (optional)">
      <select className="input" value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">— none —</option>
        {hosts.map((h) => <option key={h.id} value={h.id}>{h.name}</option>)}
      </select>
    </Field>
  )
}

const nameFromFile = (f: string) => f.replace(/\.(zip|tar\.gz|tgz)$/i, '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')
const nameFromUrl = (u: string) => (u.split('/').pop() ?? '').replace(/\.git$/, '')

function ImportArchiveModal({ open, onClose, hosts }: { open: boolean; onClose: () => void; hosts: Host[] }) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [file, setFile] = useState<File | null>(null)
  const [name, setName] = useState('')
  const [hostId, setHostId] = useState('')
  const [violations, setViolations] = useState<Violation[]>([])
  const up = useMutation({
    mutationFn: () => gitApi.importArchive(file!, name.trim(), hostId || null),
    onSuccess: (lab) => { qc.invalidateQueries({ queryKey: ['labs'] }); onClose(); navigate(`/labs/${lab.id}`) },
    onError: (e) => { if (e instanceof ApiError && e.status === 422) { setViolations(e.violations); qc.invalidateQueries({ queryKey: ['labs'] }) } },
  })
  const submit = (e: FormEvent) => { e.preventDefault(); setViolations([]); if (file && name.trim()) up.mutate() }
  return (
    <Modal open={open} onClose={onClose} title="Import lab archive">
      <form className="space-y-3" onSubmit={submit} data-testid="import-archive-form">
        <Field label="Archive (.zip / .tar.gz)" help="must contain an Ansible inventory (inventory.yml / hosts.yml) and group_vars/; intended/ and documentation/ are rebuilt">
          <input type="file" accept=".zip,.tar.gz,.tgz" className="block w-full text-xs" name="file"
            onChange={(e) => { const f = e.target.files?.[0] ?? null; setFile(f); if (f && !name) setName(nameFromFile(f.name)) }} />
        </Field>
        <Field label="Lab name"><input className="input" name="name" required value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <HostSelect hosts={hosts} value={hostId} onChange={setHostId} />
        {violations.length > 0 && (
          <div className="rounded border border-amber-300 bg-amber-50 px-2 py-1 text-xs text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-100">
            <div className="font-medium">pyavd validation failed:</div>
            <ul className="mono list-disc pl-4">{violations.map((v, i) => <li key={i}>{v.path ? `${v.path}: ` : ''}{v.message}</li>)}</ul>
          </div>
        )}
        {up.error && !(up.error instanceof ApiError && up.error.status === 422) && <ErrorBox error={up.error} />}
        <div className="flex justify-end gap-2">
          <button type="button" className="btn-secondary" onClick={onClose}>Cancel</button>
          <button className="btn-primary" disabled={!file || !name.trim() || up.isPending}>{up.isPending ? 'Importing…' : 'Import'}</button>
        </div>
      </form>
    </Modal>
  )
}

function ImportGitModal({ open, onClose, hosts }: { open: boolean; onClose: () => void; hosts: Host[] }) {
  const drawer = useJobDrawer()
  const secrets = useQuery({ queryKey: ['secrets'], queryFn: secretsApi.list, enabled: open })
  const [f, setF] = useState<GitForm>({ url: '', branch: 'main', auth_kind: 'none', token: '', secret_id: '' })
  const [name, setName] = useState('')
  const [hostId, setHostId] = useState('')
  const clone = useMutation({
    mutationFn: () => drawer.track(gitApi.importGit({ ...gitConfigBody(f), name: name.trim(), ...(hostId ? { host_id: hostId } : {}) })),
    onSuccess: () => { onClose(); setF({ url: '', branch: 'main', auth_kind: 'none', token: '', secret_id: '' }); setName('') },
  })
  const keys = (secrets.data ?? []).filter((s) => s.kind === 'ssh_key')
  const incomplete = !f.url.trim() || !name.trim() || (f.auth_kind === 'token' && !f.token) || (f.auth_kind === 'ssh_key' && !f.secret_id)
  return (
    <Modal open={open} onClose={onClose} title="Clone lab from git">
      <form className="space-y-3" data-testid="import-git-form" onSubmit={(e) => { e.preventDefault(); if (!incomplete) clone.mutate() }}>
        <Field label="Repository URL"><input className="input mono" name="url" required value={f.url} spellCheck={false}
          onChange={(e) => { setF({ ...f, url: e.target.value }); if (!name) setName(nameFromUrl(e.target.value)) }} placeholder="https://github.com/me/dc1-lab.git" /></Field>
        <div className="grid grid-cols-2 gap-2">
          <Field label="Lab name"><input className="input" name="name" required value={name} onChange={(e) => setName(e.target.value)} /></Field>
          <Field label="Branch"><input className="input mono" name="branch" value={f.branch} onChange={(e) => setF({ ...f, branch: e.target.value })} /></Field>
        </div>
        <Field label="Authentication">
          <select className="input" name="auth_kind" value={f.auth_kind} onChange={(e) => setF({ ...f, auth_kind: e.target.value as GitForm['auth_kind'] })}>
            <option value="none">none</option><option value="token">access token</option><option value="ssh_key">SSH key</option>
          </select>
        </Field>
        {f.auth_kind === 'token' && <Field label="Access token" help="stored encrypted; never shown again"><input className="input mono" type="password" autoComplete="off" name="token" value={f.token} onChange={(e) => setF({ ...f, token: e.target.value })} /></Field>}
        {f.auth_kind === 'ssh_key' && (
          <Field label="SSH key secret">
            <select className="input" name="secret_id" value={f.secret_id} onChange={(e) => setF({ ...f, secret_id: e.target.value })}>
              <option value="">— choose —</option>{keys.map((k) => <option key={k.id} value={k.id}>{k.name}</option>)}
            </select>
          </Field>
        )}
        <HostSelect hosts={hosts} value={hostId} onChange={setHostId} />
        <ErrorBox error={clone.error} />
        <div className="flex justify-end gap-2">
          <button type="button" className="btn-secondary" onClick={onClose}>Cancel</button>
          <button className="btn-primary" disabled={incomplete || clone.isPending}>{clone.isPending ? 'Starting…' : 'Clone'}</button>
        </div>
      </form>
    </Modal>
  )
}

function ImportMenu({ onPick }: { onPick: (k: 'archive' | 'git') => void }) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const fn = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false) }
    document.addEventListener('mousedown', fn)
    return () => document.removeEventListener('mousedown', fn)
  }, [open])
  return (
    <div className="relative" ref={ref}>
      <button className="btn-secondary" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((o) => !o)} data-testid="import-menu">Import ▾</button>
      {open && (
        <div role="menu" className="card absolute right-0 z-20 mt-1 w-56 py-1 shadow-lg">
          <button role="menuitem" className="block w-full px-3 py-1.5 text-left text-sm hover:bg-zinc-100 dark:hover:bg-zinc-800" onClick={() => { setOpen(false); onPick('archive') }}>Upload archive (.zip / .tar.gz)</button>
          <button role="menuitem" className="block w-full px-3 py-1.5 text-left text-sm hover:bg-zinc-100 dark:hover:bg-zinc-800" onClick={() => { setOpen(false); onPick('git') }}>Clone from git</button>
        </div>
      )}
    </div>
  )
}

export default function LabsPage() {
  const labs = useQuery({ queryKey: ['labs'], queryFn: labsApi.list })
  const hosts = useQuery({ queryKey: ['hosts'], queryFn: hostsApi.list })
  const [modal, setModal] = useState<'archive' | 'git' | null>(null)
  const hostName = (id: string | null) => (id ? hosts.data?.find((h) => h.id === id)?.name ?? id.slice(0, 8) : null)
  return (
    <div>
      <PageHeader title="Labs" sub="AVD fabrics rendered from templates, archives or git" actions={<>
        <ImportMenu onPick={setModal} />
        <Link to="/labs/new" className="btn-primary" data-tour="new-lab">New lab</Link>
      </>} />
      <Hint title="A lab is one AVD fabric.">Start with <b>New lab</b> and a template; open the lab to build the configs, deploy it on a host and validate it with ANTA.</Hint>
      <ErrorBox error={labs.error} />
      {!labs.isLoading && !labs.error && !labs.data?.length && <FirstLabOffer />}
      {labs.isLoading ? <Spinner /> : !labs.data?.length ? <Empty>No labs yet. <Link className="link" to="/labs/new">Create one</Link> or import an archive.</Empty> : (
        <div className="card overflow-x-auto">
          <table className="table">
            <thead><tr><th>Name</th><th>Slug</th><th>Template</th><th>State</th><th>Host</th><th>Target</th><th>cEOS</th><th>Last build</th><th>Updated</th><th /></tr></thead>
            <tbody>
              {labs.data.map((l) => (
                <tr key={l.id}>
                  <td><Link className="link font-medium" to={`/labs/${l.id}`}>{l.name}</Link></td>
                  <td className="mono">{l.slug}</td>
                  <td className="mono">{l.template}</td>
                  <td><LabStateBadge state={l.state} /></td>
                  <td>{l.host?.name ?? hostName(l.host_id) ?? <span className="text-zinc-400">—</span>}</td>
                  <td className="text-xs">{l.target}</td>
                  <td className="mono">{l.ceos_version ?? '—'}</td>
                  <td className="text-xs">{l.last_build_job ? `${l.last_build_job.status} · ${fmtAgo(l.last_build_job.created_at)}` : '—'}</td>
                  <td className="text-zinc-500">{fmtAgo(l.updated_at)}</td>
                  <td><a className="btn-secondary btn-sm" href={gitApi.exportUrl(l.id)} download title="zip of the lab directory">Export zip</a></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <ImportArchiveModal open={modal === 'archive'} onClose={() => setModal(null)} hosts={hosts.data ?? []} />
      <ImportGitModal open={modal === 'git'} onClose={() => setModal(null)} hosts={hosts.data ?? []} />
    </div>
  )
}
