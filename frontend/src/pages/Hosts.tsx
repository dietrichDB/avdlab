import { useState, type FormEvent, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { hostsApi } from '../api/hosts'
import { secretsApi } from '../api/secrets'
import type { BootstrapIn, BootstrapRecipe, GeneratedKey, Host, HostCapabilities, HostIn } from '../api/types'
import { useJobDrawer } from '../components/JobDrawer'
import { Badge, ConfirmButton, CopyButton, Empty, ErrorBox, Field, Hint, KV, Modal, PageHeader, Spinner } from '../components/ui'
import { cls, fmtAgo, fmtDate, fmtKb } from '../lib/format'

const PLATFORMS: { id: NonNullable<HostIn['platform']>; title: string; sub: string; beta?: boolean; steps: string[] }[] = [
  { id: 'linux', title: 'Linux server or VM', sub: 'Debian/Ubuntu/RHEL with SSH — the normal case', steps: [] },
  { id: 'mac', title: 'Mac (OrbStack VM)', sub: 'Apple Silicon or Intel; the lab runs in an OrbStack Linux VM', beta: true, steps: [
    'On the Mac: install OrbStack (orbstack.dev) and open it once.',
    'Terminal: orb create ubuntu avdlab-vm',
    'Terminal: orb -m avdlab-vm sudo apt-get install -y openssh-server && orb -m avdlab-vm sudo systemctl enable --now ssh',
    'Give the VM memory in OrbStack → Settings (16 GB+ for an 8-switch fabric).',
    'Below: Address avdlab-vm.orb.local, SSH user = your Mac user name (OrbStack creates it in the VM with your SSH key), Jump host = you@<the Mac>.',
    'Save → Probe → Bootstrap. On Apple Silicon use cEOSarm images (New lab switches to arm64 automatically).',
  ] },
  { id: 'windows', title: 'Windows (WSL2)', sub: 'the lab runs in the Containerlab WSL2 distribution', beta: true, steps: [
    'PowerShell: wsl --install, then install the Containerlab distribution from containerlab.dev/windows (wsl --install --from-file clab.wsl); WSL ≥ 2.4.4.',
    'In the distro: sudo apt-get install -y openssh-server && sudo systemctl enable --now ssh. Keep Docker Desktop\'s WSL integration OFF for this distro.',
    'Make it reachable: %UserProfile%\\.wslconfig → [wsl2] networkingMode=mirrored, memory=20GB; Windows Firewall rule for TCP 22 — or run OpenSSH on Windows and use it as the Jump host with Address localhost.',
    'Below: Address = the PC (mirrored) or localhost (jump), SSH user = your WSL user, Jump host = you@<the PC> if used.',
    'Save → Probe → Bootstrap.',
  ] },
]

const emptyHost: HostIn = { name: '', kind: 'linux', address: '', ssh_port: 22, ssh_user: 'ubuntu', ssh_key_secret_id: null, password_secret_id: null, sudo_password_secret_id: null, jump_host: null, platform: 'linux' }

export default function HostsPage() {
  const qc = useQueryClient()
  const hosts = useQuery({ queryKey: ['hosts'], queryFn: hostsApi.list })
  const [editing, setEditing] = useState<Host | 'new' | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [keyModal, setKeyModal] = useState(false)
  const [bootstrapFor, setBootstrapFor] = useState<Host | null>(null)
  const drawer = useJobDrawer()

  const remove = useMutation({
    mutationFn: (id: string) => hostsApi.remove(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['hosts'] }),
  })
  const act = useMutation({
    mutationFn: ({ id, action }: { id: string; action: 'probe' | 'verify' }) => drawer.track(action === 'probe' ? hostsApi.probe(id) : hostsApi.verify(id)),
  })

  const sel = hosts.data?.find((h) => h.id === selected) ?? null

  return (
    <div>
      <PageHeader
        title="Hosts"
        sub="Lab hosts reached over SSH"
        actions={<>
          <button className="btn-secondary" onClick={() => setKeyModal(true)} data-tour="generate-key">Generate SSH key</button>
          <button className="btn-primary" onClick={() => setEditing('new')} data-tour="add-host">Add host</button>
        </>}
      />
      <Hint title="Hosts run your labs.">Add a Linux machine with Docker that avdlab can reach over SSH — click <b>Add host</b>, then <b>Bootstrap</b> to install containerlab on it.</Hint>
      <ErrorBox error={hosts.error ?? remove.error ?? act.error} />
      {hosts.isLoading ? <Spinner /> : !hosts.data?.length ? <Empty>No hosts yet. Add a Linux box with Docker + SSH.</Empty> : (
        <div className="card overflow-x-auto">
          <table className="table">
            <thead>
              <tr><th>Name</th><th>Kind</th><th>SSH</th><th>Auth</th><th>OS</th><th>Docker</th><th>containerlab</th><th>Recs</th><th>Last probe</th><th>Actions</th></tr>
            </thead>
            <tbody>
              {hosts.data.map((h) => {
                const docker = h.summary?.docker ?? null
                const clab = h.summary?.containerlab ?? null
                const recs = h.summary?.recommendations ?? 0
                return (
                  <tr key={h.id} className={cls(selected === h.id && 'bg-blue-50 dark:bg-blue-950/30')}>
                    <td><button className="link font-medium" onClick={() => setSelected(h.id)}>{h.name}</button>{h.summary?.platform && h.summary.platform !== 'linux' && <span className="badge ml-1 bg-violet-100 text-violet-800 dark:bg-violet-900/50 dark:text-violet-200" data-testid="platform-badge" title="Linux VM on a desktop OS — beta, not yet field-tested">{h.summary.platform_label}{h.summary.beta ? ' · beta' : ''}</span>}</td>
                    <td><Badge className="bg-zinc-200 dark:bg-zinc-700">{h.kind}</Badge></td>
                    <td className="mono">{h.ssh_user}@{h.address}:{h.ssh_port}</td>
                    <td className="text-xs text-zinc-500">{h.ssh_key_secret_id ? 'key' : h.password_secret_id ? 'password' : <span className="text-amber-600">none</span>}</td>
                    <td className="text-xs">{h.summary?.os ?? <span className="text-zinc-400">?</span>}</td>
                    <td className="mono">{docker ?? <span className="text-zinc-400">?</span>}</td>
                    <td className="mono">{clab ?? <span className="text-zinc-400">?</span>}</td>
                    <td>{h.last_probe_at ? (recs ? <Badge className="bg-amber-100 text-amber-800 dark:bg-amber-900/60 dark:text-amber-200">{recs}</Badge> : <span className="text-emerald-600">ok</span>) : <span className="text-zinc-400">—</span>}</td>
                    <td className="text-zinc-500" title={fmtDate(h.last_probe_at)}>{fmtAgo(h.last_probe_at)}</td>
                    <td className="whitespace-nowrap">
                      <div className="flex gap-1">
                        <Link className="btn-secondary btn-sm" to={`/hosts/${h.id}`} title="metrics, processes, containers, terminal">Monitor</Link>
                        <button className="btn-secondary btn-sm" onClick={() => act.mutate({ id: h.id, action: 'probe' })} data-tour="probe-button">Probe</button>
                        <button className="btn-secondary btn-sm" onClick={() => setBootstrapFor(h)} data-tour="bootstrap-button">Bootstrap</button>
                        <button className="btn-secondary btn-sm" onClick={() => act.mutate({ id: h.id, action: 'verify' })}>Verify</button>
                        <button className="btn-secondary btn-sm" onClick={() => setEditing(h)}>Edit</button>
                        <ConfirmButton onConfirm={() => remove.mutate(h.id)}>Delete</ConfirmButton>
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {sel && <CapabilitiesPanel host={sel} onClose={() => setSelected(null)} />}

      <HostModal open={editing !== null} host={editing === 'new' ? null : editing} onClose={() => setEditing(null)} />
      <KeyModal open={keyModal} onClose={() => setKeyModal(false)} />
      <BootstrapModal host={bootstrapFor} onClose={() => setBootstrapFor(null)} />
    </div>
  )
}

function yesNo(v: boolean | undefined | null, bad = 'no'): ReactNode {
  if (v == null) return '—'
  return v ? 'yes' : <span className="text-red-600 dark:text-red-400">{bad}</span>
}

// ---------------------------------------------------------------- capabilities panel
export function CapabilitiesPanel({ host, onClose }: { host: Host; onClose: () => void }) {
  const caps = useQuery({ queryKey: ['hosts', host.id, 'capabilities'], queryFn: () => hostsApi.capabilities(host.id) })
  const c: HostCapabilities = caps.data?.capabilities ?? {}
  const recs = c.recommendations ?? []
  const d = c.docker ?? {}
  return (
    <section className="card mt-4" data-testid="capabilities" data-tour="capabilities">
      <div className="flex items-center justify-between gap-2 border-b border-zinc-200 px-3 py-2 dark:border-zinc-800">
        <span className="h2 flex-1">Capabilities — {host.name}</span>
        <span className="text-xs text-zinc-500">probed {fmtAgo(caps.data?.last_probe_at ?? host.last_probe_at)}</span>
        <button className="btn-secondary btn-sm" onClick={onClose}>Close</button>
      </div>
      {caps.isLoading ? <Spinner /> : !Object.keys(c).length ? <div className="p-4 text-sm text-zinc-500">Not probed yet. Run Probe to collect host facts.</div> : (
        <div className="grid gap-4 p-3 md:grid-cols-3">
          <div>
            <div className="h2 mb-1">System</div>
            <KV items={[
              ['OS', c.os?.name], ['Family', c.os?.family], ['Kernel', c.os?.kernel], ['Arch', c.os?.arch], ['CPUs', c.cpu?.nproc],
              ['Memory', c.mem?.total_gb != null ? `${c.mem.total_gb} GB` : fmtKb(c.mem?.total_kb)],
              ['Disk / free', fmtKb(c.disk?.root_free_kb)], ['Disk docker free', fmtKb(c.disk?.docker_free_kb)],
              ['python3', c.python3?.version ?? yesNo(c.python3?.installed, 'missing')], ['rsync', yesNo(c.rsync)],
              ['sudo -n', yesNo(c.sudo_nopasswd)], ['pipx', yesNo(c.pipx)],
              [c.remote_root?.path ?? 'remote root', c.remote_root ? (c.remote_root.exists ? 'exists' : <span className="text-amber-600">missing</span>) : '—'],
            ]} />
          </div>
          <div>
            <div className="h2 mb-1">Toolchain</div>
            <KV items={[
              ['Docker server', d.server_version ?? <span className="text-red-600 dark:text-red-400">missing</span>],
              ['Docker client', d.client_version],
              ['user can run docker', yesNo(d.user_can_run)],
              ['in docker group', yesNo(d.user_in_group)],
              ['containerlab', c.containerlab?.version ?? <span className="text-red-600 dark:text-red-400">missing</span>],
              ['cEOS images', c.images?.length ? <ul className="mono">{c.images.map((i) => <li key={i}>{i}</li>)}</ul> : 'none'],
            ]} />
            <div className="h2 mb-1 mt-3">sysctl</div>
            <KV items={Object.entries(c.sysctl ?? {}).map(([k, v]) => [<span className="mono" key={k}>{k}</span>, <span className="mono">{v == null ? '—' : String(v)}</span>])} />
          </div>
          <div>
            <div className="h2 mb-1">Recommendations</div>
            {recs.length === 0 ? <div className="text-sm text-emerald-600">No recommendations — host looks ready.</div> : (
              <ul className="space-y-1 text-sm" data-testid="recommendations">
                {recs.map((r, i) => (
                  <li key={i} className="rounded border border-amber-300 bg-amber-50 px-2 py-1 text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-100">{r}</li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </section>
  )
}

// ---------------------------------------------------------------- add/edit host
function HostModal({ open, host, onClose }: { open: boolean; host: Host | null; onClose: () => void }) {
  const qc = useQueryClient()
  const secrets = useQuery({ queryKey: ['secrets'], queryFn: secretsApi.list, enabled: open })
  const [form, setForm] = useState<HostIn>(emptyHost)
  const [authMode, setAuthMode] = useState<'key' | 'password'>('key')
  const [newPassword, setNewPassword] = useState('')
  const [newSudoPassword, setNewSudoPassword] = useState('')
  const [lastHost, setLastHost] = useState<Host | null | undefined>(undefined)

  // reset when the modal target changes
  if (open && lastHost !== host) {
    setLastHost(host)
    setForm(host ? { name: host.name, kind: host.kind, address: host.address, ssh_port: host.ssh_port, ssh_user: host.ssh_user, ssh_key_secret_id: host.ssh_key_secret_id, password_secret_id: host.password_secret_id, sudo_password_secret_id: host.sudo_password_secret_id ?? null, jump_host: host.jump_host ?? null, platform: host.platform ?? 'linux' } : emptyHost)
    setAuthMode(host?.password_secret_id ? 'password' : 'key')
    setNewPassword('')
    setNewSudoPassword('')
  }
  if (!open && lastHost !== undefined) setLastHost(undefined)

  const save = useMutation({
    mutationFn: async () => {
      const body: HostIn = { ...form }
      if (authMode === 'password') {
        body.ssh_key_secret_id = null
        if (newPassword) {
          const s = await secretsApi.create({ name: `host-${form.name}-password-${Date.now().toString(36)}`, kind: 'password', value: newPassword })
          body.password_secret_id = s.id
        }
      } else {
        body.password_secret_id = null
      }
      if (newSudoPassword) {
        const s = await secretsApi.create({ name: `host-${form.name}-sudo-${Date.now().toString(36)}`, kind: 'password', value: newSudoPassword })
        body.sudo_password_secret_id = s.id
      }
      return host ? hostsApi.update(host.id, body) : hostsApi.create(body)
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['hosts'] })
      onClose()
    },
  })

  const keys = (secrets.data ?? []).filter((s) => s.kind === 'ssh_key')
  const passwords = (secrets.data ?? []).filter((s) => s.kind === 'password')
  const set = <K extends keyof HostIn>(k: K, v: HostIn[K]) => setForm((f) => ({ ...f, [k]: v }))

  const submit = (e: FormEvent) => {
    e.preventDefault()
    save.mutate()
  }

  return (
    <Modal open={open} onClose={onClose} title={host ? `Edit ${host.name}` : 'Add host'}>
      <form onSubmit={submit} className="space-y-3">
        {!host && (
          <div data-testid="platform-picker">
            <span className="label">What is this host?</span>
            <div className="grid gap-2 sm:grid-cols-3">
              {PLATFORMS.map((p) => (
                <button key={p.id} type="button" onClick={() => { set('platform', p.id); if (p.id === 'linux') set('jump_host', null) }}
                  className={cls('rounded-lg border p-3 text-left text-sm transition-colors', form.platform === p.id ? 'border-cv-primary bg-cv-primary-soft' : 'border-cv-border hover:border-cv-border-strong')}
                  data-testid={`platform-${p.id}`} aria-pressed={form.platform === p.id}>
                  <div className="flex items-center gap-1.5 font-semibold">{p.title}{p.beta && <span className="badge bg-violet-100 text-[10px] text-violet-800 dark:bg-violet-900/50 dark:text-violet-200">beta</span>}</div>
                  <div className="mt-0.5 text-xs text-cv-muted">{p.sub}</div>
                </button>
              ))}
            </div>
          </div>
        )}
        {form.platform && form.platform !== 'linux' && (
          <div className="rounded-lg border border-violet-200 bg-violet-50 p-3 text-xs text-violet-950 dark:border-violet-900 dark:bg-violet-950/40 dark:text-violet-100" data-testid="platform-guide">
            <div className="mb-1 font-semibold">{PLATFORMS.find((p) => p.id === form.platform)!.title}: the lab runs in a Linux VM on that machine. Set it up once:</div>
            <ol className="list-decimal space-y-1 pl-5">
              {PLATFORMS.find((p) => p.id === form.platform)!.steps.map((st, i) => <li key={i}>{st}</li>)}
            </ol>
            <div className="mt-1 opacity-80">Beta: not yet field-tested — if something fails, the probe output and the Feedback button help us fix it.</div>
          </div>
        )}
        <div className="grid grid-cols-2 gap-3">
          <Field label="Name"><input className="input" required value={form.name} onChange={(e) => set('name', e.target.value)} /></Field>
          <Field label="Kind" help={form.kind === 'act' ? "ACT tools server: use the public SSH address ACT shows; bootstrap with recipe act-tools" : undefined}>
            <select className="input" name="kind" value={form.kind} onChange={(e) => set('kind', e.target.value as HostIn['kind'])}>
              <option value="linux">containerlab host</option>
              <option value="act">act (ACT tools server)</option>
            </select>
          </Field>
          <Field label={form.platform === 'mac' ? 'Address of the VM (as seen from the Mac)' : form.platform === 'windows' ? 'Address of the WSL distro' : 'Address'}
            help={form.platform === 'mac' ? 'OrbStack names the VM <name>.orb.local' : form.platform === 'windows' ? 'with mirrored networking: the PC\'s address; otherwise localhost:<port> via the jump host' : undefined}>
            <input className="input mono" required value={form.address} onChange={(e) => set('address', e.target.value)} placeholder={form.platform === 'mac' ? 'avdlab-vm.orb.local' : form.platform === 'windows' ? 'pc.example.net or localhost' : '10.0.0.5 or lab.example.net'} />
          </Field>
          <div className="grid grid-cols-2 gap-2">
            <Field label="SSH user"><input className="input mono" required value={form.ssh_user} onChange={(e) => set('ssh_user', e.target.value)} /></Field>
            <Field label="Port"><input className="input mono" type="number" min={1} max={65535} value={form.ssh_port} onChange={(e) => set('ssh_port', Number(e.target.value))} /></Field>
          </div>
          {form.platform !== 'linux' && (
            <Field label={<>Jump host <span className="font-normal text-cv-muted">({form.platform === 'mac' ? 'the Mac' : 'the Windows PC'}, optional when the VM is reachable directly)</span></>}
              help="avdlab connects to this machine first with the same key/password and reaches the VM through it">
              <input className="input mono" value={form.jump_host ?? ''} onChange={(e) => set('jump_host', e.target.value || null)} placeholder={form.platform === 'mac' ? 'you@mac-studio.local' : 'you@pc.example.net'} data-testid="host-jump" />
            </Field>
          )}
        </div>
        <div>
          <span className="label">Authentication</span>
          <div className="mb-2 flex gap-3 text-sm">
            <label className="flex items-center gap-1"><input type="radio" checked={authMode === 'key'} onChange={() => setAuthMode('key')} /> SSH key</label>
            <label className="flex items-center gap-1"><input type="radio" checked={authMode === 'password'} onChange={() => setAuthMode('password')} /> Password</label>
          </div>
          {authMode === 'key' ? (
            <Field label="Key secret" help="Generate one with “Generate SSH key” and paste the public key into authorized_keys on the host.">
              <select className="input" value={form.ssh_key_secret_id ?? ''} onChange={(e) => set('ssh_key_secret_id', e.target.value || null)}>
                <option value="">— none —</option>
                {keys.map((k) => <option key={k.id} value={k.id}>{k.name}</option>)}
              </select>
            </Field>
          ) : (
            <div className="grid grid-cols-2 gap-2">
              <Field label="Existing password secret">
                <select className="input" value={form.password_secret_id ?? ''} onChange={(e) => set('password_secret_id', e.target.value || null)}>
                  <option value="">— none —</option>
                  {passwords.map((k) => <option key={k.id} value={k.id}>{k.name}</option>)}
                </select>
              </Field>
              <Field label="…or new password" help="stored encrypted; never shown again">
                <input className="input" type="password" value={newPassword} autoComplete="new-password" onChange={(e) => setNewPassword(e.target.value)} />
              </Field>
            </div>
          )}
        </div>
        <div>
          <span className="label">sudo</span>
          <p className="mb-2 text-xs text-zinc-500">Bootstrap needs root. Leave empty if the user has passwordless sudo (NOPASSWD); with SSH password auth the SSH password is reused for sudo unless you set one here.</p>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Existing sudo password secret">
              <select className="input" value={form.sudo_password_secret_id ?? ''} onChange={(e) => set('sudo_password_secret_id', e.target.value || null)}>
                <option value="">— none / NOPASSWD —</option>
                {passwords.map((k) => <option key={k.id} value={k.id}>{k.name}</option>)}
              </select>
            </Field>
            <Field label="…or new sudo password" help="stored encrypted; never shown again">
              <input className="input" type="password" value={newSudoPassword} autoComplete="new-password" onChange={(e) => setNewSudoPassword(e.target.value)} />
            </Field>
          </div>
        </div>
        <ErrorBox error={save.error} />
        <div className="flex justify-end gap-2">
          <button type="button" className="btn-secondary" onClick={onClose}>Cancel</button>
          <button className="btn-primary" disabled={save.isPending}>{host ? 'Save' : 'Create'}</button>
        </div>
      </form>
    </Modal>
  )
}

// ---------------------------------------------------------------- generate key
function KeyModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient()
  const [key, setKey] = useState<GeneratedKey | null>(null)
  const gen = useMutation({
    mutationFn: hostsApi.generateKey,
    onSuccess: (k) => {
      setKey(k)
      qc.invalidateQueries({ queryKey: ['secrets'] })
    },
  })
  const close = () => { setKey(null); gen.reset(); onClose() }
  return (
    <Modal open={open} onClose={close} title="Generate SSH key" width="max-w-2xl">
      <p className="mb-3 text-sm text-zinc-600 dark:text-zinc-300">
        Creates an ed25519 keypair. The private key is stored encrypted in the secret store; add the public key to
        <code className="mono mx-1">~/.ssh/authorized_keys</code> of the SSH user on each lab host.
      </p>
      {!key ? (
        <div className="flex items-center gap-2">
          <button className="btn-primary" onClick={() => gen.mutate()} disabled={gen.isPending}>{gen.isPending ? 'Generating…' : 'Generate'}</button>
          <ErrorBox error={gen.error} />
        </div>
      ) : (
        <div className="space-y-2">
          <div className="text-xs text-zinc-500">secret <span className="mono">{key.name}</span> · id <span className="mono">{key.secret_id}</span> — select it as “Key secret” when adding a host</div>
          <textarea readOnly className="input mono h-24" value={key.public_key} onFocus={(e) => e.target.select()} data-testid="public-key" />
          <div className="flex items-center gap-2">
            <CopyButton text={key.public_key} label="Copy public key" />
            <CopyButton text={`echo '${key.public_key}' >> ~/.ssh/authorized_keys`} label="Copy install command" />
            <span className="flex-1" />
            <button className="btn-primary" onClick={close}>Done</button>
          </div>
        </div>
      )}
    </Modal>
  )
}

// ---------------------------------------------------------------- bootstrap options
function BootstrapModal({ host, onClose }: { host: Host | null; onClose: () => void }) {
  const drawer = useJobDrawer()
  const [opts, setOpts] = useState<BootstrapIn['options']>({ native_toolchain: false, clab_mode: 'native' })
  const [recipe, setRecipe] = useState<BootstrapRecipe>('clab-host')
  const [last, setLast] = useState<Host | null>(null)
  if (host !== last) { setLast(host); if (host) setRecipe(host.kind === 'act' ? 'act-tools' : 'clab-host') }
  const run = useMutation({
    mutationFn: () => drawer.track(hostsApi.bootstrap(host!.id, { recipe, options: opts })),
    onSuccess: onClose,
  })
  const clab = recipe === 'clab-host'
  return (
    <Modal open={!!host} onClose={onClose} title={`Bootstrap ${host?.name ?? ''}`}>
      <div className="space-y-3 text-sm">
        <Field label="Recipe">
          <select className="input" name="recipe" value={recipe} onChange={(e) => setRecipe(e.target.value as BootstrapRecipe)}>
            <option value="clab-host">clab-host (containerlab lab host)</option>
            <option value="act-tools">act-tools (ACT tools server)</option>
          </select>
        </Field>
        {clab
          ? <p className="text-zinc-600 dark:text-zinc-300">Recipe <span className="mono">clab-host</span>: base packages, Docker CE, containerlab, sysctls, <span className="mono">/opt/avdlab</span>, agent image. Idempotent — safe to re-run.</p>
          : <p className="text-zinc-600 dark:text-zinc-300" data-testid="act-tools-help">Recipe <span className="mono">act-tools</span>: Docker CE, <span className="mono">/opt/avdlab</span> and the agent image only (no containerlab, no sysctls) — for the ACT lab&apos;s tools server, from which every switch mgmt IP is reachable. Idempotent.</p>}
        {host && host.kind !== 'act' && !clab && <p className="text-xs text-amber-600">This host is kind {host.kind}: act-tools is meant for hosts of kind act.</p>}
        {clab && (<>
          <Field label="containerlab mode">
            <select className="input" value={opts.clab_mode} onChange={(e) => setOpts({ ...opts, clab_mode: e.target.value as 'native' | 'container' })}>
              <option value="native">native install (containerlab.dev/setup)</option>
              <option value="container">container wrapper (ghcr.io/srl-labs/clab)</option>
            </select>
          </Field>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={opts.native_toolchain} onChange={(e) => setOpts({ ...opts, native_toolchain: e.target.checked })} />
            Also install the native toolchain (pipx pyavd[ansible], anta, arista.avd collection)
          </label>
        </>)}
        <ErrorBox error={run.error} />
        <div className="flex justify-end gap-2">
          <button className="btn-secondary" onClick={onClose}>Cancel</button>
          <button className="btn-primary" onClick={() => run.mutate()} disabled={run.isPending}>Run bootstrap</button>
        </div>
      </div>
    </Modal>
  )
}
