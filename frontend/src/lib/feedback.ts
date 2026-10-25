import type { FeedbackIn, FeedbackKind, Meta } from '../api/types'
import { shortSha } from './whatsNew'

export interface FeedbackForm { kind: FeedbackKind; title: string; body: string; page: string; include_diagnostics: boolean }

export function feedbackPayload(f: FeedbackForm): FeedbackIn {
  return { kind: f.kind, title: f.title.trim(), body: f.body.trim(), page: f.page.trim(), include_diagnostics: f.include_diagnostics }
}

export function feedbackErrors(f: FeedbackForm): string[] {
  const e: string[] = []
  if (f.title.trim().length < 3) e.push('title: at least 3 characters')
  if (!f.body.trim()) e.push('description is required')
  return e
}

/** What the server will attach when "include diagnostics" is ticked (client-side preview; the server collects the rest). */
export function diagnosticsPreview(meta: Meta | undefined, userAgent: string, page: string): Record<string, unknown> {
  return {
    version: meta?.version ?? null,
    git_sha: shortSha(meta?.git_sha) ?? null,
    build_date: meta?.build_date ?? null,
    browser: userAgent,
    page,
    recent_failed_jobs: '(last 5: type, error, first log lines — secrets masked; collected server-side)',
    host_capabilities: '(summary per host; collected server-side)',
  }
}

/** 0.10.0: a `mailto:` link that opens the user's mail client with the page context prefilled (works without SMTP). */
export function feedbackMailto(to: string, f: Pick<FeedbackForm, 'kind' | 'title' | 'body' | 'page'>,
  ctx: { version?: string | null; user?: string | null; userAgent?: string; href?: string; context?: string[] }): string {
  const subject = `[avdlab] ${f.kind}: ${f.title.trim() || f.page}`
  const lines = [
    f.body.trim() || (f.kind === 'bug' ? 'What happened:\n\nWhat I expected:\n\nSteps to reproduce:\n' : ''),
    '',
    '--- context (collected automatically) ---',
    `page: ${f.page}`,
    ...(ctx.href ? [`url: ${ctx.href}`] : []),
    ...(ctx.context ?? []).map((c) => `where: ${c}`),
    `version: ${ctx.version ?? '?'}`,
    ...(ctx.user ? [`user: ${ctx.user}`] : []),
    `browser: ${ctx.userAgent ?? ''}`,
    `time: ${new Date().toISOString()}`,
  ]
  return `mailto:${encodeURIComponent(to)}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(lines.join('\n'))}`
}

