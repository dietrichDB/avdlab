import { http, HttpResponse, ws, delay } from 'msw'
import YAML from 'yaml'
import type { CVDeployParams, CVTarget, CVTargetIn, DeployParams, Job, ValidateParams } from '../api/types'
import {
  DEFAULT_VALIDATE, addJob, addValidation, antaCsv, cvDeployResultOf, cvOnboardingOf, antaMarkdown, antaResults, applyBuild, buildLog, buildResult, commit, createLab, db,
  devices, fileTree, hostOut, newId, renderFiles, runtimeNodes, stripJob, templates, topology, totalsOf, unifiedDiff, validateLabKnobs,
  type MockJob, type MockLab,
} from './data'
import { phase3Handlers } from './handlersPhase3'
import { MOCK_TOTP, SETTINGS_DEFAULTS, authDb, regDb, type MockUser } from './data'
import { metaOf } from './phase5'
import { CURRENT, p6 } from './phase6'
import type { AccountRequest, AppSettingsValues } from '../api/types'
import { phase4Handlers } from './handlersPhase4'
import { phase5Handlers } from './handlersPhase5'
import { phase6Handlers } from './handlersPhase6'
import { aiHandlers } from './handlersAi'
import { p4 } from './phase4'
import { normalizeProvisioning } from '../lib/provisioning'

const A = '/api'
const unauth = () => HttpResponse.json({ detail: 'not authenticated' }, { status: 401 })
const notFound = (what = 'not found') => HttpResponse.json({ detail: what }, { status: 404 })
const guard = () => (db.authed ? null : unauth())

try { db.authed = localStorage.getItem('avdlab-mock-auth') === '1' } catch { /* storage unavailable */ }
const persistAuth = () => { try { localStorage.setItem('avdlab-mock-auth', db.authed ? '1' : '0') } catch { /* ignore */ } }

// ---------------------------------------------------------------- job simulator
type Listener = (frame: { line: string } | { done: true }) => void
const listeners = new Map<string, Set<Listener>>()

function emit(j: MockJob, line: string) {
  j.log.push(line)
  listeners.get(j.id)?.forEach((fn) => fn({ line }))
}

/** Runs a fake job: queued → running → lines every ~150 ms → finish(). */
export function runJob(j: MockJob, lines: string[], finish?: (j: MockJob) => { error?: string; result?: Record<string, unknown> } | void, stepMs = 150) {
  setTimeout(() => {
    j.status = 'running'
    j.started_at = new Date().toISOString()
    let i = 0
    const tick = () => {
      if (j.status === 'cancelled') return end()
      if (i < lines.length) {
        emit(j, lines[i++])
        setTimeout(tick, stepMs)
      } else end()
    }
    const end = () => {
      if (j.status !== 'cancelled') {
        const r = finish?.(j) ?? {}
        j.result = r.result ?? j.result
        j.error = r.error ?? null
        j.status = r.error ? 'failed' : 'succeeded'
        if (r.error) emit(j, `[FAIL] ${r.error}`)
      }
      j.ended_at = new Date().toISOString()
      listeners.get(j.id)?.forEach((fn) => fn({ done: true }))
    }
    tick()
  }, 300)
  return HttpResponse.json({ job_id: j.id }, { status: 202 })
}

const probeLines = (h: { name: string; address: string }) => [
  `ssh ${h.address}: connected (host key known)`,
  '$ cat /etc/os-release', '$ uname -r', '$ nproc', '$ grep MemTotal /proc/meminfo', '$ df -Pk /',
  "$ docker version --format '{{.Server.Version}}'", '$ containerlab version',
  '$ sysctl -n fs.inotify.max_user_instances fs.inotify.max_user_watches net.ipv4.ip_forward vm.max_map_count fs.file-max',
  "$ docker images --format '{{.Repository}}:{{.Tag}}' | grep -iE 'ceos|arista'",
  '[ok] probe complete',
]

const bootstrapSteps = ['base packages', 'docker', 'containerlab', 'sysctls', '/opt/avdlab', 'agent image']

// ---------------------------------------------------------------- handlers
export const jobSocket = ws.link(/\/api\/jobs\/[^/]+\/stream$/)

export const handlers = [
  // v0.10.0: AI lab designer (bring your own model)
  ...aiHandlers(labOut),
  // CONTRACT-8 A (account requests, admin settings) — first: takes over GET /meta to add the public settings
  ...registrationHandlers(),
  // CONTRACT-6 (update, learning, hot-added test hosts) — first: takes over GET /meta and GET /labs/{id}/clients
  ...phase6Handlers(runJob),
  // CONTRACT-5 (editable topology, meta/changelog/notices, feedback)
  ...phase5Handlers(),
  // CONTRACT-4 (ZTP, NetBox, MSS, ACT) — first: some take over (ACT deploy) or fall through
  ...phase4Handlers(runJob),
  // CONTRACT-3 (clients/traffic, configs, git, import/export, monitoring, formats, terminals)
  ...phase3Handlers(runJob),

  // auth + users (v0.8.0, CONTRACT-7 stream A)
  ...authHandlers(),

  // secrets
  http.get(`${A}/secrets`, () => guard() ?? HttpResponse.json(db.secrets)),
  http.post(`${A}/secrets`, async ({ request }) => {
    const g = guard(); if (g) return g
    const b = (await request.json()) as { name: string; kind: string; meta?: Record<string, unknown> }
    if (db.secrets.some((s) => s.name === b.name)) return HttpResponse.json({ detail: 'secret name exists' }, { status: 409 })
    const s = { id: newId(), name: b.name, kind: b.kind as never, meta: b.meta ?? {}, created_at: new Date().toISOString() }
    db.secrets.push(s)
    return HttpResponse.json(s, { status: 201 })
  }),
  http.delete(`${A}/secrets/:id`, ({ params }) => { db.secrets = db.secrets.filter((s) => s.id !== params.id); return new HttpResponse(null, { status: 204 }) }),

  // jobs
  http.get(`${A}/jobs/types`, () => HttpResponse.json(['host.probe', 'host.bootstrap', 'host.verify', 'lab.build', 'lab.topology', 'lab.deploy', 'lab.destroy', 'lab.status', 'lab.validate', 'lab.cv_onboard', 'lab.cv_deploy', 'lab.netbox_sync', 'lab.mss_verify', 'lab.traffic_ping', 'lab.traffic_iperf', 'lab.snapshot', 'lab.git_push', 'lab.git_pull', 'lab.import_git', 'lab.sync', 'image.download', 'image.push'])),
  http.get(`${A}/jobs`, ({ request }) => {
    const g = guard(); if (g) return g
    const u = new URL(request.url)
    const lab = u.searchParams.get('lab_id'), host = u.searchParams.get('host_id'), limit = Number(u.searchParams.get('limit') ?? 100)
    return HttpResponse.json(db.jobs.filter((j) => (!lab || j.lab_id === lab) && (!host || j.host_id === host)).slice(0, limit).map(stripJob))
  }),
  http.get(`${A}/jobs/:id`, ({ params }) => {
    const g = guard(); if (g) return g
    const j = db.jobs.find((x) => x.id === params.id)
    return j ? HttpResponse.json(stripJob(j)) : notFound('job not found')
  }),
  http.get(`${A}/jobs/:id/log`, ({ params }) => HttpResponse.json({ log: db.jobs.find((x) => x.id === params.id)?.log.join('\n') ?? '' })),
  http.post(`${A}/jobs/:id/cancel`, ({ params }) => {
    const j = db.jobs.find((x) => x.id === params.id)
    if (!j || ['succeeded', 'failed', 'cancelled'].includes(j.status)) return HttpResponse.json({ cancelled: false })
    const wasRunning = j.status === 'running'
    j.status = 'cancelled'
    emit(j, 'job cancelled')
    return HttpResponse.json({ cancelled: true, was_running: wasRunning })
  }),

  // hosts — static routes first
  http.post(`${A}/hosts/keys/generate`, async ({ request }) => {
    const g = guard(); if (g) return g
    const b = ((await request.json().catch(() => ({}))) ?? {}) as { name?: string; comment?: string }
    const name = b.name || `ssh-key-${new Date().toISOString().replace(/\D/g, '').slice(0, 14)}`
    const rand = Array.from({ length: 43 }, () => 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'[Math.floor(Math.random() * 64)]).join('')
    const public_key = `ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI${rand} ${b.comment ?? 'avdlab'}`
    const s = { id: newId(), name, kind: 'ssh_key' as const, meta: { public_key, type: 'ed25519' }, created_at: new Date().toISOString() }
    db.secrets.push(s)
    return HttpResponse.json({ secret_id: s.id, name, public_key }, { status: 201 })
  }),
  http.post(`${A}/hosts/devcontainer`, async ({ request }) => {
    const g = guard(); if (g) return g
    const b = (await request.json()) as { mode: string }
    // "PK\x05\x06" + 18 zero bytes = a valid empty zip; the real backend fills it.
    const bytes = new Uint8Array(22); bytes.set([0x50, 0x4b, 0x05, 0x06])
    await delay(300)
    return new HttpResponse(bytes, { headers: { 'Content-Type': 'application/zip', 'Content-Disposition': `attachment; filename="avdlab-devcontainer-${b.mode}.zip"` } })
  }),
  http.get(`${A}/hosts/recipes`, () => HttpResponse.json([{ name: 'act-tools', options: {} }, { name: 'clab-host', options: {} }])),
  http.get(`${A}/hosts`, () => guard() ?? HttpResponse.json(db.hosts.map(hostOut))),
  http.post(`${A}/hosts`, async ({ request }) => {
    const g = guard(); if (g) return g
    const b = (await request.json()) as Record<string, unknown>
    if (db.hosts.some((h) => h.name === b.name)) return HttpResponse.json({ detail: 'host name exists' }, { status: 409 })
    const h = { id: newId(), kind: 'linux', ssh_port: 22, ssh_user: 'ubuntu', ssh_key_secret_id: null, password_secret_id: null, ...b, last_probe_at: null, created_at: new Date().toISOString(), summary: {}, capabilities: {} } as never
    db.hosts.push(h)
    return HttpResponse.json(hostOut(h), { status: 201 })
  }),
  http.get(`${A}/hosts/:id`, ({ params }) => {
    const h = db.hosts.find((x) => x.id === params.id)
    return h ? HttpResponse.json(hostOut(h)) : notFound('host not found')
  }),
  http.patch(`${A}/hosts/:id`, async ({ params, request }) => {
    const h = db.hosts.find((x) => x.id === params.id)
    if (!h) return notFound('host not found')
    Object.assign(h, await request.json())
    return HttpResponse.json(hostOut(h))
  }),
  http.delete(`${A}/hosts/:id`, ({ params }) => {
    if (db.labs.some((l) => l.host_id === params.id)) return HttpResponse.json({ detail: 'host has labs attached' }, { status: 409 })
    db.hosts = db.hosts.filter((h) => h.id !== params.id)
    return new HttpResponse(null, { status: 204 })
  }),
  http.get(`${A}/hosts/:id/capabilities`, ({ params }) => {
    const h = db.hosts.find((x) => x.id === params.id)
    return h ? HttpResponse.json({ host_id: h.id, last_probe_at: h.last_probe_at, capabilities: h.capabilities }) : notFound('host not found')
  }),
  http.post(`${A}/hosts/:id/probe`, ({ params }) => {
    const h = db.hosts.find((x) => x.id === params.id)
    if (!h) return notFound('host not found')
    const j = addJob({ type: 'host.probe', host_id: h.id })
    return runJob(j, probeLines(h), () => {
      h.last_probe_at = new Date().toISOString()
      return { result: { recommendations: h.capabilities.recommendations ?? [] } }
    })
  }),
  http.post(`${A}/hosts/:id/bootstrap`, async ({ params, request }) => {
    const h = db.hosts.find((x) => x.id === params.id)
    if (!h) return notFound('host not found')
    const body = (await request.json().catch(() => ({}))) as { recipe?: string; options?: { native_toolchain?: boolean; clab_mode?: string } }
    const j = addJob({ type: 'host.bootstrap', host_id: h.id, params: body as Record<string, unknown> })
    const actTools = body.recipe === 'act-tools'
    const steps = actTools ? ['base packages', 'docker', '/opt/avdlab', 'agent image'] : [...bootstrapSteps, ...(body.options?.native_toolchain ? ['native toolchain'] : [])]
    const lines = steps.flatMap((s) => {
      const ok = h.capabilities.recommendations?.length === 0
      return ok ? [`[skip] ${s}`] : [`[apply] ${s}${s === 'containerlab' ? ` (${body.options?.clab_mode ?? 'native'})` : ''}`, `  … running ${s}`, `[ok] ${s}`]
    })
    return runJob(j, lines, () => {
      h.capabilities = actTools
        ? { ...h.capabilities, docker: { ...h.capabilities.docker, installed: true, user_can_run: true, user_in_group: true }, recommendations: [] }
        : { ...h.capabilities, docker: { ...h.capabilities.docker, user_can_run: true, user_in_group: true }, containerlab: { installed: true, version: '0.59.0' }, recommendations: [] }
      return { result: { recipe: actTools ? 'act-tools' : 'clab-host', steps: steps.length } }
    }, 200)
  }),
  http.post(`${A}/hosts/:id/verify`, ({ params }) => {
    const h = db.hosts.find((x) => x.id === params.id)
    if (!h) return notFound('host not found')
    const j = addJob({ type: 'host.verify', host_id: h.id })
    const ok = !!h.capabilities.containerlab?.installed
    return runJob(j, ['$ containerlab version', ok ? '  version: 0.59.0' : '  containerlab: command not found', '$ docker run --rm avdlab-agent python3 -c "import pyavd, anta"', ...probeLines(h)],
      () => (ok ? { result: { ok: true } } : { error: 'containerlab missing' }))
  }),

  // images
  http.get(`${A}/images/token`, () => {
    const g = guard(); if (g) return g
    const t = db.secrets.find((s) => s.name === 'arista_token')
    return HttpResponse.json(t ? { configured: true, secret_id: t.id, name: t.name, created_at: t.created_at, hint: null }
      : { configured: false, secret_id: null, name: null, created_at: null, hint: 'Create a secret named arista_token holding your arista.com API token.' })
  }),
  http.put(`${A}/images/token`, async ({ request }) => {
    const g = guard(); if (g) return g
    const b = (await request.json()) as { value: string }
    let t = db.secrets.find((s) => s.name === 'arista_token')
    if (!t) { t = { id: newId(), name: 'arista_token', kind: 'arista_token', meta: {}, created_at: new Date().toISOString() }; db.secrets.push(t) }
    db.badToken = b.value.startsWith('bad')  // mock: tokens starting with "bad" make the catalogue 502
    return HttpResponse.json({ configured: true, secret_id: t.id, name: t.name, created_at: t.created_at, hint: null })
  }),
  http.delete(`${A}/images/token`, () => { db.secrets = db.secrets.filter((s) => s.name !== 'arista_token'); db.badToken = false; return new HttpResponse(null, { status: 204 }) }),
  http.get(`${A}/images/catalog`, ({ request }) => {
    const g = guard(); if (g) return g
    if (!db.secrets.some((s) => s.name === 'arista_token')) return HttpResponse.json({ detail: 'no arista.com token: create a secret named "arista_token"' }, { status: 409 })
    if (db.badToken) return HttpResponse.json({ detail: { message: 'arista.com catalogue unavailable: 401 Unauthorized (invalid token)', hint: 'check the token and outbound HTTPS to arista.com' } }, { status: 502 })
    const fmt = new URL(request.url).searchParams.get('fmt') ?? 'cEOS'
    const versions = ['4.34.2F', '4.34.1F', '4.34.0F', '4.33.3F', '4.33.2F', '4.33.1F', '4.32.4.1M', '4.32.3M', '4.31.6M', '4.30.9M']
    return HttpResponse.json({ fmt, versions })
  }),
  http.get(`${A}/images`, () => guard() ?? HttpResponse.json(db.images)),
  http.post(`${A}/images/download`, async ({ request }) => {
    const b = (await request.json()) as { fmt: string; version: string }
    const j = addJob({ type: 'image.download', params: b })
    return runJob(j, [`ardl get eos --format ${b.fmt} --version ${b.version} --import-docker`, 'downloading… 25%', 'downloading… 50%', 'downloading… 75%', 'downloading… 100%', 'docker import …', `[ok] arista/ceos:${b.version}`], () => {
      const arm = /arm/i.test(b.fmt)
      db.images.push({ id: newId(), fmt: b.fmt, version: b.version, arch: arm ? 'arm64' : 'x86_64', docker_tag: `arista/ceos:${b.version}${arm ? '-arm64' : ''}`, local_path: `/data/images/cEOS-lab-${b.version}.tar.xz`, sha256: null, size_bytes: 600_000_000, present_on_hosts: [], created_at: new Date().toISOString() })
      return { result: { docker_tag: `arista/ceos:${b.version}` } }
    })
  }),
  http.post(`${A}/images/upload`, async ({ request }) => {
    const fd = await request.formData()
    const file = fd.get('file') as File | null
    const version = String(fd.get('version') ?? 'unknown')
    const fmt = String(fd.get('fmt') ?? 'cEOS'), arm = /arm/i.test(fmt)
    const img = { id: newId(), fmt, version, arch: (arm ? 'arm64' : 'x86_64') as 'arm64' | 'x86_64', docker_tag: `arista/ceos:${version}${arm ? '-arm64' : ''}`, local_path: `/data/images/${file?.name ?? 'upload.tar.xz'}`, sha256: null, size_bytes: file?.size ?? 0, present_on_hosts: [], created_at: new Date().toISOString() }
    db.images.push(img)
    return HttpResponse.json(img, { status: 201 })
  }),
  http.post(`${A}/images/:id/push/:hostId`, ({ params }) => {
    const img = db.images.find((i) => i.id === params.id)
    const h = db.hosts.find((x) => x.id === params.hostId)
    if (!img || !h) return notFound()
    const j = addJob({ type: 'image.push', host_id: h.id, params: { image_id: img.id } })
    return runJob(j, [`docker save ${img.docker_tag} | ssh ${h.address} docker load`, 'Loaded image: ' + img.docker_tag], () => {
      img.present_on_hosts = [...new Set([...img.present_on_hosts, h.id])]
      h.capabilities.images = [...new Set([...(h.capabilities.images ?? []), img.docker_tag])]
    })
  }),

  // labs
  http.get(`${A}/labs/templates`, () => guard() ?? HttpResponse.json(templates)),
  http.get(`${A}/labs`, () => guard() ?? HttpResponse.json(db.labs.map(labOut))),
  http.post(`${A}/labs`, async ({ request }) => {
    const g = guard(); if (g) return g
    const b = (await request.json()) as { name: string; template: string; host_id?: string | null; ceos_version?: string | null; knobs: Record<string, unknown>; target?: 'clab' | 'act' }
    const t = templates.find((x) => x.name === b.template)
    if (!t) return HttpResponse.json({ detail: `unknown template ${b.template}` }, { status: 422 })
    if (db.labs.some((l) => l.name === b.name)) return HttpResponse.json({ detail: 'lab name exists' }, { status: 409 })
    const violations = validateLabKnobs(t, b.knobs)
    await delay(400)
    if (violations.length) return HttpResponse.json({ detail: { message: 'validation failed', violations } }, { status: 422 })
    return HttpResponse.json(labOut(createLab(b)), { status: 201 })
  }),
  http.get(`${A}/labs/:id`, ({ params }) => {
    const l = db.labs.find((x) => x.id === params.id)
    return l ? HttpResponse.json(labOut(l)) : notFound('lab not found')
  }),
  http.patch(`${A}/labs/:id`, async ({ params, request }) => {
    const l = db.labs.find((x) => x.id === params.id)
    if (!l) return notFound('lab not found')
    const b = (await request.json()) as { knobs?: Record<string, unknown>; host_id?: string | null; ceos_version?: string | null; cv_target_id?: string | null; netbox_target_id?: string | null; target?: 'clab' | 'act' }
    const t = templates.find((x) => x.name === l.template)!
    const next = { ...l.knobs, ...(b.knobs ?? {}) }
    const violations = validateLabKnobs(t, next)
    if (violations.length) return HttpResponse.json({ detail: { message: 'validation failed', violations } }, { status: 422 })
    if (b.cv_target_id) {
      const mode = (next.cloudvision as { mode?: string } | undefined)?.mode ?? 'none'
      if (mode === 'none') return HttpResponse.json({ detail: 'cloudvision.mode is none: set the cloudvision knob before binding a target' }, { status: 409 })
      if (!db.cvTargets.some((x) => x.id === b.cv_target_id)) return notFound('cv target not found')
    }
    if (b.netbox_target_id && !p4.netboxTargets.some((x) => x.id === b.netbox_target_id)) return HttpResponse.json({ detail: { message: 'NetBox target not found', violations: [{ path: 'netbox_target_id', message: 'unknown target' }] } }, { status: 422 })
    if (b.target && b.target !== l.target) {
      if (l.state === 'deployed' || l.state === 'validated') return HttpResponse.json({ detail: `lab is ${l.state}: destroy it before changing the target` }, { status: 409 })
      if (!['clab', 'act'].includes(b.target)) return HttpResponse.json({ detail: [{ loc: ['body', 'target'], msg: "string should match pattern '^(clab|act)$'" }] }, { status: 422 })
      l.target = b.target
      if (l.state === 'built') l.state = 'draft'
    }
    if ('netbox_target_id' in b) l.netbox_target_id = b.netbox_target_id || null
    if ('cv_target_id' in b) l.cv_target_id = b.cv_target_id ?? null
    if ('host_id' in b) l.host_id = b.host_id ?? null
    if ('ceos_version' in b) l.ceos_version = b.ceos_version ?? null
    if (b.knobs) {
      l.knobs = next
      l.custom_knobs = (l.custom_knobs ?? []).filter((k) => !(k in b.knobs!))
      commit(l, renderFiles(l), `knobs: ${Object.keys(b.knobs).join(', ')}`)
      if (l.state === 'built') l.state = 'draft'
    }
    return HttpResponse.json(labOut(l))
  }),
  http.delete(`${A}/labs/:id`, ({ params }) => {
    const l = db.labs.find((x) => x.id === params.id)
    if (!l) return notFound('lab not found')
    if (l.state === 'deployed') return HttpResponse.json({ detail: 'lab is deployed' }, { status: 409 })
    db.labs = db.labs.filter((x) => x !== l)
    return new HttpResponse(null, { status: 204 })
  }),
  http.get(`${A}/labs/:id/files`, ({ params }) => {
    const l = db.labs.find((x) => x.id === params.id)
    return l ? HttpResponse.json(fileTree(l)) : notFound('lab not found')
  }),
  http.get(`${A}/labs/:id/files/*`, ({ params }) => {
    const l = db.labs.find((x) => x.id === params.id)
    const path = decodeURIComponent((params[0] as string) ?? '')
    if (!l || l.files[path] === undefined) return notFound('file not found')
    return HttpResponse.json({ path, content: l.files[path] })
  }),
  http.put(`${A}/labs/:id/files/*`, async ({ params, request }) => {
    const l = db.labs.find((x) => x.id === params.id)
    const path = decodeURIComponent((params[0] as string) ?? '')
    if (!l) return notFound('lab not found')
    const { content } = (await request.json()) as { content: string }
    const doc = YAML.parseDocument(content)
    if (doc.errors.length) return HttpResponse.json({ detail: { violations: doc.errors.map((e) => ({ path, message: e.message.split('\n')[0] })) } }, { status: 422 })
    const data = doc.toJS() as Record<string, unknown> | null
    const v: { path: string; message: string }[] = []
    if (path === 'group_vars/FABRIC.yml' && !data?.fabric_name) v.push({ path: 'fabric_name', message: "fabric_name: Required key 'fabric_name' is missing" })
    if (data && 'bgp_as' in data && !/^\d+(\.\d+)?$/.test(String(data.bgp_as))) v.push({ path: 'bgp_as', message: `bgp_as: '${data.bgp_as}' does not match '^\\d+(\\.\\d+)?$'` })
    if (v.length) return HttpResponse.json({ detail: { violations: v } }, { status: 422 })
    commit(l, { [path]: content }, `edit ${path}`)
    if (path.startsWith('group_vars/') && path.includes('NETWORK_SERVICES')) l.custom_knobs = [...new Set([...(l.custom_knobs ?? []), 'tenants'])]
    if (path.startsWith('group_vars/') && path.includes('CONNECTED_ENDPOINTS')) l.custom_knobs = [...new Set([...(l.custom_knobs ?? []), 'servers'])]
    return HttpResponse.json({ path, committed: l.history[0].sha })
  }),
  http.post(`${A}/labs/:id/build`, ({ params }) => {
    const l = db.labs.find((x) => x.id === params.id)
    if (!l) return notFound('lab not found')
    const j = addJob({ type: 'lab.build', lab_id: l.id })
    l.last_build_job = stripJob(j)
    return runJob(j, buildLog(l), () => {
      const result = buildResult(l)
      applyBuild(l)
      l.last_build_job = { ...stripJob(j), result, status: 'succeeded' }
      return { result }
    }, 60)
  }),
  http.get(`${A}/labs/:id/history`, ({ params }) => {
    const l = db.labs.find((x) => x.id === params.id)
    return l ? HttpResponse.json(l.history.map(({ snapshot, ...r }) => { void snapshot; return r })) : notFound('lab not found')
  }),
  http.get(`${A}/labs/:id/diff`, ({ params, request }) => {
    const l = db.labs.find((x) => x.id === params.id)
    if (!l) return notFound('lab not found')
    const u = new URL(request.url)
    const a = l.history.find((r) => r.sha === u.searchParams.get('a'))?.snapshot ?? {}
    const b = l.history.find((r) => r.sha === u.searchParams.get('b'))?.snapshot ?? l.files
    return HttpResponse.text(unifiedDiff(a, b))
  }),
  http.get(`${A}/labs/:id/devices`, ({ params }) => {
    const l = db.labs.find((x) => x.id === params.id)
    if (!l) return notFound('lab not found')
    return HttpResponse.json(l.built ? devices(l) : [])
  }),

  // runtime (CONTRACT-2b)
  // 0.10.0 ANTA security advisories demo
  http.get(`${A}/labs/:id/psirt`, ({ params }) => {
    const l = db.labs.find((x) => x.id === params.id)
    if (!l) return notFound('lab not found')
    return HttpResponse.json({ available: ['deployed', 'validated'].includes(l.state) && !!l.host_id, last: db.psirt[l.id] ?? null })
  }),
  http.get(`${A}/labs/:id/psirt/report.md`, () => new HttpResponse('# Advisory Assessment Summary\n\n(mock)\n', { headers: { 'content-type': 'text/plain' } })),
  http.post(`${A}/labs/:id/psirt`, async ({ params, request }) => {
    const l = db.labs.find((x) => x.id === params.id)
    if (!l) return notFound('lab not found')
    if (!['deployed', 'validated'].includes(l.state)) return HttpResponse.json({ detail: `lab is ${l.state}: deploy it first` }, { status: 409 })
    const body = ((await request.json().catch(() => ({}))) as { tests?: string[] })
    const j = addJob({ type: 'lab.psirt', lab_id: l.id, host_id: l.host_id ?? undefined, params: body })
    const row = (device: string, id: string, test: string, title: string, sev: string, result: string, vuln: string, msg: string, rem: string) => ({
      device, test, advisory_id: id, advisory_title: title, advisory_severity: sev, advisory_url: `https://www.arista.com/en/support/advisories-notices/security-advisory/${id.replace('SA0', '')}`,
      advisory_result: result, advisory_messages: '', vulnerability_id: vuln, vulnerability_severity: sev, vulnerability_result: result, vulnerability_messages: msg, remediation: rem })
    const rows = [
      row('dc1-spine1', 'SA0117', 'SA117', 'EOS BGP: crafted UPDATE causes restart', 'high', 'affected', 'CVE-2025-0001', 'EOS 4.34.2F is affected', 'Upgrade to 4.34.3F or later'),
      row('dc1-leaf1a', 'SA0117', 'SA117', 'EOS BGP: crafted UPDATE causes restart', 'high', 'mitigated', 'CVE-2025-0001', 'BGP peers protected by an ACL', 'Keep the ACL until upgraded'),
      row('dc1-spine1', 'SA0140', 'SA140', 'EOS gNMI: unauthenticated subscribe', 'medium', 'not affected', 'CVE-2025-0002', '', ''),
      row('dc1-leaf1a', 'SA0140', 'SA140', 'EOS gNMI: unauthenticated subscribe', 'medium', 'not affected', 'CVE-2025-0002', '', ''),
    ]
    const assessments = rows.map((r) => ({ device: r.device, advisory_id: r.advisory_id, test: r.test, title: r.advisory_title, severity: r.advisory_severity, url: r.advisory_url, result: r.advisory_result }))
    return runJob(j, ['[psirt] 2 device(s): dc1-leaf1a, dc1-spine1; all built-in advisories', '[psirt] anta psirt is a preview feature of ANTA 1.10', '[psirt] done in 12.3s: affected 1, mitigated 1, not affected 2'], () => {
      db.psirt[l.id] = { at: Date.now() / 1000, job_id: j.id, tests: body.tests ?? [], devices: ['dc1-leaf1a', 'dc1-spine1'], duration: 12.3,
        summary: { affected: 1, inconclusive: 0, mitigated: 1, 'not affected': 2, error: 0, skipped: 0 },
        by_device: { 'dc1-spine1': { affected: 1, inconclusive: 0, mitigated: 0, 'not affected': 1, error: 0, skipped: 0 }, 'dc1-leaf1a': { affected: 0, inconclusive: 0, mitigated: 1, 'not affected': 1, error: 0, skipped: 0 } },
        advisories: [{ id: 'SA0117', title: 'EOS BGP: crafted UPDATE causes restart', severity: 'high', url: rows[0].advisory_url }, { id: 'SA0140', title: 'EOS gNMI: unauthenticated subscribe', severity: 'medium', url: rows[2].advisory_url }],
        assessments, rows, has_md: true }
      return { result: { summary: db.psirt[l.id]!.summary } }
    })
  }),
  // 0.10.0 incremental push (plan: deployed evpn-lab has 2 changed leaves)
  http.get(`${A}/labs/:id/push/plan`, ({ params }) => {
    const l = db.labs.find((x) => x.id === params.id)
    if (!l) return notFound('lab not found')
    const available = ['deployed', 'validated'].includes(l.state) && l.target !== 'act' && !!l.host_id
    const changed = db.pushChanged[l.id] ?? (available ? ['dc1-leaf1a', 'dc1-leaf1b'] : [])
    const all = ['dc1-spine1', 'dc1-spine2', 'dc1-leaf1a', 'dc1-leaf1b', 'dc1-leaf2a', 'dc1-leaf2b']
    return HttpResponse.json({ available, changed, unchanged: all.filter((h) => !changed.includes(h)), unknown: [], total: all.length, since: Date.now() / 1000 - 3600, last_job_id: null })
  }),
  http.post(`${A}/labs/:id/push`, async ({ params, request }) => {
    const l = db.labs.find((x) => x.id === params.id)
    if (!l) return notFound('lab not found')
    if (!['deployed', 'validated'].includes(l.state)) return HttpResponse.json({ detail: `lab is ${l.state}: deploy it first` }, { status: 409 })
    const body = ((await request.json().catch(() => ({}))) as { hosts?: string[] })
    const hosts = body.hosts ?? db.pushChanged[l.id] ?? ['dc1-leaf1a', 'dc1-leaf1b']
    const j = addJob({ type: 'lab.push_configs', lab_id: l.id, host_id: l.host_id ?? undefined, params: body })
    db.pushChanged[l.id] = []
    return runJob(j, [`[push] plan: ${hosts.length} changed`, `[push] pushing to ${hosts.length} device(s): ${hosts.join(', ')} — config replace over eAPI, no reboot`, '[push] done'],
      () => ({ result: { pushed: hosts, skipped: [] } }))
  }),
  http.post(`${A}/labs/:id/deploy`, async ({ params, request }) => {
    const l = db.labs.find((x) => x.id === params.id)
    if (!l) return notFound('lab not found')
    if (!['built', 'deployed', 'destroyed'].includes(l.state) || !l.built) return HttpResponse.json({ detail: `lab must be built before deploy (state: ${l.state})` }, { status: 409 })
    if (!l.host_id) return HttpResponse.json({ detail: 'lab has no host' }, { status: 409 })
    const body = { reconfigure: true, wait_eapi: true, wait_timeout: 600, ...((await request.json().catch(() => ({}))) as Partial<DeployParams>) }
    const j = addJob({ type: 'lab.deploy', lab_id: l.id, host_id: l.host_id, params: body })
    return runJob(j, deployLog(l, body), () => {
      const nodes = runtimeNodes(l)
      l.runtime = { deployed: true, nodes }
      l.state = 'deployed'
      l.settings = { ...(l.settings ?? {}), deployed_at: new Date().toISOString(), last_inspect: nodes }
      const ceos = nodes.filter((n) => n.kind === 'arista_ceos')
      return { result: { nodes, ready: ceos.map((n) => n.name), pending: [], duration: 94.2 } }
    }, 120)
  }),
  http.post(`${A}/labs/:id/destroy`, ({ params }) => {
    const l = db.labs.find((x) => x.id === params.id)
    if (!l) return notFound('lab not found')
    const j = addJob({ type: 'lab.destroy', lab_id: l.id, host_id: l.host_id ?? undefined })
    const nodes = l.runtime?.nodes ?? []
    return runJob(j, [
      `$ cd /opt/avdlab/labs/${l.slug} && sudo containerlab destroy -t clab/topology.clab.yml --cleanup`,
      `INFO Parsing & checking topology file=topology.clab.yml`,
      `INFO Destroying lab name=${l.slug}`,
      ...nodes.map((n) => `INFO Removed container name=${n.name}`),
      `INFO Removing host entries path=/etc/hosts`,
      '[ok] destroyed',
    ], () => {
      l.runtime = null
      l.state = 'destroyed'
      return { result: { destroyed: nodes.length } }
    })
  }),
  http.get(`${A}/labs/:id/status`, async ({ params }) => {
    const l = db.labs.find((x) => x.id === params.id)
    if (!l) return notFound('lab not found')
    await delay(250)
    return HttpResponse.json(l.runtime ?? { deployed: false, nodes: [] })
  }),
  http.post(`${A}/labs/:id/status/refresh`, ({ params }) => {
    const l = db.labs.find((x) => x.id === params.id)
    if (!l) return notFound('lab not found')
    const j = addJob({ type: 'lab.status', lab_id: l.id, host_id: l.host_id ?? undefined })
    const nodes = l.runtime?.nodes ?? []
    return runJob(j, [
      `$ sudo containerlab inspect -t clab/topology.clab.yml --format json`,
      `  ${nodes.length} containers`,
      ...nodes.filter((n) => n.kind === 'arista_ceos').map((n) => `  eAPI ${n.mgmt_ip?.split('/')[0]}: [ok] ready`),
    ], () => {
      if (l.runtime) l.runtime.nodes = l.runtime.nodes.map((n) => ({ ...n, eapi: n.kind === 'arista_ceos' }))
      return { result: { ...(l.runtime ?? { deployed: false, nodes: [] }) } }
    }, 100)
  }),
  http.post(`${A}/labs/:id/validate`, async ({ params, request }) => {
    const l = db.labs.find((x) => x.id === params.id)
    if (!l) return notFound('lab not found')
    if (l.state !== 'deployed' && l.state !== 'validated') return HttpResponse.json({ detail: `lab is not deployed (state: ${l.state})` }, { status: 409 })
    const p: ValidateParams = { ...DEFAULT_VALIDATE, ...((await request.json().catch(() => ({}))) as Partial<ValidateParams>) }
    const j = addJob({ type: 'lab.validate', lab_id: l.id, host_id: l.host_id ?? undefined, params: p as unknown as Record<string, unknown> })
    const results = antaResults(l, p, false)
    const devs = [...new Set(results.map((r) => r.name))]
    return runJob(j, [
      `rsync → lab1:/opt/avdlab/labs/${l.slug}`,
      `wrote playbooks/validate.yml (skip_tests: ${p.skip_tests.join(', ') || '—'}; extra fabric: ${p.extra_fabric_validation})`,
      `$ docker run --rm --network ${l.slug}-mgmt -v /opt/avdlab/labs/${l.slug}:/lab -w /lab avdlab-agent ansible-playbook playbooks/validate.yml`,
      'PLAY [Run ANTA] ****************************************************************',
      'TASK [arista.avd.anta_runner : Build ANTA catalogs] ******************************',
      `TASK [arista.avd.anta_runner : Run ANTA] ****************************************`,
      ...devs.map((d) => `  ${d}: ${results.filter((r) => r.name === d).length} tests`),
      'PLAY RECAP *********************************************************************',
      'ssh.get anta/reports/anta_report.{json,md,csv}',
      `[ok] ${results.length} results`,
    ], () => {
      const v = addValidation(l, results, j.id)
      const bad = v.totals.failure + v.totals.error
      l.state = bad ? 'deployed' : 'validated'
      const by_device = Object.fromEntries(devs.map((d) => [d, totalsOf(results.filter((r) => r.name === d))]))
      return { result: { totals: v.totals, by_device, report_paths: ['anta/reports/anta_report.json', 'anta/reports/anta_report.md', 'anta/reports/anta_report.csv'], validation_run_id: v.id } }
    }, 150)
  }),
  http.get(`${A}/labs/:id/validations`, ({ params }) => {
    const l = db.labs.find((x) => x.id === params.id)
    if (!l) return notFound('lab not found')
    return HttpResponse.json(db.validations.filter((v) => v.lab_id === l.id).map(({ results, lab_id, ...r }) => { void results; void lab_id; return r }))
  }),
  http.get(`${A}/labs/:id/validations/:runId/report`, ({ params, request }) => {
    const v = db.validations.find((x) => x.id === params.runId && x.lab_id === params.id)
    if (!v) return notFound('validation run not found')
    const fmt = new URL(request.url).searchParams.get('format') ?? 'json'
    if (fmt === 'md') return HttpResponse.text(antaMarkdown(v.results), { headers: { 'Content-Type': 'text/markdown; charset=utf-8' } })
    if (fmt === 'csv') return HttpResponse.text(antaCsv(v.results), { headers: { 'Content-Type': 'text/csv; charset=utf-8' } })
    return HttpResponse.json(v.results)
  }),

  // CloudVision (CONTRACT-2c)
  http.get(`${A}/cv/targets`, () => guard() ?? HttpResponse.json(db.cvTargets)),
  http.post(`${A}/cv/targets`, async ({ request }) => {
    const g = guard(); if (g) return g
    const b = (await request.json()) as CVTargetIn
    if (db.cvTargets.some((t) => t.name === b.name)) return HttpResponse.json({ detail: 'target name exists' }, { status: 409 })
    if (!b.name || !b.server) return HttpResponse.json({ detail: [{ loc: ['body', b.name ? 'server' : 'name'], msg: 'field required' }] }, { status: 422 })
    const t: CVTarget = { id: newId(), name: b.name, kind: b.kind, server: b.server, verify_certs: b.verify_certs ?? true, has_token: false, has_onboarding_token: false, onboarding_token_meta: null, created_at: new Date().toISOString() }
    applyTargetSecrets(t, b)
    db.cvTargets.push(t)
    return HttpResponse.json(t, { status: 201 })
  }),
  http.get(`${A}/cv/targets/:id`, ({ params }) => {
    const t = db.cvTargets.find((x) => x.id === params.id)
    return t ? HttpResponse.json(t) : notFound('cv target not found')
  }),
  http.patch(`${A}/cv/targets/:id`, async ({ params, request }) => {
    const t = db.cvTargets.find((x) => x.id === params.id)
    if (!t) return notFound('cv target not found')
    const b = (await request.json()) as Partial<CVTargetIn>
    if (b.name && b.name !== t.name && db.cvTargets.some((x) => x.name === b.name)) return HttpResponse.json({ detail: 'target name exists' }, { status: 409 })
    for (const k of ['name', 'kind', 'server', 'verify_certs'] as const) if (b[k] !== undefined) (t as unknown as Record<string, unknown>)[k] = b[k]
    applyTargetSecrets(t, b)
    return HttpResponse.json(t)
  }),
  http.delete(`${A}/cv/targets/:id`, ({ params }) => {
    if (db.labs.some((l) => l.cv_target_id === params.id)) return HttpResponse.json({ detail: 'target is bound to labs' }, { status: 409 })
    db.cvTargets = db.cvTargets.filter((t) => t.id !== params.id)
    return new HttpResponse(null, { status: 204 })
  }),
  http.post(`${A}/cv/targets/:id/test`, async ({ params }) => {
    const t = db.cvTargets.find((x) => x.id === params.id)
    if (!t) return notFound('cv target not found')
    await delay(400)
    if (!t.has_token) return HttpResponse.json({ ok: false, devices_seen: null, error: 'no service-account token stored' })
    if (t.kind === 'onprem' && t.verify_certs) return HttpResponse.json({ ok: false, devices_seen: null, error: `[SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate (${t.server})` })
    const seen = db.labs.filter((l) => l.cv_target_id === t.id && l.settings?.cv_onboarding).reduce((n, l) => n + ((l.settings!.cv_onboarding as { nodes: { streaming: boolean }[] }).nodes.filter((x) => x.streaming).length), 0)
    return HttpResponse.json({ ok: true, devices_seen: seen + 3, error: null })
  }),
  http.get(`${A}/labs/:id/cv/status`, ({ params }) => {
    const l = db.labs.find((x) => x.id === params.id)
    if (!l) return notFound('lab not found')
    const s = (l.settings ?? {}) as Record<string, unknown>
    return HttpResponse.json({ target: db.cvTargets.find((t) => t.id === l.cv_target_id) ?? null, onboarding: s.cv_onboarding ?? null, last_deploy: s.cv_last_deploy ?? null })
  }),
  http.post(`${A}/labs/:id/cv/onboard`, ({ params }) => {
    const l = db.labs.find((x) => x.id === params.id)
    if (!l) return notFound('lab not found')
    const t = db.cvTargets.find((x) => x.id === l.cv_target_id)
    if (l.state !== 'deployed' && l.state !== 'validated') return HttpResponse.json({ detail: `lab is not deployed (state: ${l.state})` }, { status: 409 })
    if (!t?.has_onboarding_token) return HttpResponse.json({ detail: 'target has no onboarding token' }, { status: 409 })
    const cv = (l.knobs?.cloudvision ?? {}) as { mode?: string }
    const tokenFile = cv.mode === 'onprem' ? '/tmp/token' : '/tmp/cv-onboarding-token'
    const j = addJob({ type: 'lab.cv_onboard', lab_id: l.id, host_id: l.host_id ?? undefined })
    const ceos = (l.runtime?.nodes ?? []).filter((n) => n.kind === 'arista_ceos')
    return runJob(j, [
      `target ${t.name} (${t.kind} ${t.server})`,
      ...ceos.flatMap((n) => [
        `$ docker cp /tmp/avdlab-cv-XXXX ${n.name}:${tokenFile}`,
        `$ docker exec ${n.name} Cli -p 15 -c $'configure\\ndaemon TerminAttr\\nshutdown\\nno shutdown'`,
      ]),
      'waiting for TerminAttr (≤120 s)…',
      ...ceos.map((n) => `  ${n.name}: TerminAttr running`),
      `GET https://${t.server}/api/resources/inventory/v1/Device/all`,
      `[ok] ${ceos.length} nodes streaming`,
    ], () => {
      const onboarding = cvOnboardingOf(l)
      l.settings = { ...(l.settings ?? {}), cv_onboarding: onboarding }
      return { result: { nodes: onboarding.nodes } }
    }, 80)
  }),
  http.post(`${A}/labs/:id/cv/deploy`, async ({ params, request }) => {
    const l = db.labs.find((x) => x.id === params.id)
    if (!l) return notFound('lab not found')
    const t = db.cvTargets.find((x) => x.id === l.cv_target_id)
    if (!l.built || l.state === 'draft') return HttpResponse.json({ detail: `lab must be built (state: ${l.state})` }, { status: 409 })
    if (!t?.has_token) return HttpResponse.json({ detail: 'lab has no CloudVision target with a token' }, { status: 409 })
    const p: CVDeployParams = { submit_workspace: true, run_change_control: false, skip_missing_devices: true, strict_tags: false, ...((await request.json().catch(() => ({}))) as Partial<CVDeployParams>) }
    const j = addJob({ type: 'lab.cv_deploy', lab_id: l.id, host_id: l.host_id ?? undefined, params: p as unknown as Record<string, unknown> })
    const onboarded = ((l.settings?.cv_onboarding as { nodes?: { name: string; streaming: boolean }[] } | undefined)?.nodes ?? [])
    const missing = devices(l).map((d) => d.hostname).filter((h) => !onboarded.some((n) => n.name.endsWith(h) && n.streaming))
    return runJob(j, [
      'wrote playbooks/avdlab-cv-deploy.yml',
      `rsync → /opt/avdlab/labs/${l.slug}`,
      `$ docker run --rm --env-file /opt/avdlab/tmp/cv-XXXX.env -v /opt/avdlab/labs/${l.slug}:/lab -w /lab avdlab-agent ansible-playbook playbooks/avdlab-cv-deploy.yml -v`,
      'PLAY [Deploy to CloudVision] ***************************************************',
      'TASK [arista.avd.cv_deploy : Deploy to CloudVision] ****************************',
      `  workspace ${p.workspace_name || `AVD ${l.slug}`} created`,
      ...missing.map((h) => `  [WARNING] ${h} not found on CloudVision`),
      p.submit_workspace ? '  workspace submitted' : '  workspace left pending',
      'AVDLAB_CV_RESULT_BEGIN', '{…}', 'AVDLAB_CV_RESULT_END',
      'removed env-file',
      '[ok] cv_deploy',
    ], () => {
      const r = cvDeployResultOf(l, p, new Date().toISOString(), p.skip_missing_devices ? missing : [])
      if (!p.skip_missing_devices && missing.length) return { error: `devices not found on CloudVision: ${missing.join(', ')}` }
      l.settings = { ...(l.settings ?? {}), cv_last_deploy: r }
      return { result: r as unknown as Record<string, unknown> }
    }, 100)
  }),

  // topology
  http.get(`${A}/topology/:id`, ({ params }) => {
    const l = db.labs.find((x) => x.id === params.id)
    if (!l) return notFound('lab not found')
    if (!l.built) return HttpResponse.json({ detail: 'lab not built' }, { status: 404 })
    return HttpResponse.json(topology(l))
  }),
  http.post(`${A}/topology/:id/regenerate`, ({ params }) => {
    const l = db.labs.find((x) => x.id === params.id)
    if (!l) return notFound('lab not found')
    const j = addJob({ type: 'lab.topology', lab_id: l.id })
    const t = topology(l)
    return runJob(j, ['reading documentation/fabric/FABRIC-topology.csv', ...t.nodes.map((n) => `  node ${n.name} (${n.kind}) ${n.mgmt_ip}`), `wrote clab/topology.clab.yml (${t.links.length} links)`, '[ok] topology'])
  }),

  // job log stream
  jobSocket.addEventListener('connection', ({ client }) => {
    const id = client.url.pathname.split('/').slice(-2)[0]
    const j = db.jobs.find((x) => x.id === id)
    if (!j) { client.send(JSON.stringify({ done: true })); client.close(); return }
    for (const line of j.log) client.send(JSON.stringify({ line }))
    if (isDone(j)) { client.send(JSON.stringify({ done: true })); client.close(); return }
    const fn: Listener = (frame) => {
      client.send(JSON.stringify(frame))
      if ('done' in frame) { listeners.get(id)?.delete(fn); client.close() }
    }
    ;(listeners.get(id) ?? listeners.set(id, new Set()).get(id)!).add(fn)
    client.addEventListener('close', () => listeners.get(id)?.delete(fn))
  }),
]

function deployLog(l: MockLab, p: DeployParams): string[] {
  const nodes = runtimeNodes(l)
  const ceos = nodes.filter((n) => n.kind === 'arista_ceos')
  const img = `arista/ceos:${l.ceos_version ?? '4.34.2F'}`
  const lines = [
    `$ docker image inspect ${img}`, `  [ok] ${img} present on host`,
    `rsync → /opt/avdlab/labs/${l.slug} (${Object.keys(l.files).length} files)`,
    `$ cd /opt/avdlab/labs/${l.slug} && sudo containerlab deploy -t clab/topology.clab.yml${p.reconfigure ? ' --reconfigure' : ''}`,
    'INFO Containerlab started version=0.59.0',
    'INFO Parsing & checking topology file=topology.clab.yml',
    `INFO Creating docker network name=${l.slug}-mgmt IPv4 subnet=${String(l.knobs?.mgmt_subnet ?? '')}`,
    ...nodes.map((n) => `INFO Creating container name=${n.name.replace(`clab-${l.slug}-`, '')}`),
    ...topology(l).links.slice(0, 6).map((k) => `INFO Created link: ${k.a}:${k.a_port} ▪┄┄▪ ${k.b}:${k.b_port}`),
    `INFO Adding host entries path=/etc/hosts`,
    '$ sudo containerlab inspect -t clab/topology.clab.yml --format json',
    `  ${nodes.length} containers running`,
  ]
  if (normalizeProvisioning(l.knobs?.provisioning).mode === 'ztp') {
    lines.push(
      'ZTP mode: cEOS nodes boot without startup-config',
      ...ceos.map((n) => `$ docker inspect -f '{{.NetworkSettings.Networks.${l.slug}-mgmt.MacAddress}}' ${n.name}`),
      `wrote ${ceos.length} dhcp-host lines to clab/ztp/dnsmasq.conf`, `$ docker exec clab-${l.slug}-ztp sh /ztp/run.sh restart`,
      `  poll: leases ${Math.ceil(ceos.length / 2)}/${ceos.length}, bootfile 0/${ceos.length}`,
      `  poll: leases ${ceos.length}/${ceos.length}, bootfile ${ceos.length}/${ceos.length}`,
    )
  }
  if (p.wait_eapi) {
    const half = Math.ceil(ceos.length / 2)
    lines.push(
      `waiting for eAPI on ${ceos.length} cEOS nodes (timeout ${p.wait_timeout}s)`,
      `  poll 1: ready 0/${ceos.length}, pending ${ceos.map((n) => n.name.split('-').slice(-2).join('-')).join(', ')}`,
      `  poll 2: ready ${half}/${ceos.length}`,
      ...ceos.map((n) => `  [ok] ${n.name} ${n.mgmt_ip?.split('/')[0]} eAPI ready`),
    )
  }
  lines.push('[ok] deployed')
  return lines
}

function applyTargetSecrets(t: CVTarget, b: Partial<CVTargetIn>) {
  // write-only: omitted = keep, null/'' = remove
  if (b.ztp_script !== undefined) {
    t.has_ztp_script = !!b.ztp_script
    t.ztp_script_meta = b.ztp_script ? { updated_at: new Date().toISOString() } : null
  }
  if (b.token !== undefined) t.has_token = !!b.token
  if (b.onboarding_token !== undefined) {
    t.has_onboarding_token = !!b.onboarding_token
    t.onboarding_token_meta = b.onboarding_token ? { expires_at: b.onboarding_token_expires_at ?? null } : null
  } else if (b.onboarding_token_expires_at !== undefined && t.has_onboarding_token) {
    t.onboarding_token_meta = { expires_at: b.onboarding_token_expires_at }
  }
}

function isDone(j: Job) { return ['succeeded', 'failed', 'cancelled'].includes(j.status) }

function labOut(l: (typeof db.labs)[number]) {
  const { files, history, built, runtime, ...rest } = l
  void files; void history; void built; void runtime
  return { ...rest, host: l.host_id ? (db.hosts.find((h) => h.id === l.host_id) ? hostOut(db.hosts.find((h) => h.id === l.host_id)!) : null) : null }
}

// ---------------------------------------------------------------- v0.8.0 (CONTRACT-7 A): auth + users mock
const meOf = (u: MockUser) => ({ user: u.username, role: u.role, must_change_password: u.must_change_password, mfa_enabled: u.mfa_enabled, recovery_codes_left: u.recovery.length })
const accountOf = ({ password: _p, totp_code: _t, recovery, ...u }: MockUser) => ({ ...u, recovery_codes_left: recovery.length })
const currentUser = (): MockUser | undefined =>
  db.authed ? authDb.users.find((u) => u.id === authDb.currentId) ?? authDb.users.find((u) => u.role === 'admin') : undefined
let auditSeq = 100
const auditEv = (username: string, event: string, detail: string | null = null) =>
  authDb.audit.unshift({ id: ++auditSeq, at: new Date().toISOString(), username, event, ip: '127.0.0.1', user_agent: 'mock', detail })
const tempPw = () => `Tmp-${Math.random().toString(36).slice(2, 10)}-${Math.random().toString(36).slice(2, 9).toUpperCase()}!`.slice(0, 20)
const grant = (u: MockUser) => {
  db.authed = true; authDb.currentId = u.id; authDb.pendingId = null; u.last_login_at = new Date().toISOString(); persistAuth()
  auditEv(u.username, 'login.ok')
  return HttpResponse.json(meOf(u))
}
const adminGuard = () => {
  const u = currentUser()
  if (!u) return unauth()
  return u.role === 'admin' ? null : HttpResponse.json({ detail: `role ${u.role} may not access user administration` }, { status: 403 })
}
const policyViolations = (pw: string, username: string) => {
  const v: string[] = []
  if (pw.length < 12) v.push('must be at least 12 characters long')
  if ([/[a-z]/, /[A-Z]/, /[0-9]/, /[^A-Za-z0-9]/].filter((r) => r.test(pw)).length < 3) v.push('must contain at least 3 of: lowercase, uppercase, digit, symbol')
  if (username.length >= 3 && pw.toLowerCase().includes(username.toLowerCase())) v.push('must not contain the username')
  return v
}
const policyError = (v: string[]) => HttpResponse.json({ detail: { message: 'password ' + v.join('; '), violations: v } }, { status: 422 })

function authHandlers() {
  return [
    http.post(`${A}/auth/login`, async ({ request }) => {
      const b = (await request.json()) as { username: string; password: string }
      const bad = () => HttpResponse.json({ detail: 'invalid username or password' }, { status: 401 })
      if (!b.username || !b.password) return bad()
      const u = authDb.users.find((x) => x.username === b.username.trim().toLowerCase())
      if (!u) return grant(authDb.users.find((x) => x.role === 'admin')!)  // mock: unknown names sign in as admin
      if (u.disabled) return bad()
      if (u.locked) return HttpResponse.json({ detail: `account locked until ${u.locked_until}`, locked_until: u.locked_until }, { status: 423 })
      if (u.password !== null && u.password !== b.password) { auditEv(u.username, 'login.failed'); return bad() }
      if (u.mfa_enabled) {
        authDb.pendingId = u.id; authDb.mfaAttempts = 0; db.authed = false
        return HttpResponse.json({ mfa_required: true })
      }
      return grant(u)
    }),
    http.post(`${A}/auth/mfa/verify`, async ({ request }) => {
      const { code } = (await request.json()) as { code: string }
      const u = authDb.users.find((x) => x.id === authDb.pendingId)
      if (!u) return HttpResponse.json({ detail: 'no pending sign-in: start again' }, { status: 401 })
      const norm = code.trim().toLowerCase()
      const ri = u.recovery.indexOf(norm)
      if (norm === u.totp_code || ri >= 0) {
        if (ri >= 0) u.recovery.splice(ri, 1)
        return grant(u)
      }
      if (++authDb.mfaAttempts >= 5) { authDb.pendingId = null; return HttpResponse.json({ detail: 'too many wrong codes: sign in again' }, { status: 401 }) }
      return HttpResponse.json({ detail: 'invalid code' }, { status: 400 })
    }),
    http.post(`${A}/auth/logout`, () => { db.authed = false; authDb.currentId = null; persistAuth(); return HttpResponse.json({ ok: true }) }),
    http.get(`${A}/auth/me`, () => { const u = currentUser(); return u ? HttpResponse.json(meOf(u)) : unauth() }),
    http.get(`${A}/auth/policy`, () => HttpResponse.json({ min_length: 12, classes_required: 3, max_repeat: 3 })),
    http.post(`${A}/auth/password`, async ({ request }) => {
      const u = currentUser(); if (!u) return unauth()
      const b = (await request.json()) as { current_password: string; new_password: string }
      if (u.password !== null && u.password !== b.current_password) return HttpResponse.json({ detail: 'current password is wrong' }, { status: 400 })
      const v = policyViolations(b.new_password, u.username)
      if (v.length) return policyError(v)
      u.password = b.new_password; u.must_change_password = false; u.password_changed_at = new Date().toISOString()
      auditEv(u.username, 'password.changed')
      return HttpResponse.json(meOf(u))
    }),
    http.post(`${A}/auth/mfa/setup`, () => {
      const u = currentUser(); if (!u) return unauth()
      if (u.mfa_enabled) return HttpResponse.json({ detail: 'MFA is already enabled: disable it first' }, { status: 409 })
      authDb.setupSecret = 'JBSWY3DPEHPK3PXP'
      const url = `otpauth://totp/avdlab:${u.username}?secret=${authDb.setupSecret}&issuer=avdlab`
      return HttpResponse.json({ secret: authDb.setupSecret, otpauth_url: url, qr_svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8" data-testid="qr-svg"><rect width="4" height="4"/><rect x="4" y="4" width="4" height="4"/></svg>' })
    }),
    http.post(`${A}/auth/mfa/enable`, async ({ request }) => {
      const u = currentUser(); if (!u) return unauth()
      const { code } = (await request.json()) as { code: string }
      if (!authDb.setupSecret) return HttpResponse.json({ detail: 'no MFA setup in progress' }, { status: 409 })
      if (code !== MOCK_TOTP) return HttpResponse.json({ detail: 'invalid code: check the clock of your phone and try again' }, { status: 400 })
      const codes = Array.from({ length: 10 }, (_, i) => `rc${i}aa-bb${i}cc`)
      u.mfa_enabled = true; u.recovery = [...codes]; authDb.setupSecret = null
      auditEv(u.username, 'mfa.enabled')
      return HttpResponse.json({ mfa_enabled: true, recovery_codes: codes })
    }),
    http.post(`${A}/auth/mfa/disable`, async ({ request }) => {
      const u = currentUser(); if (!u) return unauth()
      const b = (await request.json()) as { password: string; code: string }
      if (u.password !== null && u.password !== b.password) return HttpResponse.json({ detail: 'password is wrong' }, { status: 400 })
      if (b.code !== u.totp_code && !u.recovery.includes(b.code)) return HttpResponse.json({ detail: 'invalid code' }, { status: 400 })
      u.mfa_enabled = false; u.recovery = []
      return HttpResponse.json({ mfa_enabled: false })
    }),
    http.post(`${A}/auth/mfa/recovery-codes`, async ({ request }) => {
      const u = currentUser(); if (!u) return unauth()
      const b = (await request.json()) as { password: string }
      if (u.password !== null && u.password !== b.password) return HttpResponse.json({ detail: 'password is wrong' }, { status: 400 })
      const codes = Array.from({ length: 10 }, (_, i) => `nw${i}aa-bb${i}cc`)
      u.recovery = [...codes]
      return HttpResponse.json({ recovery_codes: codes })
    }),

    // users admin
    http.get(`${A}/users`, () => adminGuard() ?? HttpResponse.json(authDb.users.map(accountOf))),
    http.get(`${A}/users/audit`, ({ request }) => {
      const limit = Number(new URL(request.url).searchParams.get('limit') ?? 100)
      return adminGuard() ?? HttpResponse.json(authDb.audit.slice(0, limit))
    }),
    http.post(`${A}/users`, async ({ request }) => {
      const g = adminGuard(); if (g) return g
      const b = (await request.json()) as { username: string; password?: string; role: MockUser['role'] }
      const name = b.username.trim().toLowerCase()
      if (authDb.users.some((u) => u.username === name)) return HttpResponse.json({ detail: `user ${name} already exists` }, { status: 409 })
      if (b.password) { const v = policyViolations(b.password, name); if (v.length) return policyError(v) }
      const temp = b.password ? null : tempPw()
      const u: MockUser = {
        id: newId(), username: name, role: b.role, disabled: false, must_change_password: !b.password, mfa_enabled: false, recovery_codes_left: 0,
        failed_logins: 0, locked: false, locked_until: null, last_login_at: null, password_changed_at: new Date().toISOString(),
        created_at: new Date().toISOString(), password: b.password ?? temp, totp_code: MOCK_TOTP, recovery: [],
      }
      authDb.users.push(u)
      auditEv(name, 'user.created', `role ${b.role}`)
      return HttpResponse.json({ ...accountOf(u), temporary_password: temp }, { status: 201 })
    }),
    http.patch(`${A}/users/:id`, async ({ request, params }) => {
      const g = adminGuard(); if (g) return g
      const u = authDb.users.find((x) => x.id === params.id); if (!u) return notFound('user not found')
      const b = (await request.json()) as { role?: MockUser['role']; disabled?: boolean }
      const me = currentUser()!
      if (u.id === me.id && ((b.role && b.role !== 'admin') || b.disabled)) return HttpResponse.json({ detail: 'you cannot demote or disable yourself' }, { status: 409 })
      if (b.role) u.role = b.role
      if (b.disabled !== undefined) u.disabled = b.disabled
      return HttpResponse.json(accountOf(u))
    }),
    http.delete(`${A}/users/:id`, ({ params }) => {
      const g = adminGuard(); if (g) return g
      const u = authDb.users.find((x) => x.id === params.id); if (!u) return notFound('user not found')
      if (u.id === currentUser()!.id) return HttpResponse.json({ detail: 'you cannot delete yourself' }, { status: 409 })
      if (u.role === 'admin' && authDb.users.filter((x) => x.role === 'admin' && !x.disabled && x.id !== u.id).length === 0) return HttpResponse.json({ detail: 'cannot delete the last admin' }, { status: 409 })
      authDb.users = authDb.users.filter((x) => x.id !== u.id)
      return HttpResponse.json({ ok: true })
    }),
    http.post(`${A}/users/:id/reset-password`, ({ params }) => {
      const g = adminGuard(); if (g) return g
      const u = authDb.users.find((x) => x.id === params.id); if (!u) return notFound('user not found')
      const temp = tempPw()
      u.password = temp; u.must_change_password = true; u.locked = false; u.locked_until = null
      return HttpResponse.json({ ...accountOf(u), temporary_password: temp })
    }),
    http.post(`${A}/users/:id/mfa/reset`, ({ params }) => {
      const g = adminGuard(); if (g) return g
      const u = authDb.users.find((x) => x.id === params.id); if (!u) return notFound('user not found')
      u.mfa_enabled = false; u.recovery = []
      return HttpResponse.json(accountOf(u))
    }),
    http.post(`${A}/users/:id/unlock`, ({ params }) => {
      const g = adminGuard(); if (g) return g
      const u = authDb.users.find((x) => x.id === params.id); if (!u) return notFound('user not found')
      u.locked = false; u.locked_until = null; u.failed_logins = 0
      return HttpResponse.json(accountOf(u))
    }),
  ]
}

// ---------------------------------------------------------------- v0.9.0 (CONTRACT-8 A): account requests + admin settings
const effectiveSettings = (): AppSettingsValues => {
  const v = regDb.settings
  return { ...v, registration_mail_to: v.registration_mail_to || v.feedback_mail_to, smtp_from: v.smtp_from || 'avdlab@localhost' }
}
const adminSettingsOf = () => ({
  ...effectiveSettings(), has_smtp_password: !!regDb.smtpPassword, smtp_configured: !!regDb.settings.smtp_host.trim(),
  overridden: [...regDb.overridden], defaults: { ...SETTINGS_DEFAULTS, smtp_from: 'avdlab@localhost' },
})
const USERNAME_RE = /^[a-z0-9._-]{3,32}$/
const EMAIL_RE = /^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$/
const adminOnly = () => {
  const u = currentUser()
  if (!u) return unauth()
  return u.role === 'admin' ? null : HttpResponse.json({ detail: `role ${u.role} may not access admin settings` }, { status: 403 })
}

function registrationHandlers() {
  return [
    http.get(`${A}/meta`, () => {
      const s = effectiveSettings()
      return HttpResponse.json({
        ...metaOf(), version: p6.version ?? CURRENT, app_title: s.app_title, github_url: s.github_url || null,
        support_email: s.support_email || null, feedback_email: (s.feedback_mail_to || '').split(',')[0].trim() || null, registration_enabled: s.registration_enabled, intro_video_url: s.intro_video_url || null,
      })
    }),
    http.post(`${A}/register`, async ({ request }) => {
      if (!regDb.settings.registration_enabled) return HttpResponse.json({ detail: 'account requests are disabled on this server' }, { status: 403 })
      const b = (await request.json()) as { username: string; email: string; full_name?: string; reason?: string }
      const name = (b.username ?? '').trim().toLowerCase()
      if (!USERNAME_RE.test(name)) return HttpResponse.json({ detail: "username must be 3–32 characters: lowercase letters, digits, '.', '_' or '-'" }, { status: 422 })
      if (!EMAIL_RE.test((b.email ?? '').trim())) return HttpResponse.json({ detail: 'please enter a valid e-mail address' }, { status: 422 })
      if (authDb.users.some((u) => u.username === name) || regDb.requests.some((r) => r.username === name && r.status === 'pending')) {
        return HttpResponse.json({ detail: `the username ${name} is not available` }, { status: 409 })
      }
      const r: AccountRequest = {
        id: newId(), username: name, email: b.email.trim(), full_name: (b.full_name ?? '').trim(), reason: (b.reason ?? '').trim(), status: 'pending',
        ip: '127.0.0.1', role: null, user_id: null, decided_by: null, decided_at: null, decision_reason: null, created_at: new Date().toISOString(),
      }
      regDb.requests.unshift(r)
      auditEv(name, 'registration.requested', r.email)
      return HttpResponse.json({ id: r.id, status: r.status }, { status: 201 })
    }),
    http.get(`${A}/register/status/:id`, ({ params }) => {
      const r = regDb.requests.find((x) => x.id === params.id)
      return r ? HttpResponse.json({ status: r.status }) : notFound('request not found')
    }),
    http.get(`${A}/users/requests`, ({ request }) => {
      const g = adminGuard(); if (g) return g
      const st = new URL(request.url).searchParams.get('status') ?? 'pending'
      return HttpResponse.json(regDb.requests.filter((r) => st === 'all' || r.status === st))
    }),
    http.post(`${A}/users/requests/:id/approve`, async ({ request, params }) => {
      const g = adminGuard(); if (g) return g
      const r = regDb.requests.find((x) => x.id === params.id); if (!r) return notFound('account request not found')
      if (r.status !== 'pending') return HttpResponse.json({ detail: `request already ${r.status}` }, { status: 409 })
      if (authDb.users.some((u) => u.username === r.username)) return HttpResponse.json({ detail: `user ${r.username} already exists` }, { status: 409 })
      const { role = 'viewer' } = ((await request.json().catch(() => ({}))) ?? {}) as { role?: MockUser['role'] }
      const temp = tempPw()
      const u: MockUser = {
        id: newId(), username: r.username, role, disabled: false, must_change_password: true, mfa_enabled: false, recovery_codes_left: 0,
        failed_logins: 0, locked: false, locked_until: null, last_login_at: null, password_changed_at: new Date().toISOString(),
        created_at: new Date().toISOString(), password: temp, totp_code: MOCK_TOTP, recovery: [],
      }
      authDb.users.push(u)
      const me = currentUser()!
      Object.assign(r, { status: 'approved', role, user_id: u.id, decided_by: me.username, decided_at: new Date().toISOString() })
      auditEv(r.username, 'registration.approved', `by ${me.username}, role ${role}`)
      const mailed = !!regDb.settings.smtp_host
      if (mailed) regDb.mails.push({ to: r.email, subject: `[${regDb.settings.app_title}] your account is ready` })
      return HttpResponse.json({ user: accountOf(u), temporary_password: temp, request: r, mail_queued: mailed })
    }),
    http.post(`${A}/users/requests/:id/reject`, async ({ request, params }) => {
      const g = adminGuard(); if (g) return g
      const r = regDb.requests.find((x) => x.id === params.id); if (!r) return notFound('account request not found')
      if (r.status !== 'pending') return HttpResponse.json({ detail: `request already ${r.status}` }, { status: 409 })
      const { reason = null } = ((await request.json().catch(() => ({}))) ?? {}) as { reason?: string | null }
      const me = currentUser()!
      Object.assign(r, { status: 'rejected', decided_by: me.username, decided_at: new Date().toISOString(), decision_reason: reason || null })
      auditEv(r.username, 'registration.rejected', `by ${me.username}${reason ? `: ${reason}` : ''}`)
      return HttpResponse.json({ ...r, mail_queued: !!regDb.settings.smtp_host })
    }),
    http.get(`${A}/admin/settings`, () => adminOnly() ?? HttpResponse.json(adminSettingsOf())),
    http.put(`${A}/admin/settings`, async ({ request }) => {
      const g = adminOnly(); if (g) return g
      const b = (await request.json()) as Record<string, unknown>
      const keys = Object.keys(SETTINGS_DEFAULTS) as (keyof AppSettingsValues)[]
      const unknown = Object.keys(b).filter((k) => k !== 'smtp_password' && !(keys as string[]).includes(k))
      if (unknown.length) return HttpResponse.json({ detail: `unknown setting(s): ${unknown.join(', ')}` }, { status: 422 })
      if (b.github_url && !/^https?:\/\/\S+$/.test(String(b.github_url))) return HttpResponse.json({ detail: 'github_url must be an http(s) URL' }, { status: 422 })
      if (b.smtp_port != null && (typeof b.smtp_port !== 'number' || b.smtp_port < 1 || b.smtp_port > 65535)) return HttpResponse.json({ detail: 'smtp_port must be a port number 1–65535' }, { status: 422 })
      const s = regDb.settings as unknown as Record<string, unknown>
      for (const k of keys) {
        if (!(k in b)) continue
        const v = b[k]
        s[k] = v === null ? SETTINGS_DEFAULTS[k] : typeof v === 'string' ? v.trim() : v
        regDb.overridden = v === null ? regDb.overridden.filter((x) => x !== k) : [...new Set([...regDb.overridden, k])]
      }
      if (typeof b.smtp_password === 'string') regDb.smtpPassword = b.smtp_password
      auditEv(currentUser()!.username, 'settings.updated', Object.keys(b).join(', '))
      return HttpResponse.json(adminSettingsOf())
    }),
    http.post(`${A}/admin/settings/test-mail`, () => {
      const g = adminOnly(); if (g) return g
      const s = effectiveSettings()
      if (!s.smtp_host) return HttpResponse.json({ ok: false, error: 'SMTP is not configured (smtp_host is empty)', to: s.feedback_mail_to })
      regDb.mails.push({ to: s.feedback_mail_to, subject: `[${s.app_title}] test mail` })
      return HttpResponse.json({ ok: true, error: null, to: s.feedback_mail_to })
    }),
  ]
}
