import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError } from '../api/client'
import { nodesApi } from '../api/nodes'
import { topologyApi } from '../api/topology'
import { topologyEditApi } from '../api/phase5'
import { testHostApi, type TestHostIn } from '../api/phase6'
import type { Lab, LabClient } from '../api/types'
import { useJobDrawer } from './JobDrawer'
import { TerminalPanel } from './TerminalPanel'
import { PortPicker } from './TopologyEditor'
import { Badge, ConfirmButton, ErrorBox, KV } from './ui'
import { roleOf } from '../lib/topology'
import { fmtMbps } from '../lib/traffic'
import { isRunning } from '../lib/labActions'
import { cls } from '../lib/format'

const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$/
const VLANS_RE = /^\d{1,4}(-\d{1,4})?(,\d{1,4}(-\d{1,4})?)*$/
const CIDR_RE = /^(\d{1,3}\.){3}\d{1,3}\/\d{1,2}$/

export interface TestHostForm { name: string; switch: string; switch_port: string; vlans: string; mode: 'access' | 'trunk'; ipMode: 'auto' | 'manual'; ip: string; image: string }

export function testHostErrors(f: TestHostForm, existing: string[] = []): string[] {
  const e: string[] = []
  if (!NAME_RE.test(f.name)) e.push("name: letters, digits, '.', '_' and '-' only")
  else if (existing.includes(f.name)) e.push(`name: ${f.name} already exists`)
  if (!f.switch) e.push('pick a switch')
  if (!f.switch_port) e.push('pick a free switch port')
  if (!VLANS_RE.test(f.vlans)) e.push('VLANs: e.g. 11 (access) or 11-12 (trunk)')
  else if (f.mode === 'access' && /[-,]/.test(f.vlans)) e.push('access mode takes a single VLAN')
  if (f.ipMode === 'manual' && !CIDR_RE.test(f.ip)) e.push('IP: a.b.c.d/nn')
  return e
}

export function testHostPayload(f: TestHostForm): TestHostIn {
  return { name: f.name, switch: f.switch, switch_port: f.switch_port, vlans: f.vlans, mode: f.mode, ip: f.ipMode === 'auto' ? 'auto' : f.ip, image: f.image.trim() || null, deploy: true }
}

/** "Add test host": plug a linux client into a free switch port of the running fabric (POST /labs/{id}/clients). */
export function AddTestHostForm({ lab, initial, onDone, onCancel }: { lab: Lab; initial?: { switch?: string; port?: string }; onDone?: () => void; onCancel: () => void }) {
  const drawer = useJobDrawer()
  const qc = useQueryClient()
  const topo = useQuery({ queryKey: ['topology', lab.id], queryFn: () => topologyApi.get(lab.id) })
  const ports = useQuery({ queryKey: ['topology', lab.id, 'ports'], queryFn: () => topologyEditApi.ports(lab.id) })
  const switches = useMemo(() => (topo.data?.nodes ?? []).filter((n) => ['l3leaf', 'l2leaf'].includes(roleOf(n))).map((n) => n.name).sort((a, b) => a.localeCompare(b, undefined, { numeric: true })), [topo.data])
  const existing = useMemo(() => (topo.data?.nodes ?? []).map((n) => n.name), [topo.data])
  const [f, setF] = useState<TestHostForm>(() => ({
    name: '', switch: initial?.switch ?? '', switch_port: initial?.port ?? '', vlans: '11', mode: 'access', ipMode: 'auto', ip: '',
    image: String((lab.knobs ?? {}).client_image ?? ''),
  }))
  const [touched, setTouched] = useState(false)
  const set = (p: Partial<TestHostForm>) => setF((x) => ({ ...x, ...p }))
  const errors = testHostErrors(f, existing)
  const add = useMutation({
    mutationFn: () => testHostApi.add(lab.id, testHostPayload(f)),
    onSuccess: (r) => {
      if (r.job_id) drawer.openJob(r.job_id)
      qc.invalidateQueries({ queryKey: ['topology', lab.id] })
      qc.invalidateQueries({ queryKey: ['labs', lab.id, 'clients'] })
      onDone?.()
    },
  })
  const deployed = isRunning(lab.state)
  return (
    <form className="card space-y-2 p-3 text-xs" data-testid="add-test-host-form"
      onSubmit={(e) => { e.preventDefault(); setTouched(true); if (!errors.length) add.mutate() }}>
      <div className="flex items-center gap-2">
        <span className="flex-1 font-medium">Add test host <span className="font-normal text-zinc-500">— hot-plugged into the running fabric (only the switch's config is rebuilt and pushed)</span></span>
      </div>
      {!deployed && <div className="rounded bg-amber-50 px-2 py-1 text-amber-900 dark:bg-amber-950/40 dark:text-amber-100">The lab is {lab.state}: deploy it first.</div>}
      <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
        <label className="block"><span className="label">Name</span>
          <input className="input py-0.5 text-xs" value={f.name} placeholder="test1" onChange={(e) => set({ name: e.target.value })} data-testid="th-name" /></label>
        <label className="block"><span className="label">Switch</span>
          <select className="input py-0.5 text-xs" value={f.switch} onChange={(e) => set({ switch: e.target.value, switch_port: '' })} data-testid="th-switch">
            <option value="">leaf…</option>
            {switches.map((s) => <option key={s}>{s}</option>)}
          </select></label>
        <label className="block"><span className="label">Switch port</span>
          {f.switch ? <PortPicker node={f.switch} ports={ports.data} ops={[]} value={f.switch_port} onChange={(p) => set({ switch_port: p })} testId="th-port" />
            : <select className="input py-0.5 text-xs" disabled data-testid="th-port"><option>pick a switch</option></select>}</label>
        <label className="block"><span className="label">Mode</span>
          <select className="input py-0.5 text-xs" value={f.mode} onChange={(e) => set({ mode: e.target.value as TestHostForm['mode'] })} data-testid="th-mode">
            <option value="access">access</option><option value="trunk">trunk</option>
          </select></label>
        <label className="block"><span className="label">VLAN(s)</span>
          <input className="input mono py-0.5 text-xs" value={f.vlans} placeholder={f.mode === 'access' ? '11' : '11-12'} onChange={(e) => set({ vlans: e.target.value.trim() })} data-testid="th-vlans" /></label>
        <label className="block"><span className="label">IP</span>
          <span className="flex gap-1">
            <select className="input w-24 py-0.5 text-xs" value={f.ipMode} onChange={(e) => set({ ipMode: e.target.value as TestHostForm['ipMode'] })} data-testid="th-ip-mode">
              <option value="auto">auto</option><option value="manual">manual</option>
            </select>
            {f.ipMode === 'manual' && <input className="input mono py-0.5 text-xs" value={f.ip} placeholder="10.10.11.50/24" onChange={(e) => set({ ip: e.target.value.trim() })} data-testid="th-ip" />}
          </span></label>
        <label className="col-span-2 block"><span className="label">Image</span>
          <input className="input mono py-0.5 text-xs" value={f.image} placeholder="ghcr.io/srl-labs/network-multitool:latest" onChange={(e) => set({ image: e.target.value })} data-testid="th-image" /></label>
      </div>
      {f.ipMode === 'auto' && <div className="text-zinc-500">auto: the next free host address of the VLAN's SVI subnet; the gateway is the anycast SVI address.</div>}
      {touched && errors.length > 0 && <ul className="list-disc pl-4 text-red-600 dark:text-red-400" data-testid="th-errors">{errors.map((x) => <li key={x}>{x}</li>)}</ul>}
      {add.error && <ErrorBox error={add.error instanceof ApiError && add.error.violations.length ? add.error.violations.map((v) => v.message).join('; ') : add.error} />}
      <div className="flex justify-end gap-2">
        <button type="button" className="btn-secondary btn-sm" onClick={onCancel}>Cancel</button>
        <button className="btn-primary btn-sm" disabled={!deployed || add.isPending} data-testid="th-deploy">{add.isPending ? 'Starting…' : 'Deploy now'}</button>
      </div>
    </form>
  )
}

const firstVlan = (c: LabClient) => c.vlans.find((v) => v.ip) ?? c.vlans[0]

/** Quick actions of one client: ping (client or IP), iperf (to a client), exec one-liner, terminal, remove. Results inline. */
export function ClientActions({ lab, client, clients, compact = false }: { lab: Lab; client: LabClient; clients: LabClient[]; compact?: boolean }) {
  const drawer = useJobDrawer()
  const qc = useQueryClient()
  const others = clients.filter((c) => c.name !== client.name && c.vlans.some((v) => v.ip))
  const [pingDst, setPingDst] = useState(others[0]?.name ?? '')
  const [pingIp, setPingIp] = useState('')
  const [iperfDst, setIperfDst] = useState(others[0]?.name ?? '')
  const [secs, setSecs] = useState(5)
  const [cmd, setCmd] = useState('ip -br addr')
  const [term, setTerm] = useState(false)
  const up = isRunning(lab.state) && client.deployed !== false && (client.container_state ?? 'running') === 'running'
  const ping = useMutation({ mutationFn: () => testHostApi.ping(lab.id, client.name, pingDst === '__ip' ? pingIp : pingDst) })
  const iperf = useMutation({ mutationFn: () => testHostApi.iperf(lab.id, client.name, iperfDst, secs) })
  const exec = useMutation({ mutationFn: () => testHostApi.exec(lab.id, client.name, cmd) })
  const remove = useMutation({
    mutationFn: () => testHostApi.remove(lab.id, client.name),
    onSuccess: (r) => {
      if (r.job_id) drawer.openJob(r.job_id)
      qc.invalidateQueries({ queryKey: ['labs', lab.id, 'clients'] })
      qc.invalidateQueries({ queryKey: ['topology', lab.id] })
    },
  })
  const v = firstVlan(client)
  return (
    <div className="space-y-2 text-xs" data-testid={`client-actions-${client.name}`}>
      {!compact && (
        <KV items={[
          ['IP', <span className="mono" data-testid="ca-ip">{v?.ip ?? '—'}</span>],
          ['gateway', <span className="mono">{v?.gateway ?? '—'}</span>],
          ['state', <span data-testid="ca-state">{client.container_state ?? (client.deployed ? 'running' : 'not deployed')}{client.hot_added && <Badge className="ml-1 bg-cyan-100 text-cyan-800 dark:bg-cyan-900/60 dark:text-cyan-200">test host</Badge>}</span>],
          ['links', <span className="mono">{client.links.map((l) => `${l.eth}→${l.switch}:${l.switch_port}`).join(', ') || '—'}</span>],
        ]} />
      )}
      {!up && <div className="text-zinc-500">Actions need the lab and this client running.</div>}
      <div className="flex flex-wrap items-center gap-1">
        <span className="w-12 font-medium">ping</span>
        <select className="input w-auto py-0.5 text-xs" value={pingDst} onChange={(e) => setPingDst(e.target.value)} aria-label="ping destination" data-testid="ca-ping-dst">
          {others.map((c) => <option key={c.name} value={c.name}>{c.name}</option>)}
          <option value="__ip">IP address…</option>
        </select>
        {pingDst === '__ip' && <input className="input mono w-32 py-0.5 text-xs" placeholder="10.10.11.1" value={pingIp} onChange={(e) => setPingIp(e.target.value.trim())} aria-label="ping IP" data-testid="ca-ping-ip" />}
        <button className="btn-secondary btn-sm" disabled={!up || ping.isPending || !(pingDst === '__ip' ? pingIp : pingDst)} onClick={() => ping.mutate()} data-testid="ca-ping">{ping.isPending ? '…' : 'Ping'}</button>
        {ping.data && (
          <span className={cls('mono', ping.data.ok ? 'text-emerald-700 dark:text-emerald-300' : 'text-red-700 dark:text-red-300')} data-testid="ca-ping-result">
            {ping.data.target_ip}: {ping.data.received}/{ping.data.sent} received{ping.data.rtt_avg_ms != null ? `, avg ${ping.data.rtt_avg_ms.toFixed(2)} ms` : ''}
          </span>
        )}
      </div>
      <ErrorBox error={ping.error} />
      <div className="flex flex-wrap items-center gap-1">
        <span className="w-12 font-medium">iperf</span>
        <select className="input w-auto py-0.5 text-xs" value={iperfDst} onChange={(e) => setIperfDst(e.target.value)} aria-label="iperf destination" data-testid="ca-iperf-dst">
          {others.map((c) => <option key={c.name} value={c.name}>{c.name}</option>)}
        </select>
        <input className="input w-14 py-0.5 text-xs" type="number" min={1} max={30} value={secs} onChange={(e) => setSecs(Number(e.target.value) || 5)} aria-label="iperf seconds" />
        <span className="text-zinc-500">s</span>
        <button className="btn-secondary btn-sm" disabled={!up || iperf.isPending || !iperfDst} onClick={() => iperf.mutate()} data-testid="ca-iperf">{iperf.isPending ? 'running…' : 'iperf3'}</button>
        {iperf.data && (
          <span className={cls('mono', iperf.data.ok ? 'text-emerald-700 dark:text-emerald-300' : 'text-red-700 dark:text-red-300')} data-testid="ca-iperf-result">
            {iperf.data.ok ? `${fmtMbps(iperf.data.bits_per_second_received ?? iperf.data.bits_per_second_sent)} (${iperf.data.proto})` : iperf.data.error ?? 'failed'}
          </span>
        )}
      </div>
      <ErrorBox error={iperf.error} />
      <form className="flex flex-wrap items-center gap-1" onSubmit={(e) => { e.preventDefault(); exec.mutate() }}>
        <span className="w-12 font-medium">exec</span>
        <input className="input mono min-w-0 flex-1 py-0.5 text-xs" value={cmd} onChange={(e) => setCmd(e.target.value)} aria-label="command" data-testid="ca-exec-cmd" />
        <button className="btn-secondary btn-sm" disabled={!up || exec.isPending || !cmd.trim()} data-testid="ca-exec">{exec.isPending ? '…' : 'Run'}</button>
      </form>
      {exec.data && (
        <pre className="mono max-h-48 overflow-auto rounded bg-zinc-950 p-2 text-[11px] text-zinc-100" data-testid="ca-exec-result">
          <span className="text-zinc-400">$ {exec.data.cmd} (rc {exec.data.rc}){'\n'}</span>{exec.data.stdout}{exec.data.stderr && <span className="text-red-300">{exec.data.stderr}</span>}
        </pre>
      )}
      <ErrorBox error={exec.error} />
      <div className="flex flex-wrap gap-2 pt-1">
        <button className="btn-secondary btn-sm" disabled={!up} onClick={() => setTerm(true)} data-testid="ca-terminal">Open terminal</button>
        <span data-testid="ca-remove"><ConfirmButton onConfirm={() => remove.mutate()} confirmText={`Remove ${client.name}?`}>{remove.isPending ? 'Removing…' : 'Remove'}</ConfirmButton></span>
      </div>
      <ErrorBox error={remove.error} />
      {term && <TerminalPanel title={`${lab.slug} / ${client.name}`} path={(shell) => nodesApi.terminalPath(lab.id, client.name, shell)} shells={['bash']} defaultShell="bash" onClose={() => setTerm(false)} />}
    </div>
  )
}
