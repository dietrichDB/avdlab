// CONTRACT-6 mocks: update check/apply states, learning content (real lessons/glossary/tours/schema samples exported from
// backend/avdlab/learn via pyavd), config explain, hot-added test hosts.
import type { ExplainFile, ExplainLine, GlossaryTerm, KnobLearn, Lesson, LessonSummary, SchemaChild, SchemaEntry, SchemaHit, SchemaName, Tours, UpdateStatus } from '../api/phase6'
import type { LabClient, Template } from '../api/types'
import { parseConfig } from '../lib/learn'
import content from './learnContent.json'
import { db, templates, type MockLab } from './data'
import { clientPlans } from './phase3'

interface Content {
  lesson_summaries: LessonSummary[]
  lessons: Record<string, Omit<Lesson, 'passed' | 'prev' | 'next'>>
  lesson_order: string[]
  glossary: GlossaryTerm[]
  tours: Tours
  schema: Record<string, SchemaEntry>
  explain: { sample: ExplainLine[]; sections: unknown[]; explained: number; total: number }
  knob_learn: Record<string, KnobLearn | null>
  template_meta: Record<string, { tags: string[]; learn: string | null; notes: string | null }>
}
export const LEARN = content as unknown as Content

const ago = (min: number) => new Date(Date.now() - min * 60_000).toISOString()

// ---------------------------------------------------------------- templates: tags / node_count / learn / notes + knob learn
const NODE_COUNT: Record<string, number | null> = { 'single-dc-l3ls': 8, 'l2ls-fabric': 4, 'cv-pathfinder': 9, custom: null }
function decorateTemplates() {
  if (!templates.some((t) => t.name === 'cv-pathfinder')) {
    const base = templates.find((t) => t.name === 'l2ls-fabric')!
    templates.push({
      name: 'cv-pathfinder', title: 'CV Pathfinder (AutoVPN WAN, 2 sites + pathfinders)', generator: 'static', fabric_name: 'FABRIC',
      description: 'AVD cv-pathfinder example: WAN routers (edges, transit and pathfinders) with an EVPN control plane over DPS tunnels.',
      knobs: base.knobs.map((k) => ({ ...k })),
    } as Template)
  }
  for (const t of templates) {
    const m = LEARN.template_meta[t.name]
    t.tags = m?.tags ?? t.tags ?? []
    t.learn = m?.learn ?? null
    t.notes = m?.notes ?? null
    t.node_count = NODE_COUNT[t.name] ?? null
    for (const k of t.knobs) k.learn = LEARN.knob_learn[k.key] ?? null
  }
}
decorateTemplates()

// ---------------------------------------------------------------- state
export type UpdateScenario = 'available' | 'uptodate' | 'unconfigured' | 'no-self-update'
export const p6 = {
  scenario: 'available' as UpdateScenario,
  /** version the mock API reports after a simulated update */
  version: null as string | null,
  lastUpdate: null as UpdateStatus['last_update'],
  checkedAt: ago(60 * 3),
  checks: 0,
  applyRequests: [] as unknown[],
  progress: new Map<string, Set<string>>(),
  checkRequests: [] as { lesson: string; lab_id: string | null; checkpoint_id: string }[],
  /** lab id → names of hot-added test hosts / deployed flag / manual IPs */
  hot: new Map<string, Map<string, { deployed: boolean; ip: string | null }>>(),
  clientRequests: [] as unknown[],
  execRequests: [] as { client: string; kind: string; body: unknown }[],
}
export function resetPhase6() {
  p6.scenario = 'available'
  p6.version = null
  p6.lastUpdate = null
  p6.checkedAt = ago(60 * 3)
  p6.checks = 0
  p6.applyRequests = []
  p6.progress = new Map([['what-is-avd', new Set(['structured-exists'])]])
  p6.checkRequests = []
  p6.hot = new Map()
  p6.clientRequests = []
  p6.execRequests = []
}
resetPhase6()

// ---------------------------------------------------------------- §1 update
export const CURRENT = '0.6.0'
export const LATEST = '0.7.0'
export function updateStatus(): UpdateStatus {
  const current = p6.version ?? CURRENT
  const sc = p6.scenario
  const configured = sc !== 'unconfigured'
  const latest = configured ? (sc === 'uptodate' ? current : LATEST) : null
  const available = !!latest && latest !== current && current !== LATEST
  return {
    current, latest, available,
    url: configured ? `https://github.com/example/avdlab/releases/tag/v${LATEST}` : null,
    notes: configured ? `### Added\n- All AVD examples as templates (campus-fabric, cv-pathfinder, isis-ldp-ipvpn, single-dc-l3ls-ipv6)\n- In-app AVD learning: lessons, glossary, schema browser, config explainer, tours\n- Hot-add test hosts without redeploying the fabric\n### Changed\n- Self-update from the About page` : '',
    published_at: configured ? ago(60 * 20) : null, image: configured ? `ghcr.io/example/avdlab:${LATEST}` : null,
    checked_at: configured ? p6.checkedAt : null, error: null, source: configured ? 'github:example/avdlab' : null, configured,
    hint: configured ? null : 'update checks are disabled: set AVDLAB_UPDATE_SOURCE=github:<owner>/<repo> or a manifest URL',
    mode: 'source', can_self_update: sc !== 'no-self-update',
    reason: sc === 'no-self-update' ? 'docker socket not mounted at /var/run/docker.sock' : null,
    last_update: p6.lastUpdate,
  }
}

// ---------------------------------------------------------------- §3 lessons
export function progressOf(id: string, checkpoints: string[]) {
  const done = p6.progress.get(id) ?? new Set<string>()
  const passed = checkpoints.filter((c) => done.has(c))
  return { passed, total: checkpoints.length, completed: checkpoints.length > 0 && passed.length === checkpoints.length }
}
export function lessonList(): LessonSummary[] {
  return LEARN.lesson_summaries.map((l) => ({ ...l, progress: progressOf(l.id, l.checkpoints) }))
}
export function lessonDetail(id: string): Lesson | null {
  const s = LEARN.lesson_summaries.find((l) => l.id === id)
  if (!s) return null
  const order = LEARN.lesson_order
  const i = order.indexOf(id)
  const full = LEARN.lessons[id]
  const blocks = full?.blocks ?? [
    { kind: 'markdown' as const, text: `${s.summary ?? ''}\n\n*(mock: the full text of this lesson is served by the real backend — lessons 1, 4 and 11 are included in the mock.)*` },
    ...s.checkpoints.map((c) => ({ kind: 'check' as const, id: c, label: c, type: 'file_exists', args: {}, hint: null })),
  ]
  return { ...s, ...(full ?? {}), blocks, passed: [...(p6.progress.get(id) ?? [])], prev: i > 0 ? order[i - 1] : null, next: i + 1 < order.length ? order[i + 1] : null }
}
/** deterministic stand-in for learn/lessons.evaluate */
export function evaluateCheck(lessonId: string, cpId: string, lab: MockLab | undefined): { passed: boolean; detail: string; hint: string | null } | null {
  const l = lessonDetail(lessonId)
  const cp = l?.blocks.find((b) => b.kind === 'check' && b.id === cpId) as Extract<Lesson['blocks'][number], { kind: 'check' }> | undefined
  if (!cp) return null
  const hint = cp.hint ?? null
  if (!lab) return { passed: false, detail: 'no lab selected', hint }
  const a = cp.args ?? {}
  switch (cp.type) {
    case 'file_exists': {
      const path = String(a.path ?? '')
      const hit = lab.built && (lab.files[path] !== undefined || /^(intended|anta|documentation)\//.test(path))
      return { passed: !!hit, detail: hit ? `found ${path}` : `${path} does not exist yet — build the lab first`, hint }
    }
    case 'config_contains': {
      const node = String(a.node ?? '')
      const rel = String(a.file ?? `intended/configs/${node}.cfg`)
      if (!lab.built) return { passed: false, detail: `${rel} not found — build the lab first`, hint }
      return { passed: true, detail: `${rel}: found ${JSON.stringify(String(a.text ?? a.pattern ?? '').slice(0, 40))}`, hint }
    }
    case 'job_succeeded': {
      const j = db.jobs.find((x) => x.lab_id === lab.id && x.type === a.job)
      if (!j) return { passed: false, detail: `no ${String(a.job)} job has run for this lab yet`, hint }
      return { passed: j.status === 'succeeded', detail: `last ${String(a.job)} job: ${j.status}`, hint }
    }
    case 'anta_status': {
      const v = db.validations.find((x) => x.lab_id === lab.id)
      if (!v) return { passed: false, detail: 'no ANTA validation has run for this lab yet', hint }
      return { passed: (v.totals.failure ?? 0) === 0, detail: `latest run: ${JSON.stringify(v.totals)}`, hint }
    }
    case 'knob_equals': {
      const val = String(a.knob ?? '').split('.').reduce<unknown>((o, k) => (o && typeof o === 'object' ? (o as Record<string, unknown>)[k] : undefined), lab.knobs)
      const ok = String(val).toLowerCase() === String(a.value).toLowerCase()
      return { passed: ok, detail: `knob ${String(a.knob)} = ${JSON.stringify(val)}${ok ? '' : ` (expected ${JSON.stringify(a.value)})`}`, hint }
    }
  }
  return { passed: false, detail: `unknown check type ${cp.type}`, hint }
}

// ---------------------------------------------------------------- §3 schema
const skey = (name: SchemaName, key: string) => `${name}:${key}`
export function schemaLookup(name: SchemaName, key: string): SchemaEntry | null {
  const k = key.replace(/\[[^\]]*\]/g, '').replace(/\.\d+(?=\.|$)/g, '').replace(/^\.+|\.+$/g, '')
  const hit = LEARN.schema[skey(name, k)]
  if (hit) return hit
  // a leaf child of a sampled parent
  const parent = k.includes('.') ? k.slice(0, k.lastIndexOf('.')) : ''
  const child = k.slice(k.lastIndexOf('.') + 1)
  const p = LEARN.schema[skey(name, parent)]
  const c = p?.keys.find((x) => x.name === child)
  if (!c) return null
  return { schema: name, key: k, type: c.type, description: c.short, default: null, valid_values: null, min: null, max: null, required: !!c.required, deprecation: null, keys: [], items: null, doc_url: p.doc_url }
}
export function schemaSearch(q: string, limit = 25): SchemaHit[] {
  const terms = q.toLowerCase().split(/[\s.]+/).filter(Boolean)
  const seen = new Map<string, SchemaHit>()
  for (const [id, e] of Object.entries(LEARN.schema)) {
    const name = id.split(':')[0] as SchemaName
    const cands: { key: string; type: string | null; short: string }[] = [
      ...(e.key ? [{ key: e.key, type: e.type, short: (e.description ?? '').split('\n')[0] }] : []),
      ...e.keys.map((c: SchemaChild) => ({ key: e.key ? `${e.key}.${c.name}` : c.name, type: c.type, short: c.short })),
    ]
    for (const c of cands) {
      const hay = `${c.key} ${c.short}`.toLowerCase()
      const n = terms.filter((t) => hay.includes(t)).length
      if (!n || n < terms.length) continue
      const score = n * 20 + (terms.some((t) => c.key.toLowerCase().endsWith(t)) ? 10 : 0) - c.key.split('.').length
      const id2 = `${name}:${c.key}`
      if (!seen.has(id2)) seen.set(id2, { schema: name, key: c.key, type: c.type, short: c.short, score })
    }
  }
  return [...seen.values()].sort((a, b) => b.score - a.score).slice(0, limit)
}

// ---------------------------------------------------------------- §3 explain
const DOCS = 'https://avd.arista.com/6.4/ansible_collections/arista/avd/roles/eos_designs/docs/tables'
function mockRule(lab: MockLab, node: string, text: string, header: string): Partial<ExplainLine> | null {
  const pools = (lab.knobs?.pools ?? {}) as Record<string, string>
  const role = /spine/.test(node) ? 'spine' : 'l3leaf'
  const inp = (key: string, value: unknown, where: string, knob?: string) => ({ key, value, where, ...(knob ? { knob } : {}) })
  let m: RegExpExecArray | null
  if ((m = /^hostname (\S+)/.exec(text))) {
    return { rule: 'hostname', topic: 'system', why: `The hostname is the inventory hostname of the device (${m[1]}); AVD matches it with the node \`name\` under ${role}.`, inputs: [inp(`${role}.nodes[name=${m[1]}].name`, m[1], `group_vars/DC1_${role === 'spine' ? 'SPINES' : 'L3_LEAVES'}.yml`, 'dc_name')], doc_url: `${DOCS}/node-type-structure.html` }
  }
  if (header.startsWith('interface Management') && (m = /^ip address (\S+)/.exec(text))) {
    return { rule: 'mgmt-ip', topic: 'management', why: `Management IP ${m[1]} is the node's \`mgmt_ip\`, numbered from the management subnet by the l3ls generator.`, inputs: [inp(`${role}.nodes[name=${node}].mgmt_ip`, m[1], 'group_vars', 'mgmt_subnet')], doc_url: `${DOCS}/node-type-common-configuration.html` }
  }
  if (header === 'interface Loopback0' && (m = /^ip address (\S+)/.exec(text))) {
    const pool = pools.loopback ?? '10.255.0.0/27'
    return { rule: 'loopback0', topic: 'underlay', why: `Loopback0 ${m[1]} = loopback_ipv4_pool ${pool} + node id + loopback_ipv4_offset (leafs are offset by the number of spines so they never overlap).`, inputs: [inp(`${role}.defaults.loopback_ipv4_pool`, pool, 'group_vars', 'pools')], doc_url: `${DOCS}/node-type-loopback-vtep-configuration.html` }
  }
  if ((m = /^router bgp (\S+)/.exec(text))) {
    return { rule: 'bgp-as', topic: 'bgp', why: `BGP AS ${m[1]} comes from \`bgp_as\` of the node group (${role === 'spine' ? 'spine defaults' : 'one ASN per leaf pair: leaf_asn_base + pair index'}).`, inputs: [inp(role === 'spine' ? 'spine.defaults.bgp_as' : 'l3leaf.node_groups[].bgp_as', Number(m[1]), 'group_vars', role === 'spine' ? 'spine_asn' : 'leaf_asn_base')], doc_url: `${DOCS}/node-type-bgp-configuration.html` }
  }
  if ((m = /^router-id (\S+)/.exec(text))) {
    return { rule: 'bgp-router-id', topic: 'bgp', why: `The BGP router-id is Loopback0's address (${m[1]}).`, inputs: [inp('router_id', m[1], 'derived from Loopback0')], related_lines: [] }
  }
  return null
}
export function explainLines(lab: MockLab, node: string, text: string, wanted: (string | number)[]): ExplainLine[] {
  const lines = parseConfig(text)
  const sample = new Map(LEARN.explain.sample.map((s) => [s.line, s]))
  return wanted.map((w) => {
    const n = typeof w === 'number' || /^#?\d+$/.test(String(w)) ? Number(String(w).replace('#', '')) : null
    const cl = n ? lines[n - 1] : lines.find((l) => l.text.trim() === String(w).trim())
    if (!cl) return { line: String(w), no: null, section: null, parent: null, rule: null, avd_input: null, inputs: [], why: null, doc_url: null, related_lines: [], error: 'line not found in the intended config' }
    const stripped = cl.text.trim()
    const header = cl.section ? lines[cl.section - 1].text.trim() : stripped
    const base: ExplainLine = { line: stripped, no: cl.no, section: header, parent: cl.indent ? header : null, rule: null, avd_input: null, inputs: [], why: null, doc_url: null, related_lines: [] }
    if (!stripped || stripped === '!') return base
    const r = mockRule(lab, node, stripped, header)
    if (r) return { ...base, ...r, avd_input: r.inputs?.[0] ?? null, inputs: r.inputs ?? [] }
    const s = sample.get(stripped)
    if (s) return { ...s, no: cl.no, section: header, related_lines: [] }
    return base
  })
}
export function explainFile(lab: MockLab, node: string, text: string): ExplainFile {
  const lines = parseConfig(text).filter((l) => { const s = l.text.trim(); return s && s !== '!' && s !== 'end' })
  const res = explainLines(lab, node, text, lines.map((l) => l.no))
  const sections = new Map<string, { section: string | null; lines: number; explained: number; topics: Set<string> }>()
  for (const r of res) {
    const k = r.section ?? ''
    const s = sections.get(k) ?? { section: r.section, lines: 0, explained: 0, topics: new Set<string>() }
    s.lines++
    if (r.why) { s.explained++; if (r.topic) s.topics.add(r.topic) }
    sections.set(k, s)
  }
  return { host: node, lines: res, sections: [...sections.values()].map((s) => ({ ...s, topics: [...s.topics].sort() })), explained: res.filter((r) => r.why).length, total: res.length }
}

// ---------------------------------------------------------------- §4 test hosts
export const labDeployed = (l: MockLab) => l.state === 'deployed' || l.state === 'validated'
export function hotOf(labId: string) {
  let m = p6.hot.get(labId)
  if (!m) { m = new Map(); p6.hot.set(labId, m) }
  return m
}
/** GET /labs/{id}/clients with hot_added / deployed (and manual IPs of hot-added hosts) */
export function clientsOf(lab: MockLab): LabClient[] {
  const hot = hotOf(lab.id)
  return clientPlans(lab).map((c) => {
    const h = hot.get(c.name)
    if (!h) return { ...c, hot_added: false, deployed: labDeployed(lab) }
    const vlans = h.ip ? c.vlans.map((v, i) => (i === 0 ? { ...v, ip: h.ip } : v)) : c.vlans
    return { ...c, vlans, hot_added: true, deployed: h.deployed, container_state: h.deployed ? 'running' : null, mgmt_ip: null }
  })
}
