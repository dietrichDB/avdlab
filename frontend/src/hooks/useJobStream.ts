import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { jobsApi, isTerminal } from '../api/jobs'
import { wsUrl } from '../api/client'
import type { Job, JobStreamFrame } from '../api/types'

export interface JobStream {
  lines: string[]
  done: boolean
  connected: boolean
  job: Job | undefined
}

/**
 * Streams `ws(s)://<host>/api/jobs/{id}/stream` frames ({line} / {done:true}) into a line buffer,
 * and polls GET /api/jobs/{id} for the status badge until the job reaches a terminal state.
 */
export function useJobStream(jobId: string | null): JobStream {
  const [lines, setLines] = useState<string[]>([])
  const [done, setDone] = useState(false)
  const [connected, setConnected] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)

  const jobQuery = useQuery({
    queryKey: ['job', jobId],
    queryFn: () => jobsApi.get(jobId!),
    enabled: !!jobId,
    refetchInterval: (q) => (isTerminal(q.state.data?.status) ? false : 2000),
  })

  useEffect(() => {
    setLines([])
    setDone(false)
    setConnected(false)
    if (!jobId) return
    let closed = false
    let ws: WebSocket
    try {
      ws = new WebSocket(wsUrl(`/jobs/${jobId}/stream`))
    } catch {
      // fall back to fetching the log once
      jobsApi.log(jobId).then((r) => setLines(r.log.split('\n'))).catch(() => undefined)
      return
    }
    wsRef.current = ws
    const buffer: string[] = []
    let flushTimer: ReturnType<typeof setTimeout> | null = null
    const flush = () => {
      flushTimer = null
      if (!buffer.length) return
      const chunk = buffer.splice(0, buffer.length)
      setLines((prev) => prev.concat(chunk))
    }
    ws.onopen = () => setConnected(true)
    ws.onmessage = (ev: MessageEvent) => {
      let frame: JobStreamFrame
      try {
        frame = JSON.parse(typeof ev.data === 'string' ? ev.data : String(ev.data))
      } catch {
        return
      }
      if ('line' in frame) {
        buffer.push(frame.line)
        if (!flushTimer) flushTimer = setTimeout(flush, 30)
      } else if ('done' in frame && frame.done) {
        flush()
        setDone(true)
        jobQuery.refetch()
      }
    }
    ws.onclose = () => {
      flush()
      setConnected(false)
      if (!closed) {
        // server closed after `done` — or session expired; refetch status either way
        jobQuery.refetch()
      }
    }
    ws.onerror = () => {
      // if the socket never opens, show whatever log exists
      jobsApi.log(jobId).then((r) => {
        if (r.log) setLines((prev) => (prev.length ? prev : r.log.split('\n')))
      }).catch(() => undefined)
    }
    return () => {
      closed = true
      if (flushTimer) clearTimeout(flushTimer)
      try { ws.close() } catch { /* ignore */ }
      wsRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId])

  return { lines, done, connected, job: jobQuery.data }
}
