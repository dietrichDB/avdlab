import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { metaApi } from '../api/phase5'
import { LAST_SEEN_EVENT, LAST_SEEN_KEY, getLastSeen, hasUnseen, markSeen } from '../lib/whatsNew'

export function useMeta() {
  return useQuery({ queryKey: ['meta'], queryFn: metaApi.get, staleTime: 10 * 60_000 })
}

/** Tracks localStorage.lastSeenVersion across components/tabs. */
export function useLastSeen(): string | null {
  const [v, setV] = useState(getLastSeen)
  useEffect(() => {
    const on = () => setV(getLastSeen())
    const onStorage = (e: StorageEvent) => { if (e.key === LAST_SEEN_KEY) on() }
    window.addEventListener(LAST_SEEN_EVENT, on)
    window.addEventListener('storage', onStorage)
    return () => { window.removeEventListener(LAST_SEEN_EVENT, on); window.removeEventListener('storage', onStorage) }
  }, [])
  return v
}

export function useWhatsNew() {
  const meta = useMeta()
  const lastSeen = useLastSeen()
  return { meta, unseen: hasUnseen(meta.data, lastSeen), markSeen }
}
