import { useCallback, useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { learnApi } from '../api/phase6'
import { LEARN_MODE_EVENT, LEARN_MODE_KEY, getLearningMode, setLearningMode } from '../lib/learn'

/** Learning mode (persisted in localStorage, synced across components and tabs). */
export function useLearningMode(): [boolean, (on: boolean) => void] {
  const [on, setOn] = useState(getLearningMode)
  useEffect(() => {
    const sync = () => setOn(getLearningMode())
    const onStorage = (e: StorageEvent) => { if (e.key === LEARN_MODE_KEY) sync() }
    window.addEventListener(LEARN_MODE_EVENT, sync)
    window.addEventListener('storage', onStorage)
    return () => { window.removeEventListener(LEARN_MODE_EVENT, sync); window.removeEventListener('storage', onStorage) }
  }, [])
  const set = useCallback((v: boolean) => setLearningMode(v), [])
  return [on, set]
}

export function useGlossary(enabled = true) {
  return useQuery({ queryKey: ['learn', 'glossary'], queryFn: learnApi.glossary, staleTime: Infinity, enabled, retry: false })
}

export function useTours(enabled = true) {
  return useQuery({ queryKey: ['learn', 'tours'], queryFn: learnApi.tours, staleTime: Infinity, enabled, retry: false })
}
