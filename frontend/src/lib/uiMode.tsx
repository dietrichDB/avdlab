import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'

/**
 * CONTRACT-8 B: beginner / advanced UI mode, persisted in localStorage['avdlab.uiMode'] (default beginner).
 * Beginner hides advanced nav items (NetBox, Devcontainer, Jobs — still reachable by URL), advanced lab tabs and the
 * dashboard tables, and shows <Hint> callouts. Advanced shows everything.
 */
export type UiMode = 'beginner' | 'advanced'
export const UI_MODE_KEY = 'avdlab.uiMode'
/** same-tab broadcast so components outside the provider (and other providers) stay in sync */
export const UI_MODE_EVENT = 'avdlab:ui-mode'

export function getUiMode(): UiMode {
  try {
    return localStorage.getItem(UI_MODE_KEY) === 'advanced' ? 'advanced' : 'beginner'
  } catch {
    return 'beginner'
  }
}

export function setUiModeStored(m: UiMode) {
  try { localStorage.setItem(UI_MODE_KEY, m) } catch { /* storage unavailable: in-memory only */ }
  window.dispatchEvent(new CustomEvent(UI_MODE_EVENT, { detail: m }))
}

export interface UiModeState {
  mode: UiMode
  beginner: boolean
  advanced: boolean
  setMode: (m: UiMode) => void
  toggle: () => void
}

const Ctx = createContext<UiModeState | null>(null)

function useStoredMode(): UiModeState {
  const [mode, setState] = useState<UiMode>(getUiMode)
  useEffect(() => {
    const sync = () => setState(getUiMode())
    const onStorage = (e: StorageEvent) => { if (e.key === UI_MODE_KEY || e.key === null) sync() }
    window.addEventListener(UI_MODE_EVENT, sync)
    window.addEventListener('storage', onStorage)
    return () => { window.removeEventListener(UI_MODE_EVENT, sync); window.removeEventListener('storage', onStorage) }
  }, [])
  const setMode = useCallback((m: UiMode) => { setState(m); setUiModeStored(m) }, [])
  return useMemo(() => ({
    mode, beginner: mode === 'beginner', advanced: mode === 'advanced', setMode,
    toggle: () => setMode(mode === 'beginner' ? 'advanced' : 'beginner'),
  }), [mode, setMode])
}

export function UiModeProvider({ children }: { children: ReactNode }) {
  const state = useStoredMode()
  return <Ctx.Provider value={state}>{children}</Ctx.Provider>
}

/** Works with or without a provider (pages rendered standalone in tests read localStorage directly). */
export function useUiMode(): UiModeState {
  const ctx = useContext(Ctx)
  const local = useStoredMode()
  return ctx ?? local
}

/** Render children only in the given mode. */
export function OnlyIn({ mode, children }: { mode: UiMode; children: ReactNode }) {
  return useUiMode().mode === mode ? <>{children}</> : null
}
