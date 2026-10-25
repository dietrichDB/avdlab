import type { GitAuthKind, GitConfigIn, GitStatus } from '../api/types'

export interface GitPanelState {
  configured: boolean
  canPush: boolean
  canPull: boolean
  /** "in sync" | "2 ahead" | "1 behind" | "2 ahead, 1 behind" | "not fetched" */
  syncLabel: string
  tone: 'ok' | 'warn' | 'error' | 'muted'
  lastSync: string | null
}

export function gitPanelState(s: GitStatus | undefined | null): GitPanelState {
  if (!s?.configured) return { configured: false, canPush: false, canPull: false, syncLabel: 'no remote', tone: 'muted', lastSync: null }
  const ahead = s.ahead ?? null
  const behind = s.behind ?? null
  let syncLabel: string
  if (ahead === null && behind === null) syncLabel = 'not fetched'
  else {
    const parts = [ahead ? `${ahead} ahead` : '', behind ? `${behind} behind` : ''].filter(Boolean)
    syncLabel = parts.length ? parts.join(', ') : 'in sync'
  }
  const needsAuth = s.auth_kind && s.auth_kind !== 'none' && !s.has_secret
  const tone: GitPanelState['tone'] = s.last_sync && !s.last_sync.ok ? 'error' : needsAuth || (behind ?? 0) > 0 ? 'warn' : 'ok'
  return {
    configured: true,
    canPush: !needsAuth,
    canPull: !needsAuth,
    syncLabel,
    tone,
    lastSync: s.last_sync ? `${s.last_sync.direction} ${s.last_sync.ok ? 'ok' : 'failed'}` : null,
  }
}

export interface GitForm { url: string; branch: string; auth_kind: GitAuthKind; token: string; secret_id: string }

export const gitFormFrom = (s?: GitStatus | null): GitForm => ({
  url: s?.url ?? '', branch: s?.branch ?? 'main', auth_kind: (s?.auth_kind as GitAuthKind) ?? 'none', token: '', secret_id: '',
})

/**
 * PUT body. The token is write-only: an empty token field keeps the stored secret (field omitted);
 * ssh_key sends the chosen secret_id; `none` clears both.
 */
export function gitConfigBody(f: GitForm): GitConfigIn {
  const base = { url: f.url.trim(), branch: f.branch.trim() || 'main', auth_kind: f.auth_kind }
  if (f.auth_kind === 'none') return { ...base, secret_id: null, token: null }
  if (f.auth_kind === 'token') return f.token ? { ...base, token: f.token, secret_id: null } : base
  return { ...base, secret_id: f.secret_id || null }
}

/** GitHub → x-access-token, GitLab → oauth2 (informational; the backend injects it) */
export const tokenUser = (url: string) => (/gitlab/i.test(url) ? 'oauth2' : 'x-access-token')
