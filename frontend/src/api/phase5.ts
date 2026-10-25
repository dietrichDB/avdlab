import { api, ApiError } from './client'
import type {
  Changelog, EditOp, EditResult, Feedback, FeedbackCreated, FeedbackIn, FeedbackStatus, Meta, Notices, TopologyLayout, TopologyPorts,
} from './types'

/** CONTRACT-5 §1 — layout, free ports, edit ops */
export const topologyEditApi = {
  /** null when no layout is stored (404) */
  layout: async (labId: string): Promise<TopologyLayout | null> => {
    try {
      return await api.get<TopologyLayout>(`/topology/${labId}/layout`)
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) return null
      throw e
    }
  },
  saveLayout: (labId: string, body: TopologyLayout) => api.put<TopologyLayout>(`/topology/${labId}/layout`, body),
  ports: (labId: string) => api.get<TopologyPorts>(`/topology/${labId}/ports`),
  edit: (labId: string, ops: EditOp[], dry_run: boolean) => api.post<EditResult>(`/topology/${labId}/edit`, { ops, dry_run }),
}

/** CONTRACT-5 §2/§4 */
export const metaApi = {
  get: () => api.get<Meta>('/meta'),
  changelog: async (): Promise<Changelog> => normalizeChangelog(await api.get<unknown>('/meta/changelog')),
  notices: async (): Promise<Notices> => {
    const n = (await api.get<Record<string, unknown>>('/meta/notices')) ?? {}
    let copyright = typeof n.copyright === 'string' ? n.copyright : ''
    if (!copyright) {
      // older backend shape: copyright lives on /meta (copyright_notice)
      const m = (await api.get<Record<string, unknown>>('/meta').catch(() => ({}))) as Record<string, unknown>
      copyright = String(m.copyright_notice ?? m.copyright ?? '')
    }
    return { markdown: String(n.markdown ?? ''), copyright, license: String(n.license ?? n.license_text ?? '') }
  },
}

/** Contract shape `{entries, markdown}`; also accepts `{versions, raw}`. */
export function normalizeChangelog(r: unknown): Changelog {
  const o = (r ?? {}) as Record<string, unknown>
  const entries = (o.entries ?? o.versions ?? []) as Changelog['entries']
  return { entries: Array.isArray(entries) ? entries : [], markdown: String(o.markdown ?? o.raw ?? '') }
}

/** CONTRACT-5 §3 */
export const feedbackApi = {
  create: (body: FeedbackIn) => api.post<FeedbackCreated>('/feedback', body),
  list: () => api.get<Feedback[]>('/feedback'),
  setStatus: (id: string | number, status: FeedbackStatus) => api.patch<Feedback>(`/feedback/${id}`, { status }),
  remove: (id: string | number) => api.delete<void>(`/feedback/${id}`),
}
