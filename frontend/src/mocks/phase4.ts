// CONTRACT-4 mocks: ZTP status, NetBox targets + sync, MSS summary + verify, ACT topology.
import YAML from 'yaml'
import type {
  ActInfo, ActNode, MssCase, MssCaseResult, MssSummary, NetBoxSyncParams, NetBoxSyncResult, NetBoxTarget, ZtpNode, ZtpStatus,
} from '../api/types'
import { applyBuild, buildLog, buildResult, addJob, createLab, db, devices, stripJob, topology, type MockLab } from './data'
import { clientPlans } from './phase3'
import { normalizeProvisioning } from '../lib/provisioning'
import { normalizeAct } from '../lib/act'
import { decide, normalizeSegmentation, policyMatrix, segmentOf } from '../lib/mss'

const now = () => new Date().toISOString()
const ago = (min: number) => new Date(Date.now() - min * 60_000).toISOString()

export const p4 = {
  netboxTargets: [] as NetBoxTarget[],
  /** labs already synced per target (second sync = updates only) */
  nbSynced: new Set<string>(),
  /** ZTP deploy start per lab (ms) — drives the simulated DHCP → bootfile → eAPI progression */
  ztpStart: new Map<string, number>(),
  /** ACT post-processing done (the build does it; Prepare re-runs it) */
  actPrepared: new Set<string>(),
  /** traffic_runs of kind mss are kept in p3.runs by the handlers */
}

export function resetPhase4() {
  p4.netboxTargets = [
    { id: 'nbt-local', name: 'netbox-local', url: 'http://localhost:8000', verify_certs: false, has_token: true, token_meta: { updated_at: ago(60 * 24) }, created_at: ago(60 * 24 * 2), labs: [] },
    { id: 'nbt-prod', name: 'netbox-corp', url: 'https://netbox.example.net', verify_certs: true, has_token: false, token_meta: null, created_at: ago(60 * 24 * 20), labs: [] },
  ]
  p4.nbSynced = new Set()
  p4.ztpStart = new Map()
  p4.actPrepared = new Set()
  // an ACT lab (built with target act, no tools server host yet)
  if (!db.labs.some((l) => l.name === 'act-lab')) {
    const lab = createLab({ name: 'act-lab', template: 'single-dc-l3ls', host_id: null, ceos_version: '4.34.2F', target: 'act',
      knobs: { mgmt_subnet: '192.168.0.0/24', mgmt_gateway: '192.168.0.1' } })
    lab.created_at = ago(60 * 3)
    const b = addJob({ type: 'lab.build', lab_id: lab.id, status: 'succeeded', started: ago(60 * 2), ended: ago(60 * 2 - 0.4) })
    b.log = buildLog(lab); b.result = buildResult(lab)
    applyBuild(lab)
    lab.last_build_job = stripJob(b)
    p4.actPrepared.add(lab.id)
  }
}

export function netboxTargetOut(t: NetBoxTarget): NetBoxTarget {
  return { ...t, labs: db.labs.filter((l) => l.netbox_target_id === t.id).map((l) => ({ id: l.id, name: l.name })) }
}

// ---------------------------------------------------------------- ZTP
const mac = (i: number) => `00:1c:73:${(0xa0 + i).toString(16)}:${(0x10 + i * 3).toString(16).padStart(2, '0')}:${(0x42 + i).toString(16)}`

export function ztpStatus(lab: MockLab, deploying: boolean): ZtpStatus {
  const prov = normalizeProvisioning(lab.knobs?.provisioning)
  const base = String(lab.knobs?.mgmt_subnet ?? '172.16.1.0/24').split('/')[0].split('.').slice(0, 3).join('.')
  const out: ZtpStatus = { mode: prov.mode, source: prov.ztp_source, server_ip: null, nodes: [], at: null, fresh: false, error: null }
  if (prov.mode !== 'ztp' || !lab.built) return out
  out.server_ip = prov.ztp_server_ip ?? `${base}.254`
  const running = lab.state === 'deployed' || lab.state === 'validated'
  const start = p4.ztpStart.get(lab.id)
  const elapsed = start ? (Date.now() - start) / 1000 : Infinity
  out.nodes = devices(lab).map((d, i): ZtpNode => {
    const t = deploying ? elapsed : running ? Infinity : -1
    const lease = t > 1.5 + i * 0.4
    const fetched = t > 3 + i * 0.4
    const eapi = t > 5 + i * 0.6
    return {
      name: d.hostname, mac: t >= 0 ? mac(i) : null, ip: (d.mgmt_ip ?? '').split('/')[0] || null, lease, bootfile_fetched: fetched, eapi,
      bootfile: prov.ztp_source === 'cloudvision' ? `bootstrap-${d.hostname}.sh` : `${d.hostname}.cfg`,
    }
  })
  out.at = running || deploying ? Date.now() / 1000 : null
  out.fresh = running
  return out
}

// ---------------------------------------------------------------- NetBox
export function netboxSync(lab: MockLab, target: NetBoxTarget, p: NetBoxSyncParams): NetBoxSyncResult {
  const devs = devices(lab)
  const clients = p.include_clients ? clientPlans(lab) : []
  const links = topology(lab).links
  const counts: Record<string, number> = {
    tag: 2, manufacturer: 1, platform: 1, device_type: 1 + (clients.length ? 1 : 0), device_role: new Set(devs.map((d) => d.type)).size + (clients.length ? 1 : 0),
    site: 1, device: devs.length + clients.length, interface: devs.length * 9 + clients.reduce((n, c) => n + c.links.length, 0),
    vlan: 5, vrf: 1, prefix: 4 + links.filter((l) => l.type === 'spine').length, ip_address: devs.length * 3 + clients.length,
    cable: links.filter((l) => p.include_clients || !clients.some((c) => c.name === l.a || c.name === l.b)).length,
  }
  const key = `${lab.id}:${target.id}`
  const again = p4.nbSynced.has(key)
  const r: NetBoxSyncResult = { created: {}, updated: {}, deleted: {}, unchanged: {}, skipped: [], netbox_url: `${target.url.replace(/\/$/, '')}/dcim/devices/?tag=avdlab-${lab.slug}`, dry_run: p.dry_run, at: now(), target: { id: target.id, name: target.name } }
  if (!again) r.created = counts
  else {
    r.updated = { interface: 2, device: 1 }
    r.unchanged = Object.fromEntries(Object.entries(counts).map(([k, v]) => [k, Math.max(0, v - (r.updated[k] ?? 0))]))
    if (p.delete_stale) r.deleted = { ip_address: 1, interface: 2 }
  }
  if (!p.include_clients) {
    const skippedClients = clientPlans(lab)
    r.skipped = skippedClients.flatMap((c) => c.links.map((k) => `cable ${k.switch}:${k.switch_port} ↔ ${c.name}:${k.eth}: endpoint ${c.name} not synced (include_clients off)`))
  }
  if (!p.dry_run) p4.nbSynced.add(key)
  return r
}

// ---------------------------------------------------------------- MSS
export function mssSummary(lab: MockLab): Omit<MssSummary, 'last_verify'> {
  const s = normalizeSegmentation(lab.knobs?.segmentation)
  const vrfs = new Set(s.vrfs.map((v) => v.name))
  const rendered = s.enabled && vrfs.size && lab.built ? devices(lab).filter((d) => d.type === 'l3leaf').map((d) => d.hostname).sort() : []
  return { ...s, matrix: policyMatrix(s), rendered_on: rendered }
}

export function mssDerive(lab: MockLab): MssCase[] {
  const s = normalizeSegmentation(lab.knobs?.segmentation)
  const m = policyMatrix(s)
  const cl = clientPlans(lab).map((c) => ({ c, v: c.vlans.find((x) => x.default_route && x.ip) })).filter((x) => x.v)
  const out: MssCase[] = []
  const apps = [...s.applications.map((a) => a.name)]
  for (const a of cl) for (const b of cl) {
    if (a === b || !a.v || !b.v || a.v.vrf !== b.v.vrf || a.v.vid === b.v.vid) continue
    const src = segmentOf(s, a.v.vrf, a.v.ip), dst = segmentOf(s, b.v.vrf, b.v.ip)
    if (!src || !dst) continue
    const pol = m[a.v.vrf!]?.[src]?.[dst] ?? null
    for (const app of apps) {
      const d = decide(s, pol, app)
      if (d) out.push({ src: a.c.name, dst: b.c.name, application: app, expect: d === 'forward' ? 'allow' : 'deny' })
    }
  }
  return out
}

export function mssProbe(lab: MockLab, cases: MssCase[]): { cases: MssCaseResult[]; passed: number; failed: number } {
  const s = normalizeSegmentation(lab.knobs?.segmentation)
  const plans = clientPlans(lab)
  const res = cases.map((c, i): MssCaseResult => {
    const dst = plans.find((p) => p.name === c.dst)
    const ip = (c.vlan ? dst?.vlans.find((v) => v.vid === c.vlan)?.ip : dst?.vlans.find((v) => v.default_route)?.ip)?.split('/')[0] ?? null
    const app = s.applications.find((a) => a.name === c.application) ?? { name: c.application, protocol: 'any', ports: null }
    // the mock fabric honours the policy except for the last case (to show a failure)
    const observed = !ip ? 'error' : i === cases.length - 1 && cases.length > 3 ? (c.expect === 'allow' ? 'deny' : 'allow') : c.expect
    const probe = app.protocol === 'icmp' || app.protocol === 'any' ? `ping -c 2 ${ip}` : app.protocol === 'udp' ? `iperf3 -u -c ${ip} -p ${app.ports?.split(',')[0]}` : `nc -z -w2 ${ip} ${app.ports?.split(',')[0] ?? ''}`
    const detail = !ip ? `${c.dst} has no address` : `${probe}: ${observed === 'allow' ? 'succeeded' : 'timed out'}`
    return { ...c, target_ip: ip, observed: observed as MssCaseResult['observed'], pass: observed === c.expect, detail }
  })
  const passed = res.filter((r) => r.pass).length
  return { cases: res, passed, failed: res.length - passed }
}

// ---------------------------------------------------------------- ACT
export const TOOLS_NODE = 'avdlab-tools'

export function actInfo(lab: MockLab): ActInfo {
  const act = normalizeAct(lab.knobs?.act)
  const base = String(lab.knobs?.mgmt_subnet ?? '172.16.1.0/24').split('/')[0].split('.').slice(0, 3).join('.')
  const prepared = lab.target === 'act' && lab.built && p4.actPrepared.has(lab.id)
  const topology_path = 'documentation/fabric/FABRIC-topology.yml'
  const nodes: ActNode[] = []
  const clients: ActInfo['clients'] = []
  if (prepared) {
    for (const d of devices(lab)) nodes.push({ name: d.hostname, node_type: 'veos', ip_addr: d.mgmt_ip, ip: (d.mgmt_ip ?? '').split('/')[0], version: act.os_version, role: 'switch' })
    const tip = act.tools_server_ip || `${base}.254`
    nodes.push({ name: TOOLS_NODE, node_type: 'tools-server', ip_addr: `${tip}/24`, ip: tip, version: 'ubuntu-2204-lts', role: 'tools-server' })
    clientPlans(lab).forEach((c, idx) => {
      const ip = `${base}.${200 + idx}`
      nodes.push({ name: c.name, node_type: 'generic', ip_addr: `${ip}/24`, ip, version: 'ubuntu-2204-lts', role: 'client' })
      const lines = ['#!/bin/sh', `# ${c.name}: generated by avdlab (ACT generic node, ethN = EthernetN in the topology)`, 'set -e']
      if (c.bond) lines.push(`ip link add ${c.bond.name} type bond mode 802.3ad`, ...c.bond.members.map((m, i) => `ip link set eth${i + 1} down && ip link set eth${i + 1} master ${c.bond!.name}  # was ${m}`), `ip link set ${c.bond.name} up`)
      for (const v of c.vlans.filter((x) => x.ip)) {
        const parent = c.bond ? c.bond.name : 'eth1'
        const ifn = v.iface.includes('.') ? `${parent}.${v.vid}` : parent
        if (ifn.includes('.')) lines.push(`ip link add link ${parent} name ${ifn} type vlan id ${v.vid}`, `ip link set ${ifn} up`)
        lines.push(`ip addr add ${v.ip} dev ${ifn}`)
        if (v.default_route && v.gateway) lines.push(`ip route replace default via ${v.gateway} dev ${ifn}`)
      }
      clients.push({ name: c.name, ip_addr: `${ip}/24`, links: c.links.map((k, i) => [`${c.name}:Ethernet${i + 1}`, `${k.switch}:${k.switch_port}`]), script_path: `act/clients/${c.name}.sh`, script: lines.join('\n') + '\n' })
    })
  }
  const host = lab.host_id ? db.hosts.find((h) => h.id === lab.host_id) : null
  const tools = nodes.find((n) => n.role === 'tools-server')?.ip ?? '<tools server mgmt ip>'
  const hints: string[] = []
  if (lab.target !== 'act') hints.push('Set the lab target to act (lab settings) and build: the build then runs pyavd in digital-twin mode.')
  if (!prepared) hints.push('Build the lab: the ACT topology is generated by the build.')
  hints.push(
    `Download the topology (${topology_path}) and create a lab from it in act.arista.com (vEOS ${act.os_version}, node user ${act.username}).`,
    `Once the ACT lab is running, add a host of kind act for the tools server (${TOOLS_NODE}, mgmt ${tools}; use the public SSH address ACT shows for it), then Probe and Bootstrap it with recipe act-tools (Docker + agent image).`,
    'Set that host as the lab host and Deploy: avdlab syncs the lab to the tools server, waits for eAPI on every switch and pushes the intended configs with eos_config_deploy_eapi (config replace). Then Validate (ANTA) as usual.',
  )
  if (clients.length) hints.push('Clients are ubuntu generic nodes (ethN = EthernetN in the topology). Their setup scripts are in act/clients/ (synced to the tools server); run act/clients/apply-from-tools-server.sh on the tools server to apply them — avdlab does not ssh to the clients. Traffic tests are not available on ACT yet.')
  if (host && host.kind !== 'act') hints.push(`The current lab host ${host.name} is kind ${host.kind}: deploy needs a host of kind act.`)
  hints.push('Destroy only marks the lab destroyed in avdlab: delete the ACT lab in the ACT UI.')
  return { target: lab.target, prepared, topology_path, tools_server_ip: prepared ? tools : null, nodes, clients, apply_script_path: clients.length ? 'act/clients/apply-from-tools-server.sh' : null, hints }
}

export function actTopologyYaml(info: ActInfo, lab: MockLab): string {
  const act = normalizeAct(lab.knobs?.act)
  const topo = {
    veos: { username: act.username, password: act.password, version: act.os_version },
    generic: { version: 'ubuntu-2204-lts' },
    nodes: info.nodes.map((n) => ({ [n.name]: { ip_addr: n.ip_addr, node_type: n.node_type, ...(n.role !== 'switch' ? { version: n.version } : {}) } })),
    links: [
      ...topology(lab).links.filter((l) => info.nodes.some((n) => n.name === l.a) && info.nodes.some((n) => n.name === l.b)).map((l) => ({ connection: [`${l.a}:${l.a_if}`, `${l.b}:${l.b_if}`] })),
      ...info.clients.flatMap((c) => c.links.map((k) => ({ connection: k }))),
    ],
  }
  return '---\n' + YAML.stringify(topo, { lineWidth: 0 })
}

resetPhase4()
