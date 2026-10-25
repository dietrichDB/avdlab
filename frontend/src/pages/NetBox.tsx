import { useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { netboxApi } from '../api/phase4'
import type { NetBoxTarget, NetBoxTargetIn, NetBoxTestResult } from '../api/types'
import { ConfirmButton, Empty, ErrorBox, Field, Modal, PageHeader, Spinner } from '../components/ui'
import { fmtAgo, fmtDate } from '../lib/format'

export default function NetBoxPage() {
  const qc = useQueryClient()
  const targets = useQuery({ queryKey: ['netbox', 'targets'], queryFn: netboxApi.targets })
  const [editing, setEditing] = useState<NetBoxTarget | 'new' | null>(null)
  const remove = useMutation({
    mutationFn: (id: string) => netboxApi.removeTarget(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['netbox'] }),
  })

  return (
    <div>
      <PageHeader
        title="NetBox"
        sub="NetBox instances a lab's intended state (devices, interfaces, VLANs, VRFs, prefixes, IPs, cables) is synced to"
        actions={<button className="btn-primary" onClick={() => setEditing('new')}>Add target</button>}
      />
      <ErrorBox error={targets.error ?? remove.error} />
      {targets.isLoading ? <Spinner /> : !targets.data?.length ? (
        <Empty>No NetBox targets yet. Add a NetBox URL with an API token (the compose profile <span className="mono">netbox</span> starts a local one).</Empty>
      ) : (
        <div className="card overflow-x-auto">
          <table className="table" data-testid="netbox-targets">
            <thead><tr><th>Name</th><th>URL</th><th>Certs</th><th>Token</th><th>Labs</th><th>Created</th><th>Connection</th><th>Actions</th></tr></thead>
            <tbody>
              {targets.data.map((t) => (
                <tr key={t.id} data-testid="netbox-target-row">
                  <td className="font-medium">{t.name}</td>
                  <td className="mono"><a className="link" href={t.url} target="_blank" rel="noreferrer">{t.url}</a></td>
                  <td className="text-xs">{t.verify_certs ? 'verify' : <span className="text-amber-600">no verify</span>}</td>
                  <td>{t.has_token ? <span className="text-emerald-600 dark:text-emerald-400" title={t.token_meta?.updated_at ? `updated ${fmtDate(t.token_meta.updated_at)}` : undefined}>set</span> : <span className="text-zinc-400">—</span>}</td>
                  <td className="text-xs">{t.labs.length ? t.labs.map((l, i) => <span key={l.id}>{i > 0 && ', '}<Link className="link" to={`/labs/${l.id}/netbox`}>{l.name}</Link></span>) : <span className="text-zinc-400">—</span>}</td>
                  <td className="text-zinc-500" title={t.created_at ? fmtDate(t.created_at) : undefined}>{t.created_at ? fmtAgo(t.created_at) : '—'}</td>
                  <td><NetBoxTestConnection target={t} /></td>
                  <td className="whitespace-nowrap">
                    <div className="flex gap-1">
                      <button className="btn-secondary btn-sm" onClick={() => setEditing(t)}>Edit</button>
                      <ConfirmButton onConfirm={() => remove.mutate(t.id)}>Delete</ConfirmButton>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <NetBoxTargetModal open={editing !== null} target={editing === 'new' ? null : editing} onClose={() => setEditing(null)} />
    </div>
  )
}

/** POST /netbox/targets/{id}/test → {ok, version, error} shown inline */
export function NetBoxTestConnection({ target }: { target: NetBoxTarget }) {
  const test = useMutation<NetBoxTestResult, Error>({ mutationFn: () => netboxApi.testTarget(target.id) })
  const r = test.data
  return (
    <div className="flex items-center gap-2" data-testid="netbox-test">
      <button className="btn-secondary btn-sm" disabled={test.isPending || !target.has_token} onClick={() => test.mutate()}
        title={!target.has_token ? 'needs an API token' : 'pynetbox status()'}>
        {test.isPending ? 'Testing…' : 'Test'}
      </button>
      {test.error && <span className="text-xs text-red-600 dark:text-red-400">{test.error.message}</span>}
      {r && (r.ok
        ? <span className="text-xs text-emerald-600 dark:text-emerald-400">ok · NetBox {r.version ?? '?'}</span>
        : <span className="text-xs text-red-600 dark:text-red-400" title={r.error ?? undefined}>failed{r.error ? `: ${r.error}` : ''}</span>)}
    </div>
  )
}

const empty: NetBoxTargetIn = { name: '', url: 'http://localhost:8000', verify_certs: true }

function NetBoxTargetModal({ open, target, onClose }: { open: boolean; target: NetBoxTarget | null; onClose: () => void }) {
  const qc = useQueryClient()
  const [form, setForm] = useState<NetBoxTargetIn>(empty)
  const [token, setToken] = useState('')
  const [clearToken, setClearToken] = useState(false)
  const [last, setLast] = useState<NetBoxTarget | null | undefined>(undefined)
  if (open && last !== target) {
    setLast(target)
    setForm(target ? { name: target.name, url: target.url, verify_certs: target.verify_certs } : empty)
    setToken(''); setClearToken(false)
  }
  if (!open && last !== undefined) setLast(undefined)

  const save = useMutation({
    mutationFn: () => {
      const body: NetBoxTargetIn = { ...form, url: form.url.trim() }
      if (token) body.token = token // write-only: omitted = keep
      else if (clearToken) body.token = null
      return target ? netboxApi.updateTarget(target.id, body) : netboxApi.createTarget(body)
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['netbox'] }); onClose() },
  })
  const set = <K extends keyof NetBoxTargetIn>(k: K, v: NetBoxTargetIn[K]) => setForm((f) => ({ ...f, [k]: v }))
  const submit = (e: FormEvent) => { e.preventDefault(); save.mutate() }
  return (
    <Modal open={open} onClose={onClose} title={target ? `Edit ${target.name}` : 'Add NetBox target'}>
      <form onSubmit={submit} className="space-y-3" data-testid="netbox-target-form">
        <Field label="Name"><input className="input" name="name" required value={form.name} onChange={(e) => set('name', e.target.value)} /></Field>
        <Field label="URL" help="NetBox base URL (http(s)://host[:port])">
          <input className="input mono" name="url" required value={form.url} spellCheck={false} onChange={(e) => set('url', e.target.value)} />
        </Field>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" name="verify_certs" checked={form.verify_certs} onChange={(e) => set('verify_certs', e.target.checked)} />
          <span>verify TLS certificates</span>
        </label>
        <Field label="API token" help={target?.has_token ? 'a token is stored — leave blank to keep it' : 'NetBox → Admin → API tokens (write enabled)'}>
          <input className="input mono" name="token" type="password" autoComplete="off" value={token} disabled={clearToken}
            placeholder={target?.has_token ? '•••••••• (leave blank to keep)' : ''} onChange={(e) => setToken(e.target.value)} />
        </Field>
        {target?.has_token && (
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={clearToken} onChange={(e) => { setClearToken(e.target.checked); if (e.target.checked) setToken('') }} />
            <span>remove the stored token</span>
          </label>
        )}
        <ErrorBox error={save.error} />
        <div className="flex justify-end gap-2">
          <button type="button" className="btn-secondary" onClick={onClose}>Cancel</button>
          <button type="submit" className="btn-primary" disabled={save.isPending || !form.name || !form.url.trim()}>{save.isPending ? 'Saving…' : 'Save'}</button>
        </div>
      </form>
    </Modal>
  )
}
