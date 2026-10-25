import { http, HttpResponse } from 'msw'
import type { SchemaName, TestHostIn } from '../api/phase6'
import { addJob, commit, db, renderFiles, type MockJob } from './data'
import { editableTopology, metaOf, portsOf, serversOf } from './phase5'
import {
  CURRENT, LATEST, LEARN, clientsOf, evaluateCheck, explainFile, explainLines, hotOf, labDeployed, lessonDetail, lessonList, p6, progressOf,
  schemaLookup, schemaSearch, updateStatus,
} from './phase6'
import { clientPlans } from './phase3'

const A = '/api'
type RunJob = (j: MockJob, lines: string[], finish?: (j: MockJob) => { error?: string; result?: Record<string, unknown> } | void, stepMs?: number) => Response
const notFound = (what = 'not found') => HttpResponse.json({ detail: what }, { status: 404 })
const conflict = (detail: unknown) => HttpResponse.json({ detail }, { status: 409 })
const labOf = (id: unknown) => db.labs.find((l) => l.id === id)
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$/

/** CONTRACT-6 handlers (registered first: GET /meta and GET /labs/{id}/clients are taken over). */
export function phase6Handlers(runJob: RunJob) {
  return [
    // ------------------------------------------------ §1 update
    http.get(`${A}/meta`, () => HttpResponse.json({ ...metaOf(), version: p6.version ?? CURRENT })),
    http.get(`${A}/meta/update`, () => HttpResponse.json(updateStatus())),
    http.post(`${A}/meta/update/check`, () => { p6.checks++; p6.checkedAt = new Date().toISOString(); return HttpResponse.json(updateStatus()) }),
    http.get(`${A}/meta/update/log`, () => HttpResponse.json({ log: p6.lastUpdate ? '[avdlab-update] exit=0\n' : '', last_update: p6.lastUpdate })),
    http.post(`${A}/meta/update/apply`, async ({ request }) => {
      const b = ((await request.json().catch(() => ({}))) ?? {}) as { force?: boolean; to?: string }
      p6.applyRequests.push(b)
      const st = updateStatus()
      if (!st.can_self_update) return conflict({ message: 'self-update is not possible', reason: st.reason })
      if (!b.force && !st.available) return conflict({ message: `no newer release known (current ${st.current}, latest ${st.latest}): check first or pass force: true`, reason: null })
      const busy = db.jobs.find((j) => j.status === 'running' || j.status === 'queued')
      if (busy) return conflict({ message: `job ${busy.id} (${busy.type}) is running: wait for it to finish before updating`, reason: null })
      const j = addJob({ type: 'app.update', params: { to: b.to ?? LATEST } })
      const from = st.current
      return runJob(j, [
        `update ${from} → ${LATEST} (mode ${st.mode})`,
        '$ docker run -d --rm --name avdlab-update-' + j.id.slice(0, 8) + ' -v /var/run/docker.sock:/var/run/docker.sock … docker:27-cli sh -c \'git -C .. pull --ff-only && docker compose up -d --build\'',
        '[helper] Already up to date? no: fast-forward 42 files',
        '[helper] building api … done',
        '[helper] recreating avdlab-api … the API restarts now',
      ], () => {
        p6.version = LATEST
        p6.lastUpdate = { from, to: LATEST, at: new Date().toISOString(), ok: true, mode: st.mode, job_id: j.id }
        return { result: { from, to: LATEST, helper: `avdlab-update-${j.id.slice(0, 8)}` } }
      }, 120)
    }),

    // ------------------------------------------------ §3 learning
    http.get(`${A}/learn/lessons`, () => HttpResponse.json(lessonList())),
    http.get(`${A}/learn/lessons/:lid`, ({ params }) => {
      const l = lessonDetail(String(params.lid))
      return l ? HttpResponse.json(l) : notFound('lesson not found')
    }),
    http.post(`${A}/learn/lessons/:lid/check`, async ({ params, request }) => {
      const b = (await request.json()) as { lab_id: string | null; checkpoint_id: string }
      const lid = String(params.lid)
      p6.checkRequests.push({ lesson: lid, lab_id: b.lab_id, checkpoint_id: b.checkpoint_id })
      if (!lessonDetail(lid)) return notFound('lesson not found')
      const r = evaluateCheck(lid, b.checkpoint_id, b.lab_id ? labOf(b.lab_id) : undefined)
      if (!r) return notFound('checkpoint not found')
      if (r.passed) { const s = p6.progress.get(lid) ?? new Set<string>(); s.add(b.checkpoint_id); p6.progress.set(lid, s) }
      return HttpResponse.json({ lesson_id: lid, checkpoint_id: b.checkpoint_id, passed: r.passed, detail: r.detail, hint: r.passed ? null : r.hint })
    }),
    http.get(`${A}/learn/progress`, () => {
      const lessons = Object.fromEntries(LEARN.lesson_summaries.map((l) => [l.id, progressOf(l.id, l.checkpoints)]))
      return HttpResponse.json({
        lessons, completed: Object.values(lessons).filter((x) => x.completed).length, total: LEARN.lesson_summaries.length,
        items: [...p6.progress.entries()].flatMap(([lesson_id, s]) => [...s].map((checkpoint_id) => ({ lesson_id, checkpoint_id, lab_id: null, passed_at: null }))),
      })
    }),
    http.get(`${A}/learn/glossary`, () => HttpResponse.json(LEARN.glossary)),
    http.get(`${A}/learn/tours`, () => HttpResponse.json(LEARN.tours)),
    http.get(`${A}/learn/schema/search`, ({ request }) => {
      const u = new URL(request.url)
      return HttpResponse.json(schemaSearch(u.searchParams.get('q') ?? '', Number(u.searchParams.get('limit') ?? 25)))
    }),
    http.get(`${A}/learn/schema/:name`, ({ params, request }) => {
      const name = String(params.name) as SchemaName
      if (name !== 'eos_designs' && name !== 'eos_cli_config_gen') return notFound(`unknown schema ${name}`)
      const key = new URL(request.url).searchParams.get('key') ?? ''
      const e = schemaLookup(name, key)
      return e ? HttpResponse.json(e) : HttpResponse.json({ detail: { message: `no key '${key}' in ${name}`, suggestions: schemaSearch(key.split('.').pop() ?? key, 5).map((h) => h.key) } }, { status: 404 })
    }),
    http.get(`${A}/learn/explain`, ({ request }) => {
      const u = new URL(request.url)
      const lab = labOf(u.searchParams.get('lab_id'))
      if (!lab) return notFound('lab not found')
      const node = u.searchParams.get('node') ?? ''
      const text = lab.files[`intended/configs/${node}.cfg`]
      if (!text) return notFound(`no intended config for '${node}' — build the lab first`)
      return HttpResponse.json(explainLines(lab, node, text, [u.searchParams.get('line') ?? ''])[0])
    }),
    http.post(`${A}/learn/explain`, async ({ request }) => {
      const b = (await request.json()) as { lab_id: string; node: string; lines?: (string | number)[]; all?: boolean }
      const lab = labOf(b.lab_id)
      if (!lab) return notFound('lab not found')
      const text = lab.files[`intended/configs/${b.node}.cfg`]
      if (!text) return notFound(`no intended config for '${b.node}' — build the lab first`)
      if (b.all || !b.lines?.length) return HttpResponse.json(explainFile(lab, b.node, text))
      const lines = explainLines(lab, b.node, text, b.lines)
      return HttpResponse.json({ host: b.node, lines, explained: lines.filter((l) => l.why).length, total: lines.length })
    }),

    // ------------------------------------------------ §4 test hosts
    http.get(`${A}/labs/:id/clients`, ({ params }) => {
      const l = labOf(params.id)
      if (!l) return notFound('lab not found')
      if (!l.built) return HttpResponse.json({ detail: 'lab not built' }, { status: 404 })
      return HttpResponse.json(clientsOf(l))
    }),
    http.post(`${A}/labs/:id/clients`, async ({ params, request }) => {
      const l = labOf(params.id)
      if (!l) return notFound('lab not found')
      const b = (await request.json()) as TestHostIn
      p6.clientRequests.push(b)
      if (!l.built) return conflict(`lab is ${l.state}: build it first`)
      if (b.deploy && !labDeployed(l)) return conflict(`lab is not deployed (state: ${l.state})`)
      const v: { path: string; message: string }[] = []
      if (!NAME_RE.test(b.name ?? '')) v.push({ path: 'name', message: "letters, digits, '.', '_' and '-' only (max 63)" })
      const topo = editableTopology(l)
      if (topo.nodes.some((n) => n.name === b.name)) v.push({ path: 'name', message: `${b.name} already exists` })
      if (!topo.nodes.some((n) => n.name === b.switch)) v.push({ path: 'switch', message: `unknown switch ${b.switch}` })
      else if (!portsOf(topo)[b.switch]?.free.includes(b.switch_port)) v.push({ path: 'switch_port', message: `${b.switch} ${b.switch_port} is not free` })
      if (b.mode === 'access' && /[-,]/.test(b.vlans)) v.push({ path: 'vlans', message: 'access mode takes a single VLAN' })
      if (v.length) return HttpResponse.json({ detail: { message: 'invalid test host', violations: v } }, { status: 422 })
      // add_endpoint op: AVD inputs and topology stay truthful
      l.knobs = { ...l.knobs, servers: [...serversOf(l), { name: b.name, adapters: [{ endpoint_ports: ['eth1'], switch_ports: [b.switch_port], switches: [b.switch], vlans: b.vlans, mode: b.mode }] }] }
      commit(l, renderFiles(l), `test host: add ${b.name} on ${b.switch} ${b.switch_port}`)
      hotOf(l.id).set(b.name, { deployed: false, ip: b.ip && b.ip !== 'auto' ? b.ip : null })
      const base = { client: b.name, switch: b.switch, switch_port: b.switch_port, rebuilt: [b.switch], push_hosts: [b.switch], commit: l.history[0]?.sha ?? null }
      if (!b.deploy) return HttpResponse.json({ job_id: null, ...base })
      const j = addJob({ type: 'lab.client_deploy', lab_id: l.id, host_id: l.host_id ?? undefined, params: b as unknown as Record<string, unknown> })
      const port = b.switch_port.replace('Ethernet', 'eth')
      runJob(j, [
        `partial build: ${b.switch} (structured config + config)`,
        `$ ansible-playbook push.yml --limit ${b.switch}  # eos_config replace`,
        `$ docker run -d --name ${b.name} --network none --label avdlab.lab=${l.slug} --label avdlab.role=client ${b.image ?? 'ghcr.io/srl-labs/network-multitool:latest'} sleep infinity`,
        `$ containerlab tools veth create -a ${b.switch}:${port} -b ${b.name}:eth1`,
        `$ docker exec ${b.name} sh /setup.sh`,
        '$ ping -c 3 <gateway> → 3/3 received',
      ], () => {
        const h = hotOf(l.id).get(b.name)
        if (h) h.deployed = true
        const c = clientsOf(l).find((x) => x.name === b.name)
        const vl = c?.vlans.find((x) => x.ip) ?? c?.vlans[0]
        return { result: { client: b.name, switch: b.switch, port: b.switch_port, ip: vl?.ip ?? null, gateway: vl?.gateway ?? null, reachable: true } }
      }, 60)
      return HttpResponse.json({ job_id: j.id, ...base }, { status: 202 })
    }),
    http.delete(`${A}/labs/:id/clients/:name`, ({ params }) => {
      const l = labOf(params.id)
      if (!l) return notFound('lab not found')
      const name = String(params.name)
      if (!serversOf(l).some((s) => s.name === name)) return notFound(`${name} is not a client of this lab`)
      l.knobs = { ...l.knobs, servers: serversOf(l).filter((s) => s.name !== name) }
      commit(l, renderFiles(l), `test host: remove ${name}`)
      hotOf(l.id).delete(name)
      if (!labDeployed(l)) return HttpResponse.json({ job_id: null, client: name, push_hosts: [], removed_from_inputs: true })
      const j = addJob({ type: 'lab.client_remove', lab_id: l.id, host_id: l.host_id ?? undefined, params: { name } })
      runJob(j, [`$ docker rm -f ${name}`, 'remove_endpoint op + partial build', '$ push switch config (--limit)'], () => ({ result: { client: name } }), 60)
      return HttpResponse.json({ job_id: j.id, client: name, push_hosts: [], removed_from_inputs: true }, { status: 202 })
    }),
    http.post(`${A}/labs/:id/clients/:name/exec`, async ({ params, request }) => {
      const l = labOf(params.id)
      if (!l) return notFound('lab not found')
      if (!labDeployed(l)) return conflict(`lab is not deployed (state: ${l.state})`)
      const b = (await request.json()) as { cmd: string }
      p6.execRequests.push({ client: String(params.name), kind: 'exec', body: b })
      const c = clientsOf(l).find((x) => x.name === params.name)
      if (!c) return notFound(`${String(params.name)} is not a client of this lab`)
      const ip = c.vlans.find((v) => v.ip)?.ip ?? '—'
      const stdout = /ip -br a/.test(b.cmd) ? `lo               UNKNOWN        127.0.0.1/8\neth1@if42        UP             ${ip}\n` : `ran: ${b.cmd}\n`
      return HttpResponse.json({ client: c.name, cmd: b.cmd, rc: 0, stdout, stderr: '' })
    }),
    http.post(`${A}/labs/:id/clients/:name/ping`, async ({ params, request }) => {
      const l = labOf(params.id)
      if (!l) return notFound('lab not found')
      if (!labDeployed(l)) return conflict(`lab is not deployed (state: ${l.state})`)
      const b = (await request.json()) as { dst: string; count?: number }
      p6.execRequests.push({ client: String(params.name), kind: 'ping', body: b })
      const count = b.count ?? 3
      const dstC = clientPlans(l).find((c) => c.name === b.dst)
      const target = dstC ? dstC.vlans.find((v) => v.ip)?.ip?.split('/')[0] ?? null : /^(\d{1,3}\.){3}\d{1,3}$/.test(b.dst) ? b.dst : null
      if (!target) return HttpResponse.json({ detail: `dst must be a client name or an IPv4 address: '${b.dst}'` }, { status: 422 })
      const ok = !target.startsWith('192.0.2.')
      return HttpResponse.json({ src: params.name, dst: b.dst, target_ip: target, sent: count, received: ok ? count : 0, loss_pct: ok ? 0 : 100, rtt_avg_ms: ok ? 1.37 : null, ok, output: '' })
    }),
    http.post(`${A}/labs/:id/clients/:name/iperf`, async ({ params, request }) => {
      const l = labOf(params.id)
      if (!l) return notFound('lab not found')
      if (!labDeployed(l)) return conflict(`lab is not deployed (state: ${l.state})`)
      const b = (await request.json()) as { dst: string; seconds?: number }
      p6.execRequests.push({ client: String(params.name), kind: 'iperf', body: b })
      const dstC = clientPlans(l).find((c) => c.name === b.dst)
      if (!dstC) return HttpResponse.json({ detail: `unknown client ${b.dst}` }, { status: 422 })
      return HttpResponse.json({ src: params.name, dst: b.dst, target_ip: dstC.vlans.find((v) => v.ip)?.ip?.split('/')[0] ?? null, proto: 'tcp', ok: true, error: null, seconds: b.seconds ?? 5, bits_per_second_sent: 9.41e9, bits_per_second_received: 9.38e9, retransmits: 12 })
    }),
  ]
}
