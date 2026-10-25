import { useEffect, useRef } from 'react'
import { Terminal as XTerm } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { wsUrl } from '../api/client'

export type TermState = 'connecting' | 'open' | 'closed'

/** `path` is relative to /api and may already carry a query (e.g. ?shell=cli); cols/rows are appended. */
export function terminalUrl(path: string, cols: number, rows: number): string {
  return wsUrl(`${path}${path.includes('?') ? '&' : '?'}cols=${cols}&rows=${rows}`)
}

/**
 * xterm.js over the CONTRACT-3 §7 WebSocket: binary frames both ways, `{"resize": {cols, rows}}` text frames on fit.
 */
export default function Terminal({ path, onState }: { path: string; onState?: (s: TermState, info?: string) => void }) {
  const box = useRef<HTMLDivElement>(null)
  const stateCb = useRef(onState)
  stateCb.current = onState

  useEffect(() => {
    const el = box.current
    if (!el) return
    const term = new XTerm({
      cursorBlink: true, convertEol: false, fontSize: 13, scrollback: 5000,
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
      theme: { background: '#09090b', foreground: '#e4e4e7', cursor: '#60a5fa', selectionBackground: '#3f3f46' },
    })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(el)
    try { fit.fit() } catch { /* not laid out yet */ }
    const enc = new TextEncoder()
    const ws = new WebSocket(terminalUrl(path, term.cols || 120, term.rows || 32))
    ws.binaryType = 'arraybuffer'
    stateCb.current?.('connecting')
    const sendResize = (cols: number, rows: number) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ resize: { cols, rows } }))
    }
    ws.onopen = () => { stateCb.current?.('open'); sendResize(term.cols, term.rows); term.focus() }
    ws.onmessage = (ev: MessageEvent) => {
      const d = ev.data
      if (d instanceof ArrayBuffer) term.write(new Uint8Array(d))
      else if (typeof Blob !== 'undefined' && d instanceof Blob) d.arrayBuffer().then((b) => term.write(new Uint8Array(b)))
      else if (ArrayBuffer.isView(d)) term.write(new Uint8Array(d.buffer, d.byteOffset, d.byteLength))
      else term.write(String(d))
    }
    ws.onclose = (ev) => {
      const why = ev.code === 4401 ? 'not authenticated' : ev.reason || `code ${ev.code}`
      term.write(`\r\n\x1b[90m[connection closed: ${why}]\x1b[0m\r\n`)
      stateCb.current?.('closed', why)
    }
    const dataSub = term.onData((s) => { if (ws.readyState === WebSocket.OPEN) ws.send(enc.encode(s)) })
    const binSub = term.onBinary((s) => {
      if (ws.readyState !== WebSocket.OPEN) return
      const b = new Uint8Array(s.length)
      for (let i = 0; i < s.length; i++) b[i] = s.charCodeAt(i) & 0xff
      ws.send(b)
    })
    const resizeSub = term.onResize(({ cols, rows }) => sendResize(cols, rows))
    const ro = new ResizeObserver(() => { try { fit.fit() } catch { /* hidden */ } })
    ro.observe(el)
    return () => {
      ro.disconnect()
      dataSub.dispose(); binSub.dispose(); resizeSub.dispose()
      ws.onclose = null
      try { ws.close() } catch { /* ignore */ }
      term.dispose()
    }
  }, [path])

  return <div ref={box} className="h-full min-h-[18rem] w-full bg-zinc-950 p-1" data-testid="terminal" />
}
