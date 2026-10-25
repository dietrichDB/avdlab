import { useEffect, useState } from 'react'

/** tailwind darkMode: 'media' → follow prefers-color-scheme */
export function useDarkMode(): boolean {
  const q = typeof window !== 'undefined' && window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null
  const [dark, setDark] = useState(!!q?.matches)
  useEffect(() => {
    if (!q) return
    const fn = (e: MediaQueryListEvent) => setDark(e.matches)
    q.addEventListener?.('change', fn)
    return () => q.removeEventListener?.('change', fn)
  }, [q])
  return dark
}
