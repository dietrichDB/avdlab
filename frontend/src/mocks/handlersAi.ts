import { http, HttpResponse, delay } from 'msw'
import type { AiDesignIn, AiDesignOut, AiLinkIn, AiLinkInfo, AiSharedSettings, AiSharedSettingsIn, AiTopologyOp } from '../api/types'
import { commit, db, renderFiles, templates, validateLabKnobs, type MockLab } from './data'

const A = '/api'

/** v0.10.0: AI lab designer mock state (tests reset it with `resetAi`) */
export const aiDb = {
  link: null as AiLinkInfo | null,
  shared: { ai_default_provider: '', ai_default_base_url: '', ai_default_model: '', ai_shared_allowed: false, has_ai_default_api_key: false, configured: false } as AiSharedSettings,
  designCalls: [] as AiDesignIn[],
  applyCalls: [] as { lab_id: string; knobs: Record<string, unknown>; topology_ops: AiTopologyOp[] }[],
}

export function resetAi(linked = true) {
  aiDb.link = linked ? { provider: 'anthropic', base_url: '', model: 'claude-sonnet-4-5', has_api_key: true, created_at: new Date().toISOString(), updated_at: new Date().toISOString() } : null
  aiDb.shared = { ai_default_provider: '', ai_default_base_url: '', ai_default_model: '', ai_shared_allowed: false, has_ai_default_api_key: false, configured: false }
  aiDb.designCalls = []
  aiDb.applyCalls = []
}
resetAi()

const sharedOk = () => aiDb.shared.ai_shared_allowed && aiDb.shared.configured
const linkView = () => ({
  linked: !!aiDb.link, link: aiDb.link,
  shared: { available: sharedOk(), provider: sharedOk() ? aiDb.shared.ai_default_provider : null, model: sharedOk() ? aiDb.shared.ai_default_model : null },
  effective: aiDb.link ? 'user' : sharedOk() ? 'shared' : null,
})

export const ACME_TENANTS = [{ name: 'ACME', mac_vrf_vni_base: 10000, vrfs: [{ name: 'ACME_PROD', vrf_vni: 10, svis: [
  { id: 110, name: 'APP', enabled: true, ip_address_virtual: '10.1.110.1/24' },
  { id: 120, name: 'DB', enabled: true, ip_address_virtual: '10.1.120.1/24' }] }] }]

const SERVER_OP: AiTopologyOp = { op: 'add_endpoint', name: 'srv-ai-1', adapters: [{ switches: ['dc1-leaf1a', 'dc1-leaf1b'], switch_ports: ['Ethernet7', 'Ethernet7'], mode: 'trunk', vlans: '110,120', port_channel: { mode: 'active' } }] }

const MOCK_DIFF = `--- a/group_vars/NETWORK_SERVICES.yml
+++ b/group_vars/NETWORK_SERVICES.yml
@@ -1,3 +1,12 @@
 ---
 tenants:
-- name: TENANT1
+- name: ACME
+  mac_vrf_vni_base: 10000
+  vrfs:
+  - name: ACME_PROD
+    vrf_vni: 10
--- a/group_vars/CONNECTED_ENDPOINTS.yml
+++ b/group_vars/CONNECTED_ENDPOINTS.yml
@@ -20,3 +20,10 @@
+- name: srv-ai-1
+  adapters:
+  - switches: [dc1-leaf1a, dc1-leaf1b]
`

function base(out: Partial<AiDesignOut>): AiDesignOut {
  return { kind: 'proposal', summary: '', assumptions: [], knobs: {}, knob_changes: [], topology_ops: [], dry_run: null, assistant_message: '', repaired: false,
    source: aiDb.link ? 'user' : 'shared', provider: aiDb.link?.provider ?? 'openai', model: aiDb.link?.model ?? aiDb.shared.ai_default_model, ...out }
}

/** Canned designer: "server" without context → a question; "invalid" → a proposal that failed validation; else a proposal. */
export function mockDesign(b: AiDesignIn): AiDesignOut {
  const p = b.prompt.toLowerCase()
  const lab = b.lab_id ? db.labs.find((l) => l.id === b.lab_id) : undefined
  const current = (lab?.knobs ?? b.current_knobs ?? {}) as Record<string, unknown>
  if (/server/.test(p) && !(b.history ?? []).length && !/pair/.test(p)) {
    const q = 'Which leaf pair should the server connect to, and which VLANs does it need?'
    return base({ kind: 'question', question: q, summary: q, assistant_message: q })
  }
  if (/invalid/.test(p)) {
    return base({ summary: '9 spines', knobs: { spine_count: 9 }, knob_changes: [{ key: 'spine_count', old: current.spine_count ?? 2, new: 9 }],
      dry_run: { valid: false, violations: [{ path: 'knobs.spine_count', message: 'spine_count: must be <= 4' }], changed_files: [], diff: '' }, repaired: true,
      assistant_message: 'I called propose_lab_changes with: {"knobs":{"spine_count":9}}' })
  }
  if (!lab) {
    const knobs = { spine_count: 3, leaf_pairs: 4, l2leaf_per_pair: 0, tenants: ACME_TENANTS, cloudvision: { mode: 'cvaas', region: 'auto', servers: [], disable_aaa: true, topology_hints: true, device_tags: {} } }
    return base({
      summary: '3 spines and 4 MLAG leaf pairs without L2 leafs, tenant ACME with VLANs 110 and 120, streaming to CloudVision as-a-Service.',
      assumptions: ['VRF ACME_PROD with L3VNI 10', 'anycast gateways 10.1.110.1/24 and 10.1.120.1/24', 'CVaaS region auto'],
      knobs, knob_changes: Object.entries(knobs).map(([k, v]) => ({ key: k, old: current[k], new: v })),
      dry_run: { valid: true, violations: [], changed_files: ['group_vars/DC1_SPINES.yml', 'group_vars/DC1_L3_LEAVES.yml', 'group_vars/NETWORK_SERVICES.yml', 'group_vars/FABRIC.yml', 'inventory.yml'], diff: MOCK_DIFF, nodes: 11 },
      assistant_message: 'I called propose_lab_changes with: {"knobs":{"spine_count":3,"leaf_pairs":4}}',
    })
  }
  const knobs = { leaf_pairs: 3, tenants: ACME_TENANTS }
  return base({
    summary: 'Adds a third MLAG leaf pair, tenant ACME with VLANs 110/120 and server srv-ai-1 dual-homed to leaf pair 1.',
    assumptions: ['VRF ACME_PROD with L3VNI 10', 'srv-ai-1 uses Ethernet7 on dc1-leaf1a/b (free) as an LACP port-channel'],
    knobs, knob_changes: Object.entries(knobs).map(([k, v]) => ({ key: k, old: current[k], new: v })),
    topology_ops: [SERVER_OP],
    dry_run: { valid: true, violations: [], changed_files: ['group_vars/CONNECTED_ENDPOINTS.yml', 'group_vars/DC1_L3_LEAVES.yml', 'group_vars/NETWORK_SERVICES.yml', 'inventory.yml'], diff: MOCK_DIFF, warnings: [], nodes: 10 },
    assistant_message: 'I called propose_lab_changes with: {"knobs":{"leaf_pairs":3},"topology_ops":[{"op":"add_endpoint","name":"srv-ai-1"}]}',
  })
}

export function aiHandlers(labOut: (l: MockLab) => unknown) {
  return [
    http.get(`${A}/ai/link`, () => HttpResponse.json(linkView())),
    http.put(`${A}/ai/link`, async ({ request }) => {
      const b = (await request.json()) as AiLinkIn
      if (!['anthropic', 'openai'].includes(b.provider)) return HttpResponse.json({ detail: { message: 'provider must be one of [anthropic, openai]', violations: [] } }, { status: 422 })
      if (!b.model?.trim()) return HttpResponse.json({ detail: { message: 'model is required', violations: [] } }, { status: 422 })
      const keep = aiDb.link?.has_api_key ?? false
      aiDb.link = { provider: b.provider, base_url: b.base_url ?? '', model: b.model, has_api_key: b.api_key === undefined || b.api_key === null ? keep : !!b.api_key,
        created_at: aiDb.link?.created_at ?? new Date().toISOString(), updated_at: new Date().toISOString() }
      return HttpResponse.json(linkView())
    }),
    http.delete(`${A}/ai/link`, () => { aiDb.link = null; return new HttpResponse(null, { status: 204 }) }),
    http.post(`${A}/ai/link/test`, async ({ request }) => {
      const b = ((await request.json().catch(() => ({}))) ?? {}) as Partial<AiLinkIn>
      await delay(200)
      if ((b.provider ?? aiDb.link?.provider) === 'anthropic' && !b.api_key && !aiDb.link?.has_api_key) {
        return HttpResponse.json({ ok: false, models: [], model_listed: false, error: 'anthropic HTTP 401: invalid x-api-key' })
      }
      const models = (b.provider ?? aiDb.link?.provider) === 'anthropic' ? ['claude-sonnet-4-5', 'claude-opus-4-1'] : ['llama3.1', 'qwen2.5']
      const model = b.model ?? aiDb.link?.model ?? ''
      return HttpResponse.json({ ok: true, models, model_listed: models.includes(model), error: null })
    }),
    http.post(`${A}/ai/design`, async ({ request }) => {
      const b = (await request.json()) as AiDesignIn
      if (!aiDb.link && !sharedOk()) return HttpResponse.json({ detail: { message: 'no AI model linked: link Claude, OpenAI or a local model on your Account page', code: 'no_ai_link' } }, { status: 409 })
      if (b.lab_id && !db.labs.some((l) => l.id === b.lab_id)) return HttpResponse.json({ detail: 'lab not found' }, { status: 404 })
      aiDb.designCalls.push(b)
      await delay(300)
      const out = mockDesign(b)
      if (!b.lab_id && !b.template) {  // from scratch: the mock always picks single-dc-l3ls
        return HttpResponse.json({ ...out, template: 'single-dc-l3ls', template_reason: 'generic EVPN spine/leaf fabric', lab_name: 'ai-lab' })
      }
      return HttpResponse.json(out)
    }),
    http.post(`${A}/ai/design/apply`, async ({ request }) => {
      const b = (await request.json()) as { lab_id: string; knobs: Record<string, unknown>; topology_ops: AiTopologyOp[] }
      aiDb.applyCalls.push(b)
      const l = db.labs.find((x) => x.id === b.lab_id)
      if (!l) return HttpResponse.json({ detail: 'lab not found' }, { status: 404 })
      const t = templates.find((x) => x.name === l.template)!
      const next: Record<string, unknown> = { ...l.knobs, ...b.knobs }
      for (const op of b.topology_ops ?? []) {
        if (op.op === 'add_endpoint') next.servers = [...((next.servers as unknown[]) ?? []), { name: op.name, adapters: op.adapters }]
      }
      const violations = validateLabKnobs(t, next)
      if (violations.length) return HttpResponse.json({ detail: { message: `${violations.length} violation(s)`, violations } }, { status: 422 })
      await delay(300)
      l.knobs = next
      if (Object.keys(b.knobs).length) commit(l, renderFiles(l), `knobs: ${Object.keys(b.knobs).join(', ')}`)
      if (b.topology_ops?.length) commit(l, renderFiles(l), `topology edit: ${b.topology_ops.map((o) => `${o.op} ${String(o.name ?? '')}`.trim()).join('; ')}`)
      if (l.state === 'built') l.state = 'draft'
      return HttpResponse.json({ lab: labOut(l), knob_changes: Object.entries(b.knobs).map(([k, v]) => ({ key: k, old: null, new: v })),
        topology: b.topology_ops?.length ? { commit: l.history[0]?.sha ?? null, changed_files: ['group_vars/CONNECTED_ENDPOINTS.yml'], warnings: [] } : null, notes: [] })
    }),
    http.get(`${A}/admin/ai`, () => HttpResponse.json(aiDb.shared)),
    http.put(`${A}/admin/ai`, async ({ request }) => {
      const b = (await request.json()) as AiSharedSettingsIn
      const s = { ...aiDb.shared }
      if (b.ai_default_provider !== undefined) s.ai_default_provider = b.ai_default_provider ?? ''
      if (b.ai_default_base_url !== undefined) s.ai_default_base_url = b.ai_default_base_url ?? ''
      if (b.ai_default_model !== undefined) s.ai_default_model = b.ai_default_model ?? ''
      if (b.ai_shared_allowed !== undefined) s.ai_shared_allowed = !!b.ai_shared_allowed
      if (b.ai_default_api_key !== undefined && b.ai_default_api_key !== null) s.has_ai_default_api_key = b.ai_default_api_key !== ''
      s.configured = !!s.ai_default_provider && !!s.ai_default_model
      aiDb.shared = s
      return HttpResponse.json(s)
    }),
    http.post(`${A}/admin/ai/test`, () => HttpResponse.json(aiDb.shared.configured
      ? { ok: true, models: [aiDb.shared.ai_default_model], model_listed: true, error: null }
      : { ok: false, models: [], model_listed: false, error: 'set a provider and a model first' })),
  ]
}
