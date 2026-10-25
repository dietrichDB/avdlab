import type { Changelog, EditOp, EndpointAdapter, Feedback, Meta, Notices, Topology, TopologyLayout, TopologyPorts, Violation } from '../api/types'
import { roleOf } from '../lib/topology'
import { uplinksOf, uplinkTargets } from '../lib/topologyEdit'
import { templates, topology, type MockLab } from './data'

const ago = (min: number) => new Date(Date.now() - min * 60_000).toISOString()

// ---------------------------------------------------------------- meta / changelog / notices
export const CHANGELOG: Changelog['entries'] = [
  { version: '0.6.0', date: '2026-09-26', sections: {
    Added: ['Editable topology: drag layout, move uplinks, add/remove servers, leaf pairs, L2 leafs and spines — validated with pyavd before commit', 'Changelog page and "what\'s new" dot on the version badge', 'Feedback form with optional diagnostics and GitHub issue creation; `/feedback` admin page', 'About page with copyright, licence and third-party notices', 'avdlab logo and favicon'],
    Changed: ['SSH connect retries transient failures with backoff and reports descriptive errors'],
    Fixed: ['CloudVision onboarding writes the token inside the cEOS namespace (tmpfs on /tmp hid `docker cp`)'],
  } },
  { version: '0.5.0', date: '2026-09-22', sections: {
    Added: ['ZTP provisioning mode with DHCP → bootfile → eAPI timeline', 'NetBox targets and lab sync', 'MSS foundation: segmentation matrix and verification', 'Arista Cloud Test (ACT) target'],
  } },
  { version: '0.4.0', date: '2026-09-22', sections: {
    Added: ['Per-device CloudVision tags', 'Kill running jobs and stray containers'],
    Fixed: ['Imported labs with validation errors are created as drafts with warnings'],
  } },
  { version: '0.3.0', date: '2026-09-22', sections: {
    Added: ['Clients: plan, ping matrix and iperf runs', 'Devices: running config, diff and in-browser terminal', 'Git push/pull, import and export of labs', 'Host monitoring charts'],
  } },
  { version: '0.2.0', date: '2026-09-22', sections: {
    Added: ['containerlab deploy / destroy / status and ANTA validation', 'CloudVision targets, onboarding and workspace deploy'],
    Changed: ['Hosts support sudo passwords (`sudo -S`)'],
  } },
  { version: '0.1.0', date: '2026-09-22', sections: {
    Added: ['Auth, secret store and job engine', 'Hosts with probe/bootstrap, cEOS images, labs from AVD templates, pyavd build and topology'],
  } },
]

export const NOTICES_MD = `# Third-party notices

avdlab bundles or uses the following third-party software. Each component remains under its own licence.

| Component | Licence | Notes |
|---|---|---|
| Arista AVD (pyavd) | Apache-2.0 | [github.com/aristanetworks/avd](https://github.com/aristanetworks/avd) |
| ANTA | Apache-2.0 | [github.com/aristanetworks/anta](https://github.com/aristanetworks/anta) |
| containerlab | BSD-3-Clause | |
| FastAPI | MIT | |
| React | MIT | |
| Tailwind CSS | MIT | |
| xterm.js | MIT | |
| recharts | MIT | |
| React Flow | MIT | |
| pynetbox | Apache-2.0 | |
| eos-downloader | Apache-2.0 | |
| network-multitool image | MIT | |
| cEOS-lab | Arista EULA | not redistributed — download with your own Arista account |
`

export const LICENSE_TEXT = `Copyright (c) 2026 Dietrich De Bruyne — Systems Engineer, Arista Networks. All rights reserved.

This software is proprietary. No licence is granted to copy, modify or distribute it without prior written permission.`

export function metaOf(): Meta {
  return {
    version: '0.6.0', git_sha: '3385468f0c2d9e8b7a6f5e4d3c2b1a0918273645', build_date: '2026-09-26T08:00:00Z',
    avd_version: '5.7.2', anta_version: '1.6.0', python: '3.12.7', changelog_latest: CHANGELOG[0],
  }
}

export function changelogOf(): Changelog {
  const md = ['# Changelog', '', ...CHANGELOG.flatMap((e) => [`## [${e.version}] - ${e.date}`, '', ...Object.entries(e.sections).flatMap(([k, v]) => [`### ${k}`, '', ...v.map((x) => `- ${x}`), ''])])].join('\n')
  return { entries: CHANGELOG, markdown: md }
}

export function noticesOf(): Notices {
  return { markdown: NOTICES_MD, copyright: '© 2026 Dietrich De Bruyne — Systems Engineer, Arista Networks. All rights reserved.', license: LICENSE_TEXT }
}

// ---------------------------------------------------------------- state
export const p5 = {
  layouts: new Map<string, TopologyLayout>(),
  feedback: [] as (Feedback & { diagnostics: Record<string, unknown> | null })[],
  feedbackSeq: 1,
  /** last /edit request bodies (tests inspect the payload) */
  editRequests: [] as { ops: EditOp[]; dry_run: boolean }[],
  feedbackRequests: [] as unknown[],
}

export function resetPhase5() {
  p5.layouts = new Map()
  p5.feedback = [
    { id: 1, kind: 'bug', title: 'Deploy log stops scrolling', body: 'After ~500 lines the job drawer stops auto-scrolling.', page: '/labs', status: 'new', github_issue_url: null, created_at: ago(90), diagnostics: null },
    { id: 2, kind: 'suggestion', title: 'Export topology as PNG', body: 'Would be handy for documentation.', page: '/labs/x/topology', status: 'triaged', github_issue_url: 'https://github.com/example/avdlab/issues/12', created_at: ago(60 * 26), diagnostics: null },
  ]
  p5.feedbackSeq = 3
  p5.editRequests = []
  p5.feedbackRequests = []
}
resetPhase5()

// ---------------------------------------------------------------- topology with endpoints, l2 leafs and overrides
type Overrides = Record<string, Record<string, { switch: string; switch_port: string }>>
interface Server { name: string; type?: string; adapters: EndpointAdapter[] }

export const isGenerated = (lab: MockLab) => templates.find((t) => t.name === lab.template)?.generator === 'l3ls'

export function overridesOf(lab: MockLab): Overrides {
  return ((lab.settings ?? {}).topology_overrides ?? {}) as Overrides
}

export function editableTopology(lab: MockLab): Topology {
  const base = topology(lab)
  const names = new Set(base.nodes.map((n) => n.name))
  const links = base.links.filter((l) => l.type !== 'server')
  // l2 leafs: dual-homed to their pair
  const l2 = base.nodes.filter((n) => n.role === 'l2leaf')
  for (const n of l2) {
    const m = /^(.*leaf)(\d+)([c-z])$/.exec(n.name)
    if (!m) continue
    const c = m[3].charCodeAt(0) - 99
    ;['a', 'b'].forEach((s, i) => {
      const parent = `${m[1]}${m[2]}${s}`
      if (names.has(parent)) links.push({ a: parent, a_if: `Ethernet${7 + c}`, b: n.name, b_if: `Ethernet${i + 1}`, a_port: `eth${7 + c}`, b_port: `eth${i + 1}`, type: 'l2leaf' })
    })
  }
  for (const s of serversOf(lab)) for (const a of s.adapters) a.switches.forEach((sw, i) => {
    if (names.has(sw) && a.switch_ports[i]) links.push({ a: sw, a_if: a.switch_ports[i], b: s.name, b_if: a.endpoint_ports[i] ?? `eth${i + 1}`, a_port: a.switch_ports[i].replace('Ethernet', 'eth'), b_port: a.endpoint_ports[i] ?? `eth${i + 1}`, type: 'server' })
  })
  const topo: Topology = { ...base, links }
  // node-level uplink overrides (set_uplink)
  for (const [node, byIndex] of Object.entries(overridesOf(lab))) {
    const ups = uplinksOf(topo, node)
    for (const [idx, o] of Object.entries(byIndex)) {
      const u = ups[Number(idx)]
      if (!u || !names.has(o.switch)) continue
      const l = topo.links[Number(u.edgeKey)]
      if (l.a === node) topo.links[Number(u.edgeKey)] = { ...l, b: o.switch, b_if: o.switch_port, b_port: o.switch_port.replace('Ethernet', 'eth') }
      else topo.links[Number(u.edgeKey)] = { ...l, a: o.switch, a_if: o.switch_port, a_port: o.switch_port.replace('Ethernet', 'eth') }
    }
  }
  return topo
}

export function serversOf(lab: MockLab): Server[] {
  const s = (lab.knobs ?? {}).servers
  return (Array.isArray(s) ? s : []) as Server[]
}

const PORT_COUNT: Record<string, number> = { spine: 8, l3leaf: 12, l2leaf: 8, host: 4, other: 4 }

export function portsOf(topo: Topology): TopologyPorts {
  const out: TopologyPorts = {}
  for (const n of topo.nodes) {
    const role = roleOf(n)
    const used = new Set<string>()
    for (const l of topo.links) { if (l.a === n.name) used.add(l.a_if); if (l.b === n.name) used.add(l.b_if) }
    const all = Array.from({ length: PORT_COUNT[role] }, (_, i) => (role === 'host' ? `eth${i + 1}` : `Ethernet${i + 1}`))
    const sort = (xs: string[]) => xs.sort((a, b) => a.localeCompare(b, undefined, { numeric: true }))
    out[n.name] = { used: sort([...used]), free: all.filter((p) => !used.has(p)) }
  }
  return out
}

// ---------------------------------------------------------------- edit ops
export type EditOutcome =
  | { ok: true; lab: MockLab; graph: Topology; changed_files: string[]; warnings: string[] }
  | { ok: false; status: 409; detail: { message: string; hint?: string } }
  | { ok: false; status: 422; detail: { message: string; violations: Violation[]; op_index: number } }

const TOPO_OPS = new Set(['add_leaf_pair', 'remove_last_leaf_pair', 'set_l2leaf_per_pair', 'set_spine_count'])

/** Applies ops in order to a copy of the lab (knobs + overrides) and validates each step. */
export function applyOps(lab: MockLab, ops: EditOp[]): EditOutcome {
  const copy: MockLab = { ...lab, knobs: structuredClone(lab.knobs ?? {}), settings: structuredClone(lab.settings ?? {}) }
  const changed = new Set<string>()
  const warnings: string[] = []
  const bad = (i: number, message: string, path?: string): EditOutcome => ({ ok: false, status: 422, detail: { message: `change ${i + 1} (${ops[i].op}) is invalid`, violations: [{ path, message }], op_index: i } })
  for (let i = 0; i < ops.length; i++) {
    const o = ops[i]
    const topo = editableTopology(copy)
    const ports = portsOf(topo)
    const k = copy.knobs as Record<string, unknown>
    if (TOPO_OPS.has(o.op) && !isGenerated(copy)) {
      return { ok: false, status: 409, detail: { message: `${o.op} is only available for generated l3ls templates`, hint: 'edit group_vars (Files tab) for custom labs' } }
    }
    if ((o as { op: string }).op === 'rename_node') return { ok: false, status: 409, detail: { message: 'renaming nodes is not supported (hostnames are keys everywhere)' } }
    switch (o.op) {
      case 'set_uplink': {
        const ups = uplinksOf(topo, o.node)
        if (!ups[o.index]) return bad(i, `${o.node} has no uplink #${o.index + 1}`, `${o.node}.uplink_switches[${o.index}]`)
        if (!uplinkTargets(topo, o.node).includes(o.switch)) return bad(i, `${o.switch} is not a valid uplink switch for ${o.node}`, `${o.node}.uplink_switches[${o.index}]`)
        if (!ports[o.switch]?.free.includes(o.switch_port)) return bad(i, `${o.switch} ${o.switch_port} is not a free port`, `${o.node}.uplink_switch_interfaces[${o.index}]`)
        const ov = overridesOf(copy)
        ov[o.node] = { ...(ov[o.node] ?? {}), [o.index]: { switch: o.switch, switch_port: o.switch_port } }
        copy.settings = { ...copy.settings, topology_overrides: ov }
        if (ups.filter((u) => u.switch === o.switch).length) warnings.push(`${o.node} now has two uplinks to ${o.switch}`)
        changed.add('group_vars/FABRIC.yml')
        break
      }
      case 'add_endpoint': case 'update_endpoint': {
        const servers = serversOf(copy)
        const exists = servers.some((s) => s.name === o.name)
        if (o.op === 'add_endpoint' && (exists || topo.nodes.some((n) => n.name === o.name))) return bad(i, `${o.name} already exists`, `${o.type}.name`)
        if (o.op === 'update_endpoint' && !exists) return bad(i, `endpoint ${o.name} not found`)
        const own = new Set(o.op === 'update_endpoint' ? servers.find((s) => s.name === o.name)!.adapters.flatMap((a) => a.switches.map((s, j) => `${s}|${a.switch_ports[j]}`)) : [])
        for (const a of o.adapters) {
          if (!a.vlans) return bad(i, 'vlans is required', 'adapters[0].vlans')
          for (let j = 0; j < a.switches.length; j++) {
            const sw = a.switches[j], p = a.switch_ports[j]
            if (!ports[sw]) return bad(i, `switch ${sw} not found`, `adapters[0].switches[${j}]`)
            if (!ports[sw].free.includes(p) && !own.has(`${sw}|${p}`)) return bad(i, `${sw} ${p} is not a free port`, `adapters[0].switch_ports[${j}]`)
          }
        }
        const entry: Server = { name: o.name, adapters: o.adapters }
        k.servers = o.op === 'add_endpoint' ? [...servers, entry] : servers.map((s) => (s.name === o.name ? { ...s, adapters: o.adapters } : s))
        changed.add('group_vars/CONNECTED_ENDPOINTS.yml')
        break
      }
      case 'remove_endpoint': {
        const servers = serversOf(copy)
        if (!servers.some((s) => s.name === o.name)) return bad(i, `endpoint ${o.name} not found`)
        k.servers = servers.filter((s) => s.name !== o.name)
        changed.add('group_vars/CONNECTED_ENDPOINTS.yml')
        break
      }
      case 'add_leaf_pair': {
        const n = Number(k.leaf_pairs ?? 2)
        if (n >= 8) return bad(i, 'at most 8 leaf pairs', 'knobs.leaf_pairs')
        k.leaf_pairs = n + 1
        changed.add('group_vars/FABRIC.yml')
        break
      }
      case 'remove_last_leaf_pair': {
        const n = Number(k.leaf_pairs ?? 2)
        if (n <= 1) return bad(i, 'at least 1 leaf pair', 'knobs.leaf_pairs')
        const gone = [`dc1-leaf${n}a`, `dc1-leaf${n}b`]
        const attached = serversOf(copy).filter((s) => s.adapters.some((a) => a.switches.some((x) => gone.includes(x)))).map((s) => s.name)
        if (attached.length) return bad(i, `endpoints still attached to ${gone.join('/')}: ${attached.join(', ')}`, 'knobs.leaf_pairs')
        k.leaf_pairs = n - 1
        changed.add('group_vars/FABRIC.yml')
        break
      }
      case 'set_l2leaf_per_pair': {
        if (!Number.isInteger(o.n) || o.n < 0 || o.n > 4) return bad(i, 'l2leaf_per_pair must be 0..4', 'knobs.l2leaf_per_pair')
        k.l2leaf_per_pair = o.n
        changed.add('group_vars/FABRIC.yml')
        break
      }
      case 'set_spine_count': {
        if (!Number.isInteger(o.n) || o.n < 1 || o.n > 4) return bad(i, 'spine_count must be 1..4', 'knobs.spine_count')
        k.spine_count = o.n
        changed.add('group_vars/FABRIC.yml')
        break
      }
    }
  }
  changed.add('clab/topology.clab.yml')
  return { ok: true, lab: copy, graph: editableTopology(copy), changed_files: [...changed].sort(), warnings }
}
