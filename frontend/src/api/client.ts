import type { Violation } from './types'

export class ApiError extends Error {
  status: number
  detail: unknown
  constructor(status: number, detail: unknown, message?: string) {
    super(message ?? `HTTP ${status}`)
    this.status = status
    this.detail = detail
  }

  /** Normalise the many shapes a 422 may take into a flat violation list. */
  get violations(): Violation[] {
    return extractViolations(this.detail)
  }
}

/** Human-readable text for an error body: a string detail, `{message, hint}`, FastAPI `{detail: [...]}`, else `HTTP <status>`.
 *  (Before 0.10.0 every structured error showed as a bare "HTTP 502".) */
export function errorMessage(status: number, detail: unknown): string {
  if (typeof detail === 'string' && detail.trim()) return detail
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    const o = detail as Record<string, unknown>
    const parts = [o.message, o.error, o.msg].filter((x): x is string => typeof x === 'string' && !!x)
    if (parts.length) return `${parts[0]}${typeof o.hint === 'string' && o.hint ? ` — ${o.hint}` : ''}`
  }
  const v = extractViolations(detail)
  if (v.length && v[0].message && !v[0].message.startsWith('{')) return `HTTP ${status}: ${v.slice(0, 3).map((x) => (x.path ? `${x.path}: ` : '') + x.message).join('; ')}`
  return `HTTP ${status}`
}

export function extractViolations(detail: unknown): Violation[] {
  if (!detail) return []
  if (typeof detail === 'string') return [{ message: detail }]
  if (Array.isArray(detail)) {
    return detail.map((d) => {
      if (typeof d === 'string') return { message: d }
      const o = d as Record<string, unknown>
      const loc = Array.isArray(o.loc) ? o.loc.join('.') : (o.path as string | undefined)
      return { path: loc, message: String(o.msg ?? o.message ?? JSON.stringify(d)) }
    })
  }
  if (typeof detail === 'object') {
    const o = detail as Record<string, unknown>
    // a detail of {message, violations: []} must still surface the message (0.10.0: AI designer 502s looked empty)
    if (Array.isArray(o.violations) && o.violations.length) return extractViolations(o.violations)
    if (o.message) return [{ message: String(o.message), path: o.path as string | undefined }]
    if (o.detail) return extractViolations(o.detail)
  }
  return [{ message: JSON.stringify(detail) }]
}

let onUnauthorized: (() => void) | null = null
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn
}

interface RequestOptions {
  method?: string
  body?: unknown
  raw?: boolean
  headers?: Record<string, string>
  /** don't trigger the global 401 handler (used by /auth/me and login) */
  silent401?: boolean
}

export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json', ...(opts.headers ?? {}) }
  let body: BodyInit | undefined
  if (opts.body instanceof FormData) {
    body = opts.body
  } else if (opts.body !== undefined) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(opts.body)
  }
  const res = await fetch(`/api${path}`, { method: opts.method ?? 'GET', headers, body, credentials: 'include' })
  if (res.status === 401 && !opts.silent401) onUnauthorized?.()
  if (!res.ok) {
    let detail: unknown = null
    const text = await res.text()
    try {
      detail = JSON.parse(text)
      if (detail && typeof detail === 'object' && 'detail' in (detail as object)) detail = (detail as { detail: unknown }).detail
    } catch {
      detail = text
    }
    throw new ApiError(res.status, detail, errorMessage(res.status, detail))
  }
  if (opts.raw) return res as unknown as T
  if (res.status === 204) return undefined as T
  const ct = res.headers.get('content-type') ?? ''
  if (ct.includes('application/json')) return (await res.json()) as T
  return (await res.text()) as unknown as T
}

export const api = {
  get: <T>(path: string, opts?: RequestOptions) => request<T>(path, { ...opts, method: 'GET' }),
  post: <T>(path: string, body?: unknown, opts?: RequestOptions) => request<T>(path, { ...opts, method: 'POST', body }),
  put: <T>(path: string, body?: unknown, opts?: RequestOptions) => request<T>(path, { ...opts, method: 'PUT', body }),
  patch: <T>(path: string, body?: unknown, opts?: RequestOptions) => request<T>(path, { ...opts, method: 'PATCH', body }),
  delete: <T>(path: string, opts?: RequestOptions) => request<T>(path, { ...opts, method: 'DELETE' }),
}

export function wsUrl(path: string): string {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${window.location.host}/api${path}`
}
