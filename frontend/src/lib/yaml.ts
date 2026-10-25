import YAML from 'yaml'

export function toYaml(v: unknown): string {
  if (v === undefined || v === null) return ''
  if (typeof v === 'string') return v
  try {
    return YAML.stringify(v, { lineWidth: 0 })
  } catch {
    return String(v)
  }
}

export type ParseResult = { ok: true; value: unknown } | { ok: false; error: string }

export function parseYaml(text: string): ParseResult {
  try {
    const doc = YAML.parseDocument(text)
    if (doc.errors.length) return { ok: false, error: doc.errors.map((e) => e.message.split('\n')[0]).join('; ') }
    return { ok: true, value: doc.toJS() }
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e) }
  }
}
