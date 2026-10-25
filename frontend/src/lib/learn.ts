import type { GlossaryTerm, LessonBlock, LessonLevel, LessonProgress, LessonSummary, SchemaName, Tour } from '../api/phase6'

// ---------------------------------------------------------------- learning mode (persisted)
export const LEARN_MODE_KEY = 'avdlab.learningMode'
export const LEARN_MODE_EVENT = 'avdlab:learning-mode'
export const LEARN_LAB_KEY = 'avdlab.learn.lab'
export const FIRST_LAB_DISMISSED_KEY = 'avdlab.tour.first-lab.dismissed'

/** Learning mode defaults to on (a fresh browser should show the (i) popovers and explain panels); "0" turns it off. */
export function getLearningMode(): boolean {
  try { return localStorage.getItem(LEARN_MODE_KEY) !== '0' } catch { return true }
}
export function setLearningMode(on: boolean) {
  try { localStorage.setItem(LEARN_MODE_KEY, on ? '1' : '0') } catch { /* storage unavailable */ }
  window.dispatchEvent(new Event(LEARN_MODE_EVENT))
}
export function getLS(key: string): string | null {
  try { return localStorage.getItem(key) } catch { return null }
}
export function setLS(key: string, v: string | null) {
  try { if (v === null) localStorage.removeItem(key); else localStorage.setItem(key, v) } catch { /* ignore */ }
}

// ---------------------------------------------------------------- knob learn
/** YAML turns "Renaming X: consequence" into {Renaming X: consequence} — render both shapes as one line. */
export function pitfallText(p: unknown): string {
  if (typeof p === 'string') return p
  if (p && typeof p === 'object') return Object.entries(p as Record<string, unknown>).map(([k, v]) => (v == null ? k : `${k}: ${String(v)}`)).join('; ')
  return String(p ?? '')
}

// ---------------------------------------------------------------- glossary
const norm = (s: string) => s.trim().toLowerCase().replace(/[\s_-]+/g, ' ')
export function findTerm(glossary: GlossaryTerm[] | undefined, name: string): GlossaryTerm | undefined {
  if (!glossary) return undefined
  const n = norm(name)
  return glossary.find((t) => norm(t.term) === n) ?? glossary.find((t) => t.aliases.some((a) => norm(a) === n))
    ?? (n.endsWith('s') ? findTerm(glossary, n.slice(0, -1)) : undefined)
}
export function filterGlossary(glossary: GlossaryTerm[], q: string): GlossaryTerm[] {
  const n = q.trim().toLowerCase()
  if (!n) return glossary
  return glossary.filter((t) => t.term.toLowerCase().includes(n) || t.aliases.some((a) => a.toLowerCase().includes(n)) || t.short.toLowerCase().includes(n))
}

// ---------------------------------------------------------------- EOS config sections (mirror of learn/explain.parse_config)
export interface CfgLine { no: number; text: string; indent: number; section: number | null }
export function parseConfig(text: string): CfgLine[] {
  const out: CfgLine[] = []
  const stack: { indent: number; no: number }[] = []
  text.split('\n').forEach((raw, i) => {
    const no = i + 1
    const body = raw.replace(/\s+$/, '')
    const stripped = body.trim()
    const indent = body.length - body.replace(/^ +/, '').length
    if (!stripped || stripped === '!' || stripped.startsWith('! ')) {
      out.push({ no, text: raw, indent, section: stack.length && indent > 0 ? stack[0].no : null })
      if (indent === 0) stack.length = 0
      return
    }
    while (stack.length && stack[stack.length - 1].indent >= indent) stack.pop()
    const section = stack.length ? stack[0].no : no
    out.push({ no, text: raw, indent, section })
    stack.push({ indent, no })
  })
  return out
}
export const isExplainable = (l: CfgLine) => { const s = l.text.trim(); return !!s && s !== '!' && !s.startsWith('! ') && s !== 'end' }

/** intended/configs/<host>.cfg → host */
export function configHost(path: string | null | undefined): string | null {
  const m = /^intended\/configs\/([^/]+)\.cfg$/.exec(path ?? '')
  return m ? m[1] : null
}

// ---------------------------------------------------------------- YAML key path at a line (schema browser from the editor)
/** Dotted schema path of the key on `lineNo` (1-based): parents by indentation, list items without index. */
export function yamlPathAt(text: string, lineNo: number): string | null {
  const lines = text.split('\n')
  const keyOf = (l: string): { indent: number; key: string } | null => {
    const m = /^(\s*)(- +)?([A-Za-z_<][\w<>.-]*)\s*:(\s|$)/.exec(l)
    if (!m) return null
    return { indent: m[1].length + (m[2]?.length ?? 0), key: m[3] }
  }
  const cur = keyOf(lines[lineNo - 1] ?? '')
  if (!cur) return null
  const parts = [cur.key]
  let indent = cur.indent
  for (let i = lineNo - 2; i >= 0 && indent > 0; i--) {
    const l = lines[i]
    if (!l.trim() || l.trim().startsWith('#')) continue
    const k = keyOf(l)
    const dash = /^(\s*)- */.exec(l)
    if (k && k.indent < indent) {
      parts.unshift(k.key)
      indent = dash ? dash[1].length : k.indent
    } else if (dash && dash[1].length < indent) {
      // "- name: x" sibling of the current key (same item) or a bare "-": the item starts at the dash column
      indent = dash[1].length
    }
  }
  return parts.join('.')
}

export function schemaForFile(path: string | null | undefined): SchemaName | null {
  if (!path) return null
  if (/^intended\/structured_configs\//.test(path)) return 'eos_cli_config_gen'
  if (/^(group_vars|host_vars)\//.test(path) || path === 'inventory.yml') return 'eos_designs'
  return null
}

// ---------------------------------------------------------------- lessons
export const LEVELS: LessonLevel[] = ['intro', 'core', 'advanced']
export function byLevel(lessons: LessonSummary[]): Record<LessonLevel, LessonSummary[]> {
  const out: Record<LessonLevel, LessonSummary[]> = { intro: [], core: [], advanced: [] }
  for (const l of [...lessons].sort((a, b) => (a.order ?? 999) - (b.order ?? 999))) (out[l.level] ?? out.core).push(l)
  return out
}
export function progressPct(p: LessonProgress | undefined | null): number {
  if (!p || !p.total) return 0
  return Math.round((100 * p.passed.length) / p.total)
}

/** What a :::try block does in the app. */
export type TryPlan =
  | { kind: 'navigate'; to: string }
  | { kind: 'request'; method: string; path: string }
  | { kind: 'knob'; knob: string; value: unknown }
  | { kind: 'need-lab' }
export function tryPlan(b: Extract<LessonBlock, { kind: 'try' }>, labId: string | null): TryPlan {
  const a = b.args ?? {}
  if (b.action === 'open_tab' && typeof a.page === 'string') return { kind: 'navigate', to: a.page }
  if (!labId) return { kind: 'need-lab' }
  switch (b.action) {
    case 'open_file': {
      const qs = new URLSearchParams({ path: String(a.path ?? '') })
      if (a.line !== undefined && a.line !== null) qs.set('line', String(a.line))
      return { kind: 'navigate', to: `/labs/${labId}/files?${qs.toString()}` }
    }
    case 'open_tab': return { kind: 'navigate', to: `/labs/${labId}/${String(a.tab ?? 'overview')}` }
    case 'run_job': {
      const ep = b.endpoint ?? { method: 'POST', path: `/api/labs/{lab_id}/${String(a.job ?? '').replace(/^lab\./, '')}` }
      return { kind: 'request', method: ep.method, path: ep.path.replace('{lab_id}', labId).replace(/^\/api/, '') }
    }
    case 'set_knob': return { kind: 'knob', knob: String(a.knob), value: a.value }
  }
}
/** set_knob: object values are merged into the current object knob, anything else replaces it. */
export function mergeKnob(knobs: Record<string, unknown>, knob: string, value: unknown): Record<string, unknown> {
  const cur = knobs[knob]
  const merged = value && typeof value === 'object' && !Array.isArray(value) && cur && typeof cur === 'object' && !Array.isArray(cur)
    ? { ...(cur as object), ...(value as object) } : value
  return { ...knobs, [knob]: merged }
}

// ---------------------------------------------------------------- tours
export interface TourState { id: string; step: number }
export type TourAction = { type: 'start'; id: string } | { type: 'next'; count: number } | { type: 'prev' } | { type: 'goto'; step: number; count: number } | { type: 'end' }
/** Step engine: `next` past the last step (count = steps in the tour) ends the tour. */
export function tourReducer(s: TourState | null, a: TourAction): TourState | null {
  if (a.type === 'end') return null
  if (a.type === 'start') return { id: a.id, step: 0 }
  if (!s) return s
  if (a.type === 'next') return s.step + 1 >= a.count ? null : { ...s, step: s.step + 1 }
  if (a.type === 'prev') return { ...s, step: Math.max(0, s.step - 1) }
  return { ...s, step: Math.min(Math.max(0, a.step), a.count - 1) }
}
export function resolveTourRoute(route: string, labId: string | null): string | null {
  if (!route.includes(':id')) return route
  return labId ? route.replace(':id', labId) : null
}
/** data-tour name of a step selector ('[data-tour="x"]' → x) */
export const tourName = (selector: string) => /data-tour="([^"]+)"/.exec(selector)?.[1] ?? selector
/** Where a step's element lives when it is not on the current page (tours.yml has one route per tour). */
export const TOUR_ROUTE_HINTS: Record<string, string> = {
  'new-lab': '/labs', 'template-card': '/labs/new', 'knobs-form': '/labs/new',
  'build-button': '/labs/:id', 'files-tab': '/labs/:id', 'deploy-button': '/labs/:id', 'validate-button': '/labs/:id/validation',
  'validation-totals': '/labs/:id/validation', 'validation-table': '/labs/:id/validation', 'report-download': '/labs/:id/validation',
  'topology-graph': '/labs/:id/topology', 'edit-mode': '/labs/:id/topology', 'add-endpoint': '/labs/:id/topology', 'add-test-host': '/labs/:id/topology', 'node-inspector': '/labs/:id/topology',
  'cv-targets': '/cloudvision', 'cv-knob': '/labs/:id/settings', 'cv-onboard': '/labs/:id/cloudvision', 'cv-deploy': '/labs/:id/cloudvision',
  'add-host': '/hosts', 'generate-key': '/hosts', 'probe-button': '/hosts', 'bootstrap-button': '/hosts', 'capabilities': '/hosts',
}
export function tourStepCount(tour: Tour | undefined) { return tour?.steps.length ?? 0 }
