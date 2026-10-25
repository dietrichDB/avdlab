// CONTRACT-3 mocks: clients & traffic, running configs & snapshots, git, import/export, VS Code links, host monitoring, terminals.
import type {
  ConfigDiff, ContainerMetricPoint, GitStatus, HostContainer, HostMetricPoint, HostMetrics, HostProcess, IperfFlow, IperfResultRow,
  LabClient, OpenLinks, PingResult, PingResultRow, Snapshot, TrafficRun,
} from '../api/types'
import { createLab, db, devices, newId, runtimeNodes, unifiedDiff, type MockLab } from './data'

const now = () => new Date().toISOString()
const ago = (min: number) => new Date(Date.now() - min * 60_000).toISOString()

// ---------------------------------------------------------------- state
export const p3 = {
  runs: [] as TrafficRun[],
  snapshots: new Map<string, Snapshot[]>(),
  git: new Map<string, GitStatus>(),
  /** containers removed (rm -f) / stopped per host (CONTRACT-3b §3) */
  removed: new Map<string, Set<string>>(),
  stopped: new Map<string, Set<string>>(),
}

export function resetPhase3() {
  p3.runs = []
  p3.snapshots = new Map()
  p3.git = new Map()
  p3.removed = new Map()
  p3.stopped = new Map()
  const lab = db.labs.find((l) => l.name === 'evpn-lab')
  if (lab) {
    p3.git.set(lab.id, {
      configured: true, url: 'https://github.com/netops/evpn-lab.git', branch: 'main', auth_kind: 'token', has_secret: true,
      ahead: 2, behind: 0, last_sync: { at: ago(90), direction: 'push', ok: true, message: 'main -> main (3 commits)' },
    })
    p3.snapshots.set(lab.id, [{ id: stamp(new Date(Date.now() - 20 * 60_000)), created_at: ago(20), nodes: devices(lab).length, commands: SNAP_CMDS }])
    const ping = pingResult(lab, null)
    p3.runs.push({ id: newId(), lab_id: lab.id, job_id: null, kind: 'ping', params: { pairs: null, count: 3, size: 56 }, results: ping, created_at: ago(15) })
  }
}

const SNAP_CMDS = ['show running-config', 'show version', 'show lldp neighbors', 'show ip bgp summary', 'show bgp evpn summary', 'show mlag', 'show vxlan vni']
const pad = (n: number) => String(n).padStart(2, '0')
const stamp = (d: Date) => `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`

// ---------------------------------------------------------------- clients (CONTRACT-3 §1 derivation, simplified)
function parseVlans(v: unknown): number[] {
  const out: number[] = []
  for (const part of String(v ?? '').split(',')) {
    const [a, b] = part.split('-').map((x) => Number(x.trim()))
    if (!Number.isFinite(a) || !a) continue
    for (let i = a; i <= (Number.isFinite(b) && b ? b : a); i++) out.push(i)
  }
  return out
}

interface Svi { id: number; ip_address_virtual?: string; name?: string }
function svis(lab: MockLab): { vid: number; cidr: string | null; tenant: string; vrf: string }[] {
  const out: { vid: number; cidr: string | null; tenant: string; vrf: string }[] = []
  for (const t of (lab.knobs?.tenants ?? []) as { name: string; vrfs?: { name: string; svis?: Svi[] }[]; l2vlans?: { id: number }[] }[]) {
    for (const v of t.vrfs ?? []) for (const s of v.svis ?? []) out.push({ vid: s.id, cidr: s.ip_address_virtual ?? null, tenant: t.name, vrf: v.name })
    for (const l of t.l2vlans ?? []) out.push({ vid: l.id, cidr: null, tenant: t.name, vrf: '' })
  }
  return out
}

export function clientPlans(lab: MockLab): LabClient[] {
  const servers = (lab.knobs?.servers ?? []) as { name: string; adapters: { endpoint_ports: string[]; switch_ports: string[]; switches: string[]; vlans?: string; mode?: string; port_channel?: unknown }[] }[]
  const sv = svis(lab)
  const deployed = lab.state === 'deployed' || lab.state === 'validated'
  const base = String(lab.knobs?.mgmt_subnet ?? '172.16.1.0/24').split('/')[0].split('.').slice(0, 3).join('.')
  return servers.map((s, idx) => {
    const a = s.adapters[0]
    const links = a.endpoint_ports.map((eth, i) => ({ eth, switch: a.switches[i], switch_port: a.switch_ports[i] }))
    const bond = a.port_channel ? { name: 'bond0', members: [...a.endpoint_ports] } : null
    const ifname = bond ? 'bond0' : a.endpoint_ports[0]
    let first = true
    const vlans = parseVlans(a.vlans).map((vid) => {
      const svi = sv.find((x) => x.vid === vid)
      let ip: string | null = null, gateway: string | null = null
      if (svi?.cidr) {
        const [addr, len] = svi.cidr.split('/')
        const o = addr.split('.')
        ip = `${o[0]}.${o[1]}.${o[2]}.${100 + idx}/${len}`
        gateway = addr
      }
      const default_route = !!ip && first
      if (ip) first = false
      return { vid, iface: a.mode === 'trunk' ? `${ifname}.${vid}` : ifname, ip, gateway, default_route, tenant: svi?.tenant ?? null, vrf: svi?.vrf || null }
    })
    return {
      name: s.name, image: String(lab.knobs?.client_image ?? 'ghcr.io/srl-labs/network-multitool:latest'), links, bond, vlans,
      container_state: deployed ? 'running' : null, mgmt_ip: deployed ? `${base}.${200 + idx}` : null,
    }
  })
}

const firstIp = (c: LabClient) => c.vlans.find((v) => v.ip)?.ip?.split('/')[0] ?? null

/** deterministic mesh: everything reachable except dc1-host2 → dc1-host3 (100 %) and dc1-host3 → dc1-host2 (33 %) */
export function pingResult(lab: MockLab, pairs: { src: string; dst: string }[] | null, count = 3): PingResult {
  const cs = clientPlans(lab).filter((c) => firstIp(c))
  const list = pairs ?? cs.flatMap((a) => cs.filter((b) => b !== a).map((b) => ({ src: a.name, dst: b.name })))
  const results: PingResultRow[] = list.map(({ src, dst }, i) => {
    const d = cs.find((c) => c.name === dst)
    const loss = src.endsWith('host2') && dst.endsWith('host3') ? 100 : src.endsWith('host3') && dst.endsWith('host2') ? 33.3 : 0
    const received = Math.round(count * (1 - loss / 100))
    return { src, dst, target_ip: d ? firstIp(d) : null, sent: count, received, loss_pct: loss, rtt_avg_ms: received ? 0.8 + (i % 5) * 0.37 : null, ok: loss === 0 }
  })
  const matrix: PingResult['matrix'] = {}
  for (const r of results) (matrix[r.src] ??= {})[r.dst] = r.loss_pct
  return { results, matrix }
}

function parseBw(b: string | null): number {
  if (!b) return 1e6
  const m = /^([\d.]+)\s*([KMG]?)/i.exec(b)
  if (!m) return 1e6
  return Number(m[1]) * ({ K: 1e3, M: 1e6, G: 1e9 }[m[2].toUpperCase() as 'K' | 'M' | 'G'] ?? 1)
}

export function iperfResult(lab: MockLab, flows: IperfFlow[]): IperfResultRow[] {
  const names = new Set(clientPlans(lab).filter((c) => firstIp(c)).map((c) => c.name))
  return flows.map((f, i) => {
    if (!names.has(f.src) || !names.has(f.dst)) return { src: f.src, dst: f.dst, proto: f.proto, seconds: 0, bits_per_second_sent: 0, bits_per_second_received: 0, retransmits: null, jitter_ms: null, lost_percent: null, ok: false, error: `unknown client ${names.has(f.src) ? f.dst : f.src}` }
    if (f.proto === 'udp') {
      const bw = parseBw(f.bandwidth) * f.parallel
      return { src: f.src, dst: f.dst, proto: 'udp', seconds: f.duration, bits_per_second_sent: bw, bits_per_second_received: bw * 0.997, retransmits: null, jitter_ms: 0.011 + i * 0.004, lost_percent: 0.3, ok: true, error: null }
    }
    const bps = 3.9e9 + (i % 3) * 4.1e8
    return { src: f.src, dst: f.dst, proto: 'tcp', seconds: f.duration, bits_per_second_sent: bps, bits_per_second_received: bps * 0.999, retransmits: 12 * f.parallel + i, jitter_ms: null, lost_percent: null, ok: true, error: null }
  })
}

// ---------------------------------------------------------------- running configs
const hostOf = (lab: MockLab, node: string) => devices(lab).find((d) => d.hostname === node || `clab-${lab.slug}-${d.hostname}` === node)

export function intendedConfig(lab: MockLab, node: string) {
  return lab.files[`intended/configs/${node}.cfg`] ?? `!\nhostname ${node}\n!\nend\n`
}

/** leaf2b drifts (manual description + missing loopback) to show a non-empty diff */
export function runningConfig(lab: MockLab, node: string) {
  let cfg = intendedConfig(lab, node)
  cfg = `! Command: show running-config\n! device: ${node} (cEOSLab, EOS-${lab.ceos_version ?? '4.34.2F'})\n!\n! boot system flash:/cEOS-lab.swi\n!\nno aaa root\n!\nusername admin privilege 15 role network-admin secret sha512 $6$mock\n` + cfg.replace(/^!\n/, '')
  if (node.endsWith('leaf2b')) {
    cfg = cfg.replace('interface Loopback0\n', 'interface Ethernet1\n   description added-by-hand\n   shutdown\n!\ninterface Loopback0\n').replace(/router bgp (\S+)\n/, 'router bgp $1\n   maximum-paths 2\n')
  }
  return cfg
}

const normalise = (c: string) => c.split('\n').map((l) => l.replace(/\s+$/, '')).filter((l) => l && !l.startsWith('!') && l !== 'end').join('\n') + '\n'

export function configDiff(lab: MockLab, node: string): ConfigDiff {
  const intended = intendedConfig(lab, node)
  const running = runningConfig(lab, node)
  // the real backend also normalises the extra aaa/username lines produced by EOS defaults; mimic that
  const strip = (s: string) => s.split('\n').filter((l) => !/^(no aaa root|username admin )/.test(l)).join('\n')
  const a = normalise(intended), b = normalise(strip(running))
  const identical = a === b
  const diff = identical ? '' : unifiedDiff({ [`${node} intended`]: a }, { [`${node} intended`]: b })
    .replace(`--- a/${node} intended`, `--- intended/configs/${node}.cfg`).replace(`+++ b/${node} intended`, `+++ running-config (${node})`)
    .split('\n').filter((l) => !l.startsWith('diff --git')).join('\n')
  return { node, intended, running, diff, identical }
}

export function snapshotText(lab: MockLab, node: string, slug: string) {
  const d = hostOf(lab, node)
  if (slug === 'show-running-config') return runningConfig(lab, node)
  if (slug === 'show-version') return `Arista cEOSLab\nHardware version:\nSerial number: ${d?.serial ?? ''}\nSoftware image version: ${lab.ceos_version ?? '4.34.2F'}-cEOS\nArchitecture: ${String(lab.knobs?.ceos_arch ?? 'x86_64')}\nUptime: 1 hour and 12 minutes\n`
  if (slug === 'show-ip-bgp-summary') return `BGP summary information for VRF default\nRouter identifier ${String(d?.loopback0 ?? '').split('/')[0]}, local AS number ${d?.bgp_as}\nNeighbor Status Codes: m - Under maintenance\n  Neighbor    V AS    MsgRcvd MsgSent InQ OutQ Up/Down State  PfxRcd PfxAcc\n  10.255.255.0 4 65100    412     409   0    0 01:10:02 Estab  8      8\n  10.255.255.2 4 65100    410     408   0    0 01:10:01 Estab  8      8\n`
  return `${slug.replace(/-/g, ' ')} (${node})\n…\n`
}

// ---------------------------------------------------------------- git / links
export function gitStatus(labId: string): GitStatus {
  return p3.git.get(labId) ?? { configured: false, url: null, branch: null, auth_kind: null, has_secret: false, ahead: null, behind: null, last_sync: null }
}

export function openLinks(lab: MockLab): OpenLinks {
  const h = db.hosts.find((x) => x.id === lab.host_id)
  const user = h?.ssh_user ?? 'ubuntu'
  const hostPart = h ? `${h.address}${h.ssh_port !== 22 ? `:${h.ssh_port}` : ''}` : 'host'
  const base = `vscode://vscode-remote/ssh-remote+${user}@${hostPart}/opt/avdlab/labs/${lab.slug}`
  const git = p3.git.get(lab.id)
  return {
    remote_ssh: base,
    clab_topology_path: `/opt/avdlab/labs/${lab.slug}/clab/topology.clab.yml`,
    remote_ssh_topology: `${base}/clab/topology.clab.yml`,
    devcontainer: git?.configured && git.url ? `vscode://ms-vscode-remote.remote-containers/cloneInVolume?url=${encodeURIComponent(git.url)}` : null,
    ssh_command: `ssh ${h && h.ssh_port !== 22 ? `-p ${h.ssh_port} ` : ''}${user}@${h?.address ?? 'host'}`,
    hints: [
      'Install the VS Code extensions ms-vscode-remote.remote-ssh and srl-labs.vscode-containerlab.',
      'The containerlab extension discovers *.clab.yml in the opened folder: opening the remote lab folder is enough.',
      'Run "Sync files to host" first so the remote folder has the current files (deploy also syncs).',
      `Add ${h?.address ?? 'the host'} to ~/.ssh/config with the same key avdlab uses so Remote-SSH can log in.`,
    ],
  }
}

export function importLab(name: string, hostId: string | null, warnings: string[] = []) {
  const lab = createLab({ name, template: 'custom', host_id: hostId, knobs: {} })
  lab.settings = { ...(lab.settings ?? {}), fabric_name: 'FABRIC', import_warnings: warnings }
  return lab
}

// ---------------------------------------------------------------- host metrics
const GiB = 1024 ** 3
/** 360 samples, 20 s apart, ending now: smooth deterministic waves */
export function hostMetrics(hostId: string, n = 360, stepS = 20): HostMetrics {
  const h = db.hosts.find((x) => x.id === hostId)
  const ncpu = h?.capabilities.cpu?.nproc ?? 8
  const memTotal = (h?.capabilities.mem?.total_kb ?? 16_000_000) * 1024
  const end = Math.floor(Date.now() / 1000 / stepS) * stepS * 1000
  const seed = hostId.length
  const points: HostMetricPoint[] = []
  for (let i = 0; i < n; i++) {
    const t = end - (n - 1 - i) * stepS * 1000
    const x = i / n
    const cpu = Math.max(1, Math.min(99, 22 + 14 * Math.sin(x * 9 + seed) + 8 * Math.sin(x * 37) + (i > n * 0.8 ? 18 : 0)))
    const used = memTotal * (0.42 + 0.06 * Math.sin(x * 5 + seed) + (i > n * 0.8 ? 0.12 : 0))
    points.push({
      at: new Date(t).toISOString(),
      cpu_pct: Math.round(cpu * 10) / 10,
      load1: Math.round((cpu / 100) * ncpu * 100) / 100,
      load5: Math.round((cpu / 100) * ncpu * 0.9 * 100) / 100,
      load15: Math.round((cpu / 100) * ncpu * 0.8 * 100) / 100,
      mem_total: memTotal, mem_used: Math.round(used), mem_available: Math.round(memTotal - used - memTotal * 0.08),
      disk: {
        '/': { size: 400 * GiB, used: Math.round((88 + x * 0.6) * GiB) },
        '/var/lib/docker': { size: 900 * GiB, used: Math.round((210 + x * 4) * GiB) },
      },
    })
  }
  const names = containerNames(hostId)
  const containers: Record<string, ContainerMetricPoint[]> = {}
  names.forEach((name, k) => {
    const baseMem = name.includes('spine') ? 1.05 : name.includes('leaf') ? 1.25 : name.includes('host') ? 0.03 : 0.2 + k * 0.01
    containers[name] = points.map((p, i) => ({
      at: p.at,
      cpu_pct: Math.round((3 + 2 * Math.sin(i / 20 + k)) * 10) / 10,
      mem_used: Math.round((baseMem + 0.05 * Math.sin(i / 30 + k)) * GiB),
      mem_limit: memTotal,
    }))
  })
  return { host_id: hostId, points, containers, sampled_at: points[points.length - 1]?.at ?? null, watched: true }
}

function containerNames(hostId: string): string[] {
  const labs = db.labs.filter((l) => l.host_id === hostId && l.runtime?.deployed)
  const names = labs.flatMap((l) => runtimeNodes(l).map((n) => n.name))
  return [...names, 'avdlab-agent', 'registry']
}

export function hostContainers(hostId: string): HostContainer[] {
  const labs = db.labs.filter((l) => l.host_id === hostId && l.runtime?.deployed)
  const out: HostContainer[] = labs.flatMap((l) => runtimeNodes(l).map((n) => ({
    name: n.name, image: n.image ?? '', state: 'running', status: 'Up 4 hours', labels: { 'avdlab.lab': l.slug, 'containerlab': l.slug },
  })))
  out.push({ name: 'avdlab-agent', image: 'avdlab-agent:latest', state: 'exited', status: 'Exited (0) 29 minutes ago', labels: {} })
  out.push({ name: 'registry', image: 'registry:2', state: 'running', status: 'Up 3 days', labels: {} })
  const removed = p3.removed.get(hostId) ?? new Set<string>()
  const stopped = p3.stopped.get(hostId) ?? new Set<string>()
  return out.filter((c) => !removed.has(c.name))
    .map((c) => (stopped.has(c.name) ? { ...c, state: 'exited', status: 'Exited (137) just now' } : c))
}

/** DELETE /hosts/{id}/containers/{name}?force= — mirrors hosts/monitor.py kill_container */
export function killContainer(hostId: string, name: string, force: boolean, confirm: string | undefined):
  { status: number; body: Record<string, unknown> } {
  const c = hostContainers(hostId).find((x) => x.name === name)
  if (!c) return { status: 404, body: { detail: `container '${name}' not found` } }
  const slug = c.labels['avdlab.lab'] ?? c.labels['containerlab']
  const lab = slug ? db.labs.find((l) => l.slug === slug) : undefined
  if (lab && (lab.state === 'deployed' || lab.state === 'validated') && confirm !== name) {
    return { status: 409, body: { detail: { message: `${name} belongs to lab ${lab.name} (${lab.state}): send body {"confirm": "<container name>"}`, lab_id: lab.id, lab: lab.name, state: lab.state } } }
  }
  const bucket = force ? p3.removed : p3.stopped
  if (!bucket.has(hostId)) bucket.set(hostId, new Set())
  bucket.get(hostId)!.add(name)
  return { status: 200, body: { name, action: force ? 'rm' : 'stop', removed: true, returncode: 0, output: name } }
}

export function hostProcesses(hostId: string): { at: string; processes: HostProcess[] } {
  const cs = hostContainers(hostId).filter((c) => c.state === 'running')
  const procs: HostProcess[] = cs.flatMap((c, i) => c.name.includes('host')
    ? [{ pid: 40000 + i, user: 'root', cpu_pct: 0.1, mem_pct: 0.1, rss: 12_000, command: 'iperf3 -s -D' }]
    : [{ pid: 30000 + i * 10, user: 'root', cpu_pct: 2.1 + (i % 4), mem_pct: 1.6 + (i % 3) * 0.2, rss: 1_050_000 + i * 20_000, command: `/usr/bin/ConfigAgent (${c.name})` }])
  procs.push(
    { pid: 1123, user: 'root', cpu_pct: 1.2, mem_pct: 0.4, rss: 120_000, command: '/usr/bin/dockerd -H fd:// --containerd=/run/containerd/containerd.sock' },
    { pid: 988, user: 'root', cpu_pct: 0.4, mem_pct: 0.2, rss: 60_000, command: '/usr/bin/containerd' },
    { pid: 1, user: 'root', cpu_pct: 0, mem_pct: 0.1, rss: 13_000, command: '/sbin/init' },
  )
  return { at: now(), processes: procs.sort((a, b) => b.mem_pct - a.mem_pct).slice(0, 40) }
}

resetPhase3()
