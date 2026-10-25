import { http, HttpResponse, delay } from 'msw'
import type { MssVerifyParams, NetBoxSyncParams, NetBoxTarget, NetBoxTargetIn } from '../api/types'
import { addJob, db, newId, type MockJob, type MockLab } from './data'
import { p3 } from './phase3'
import { actInfo, actTopologyYaml, mssDerive, mssProbe, mssSummary, netboxSync, netboxTargetOut, p4, ztpStatus } from './phase4'
import { ACT_SNAPSHOT_REASON, ACT_TRAFFIC_REASON } from '../lib/act'
import { normalizeProvisioning } from '../lib/provisioning'
import { normalizeSegmentation } from '../lib/mss'

type RunJob = (j: MockJob, lines: string[], finish?: (j: MockJob) => { error?: string; result?: Record<string, unknown> } | void, stepMs?: number) => Response

const A = '/api'
const notFound = (what = 'not found') => HttpResponse.json({ detail: what }, { status: 404 })
const conflict = (detail: unknown) => HttpResponse.json({ detail }, { status: 409 })
const labOf = (id: unknown) => db.labs.find((l) => l.id === id)
const deployed = (l: MockLab) => l.state === 'deployed' || l.state === 'validated'
const running = (l: MockLab, type: string) => db.jobs.some((j) => j.lab_id === l.id && j.type === type && (j.status === 'queued' || j.status === 'running'))

/** CONTRACT-4 handlers; registered before the generic ones so they can take over (ACT) or fall through (return undefined). */
export function phase4Handlers(runJob: RunJob) {
  return [
    // ------------------------------------------------ A: ZTP
    http.get(`${A}/labs/:id/ztp`, async ({ params }) => {
      const l = labOf(params.id)
      if (!l) return notFound('lab not found')
      await delay(150)
      return HttpResponse.json(ztpStatus(l, running(l, 'lab.deploy')))
    }),

    // ------------------------------------------------ B: NetBox
    http.get(`${A}/netbox/targets`, () => HttpResponse.json(p4.netboxTargets.map(netboxTargetOut))),
    http.post(`${A}/netbox/targets`, async ({ request }) => {
      const b = (await request.json()) as NetBoxTargetIn
      if (!b.name || !b.url) return HttpResponse.json({ detail: [{ loc: ['body', b.name ? 'url' : 'name'], msg: 'field required' }] }, { status: 422 })
      if (!/^https?:\/\//.test(b.url)) return HttpResponse.json({ detail: 'url must start with http:// or https://' }, { status: 422 })
      if (p4.netboxTargets.some((t) => t.name === b.name)) return conflict('a NetBox target with this name exists')
      const t: NetBoxTarget = { id: newId(), name: b.name, url: b.url.replace(/\/$/, ''), verify_certs: b.verify_certs ?? true, has_token: !!b.token, token_meta: b.token ? { updated_at: new Date().toISOString() } : null, created_at: new Date().toISOString(), labs: [] }
      p4.netboxTargets.push(t)
      return HttpResponse.json(netboxTargetOut(t), { status: 201 })
    }),
    http.get(`${A}/netbox/targets/:tid`, ({ params }) => {
      const t = p4.netboxTargets.find((x) => x.id === params.tid)
      return t ? HttpResponse.json(netboxTargetOut(t)) : notFound('NetBox target not found')
    }),
    http.patch(`${A}/netbox/targets/:tid`, async ({ params, request }) => {
      const t = p4.netboxTargets.find((x) => x.id === params.tid)
      if (!t) return notFound('NetBox target not found')
      const b = (await request.json()) as Partial<NetBoxTargetIn>
      if (b.name && b.name !== t.name && p4.netboxTargets.some((x) => x.name === b.name)) return conflict('a NetBox target with this name exists')
      if (b.name) t.name = b.name
      if (b.url) t.url = b.url.replace(/\/$/, '')
      if (b.verify_certs !== undefined) t.verify_certs = b.verify_certs
      if (b.token !== undefined) { t.has_token = !!b.token; t.token_meta = b.token ? { updated_at: new Date().toISOString() } : null }
      return HttpResponse.json(netboxTargetOut(t))
    }),
    http.delete(`${A}/netbox/targets/:tid`, ({ params }) => {
      const bound = db.labs.filter((l) => l.netbox_target_id === params.tid).map((l) => l.name)
      if (bound.length) return conflict({ message: `target is used by ${bound.length} lab(s): unbind them first`, labs: bound })
      p4.netboxTargets = p4.netboxTargets.filter((t) => t.id !== params.tid)
      return new HttpResponse(null, { status: 204 })
    }),
    http.post(`${A}/netbox/targets/:tid/test`, async ({ params }) => {
      const t = p4.netboxTargets.find((x) => x.id === params.tid)
      if (!t) return notFound('NetBox target not found')
      await delay(300)
      if (!t.has_token) return HttpResponse.json({ ok: false, version: null, error: 'no API token stored' })
      if (t.url.startsWith('https://') && t.verify_certs) return HttpResponse.json({ ok: false, version: null, error: `[SSL: CERTIFICATE_VERIFY_FAILED] (${t.url})` })
      return HttpResponse.json({ ok: true, version: '4.1.7', error: null })
    }),
    http.get(`${A}/labs/:id/netbox/status`, ({ params }) => {
      const l = labOf(params.id)
      if (!l) return notFound('lab not found')
      const t = p4.netboxTargets.find((x) => x.id === l.netbox_target_id)
      return HttpResponse.json({ target: t ? netboxTargetOut(t) : null, last_sync: (l.settings as Record<string, unknown> | undefined)?.netbox_last_sync ?? null })
    }),
    http.post(`${A}/labs/:id/netbox/sync`, async ({ params, request }) => {
      const l = labOf(params.id)
      if (!l) return notFound('lab not found')
      if (!l.built) return conflict(`lab is ${l.state}: build it before syncing to NetBox`)
      const t = p4.netboxTargets.find((x) => x.id === l.netbox_target_id)
      if (!t) return conflict("lab has no NetBox target: pick one in the lab's NetBox tab")
      if (!t.has_token) return conflict(`NetBox target ${t.name} has no API token`)
      if (running(l, 'lab.netbox_sync')) return conflict({ message: 'lab.netbox_sync is already running for this lab' })
      const p: NetBoxSyncParams = { delete_stale: false, dry_run: false, include_clients: true, ...((await request.json().catch(() => ({}))) as Partial<NetBoxSyncParams>) }
      const j = addJob({ type: 'lab.netbox_sync', lab_id: l.id, host_id: l.host_id ?? undefined, params: p as unknown as Record<string, unknown> })
      const r = netboxSync(l, t, p)
      const line = (k: 'created' | 'updated' | 'deleted') => `[netbox] ${p.dry_run ? 'would be ' : ''}${k}: ${Object.entries(r[k]).map(([a, b]) => `${a} ${b}`).join(', ') || 'nothing'}`
      return runJob(j, [
        `NetBox ${t.url} (target ${t.name}) delete_stale=${p.delete_stale} dry_run=${p.dry_run} include_clients=${p.include_clients}`,
        'pynetbox status(): NetBox 4.1.7', 'tags avdlab, avdlab:' + l.slug, 'manufacturer Arista · platform Arista EOS · device types · roles · site',
        'devices …', 'interfaces …', 'VLANs · VRFs · prefixes · IP addresses …', 'cables (topology.csv) …',
        line('created'), line('updated'), line('deleted'), ...r.skipped.map((s) => `[netbox] skipped: ${s}`), '[ok] sync complete',
      ], (job) => {
        const res = { ...r, job_id: job.id }
        l.settings = { ...(l.settings ?? {}), netbox_last_sync: res }
        return { result: res as unknown as Record<string, unknown> }
      }, 100)
    }),

    // ------------------------------------------------ C: MSS
    http.get(`${A}/labs/:id/mss`, ({ params }) => {
      const l = labOf(params.id)
      if (!l) return notFound('lab not found')
      const run = p3.runs.find((r) => r.lab_id === l.id && r.kind === 'mss')
      const res = run?.results as { passed?: number; failed?: number } | undefined
      return HttpResponse.json({ ...mssSummary(l), last_verify: run ? { run_id: run.id, job_id: run.job_id, passed: res?.passed ?? null, failed: res?.failed ?? null, created_at: run.created_at } : null })
    }),
    http.post(`${A}/labs/:id/mss/verify`, async ({ params, request }) => {
      const l = labOf(params.id)
      if (!l) return notFound('lab not found')
      if (!deployed(l)) return conflict(`lab is not deployed (state: ${l.state})`)
      const b = { cases: null, ...((await request.json().catch(() => ({}))) as Partial<MssVerifyParams>) } as MssVerifyParams
      const seg = normalizeSegmentation(l.knobs?.segmentation)
      let cases = b.cases
      if (cases === null) {
        if (!seg.vrfs.length) return conflict('no segments defined: set knobs.segmentation first (or pass explicit cases)')
        cases = mssDerive(l)
        if (!cases.length) return conflict('no case can be derived: no client pair is routed between known segments')
      } else {
        if (!cases.length) return HttpResponse.json({ detail: { message: 'cases is empty', violations: [{ path: 'cases', message: 'empty' }] } }, { status: 422 })
        const bad = [...new Set(cases.map((c) => c.application).filter((a) => a !== 'app-match-all' && !seg.applications.some((x) => x.name === a)))]
        if (bad.length) return HttpResponse.json({ detail: { message: `unknown application(s): ${bad.join(', ')}`, violations: bad.map((a) => ({ path: 'cases.application', message: `unknown application ${a}` })) } }, { status: 422 })
      }
      const j = addJob({ type: 'lab.mss_verify', lab_id: l.id, host_id: l.host_id ?? undefined, params: b as unknown as Record<string, unknown> })
      const out = mssProbe(l, cases)
      return runJob(j, [
        `${cases.length} case(s)${b.cases === null ? ' (auto-derived from the policy matrix)' : ''}`,
        ...out.cases.map((c) => `${c.src} → ${c.dst} ${c.application}: expect ${c.expect}, observed ${c.observed} — ${c.pass ? 'PASS' : 'FAIL'} (${c.detail})`),
        `[ok] ${out.passed} passed, ${out.failed} failed`,
      ], (job) => {
        p3.runs.unshift({ id: newId(), lab_id: l.id, job_id: job.id, kind: 'mss', params: b as unknown as Record<string, unknown>, results: { ...out, results: out.cases }, created_at: new Date().toISOString() })
        return { result: out as unknown as Record<string, unknown> }
      }, 80)
    }),

    // ------------------------------------------------ D: ACT
    http.get(`${A}/labs/:id/act`, ({ params }) => {
      const l = labOf(params.id)
      return l ? HttpResponse.json(actInfo(l)) : notFound('lab not found')
    }),
    http.get(`${A}/labs/:id/act/topology`, ({ params }) => {
      const l = labOf(params.id)
      if (!l) return notFound('lab not found')
      const info = actInfo(l)
      if (!info.prepared) return notFound('no ACT topology: set the lab target to act and build the lab')
      return new HttpResponse(actTopologyYaml(info, l), { headers: { 'Content-Type': 'application/x-yaml', 'Content-Disposition': 'attachment; filename="FABRIC-topology.yml"' } })
    }),
    http.post(`${A}/labs/:id/act/prepare`, async ({ params }) => {
      const l = labOf(params.id)
      if (!l) return notFound('lab not found')
      if (l.target !== 'act') return conflict('lab target is not act: set it in the lab settings and build')
      if (!l.built) return conflict('no ACT topology: build the lab with target act first')
      await delay(200)
      p4.actPrepared.add(l.id)
      return HttpResponse.json(actInfo(l))
    }),
    // build: an act lab's build also writes the ACT topology (falls through to the generic build)
    http.post(`${A}/labs/:id/build`, ({ params }) => {
      const l = labOf(params.id)
      if (l?.target === 'act') p4.actPrepared.add(l.id)
      return undefined
    }),
    // deploy: ACT executor (no containerlab); ZTP labs record the start and fall through
    http.post(`${A}/labs/:id/deploy`, ({ params }) => {
      const l = labOf(params.id)
      if (!l) return undefined
      if (l.target !== 'act') {
        if (normalizeProvisioning(l.knobs?.provisioning).mode === 'ztp') p4.ztpStart.set(l.id, Date.now() + 2000)
        return undefined
      }
      const host = db.hosts.find((h) => h.id === l.host_id)
      if (!host) return conflict('lab has no host: set the ACT tools server (host of kind act) as the lab host')
      if (host.kind !== 'act') return conflict(`host ${host.name} is kind ${host.kind}: an ACT lab deploys through a host of kind act (the ACT tools server)`)
      if (!l.built || !p4.actPrepared.has(l.id)) return conflict('no ACT topology: build the lab with target act first')
      const info = actInfo(l)
      const sw = info.nodes.filter((n) => n.role === 'switch')
      const j = addJob({ type: 'lab.deploy', lab_id: l.id, host_id: host.id, params: { target: 'act' } })
      return runJob(j, [
        `rsync → ${host.address}:/opt/avdlab/labs/${l.slug} (tools server)`,
        'target act: containerlab skipped',
        `waiting for eAPI on ${sw.length} switches from the tools server`,
        ...sw.map((n) => `  [ok] ${n.name} ${n.ip} eAPI ready`),
        `$ docker run --rm -v /opt/avdlab/labs/${l.slug}:/lab -w /lab avdlab-agent ansible-playbook playbooks/deploy.yml`,
        'TASK [arista.avd.eos_config_deploy_eapi : Replace configuration with intended configuration]',
        ...sw.map((n) => `changed: [${n.name}]`),
        '[ok] deployed (ACT)',
      ], () => {
        const nodes = sw.map((n) => ({ name: n.name, kind: 'veos', state: 'running', mgmt_ip: n.ip, image: null, eapi: true }))
        l.runtime = { deployed: true, nodes }
        l.state = 'deployed'
        l.settings = { ...(l.settings ?? {}), deployed_at: new Date().toISOString() }
        return { result: { nodes, ready: nodes.map((n) => n.name), pending: [] } }
      }, 100)
    }),
    http.post(`${A}/labs/:id/destroy`, ({ params }) => {
      const l = labOf(params.id)
      if (l?.target !== 'act') return undefined
      const j = addJob({ type: 'lab.destroy', lab_id: l.id, host_id: l.host_id ?? undefined })
      return runJob(j, ['target act: nothing to tear down from avdlab', 'destroy the ACT lab in the ACT UI (act.arista.com)', '[ok] marked destroyed'], () => {
        l.runtime = null; l.state = 'destroyed'
        return { result: { destroyed: 0 } }
      })
    }),
    http.post(`${A}/labs/:id/traffic/ping`, ({ params }) => (labOf(params.id)?.target === 'act' && deployed(labOf(params.id)!) ? conflict(ACT_TRAFFIC_REASON) : undefined)),
    http.post(`${A}/labs/:id/traffic/iperf`, ({ params }) => (labOf(params.id)?.target === 'act' && deployed(labOf(params.id)!) ? conflict(ACT_TRAFFIC_REASON) : undefined)),
    http.post(`${A}/labs/:id/snapshot`, ({ params }) => (labOf(params.id)?.target === 'act' && deployed(labOf(params.id)!) ? conflict(ACT_SNAPSHOT_REASON) : undefined)),
  ]
}

