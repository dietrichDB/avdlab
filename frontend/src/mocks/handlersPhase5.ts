import { http, HttpResponse } from 'msw'
import type { EditOp, FeedbackIn, FeedbackStatus, TopologyLayout } from '../api/types'
import { commit, db, renderFiles } from './data'
import { applyOps, changelogOf, editableTopology, metaOf, noticesOf, p5, portsOf } from './phase5'

const A = '/api'
const notFound = (what = 'not found') => HttpResponse.json({ detail: what }, { status: 404 })
const labOf = (id: unknown) => db.labs.find((l) => l.id === id)

/** CONTRACT-5 handlers (registered first: GET /topology/{id} is taken over to add layout, endpoints and overrides). */
export function phase5Handlers() {
  return [
    // ------------------------------------------------ §1 editable topology
    http.get(`${A}/topology/:id`, ({ params }) => {
      const l = labOf(params.id)
      if (!l) return notFound('lab not found')
      if (!l.built) return HttpResponse.json({ detail: 'lab not built' }, { status: 404 })
      const layout = p5.layouts.get(l.id)
      return HttpResponse.json({ ...editableTopology(l), ...(layout ? { layout } : {}) })
    }),
    http.get(`${A}/topology/:id/layout`, ({ params }) => {
      const layout = p5.layouts.get(String(params.id))
      return layout ? HttpResponse.json(layout) : notFound('no layout stored')
    }),
    http.put(`${A}/topology/:id/layout`, async ({ params, request }) => {
      const l = labOf(params.id)
      if (!l) return notFound('lab not found')
      const b = (await request.json()) as TopologyLayout
      if (!b || typeof b.positions !== 'object') return HttpResponse.json({ detail: 'positions required' }, { status: 422 })
      const layout = { positions: b.positions, locked: !!b.locked }
      p5.layouts.set(l.id, layout)
      return HttpResponse.json(layout)
    }),
    http.get(`${A}/topology/:id/ports`, ({ params }) => {
      const l = labOf(params.id)
      if (!l) return notFound('lab not found')
      return HttpResponse.json(portsOf(editableTopology(l)))
    }),
    http.post(`${A}/topology/:id/edit`, async ({ params, request }) => {
      const l = labOf(params.id)
      if (!l) return notFound('lab not found')
      const b = (await request.json()) as { ops: EditOp[]; dry_run?: boolean }
      p5.editRequests.push({ ops: b.ops, dry_run: !!b.dry_run })
      if (!Array.isArray(b.ops) || !b.ops.length) return HttpResponse.json({ detail: { message: 'no ops', violations: [], op_index: null } }, { status: 422 })
      const r = applyOps(l, b.ops)
      if (!r.ok) return HttpResponse.json({ detail: r.detail }, { status: r.status })
      if (!b.dry_run) {
        l.knobs = r.lab.knobs
        l.settings = r.lab.settings
        commit(l, renderFiles(l), `topology edit: ${b.ops.map((o) => o.op).join(', ')}`)
      }
      return HttpResponse.json({ graph: r.graph, changed_files: r.changed_files, warnings: r.warnings })
    }),

    // ------------------------------------------------ §2/§4 meta
    http.get(`${A}/meta`, () => HttpResponse.json(metaOf())),
    http.get(`${A}/meta/changelog`, () => HttpResponse.json(changelogOf())),
    http.get(`${A}/meta/notices`, () => HttpResponse.json(noticesOf())),

    // ------------------------------------------------ §3 feedback
    http.get(`${A}/feedback`, () => HttpResponse.json(p5.feedback.map(({ diagnostics, ...f }) => { void diagnostics; return f }))),
    http.post(`${A}/feedback`, async ({ request }) => {
      const b = (await request.json()) as FeedbackIn
      p5.feedbackRequests.push(b)
      if (!['bug', 'suggestion', 'question'].includes(b.kind) || !b.title?.trim()) return HttpResponse.json({ detail: [{ loc: ['body', 'title'], msg: 'field required' }] }, { status: 422 })
      const m = metaOf()
      const diagnostics = b.include_diagnostics
        ? { version: m.version, git_sha: m.git_sha, browser: request.headers.get('user-agent') ?? 'unknown', recent_failed_jobs: db.jobs.filter((j) => j.status === 'failed').slice(0, 5).map((j) => ({ type: j.type, error: j.error })), hosts: db.hosts.map((h) => h.name) }
        : null
      const id = p5.feedbackSeq++
      p5.feedback.unshift({ id, kind: b.kind, title: b.title, body: b.body, page: b.page, status: 'new', github_issue_url: null, created_at: new Date().toISOString(), diagnostics })
      return HttpResponse.json({ id, github_issue_url: null, diagnostics }, { status: 201 })
    }),
    http.patch(`${A}/feedback/:fid`, async ({ params, request }) => {
      const f = p5.feedback.find((x) => String(x.id) === params.fid)
      if (!f) return notFound('feedback not found')
      const b = (await request.json()) as { status: FeedbackStatus }
      if (!['new', 'triaged', 'done'].includes(b.status)) return HttpResponse.json({ detail: 'invalid status' }, { status: 422 })
      f.status = b.status
      const { diagnostics, ...out } = f
      void diagnostics
      return HttpResponse.json(out)
    }),
    http.delete(`${A}/feedback/:fid`, ({ params }) => {
      p5.feedback = p5.feedback.filter((x) => String(x.id) !== params.fid)
      return new HttpResponse(null, { status: 204 })
    }),
  ]
}
