import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { cvApi } from '../api/cloudvision'
import type { CVKind, CVTarget, CVTargetIn, CVTestResult } from '../api/types'
import { Badge, ConfirmButton, Empty, ErrorBox, Field, Modal, PageHeader, Spinner } from '../components/ui'
import { CVAAS_SERVERS, isExpired } from '../lib/cloudvision'
import { fmtAgo, fmtDate } from '../lib/format'
import { Term } from '../components/learn/Term'

export default function CloudVisionPage() {
  const qc = useQueryClient()
  const targets = useQuery({ queryKey: ['cv', 'targets'], queryFn: cvApi.targets })
  const [editing, setEditing] = useState<CVTarget | 'new' | null>(null)
  const remove = useMutation({
    mutationFn: (id: string) => cvApi.removeTarget(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['cv'] }),
  })

  return (
    <div>
      <PageHeader
        title="CloudVision"
        sub={<><Term k="CVaaS">CVaaS</Term> / on-prem <Term k="CloudVision">CloudVision</Term> targets for onboarding (<Term k="TerminAttr">TerminAttr</Term>) and <Term k="cv_deploy">cv_deploy</Term></>}
        actions={<button className="btn-primary" onClick={() => setEditing('new')} data-tour="cv-targets">Add target</button>}
      />
      <ErrorBox error={targets.error ?? remove.error} />
      {targets.isLoading ? <Spinner /> : !targets.data?.length ? <Empty>No CloudVision targets yet. Add a CVaaS tenant or an on-prem cluster with a service-account token.</Empty> : (
        <div className="card overflow-x-auto">
          <table className="table" data-testid="cv-targets">
            <thead><tr><th>Name</th><th>Kind</th><th>Server</th><th>Certs</th><th>Token</th><th>Onboarding token</th><th>ZTP script</th><th>Created</th><th>Connection</th><th>Actions</th></tr></thead>
            <tbody>
              {targets.data.map((t) => (
                <tr key={t.id}>
                  <td className="font-medium">{t.name}</td>
                  <td><KindBadge kind={t.kind} /></td>
                  <td className="mono">{t.server}</td>
                  <td className="text-xs">{t.verify_certs ? 'verify' : <span className="text-amber-600">no verify</span>}</td>
                  <td><TokenMark has={t.has_token} /></td>
                  <td><OnboardingTokenMark target={t} /></td>
                  <td><ZtpScriptBadge target={t} /></td>
                  <td className="text-zinc-500" title={fmtDate(t.created_at)}>{fmtAgo(t.created_at)}</td>
                  <td><TestConnection target={t} /></td>
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
      <TargetModal open={editing !== null} target={editing === 'new' ? null : editing} onClose={() => setEditing(null)} />
    </div>
  )
}

export function KindBadge({ kind }: { kind: CVKind | string }) {
  return <Badge className={kind === 'cvaas' ? 'bg-sky-100 text-sky-800 dark:bg-sky-900/60 dark:text-sky-200' : 'bg-zinc-200 dark:bg-zinc-700'}>{kind === 'cvaas' ? 'CVaaS' : 'on-prem'}</Badge>
}

function TokenMark({ has }: { has: boolean }) {
  return has ? <span className="text-emerald-600 dark:text-emerald-400">set</span> : <span className="text-zinc-400">—</span>
}

export function OnboardingTokenMark({ target: t }: { target: CVTarget }) {
  if (!t.has_onboarding_token) return <span className="text-zinc-400">—</span>
  const exp = t.onboarding_token_meta?.expires_at
  if (!exp) return <span className="text-emerald-600 dark:text-emerald-400">set</span>
  return isExpired(exp)
    ? <span className="text-red-600 dark:text-red-400" title={fmtDate(exp)}>expired {fmtAgo(exp)}</span>
    : <span className="text-emerald-600 dark:text-emerald-400" title={fmtDate(exp)}>set, expires {fmtDate(exp)}</span>
}

export function ZtpScriptBadge({ target: t }: { target: CVTarget }) {
  return t.has_ztp_script
    ? <span data-testid="ztp-script-badge" title={t.ztp_script_meta?.updated_at ? `updated ${fmtDate(t.ztp_script_meta.updated_at)}` : undefined}>
        <Badge className="bg-emerald-100 text-emerald-800 dark:bg-emerald-900/60 dark:text-emerald-200">ZTP script</Badge>
      </span>
    : <span className="text-zinc-400">—</span>
}

/** "Test connection" → POST /cv/targets/{id}/test, result shown inline. */
export function TestConnection({ target }: { target: CVTarget }) {
  const test = useMutation<CVTestResult, Error>({ mutationFn: () => cvApi.testTarget(target.id) })
  const r = test.data
  return (
    <div className="flex items-center gap-2" data-testid="cv-test">
      <button className="btn-secondary btn-sm" disabled={test.isPending || !target.has_token} onClick={() => test.mutate()}
        title={!target.has_token ? 'needs a service-account token' : 'GET /api/resources/inventory/v1/Device/all'}>
        {test.isPending ? 'Testing…' : 'Test connection'}
      </button>
      {test.error && <span className="text-xs text-red-600 dark:text-red-400">{test.error.message}</span>}
      {r && (r.ok
        ? <span className="text-xs text-emerald-600 dark:text-emerald-400">ok · {r.devices_seen ?? 0} devices</span>
        : <span className="text-xs text-red-600 dark:text-red-400" title={r.error ?? undefined}>failed{r.error ? `: ${r.error}` : ''}</span>)}
    </div>
  )
}

const empty: CVTargetIn = { name: '', kind: 'cvaas', server: 'www.arista.io', verify_certs: true }
const CUSTOM = '__custom__'

function TargetModal({ open, target, onClose }: { open: boolean; target: CVTarget | null; onClose: () => void }) {
  const qc = useQueryClient()
  const [form, setForm] = useState<CVTargetIn>(empty)
  const [token, setToken] = useState('')
  const [obToken, setObToken] = useState('')
  const [obExpires, setObExpires] = useState('')
  const [clearOb, setClearOb] = useState(false)
  const [ztpScript, setZtpScript] = useState('')
  const [clearZtp, setClearZtp] = useState(false)
  const [customServer, setCustomServer] = useState(false)
  const [last, setLast] = useState<CVTarget | null | undefined>(undefined)

  if (open && last !== target) {
    setLast(target)
    setForm(target ? { name: target.name, kind: target.kind, server: target.server, verify_certs: target.verify_certs } : empty)
    setToken(''); setObToken(''); setClearOb(false); setZtpScript(''); setClearZtp(false)
    setObExpires(target?.onboarding_token_meta?.expires_at?.slice(0, 10) ?? '')
    setCustomServer(!!target && target.kind === 'cvaas' && !CVAAS_SERVERS.some((s) => s.value === target.server))
  }
  if (!open && last !== undefined) setLast(undefined)

  const save = useMutation({
    mutationFn: () => {
      const body: CVTargetIn = { ...form, server: form.server.trim() }
      // write-only secrets: omitted = keep the stored one
      if (token) body.token = token
      if (obToken) {
        body.onboarding_token = obToken
        body.onboarding_token_expires_at = obExpires ? new Date(`${obExpires}T23:59:59Z`).toISOString() : null
      } else if (clearOb) {
        body.onboarding_token = null
      } else if (target?.has_onboarding_token && obExpires !== (target.onboarding_token_meta?.expires_at?.slice(0, 10) ?? '')) {
        body.onboarding_token_expires_at = obExpires ? new Date(`${obExpires}T23:59:59Z`).toISOString() : null
      }
      // ZTP bootstrap script (CONTRACT-4 A): write-only like the tokens
      if (ztpScript.trim()) body.ztp_script = ztpScript
      else if (clearZtp) body.ztp_script = null
      return target ? cvApi.updateTarget(target.id, body) : cvApi.createTarget(body)
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['cv'] }); onClose() },
  })
  const set = <K extends keyof CVTargetIn>(k: K, v: CVTargetIn[K]) => setForm((f) => ({ ...f, [k]: v }))
  const submit = (e: FormEvent) => { e.preventDefault(); save.mutate() }
  const preset = CVAAS_SERVERS.some((s) => s.value === form.server) && !customServer ? form.server : CUSTOM

  return (
    <Modal open={open} onClose={onClose} title={target ? `Edit ${target.name}` : 'Add CloudVision target'}>
      <form onSubmit={submit} className="space-y-3" data-testid="cv-target-form">
        <div className="grid grid-cols-2 gap-3">
          <Field label="Name"><input className="input" name="name" required value={form.name} onChange={(e) => set('name', e.target.value)} /></Field>
          <Field label="Kind">
            <select className="input" name="kind" value={form.kind} onChange={(e) => {
              const kind = e.target.value as CVKind
              setForm((f) => ({ ...f, kind, server: kind === 'cvaas' ? 'www.arista.io' : '' }))
              setCustomServer(false)
            }}>
              <option value="cvaas">CVaaS</option>
              <option value="onprem">on-prem</option>
            </select>
          </Field>
        </div>
        {form.kind === 'cvaas' ? (
          <Field label="Server" help="regional CVaaS API endpoint of your tenant">
            <select className="input" name="server_preset" value={preset} onChange={(e) => {
              if (e.target.value === CUSTOM) { setCustomServer(true) } else { setCustomServer(false); set('server', e.target.value) }
            }}>
              {CVAAS_SERVERS.map((s) => <option key={s.value} value={s.value}>{s.label === s.value ? s.value : `${s.value} — ${s.label}`}</option>)}
              <option value={CUSTOM}>custom…</option>
            </select>
            {preset === CUSTOM && (
              <input className="input mono mt-1" name="server" required value={form.server} placeholder="www.cv-prod-….arista.io" spellCheck={false} onChange={(e) => set('server', e.target.value)} />
            )}
          </Field>
        ) : (
          <Field label="Server" help="CVP cluster address (host or IP)">
            <input className="input mono" name="server" required value={form.server} placeholder="cvp.example.net" spellCheck={false} onChange={(e) => set('server', e.target.value)} />
          </Field>
        )}
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" name="verify_certs" checked={form.verify_certs} onChange={(e) => set('verify_certs', e.target.checked)} />
          <span>verify TLS certificates</span>
        </label>
        <Field label="Service-account token" help={target?.has_token ? 'a token is stored — leave blank to keep it' : 'CloudVision → Settings → Service accounts → Generate token'}>
          <input className="input mono" name="token" type="password" autoComplete="off" value={token}
            placeholder={target?.has_token ? '•••••••• (leave blank to keep)' : ''} onChange={(e) => setToken(e.target.value)} />
        </Field>
        <div className="grid grid-cols-[1fr_10rem] gap-3">
          <Field label="Onboarding token" help={target?.has_onboarding_token ? 'stored — leave blank to keep it' : 'Devices → Onboard → Generate token (used by TerminAttr)'}>
            <input className="input mono" name="onboarding_token" type="password" autoComplete="off" value={obToken} disabled={clearOb}
              placeholder={target?.has_onboarding_token ? '•••••••• (leave blank to keep)' : ''} onChange={(e) => setObToken(e.target.value)} />
          </Field>
          <Field label="Expires (optional)">
            <input className="input" name="onboarding_token_expires_at" type="date" value={obExpires} disabled={clearOb} onChange={(e) => setObExpires(e.target.value)} />
          </Field>
        </div>
        {target?.has_onboarding_token && (
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={clearOb} onChange={(e) => { setClearOb(e.target.checked); if (e.target.checked) setObToken('') }} />
            <span>remove the stored onboarding token</span>
          </label>
        )}
        <Field label={<span className="flex items-center gap-2">ZTP bootstrap script <span className="font-normal text-zinc-400">(optional, write-only)</span>{target?.has_ztp_script && <Badge className="bg-emerald-100 text-emerald-800 dark:bg-emerald-900/60 dark:text-emerald-200">stored</Badge>}</span>}
          help={target?.has_ztp_script ? 'a script is stored — leave blank to keep it' : 'CloudVision → Devices → Onboard → ZTP: the script served to cEOS in provisioning mode ztp (source cloudvision)'}>
          <textarea className="input mono h-28 text-xs" name="ztp_script" value={ztpScript} disabled={clearZtp} spellCheck={false} autoComplete="off"
            placeholder={target?.has_ztp_script ? '(stored — not shown)' : '#!/usr/bin/env python3\n…'} onChange={(e) => setZtpScript(e.target.value)} />
        </Field>
        {target?.has_ztp_script && (
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" name="clear_ztp_script" checked={clearZtp} onChange={(e) => { setClearZtp(e.target.checked); if (e.target.checked) setZtpScript('') }} />
            <span>remove the stored ZTP script</span>
          </label>
        )}
        <ErrorBox error={save.error} />
        <div className="flex justify-end gap-2">
          <button type="button" className="btn-secondary" onClick={onClose}>Cancel</button>
          <button type="submit" className="btn-primary" disabled={save.isPending || !form.name || !form.server.trim()}>{save.isPending ? 'Saving…' : 'Save'}</button>
        </div>
      </form>
    </Modal>
  )
}
