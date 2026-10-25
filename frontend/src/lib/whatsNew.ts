import type { Meta } from '../api/types'

export const LAST_SEEN_KEY = 'lastSeenVersion'
export const LAST_SEEN_EVENT = 'avdlab:last-seen-version'

export function getLastSeen(): string | null {
  try { return localStorage.getItem(LAST_SEEN_KEY) } catch { return null }
}

/** Remember the version the user has seen the changelog for; notifies other components in this tab. */
export function markSeen(version: string) {
  try { localStorage.setItem(LAST_SEEN_KEY, version) } catch { /* storage unavailable */ }
  try { window.dispatchEvent(new CustomEvent(LAST_SEEN_EVENT, { detail: version })) } catch { /* no window */ }
}

/** Latest released version: the newest changelog entry, else the running version. */
export function latestVersion(meta: Meta | undefined | null): string | null {
  return meta?.changelog_latest?.version ?? meta?.version ?? null
}

/** "What's new" dot: the latest version differs from localStorage.lastSeenVersion. */
export function hasUnseen(meta: Meta | undefined | null, lastSeen: string | null): boolean {
  const v = latestVersion(meta)
  return !!v && v !== lastSeen
}

export function shortSha(sha: string | null | undefined): string | null {
  return sha ? sha.slice(0, 7) : null
}
