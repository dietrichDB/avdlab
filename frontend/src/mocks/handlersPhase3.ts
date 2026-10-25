import { http, HttpResponse, ws, delay } from 'msw'
import type { GitAuthKind, IperfFlow, PingParams } from '../api/types'
import { addJob, db, newId, type MockJob, type MockLab } from './data'
import {
  clientPlans, configDiff, gitStatus, hostContainers, killContainer, hostMetrics, hostProcesses, importLab, iperfResult, openLinks, p3, pingResult,
  runningConfig, snapshotText,
} from './phase3'

type RunJob = (j: MockJob, lines: string[], finish?: (j: MockJob) => { error?: string; result?: Record<string, unknown> } | void, stepMs?: number) => Response

const A = '/api'
const notFound = (what = 'not found') => HttpResponse.json({ detail: what }, { status: 404 })
const conflict = (detail: string) => HttpResponse.json({ detail }, { status: 409 })
const labOf = (id: unknown) => db.labs.find((l) => l.id === id)
const deployed = (l: MockLab) => l.state === 'deployed' || l.state === 'validated'
const emptyZip = () => { const b = new Uint8Array(22); b.set([0x50, 0x4b, 0x05, 0x06]); return b }

// ---------------------------------------------------------------- fake terminals (CONTRACT-3 §7)
export const nodeTerminal = ws.link(/\/api\/labs\/[^/]+\/nodes\/[^/]+\/terminal/)
export const hostTerminal = ws.link(/\/api\/hosts\/[^/]+\/terminal/)
const enc = new TextEncoder()
const dec = new TextDecoder()

function attachFakeShell(client: { url: URL; send: (d: ArrayBufferLike | ArrayBufferView | string) => void; addEventListener: (t: 'message', fn: (e: MessageEvent) => void) => void }, banner: string, prompt: string) {
  const out = (s: string) => client.send(enc.encode(s))
  out(banner.replace(/\n/g, '\r\n'))
  out(prompt)
  let line = ''
  const handle = (text: string) => {
    for (const ch of text) {
      if (ch === '\r' || ch === '\n') {
        const cmd = line.trim()
        line = ''
        out('\r\n')
        if (cmd === 'exit') { out('logout\r\n'); return }
        if (cmd) out(`(mock) ${cmd}: ok\r\n`)
        out(prompt)
      } else if (ch === '\x7f') {
        if (line) { line = line.slice(0, -1); out('\b \b') }
      } else if (ch === '\x03') {
        line = ''; out('^C\r\n' + prompt)
      } else { line += ch; out(ch) }
    }
  }
  client.addEventListener('message', (ev: MessageEvent) => {
    const d = ev.data
    if (typeof d === 'string') {
      // JSON resize control frame
      try { const o = JSON.parse(d); if (o?.resize) { out(`\x1b[90m[resized to ${o.resize.cols}x${o.resize.rows}]\x1b[0m\r\n${prompt}${line}`); return } } catch { /* plain text */ }
      handle(d)
    } else if (d instanceof ArrayBuffer || ArrayBuffer.isView(d)) {
      handle(dec.decode(d as ArrayBuffer))
    } else if (typeof Blob !== 'undefined' && d instanceof Blob) {
      d.arrayBuffer().then((b) => handle(dec.decode(b)))
    }
  })
}

export function phase3Handlers(runJob: RunJob) {
  return [
    // ------------------------------------------------ §4 import / export (before /labs/:id routes)
    http.post(`${A}/labs/import/git`, async ({ request }) => {
      const b = (await request.json()) as { name: string; url: string; branch?: string; auth_kind: GitAuthKind; token?: string | null; secret_id?: string | null; host_id?: string | null }
      if (!b.name || !b.url) return HttpResponse.json({ detail: [{ loc: ['body', b.name ? 'url' : 'name'], msg: 'field required' }] }, { status: 422 })
      if (db.labs.some((l) => l.name === b.name)) return conflict('lab name exists')
      const j = addJob({ type: 'lab.import_git', params: { name: b.name, url: b.url, branch: b.branch ?? 'main', auth_kind: b.auth_kind } })
      return runJob(j, [
        `$ git clone --branch ${b.branch ?? 'main'} ${b.url} labs/${b.name}`,
        b.auth_kind === 'token' ? '  using GIT_ASKPASS helper (token not logged)' : b.auth_kind === 'ssh_key' ? '  GIT_SSH_COMMAND=ssh -i <tmp key>' : '  anonymous',
        'Cloning… done.', 'found inventory.yml + group_vars/ (6 files)', 'fabric_name: FABRIC', 'pyavd.validate_inputs …', '  [ok] 0 violations', '[ok] imported',
      ], (job) => {
        const lab = importLab(b.name, b.host_id ?? null)
        job.lab_id = lab.id
        p3.git.set(lab.id, { configured: true, url: b.url, branch: b.branch ?? 'main', auth_kind: b.auth_kind, has_secret: b.auth_kind !== 'none', ahead: 0, behind: 0, last_sync: { at: new Date().toISOString(), direction: 'pull', ok: true, message: 'cloned' } })
        return { result: { lab_id: lab.id } }
      })
    }),
    http.post(`${A}/labs/import`, async ({ request }) => {
      const fd = await request.formData()
      const file = fd.get('file') as File | null
      const name = String(fd.get('name') ?? '').trim()
      if (!file || !name) return HttpResponse.json({ detail: [{ loc: ['body', file ? 'name' : 'file'], msg: 'field required' }] }, { status: 422 })
      if (!/\.(zip|tar\.gz|tgz)$/i.test(file.name)) return HttpResponse.json({ detail: 'archive must be .zip or .tar.gz' }, { status: 422 })
      if (db.labs.some((l) => l.name === name)) return conflict('lab name exists')
      await delay(400)
      const warnings = /invalid|bad/i.test(file.name) ? ["DC1.yml: l3leaf.defaults.bgp_as: Invalid type 'str'. Expected 'int'"] : []
      const lab = importLab(name, (fd.get('host_id') as string) || null, warnings)
      const { files, history, built, runtime, ...rest } = lab
      void files; void history; void built; void runtime
      return HttpResponse.json(rest, { status: 201 })
    }),
    http.get(`${A}/labs/:id/export`, ({ params }) => {
      const l = labOf(params.id)
      if (!l) return notFound('lab not found')
      return new HttpResponse(emptyZip(), { headers: { 'Content-Type': 'application/zip', 'Content-Disposition': `attachment; filename="${l.slug}.zip"` } })
    }),

    // ------------------------------------------------ §1 clients & traffic
    http.get(`${A}/labs/:id/clients`, ({ params }) => {
      const l = labOf(params.id)
      if (!l) return notFound('lab not found')
      if (!l.built) return HttpResponse.json({ detail: 'lab not built' }, { status: 404 })
      return HttpResponse.json(clientPlans(l))
    }),
    http.post(`${A}/labs/:id/traffic/ping`, async ({ params, request }) => {
      const l = labOf(params.id)
      if (!l) return notFound('lab not found')
      if (!deployed(l)) return conflict(`lab is not deployed (state: ${l.state})`)
      const b = { pairs: null, count: 3, size: 56, ...((await request.json().catch(() => ({}))) as Partial<PingParams>) }
      const r = pingResult(l, b.pairs, b.count)
      const j = addJob({ type: 'lab.traffic_ping', lab_id: l.id, host_id: l.host_id ?? undefined, params: b as unknown as Record<string, unknown> })
      return runJob(j, [
        `${r.results.length} pairs, count ${b.count}, size ${b.size}${b.vlan ? `, vlan ${b.vlan}` : ''}`,
        ...r.results.map((x) => `$ docker exec clab-${l.slug}-${x.src} ping -c ${b.count} -s ${b.size} ${x.target_ip} → ${x.received}/${x.sent}${x.rtt_avg_ms != null ? ` avg ${x.rtt_avg_ms.toFixed(2)} ms` : ''}`),
        `[ok] ${r.results.filter((x) => x.ok).length}/${r.results.length} pairs clean`,
      ], () => {
        p3.runs.unshift({ id: newId(), lab_id: l.id, job_id: j.id, kind: 'ping', params: b as unknown as Record<string, unknown>, results: r, created_at: new Date().toISOString() })
        return { result: r as unknown as Record<string, unknown> }
      }, 80)
    }),
    http.post(`${A}/labs/:id/traffic/iperf`, async ({ params, request }) => {
      const l = labOf(params.id)
      if (!l) return notFound('lab not found')
      if (!deployed(l)) return conflict(`lab is not deployed (state: ${l.state})`)
      const b = (await request.json()) as { flows: IperfFlow[]; sequential?: boolean }
      if (!b.flows?.length) return HttpResponse.json({ detail: 'no flows' }, { status: 422 })
      const rows = iperfResult(l, b.flows)
      const j = addJob({ type: 'lab.traffic_iperf', lab_id: l.id, host_id: l.host_id ?? undefined, params: b as unknown as Record<string, unknown> })
      return runJob(j, b.flows.flatMap((f) => [
        `$ docker exec clab-${l.slug}-${f.dst} sh -c 'pgrep iperf3 || iperf3 -s -D'`,
        `$ docker exec clab-${l.slug}-${f.src} iperf3 -c <${f.dst}> -J -t ${f.duration}${f.proto === 'udp' ? ` -u -b ${f.bandwidth ?? '1M'}` : ''}${f.parallel > 1 ? ` -P ${f.parallel}` : ''}${f.reverse ? ' -R' : ''}`,
      ]).concat(['[ok] iperf done']), () => {
        p3.runs.unshift({ id: newId(), lab_id: l.id, job_id: j.id, kind: 'iperf', params: b as unknown as Record<string, unknown>, results: { results: rows }, created_at: new Date().toISOString() })
        return { result: { results: rows } as unknown as Record<string, unknown> }
      }, 250)
    }),
    http.get(`${A}/labs/:id/traffic/runs`, ({ params }) => HttpResponse.json(p3.runs.filter((r) => r.lab_id === params.id).map((r) => ({ ...r, results: undefined })))),
    http.get(`${A}/labs/:id/traffic/runs/:runId`, ({ params }) => {
      const r = p3.runs.find((x) => x.id === params.runId && x.lab_id === params.id)
      return r ? HttpResponse.json(r) : notFound('run not found')
    }),

    // ------------------------------------------------ §3 running configs & snapshots
    http.get(`${A}/labs/:id/nodes/:node/running-config`, async ({ params }) => {
      const l = labOf(params.id)
      if (!l) return notFound('lab not found')
      if (!deployed(l)) return conflict('lab is not deployed')
      await delay(300)
      return HttpResponse.json({ node: params.node, config: runningConfig(l, String(params.node)), fetched_at: new Date().toISOString() })
    }),
    http.get(`${A}/labs/:id/nodes/:node/config-diff`, async ({ params }) => {
      const l = labOf(params.id)
      if (!l) return notFound('lab not found')
      if (!deployed(l)) return conflict('lab is not deployed')
      await delay(300)
      return HttpResponse.json(configDiff(l, String(params.node)))
    }),
    http.post(`${A}/labs/:id/snapshot`, async ({ params, request }) => {
      const l = labOf(params.id)
      if (!l) return notFound('lab not found')
      if (!deployed(l)) return conflict('lab is not deployed')
      const b = (await request.json().catch(() => ({}))) as { commands?: string[] }
      const commands = b.commands?.length ? b.commands : ['show running-config', 'show version']
      const nodes = (l.runtime?.nodes ?? []).filter((n) => n.kind === 'arista_ceos')
      const d = new Date()
      const pad = (n: number) => String(n).padStart(2, '0')
      const id = `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`
      const j = addJob({ type: 'lab.snapshot', lab_id: l.id, host_id: l.host_id ?? undefined, params: { commands } })
      return runJob(j, [...nodes.map((n) => `${n.name}: ${commands.length} commands`), `wrote snapshots/${id}/ (${nodes.length} nodes)`, `git commit: snapshot ${id}`, '[ok] snapshot'], () => {
        const list = p3.snapshots.get(l.id) ?? []
        list.unshift({ id, created_at: d.toISOString(), nodes: nodes.length, commands })
        p3.snapshots.set(l.id, list)
        return { result: { snapshot: id } }
      }, 60)
    }),
    http.get(`${A}/labs/:id/snapshots`, ({ params }) => HttpResponse.json(p3.snapshots.get(String(params.id)) ?? [])),
    http.get(`${A}/labs/:id/snapshots/:snap/:node/:slug`, ({ params }) => {
      const l = labOf(params.id)
      const s = (p3.snapshots.get(String(params.id)) ?? []).find((x) => x.id === params.snap)
      if (!l || !s) return notFound('snapshot not found')
      return HttpResponse.text(snapshotText(l, String(params.node), String(params.slug)))
    }),

    // ------------------------------------------------ §2 git
    http.get(`${A}/labs/:id/git`, ({ params }) => (labOf(params.id) ? HttpResponse.json(gitStatus(String(params.id))) : notFound('lab not found'))),
    http.put(`${A}/labs/:id/git`, async ({ params, request }) => {
      const l = labOf(params.id)
      if (!l) return notFound('lab not found')
      const b = (await request.json()) as { url: string; branch?: string; auth_kind: GitAuthKind; secret_id?: string | null; token?: string | null }
      if (!b.url) return HttpResponse.json({ detail: [{ loc: ['body', 'url'], msg: 'field required' }] }, { status: 422 })
      if (/:\/\/[^/]*@/.test(b.url)) return HttpResponse.json({ detail: 'credentials must not be part of the URL' }, { status: 422 })
      const prev = gitStatus(l.id)
      let has_secret = false
      if (b.auth_kind === 'token') {
        if (b.token) {
          db.secrets.push({ id: newId(), name: `git-${l.slug}`, kind: 'generic', meta: { purpose: 'git' }, created_at: new Date().toISOString() })
          has_secret = true
        } else has_secret = !!b.secret_id || (prev.auth_kind === 'token' && !!prev.has_secret)
      } else if (b.auth_kind === 'ssh_key') has_secret = !!b.secret_id || (prev.auth_kind === 'ssh_key' && !!prev.has_secret)
      p3.git.set(l.id, { ...prev, configured: true, url: b.url, branch: b.branch || 'main', auth_kind: b.auth_kind, has_secret, ahead: prev.ahead ?? null, behind: prev.behind ?? null })
      return HttpResponse.json({ url: b.url, branch: b.branch || 'main', auth_kind: b.auth_kind, has_secret })
    }),
    http.delete(`${A}/labs/:id/git`, ({ params }) => { p3.git.delete(String(params.id)); return new HttpResponse(null, { status: 204 }) }),
    http.post(`${A}/labs/:id/git/push`, async ({ params, request }) => {
      const l = labOf(params.id)
      const g = l && gitStatus(l.id)
      if (!l || !g) return notFound('lab not found')
      if (!g.configured) return conflict('no git remote configured')
      const b = (await request.json().catch(() => ({}))) as { force?: boolean }
      const j = addJob({ type: 'lab.git_push', lab_id: l.id, params: { force: !!b.force } })
      return runJob(j, [`$ git push${b.force ? ' --force' : ''} origin ${g.branch}`, `To ${g.url}`, `   a1b2c3d..e4f5a6b  ${g.branch} -> ${g.branch}`, '[ok] pushed'], () => {
        p3.git.set(l.id, { ...g, ahead: 0, behind: 0, last_sync: { at: new Date().toISOString(), direction: 'push', ok: true, message: `${g.ahead ?? 0} commit(s) pushed` } })
      })
    }),
    http.post(`${A}/labs/:id/git/pull`, ({ params }) => {
      const l = labOf(params.id)
      const g = l && gitStatus(l.id)
      if (!l || !g) return notFound('lab not found')
      if (!g.configured) return conflict('no git remote configured')
      const j = addJob({ type: 'lab.git_pull', lab_id: l.id })
      return runJob(j, [`$ git fetch origin ${g.branch}`, `$ git pull --ff-only origin ${g.branch}`, 'Already up to date.', 'pyavd.validate_inputs … [ok]', '[ok] pulled'], () => {
        p3.git.set(l.id, { ...g, behind: 0, last_sync: { at: new Date().toISOString(), direction: 'pull', ok: true, message: 'fast-forward, 0 files changed' } })
      })
    }),

    // ------------------------------------------------ §8 VS Code
    http.get(`${A}/labs/:id/open-links`, ({ params }) => {
      const l = labOf(params.id)
      if (!l) return notFound('lab not found')
      if (!l.host_id) return conflict('lab has no host')
      return HttpResponse.json(openLinks(l))
    }),
    http.post(`${A}/labs/:id/sync`, ({ params }) => {
      const l = labOf(params.id)
      if (!l) return notFound('lab not found')
      if (!l.host_id) return conflict('lab has no host')
      const j = addJob({ type: 'lab.sync', lab_id: l.id, host_id: l.host_id })
      return runJob(j, [`$ rsync -az --delete --exclude .git ./ ${l.host_id}:/opt/avdlab/labs/${l.slug}/`, `sent ${Object.keys(l.files).length} files`, '[ok] synced'])
    }),

    // ------------------------------------------------ §5 host monitoring
    http.get(`${A}/hosts/:id/metrics`, ({ params }) => (db.hosts.some((h) => h.id === params.id) ? HttpResponse.json(hostMetrics(String(params.id))) : notFound('host not found'))),
    http.get(`${A}/hosts/:id/processes`, ({ params }) => (db.hosts.some((h) => h.id === params.id) ? HttpResponse.json(hostProcesses(String(params.id))) : notFound('host not found'))),
    http.get(`${A}/hosts/:id/containers`, ({ params }) => (db.hosts.some((h) => h.id === params.id) ? HttpResponse.json(hostContainers(String(params.id))) : notFound('host not found'))),
    http.delete(`${A}/hosts/:id/containers/:name`, async ({ params, request }) => {
      if (!db.hosts.some((h) => h.id === params.id)) return notFound('host not found')
      const force = new URL(request.url).searchParams.get('force') === 'true'
      let confirm: string | undefined
      try { confirm = ((await request.json()) as { confirm?: string } | null)?.confirm } catch { confirm = undefined }
      const r = killContainer(String(params.id), String(params.name), force, confirm)
      return HttpResponse.json(r.body, { status: r.status })
    }),

    // ------------------------------------------------ §6 image formats
    http.get(`${A}/images/formats`, () => HttpResponse.json([
      { fmt: 'cEOS', arch: 'x86_64', label: 'cEOS (x86_64)' },
      { fmt: 'cEOS64', arch: 'x86_64', label: 'cEOS64 (x86_64, 64-bit)' },
      { fmt: 'cEOSarm', arch: 'arm64', label: 'cEOSarm (arm64)' },
    ])),

    // ------------------------------------------------ §7 terminals
    nodeTerminal.addEventListener('connection', ({ client }) => {
      const parts = client.url.pathname.split('/')
      const node = decodeURIComponent(parts[parts.length - 2])
      const shell = client.url.searchParams.get('shell') ?? 'cli'
      const size = `${client.url.searchParams.get('cols') ?? '?'}x${client.url.searchParams.get('rows') ?? '?'}`
      attachFakeShell(client, shell === 'bash'
        ? `\x1b[1;34m[avdlab mock]\x1b[0m docker exec -it ${node} bash (${size})\nThis is a fake terminal: input is echoed.\n`
        : `\x1b[1;34m[avdlab mock]\x1b[0m docker exec -it ${node} Cli (${size})\nArista cEOSLab (mock) — input is echoed.\n\n`, shell === 'bash' ? `[root@${node} /]# ` : `${node}#`)
    }),
    hostTerminal.addEventListener('connection', ({ client }) => {
      const id = client.url.pathname.split('/').slice(-2)[0]
      const h = db.hosts.find((x) => x.id === id)
      attachFakeShell(client, `\x1b[1;34m[avdlab mock]\x1b[0m ssh ${h?.ssh_user ?? 'user'}@${h?.address ?? id}: $SHELL -l\nWelcome to ${h?.capabilities.os?.name ?? 'Linux'} (mock, input is echoed)\n`, `${h?.ssh_user ?? 'user'}@${h?.name ?? 'host'}:~$ `)
    }),
  ]
}
