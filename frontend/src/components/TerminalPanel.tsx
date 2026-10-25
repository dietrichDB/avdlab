import { lazy, Suspense, useState } from 'react'
import type { TermState } from './Terminal'
import { Spinner } from './ui'
import { cls } from '../lib/format'

const Terminal = lazy(() => import('./Terminal'))

/**
 * Docked terminal panel. `shells` enables the CLI/bash switch (node terminals); switching reconnects.
 */
export function TerminalPanel({ title, path, shells, defaultShell, onClose }: {
  title: string
  /** api path without cols/rows; for node terminals a function of the shell */
  path: string | ((shell: 'cli' | 'bash') => string)
  shells?: ('cli' | 'bash')[]
  defaultShell?: 'cli' | 'bash'
  onClose: () => void
}) {
  const [shell, setShell] = useState<'cli' | 'bash'>(defaultShell ?? shells?.[0] ?? 'cli')
  const [gen, setGen] = useState(0)
  const [state, setState] = useState<{ s: TermState; info?: string }>({ s: 'connecting' })
  const [tall, setTall] = useState(false)
  const p = typeof path === 'function' ? path(shell) : path
  return (
    <div data-testid="terminal-panel" className={cls('fixed inset-x-0 bottom-0 z-30 flex flex-col border-t border-zinc-300 bg-zinc-900 shadow-2xl dark:border-zinc-700', tall ? 'h-[85vh]' : 'h-[45vh]')}>
      <div className="flex items-center gap-2 border-b border-zinc-700 px-3 py-1 text-xs text-zinc-200">
        <span className={cls('inline-block h-2 w-2 rounded-full', state.s === 'open' ? 'bg-emerald-400' : state.s === 'connecting' ? 'bg-amber-400' : 'bg-zinc-500')} title={state.info ?? state.s} />
        <span className="font-semibold">{title}</span>
        {shells && shells.length > 1 && (
          <div className="ml-2 flex overflow-hidden rounded border border-zinc-600" role="group" aria-label="shell">
            {shells.map((s) => (
              <button key={s} type="button" aria-pressed={s === shell} onClick={() => setShell(s)}
                className={cls('px-2 py-0.5', s === shell ? 'bg-blue-600 text-white' : 'hover:bg-zinc-800')}>{s === 'cli' ? 'CLI' : 'bash'}</button>
            ))}
          </div>
        )}
        <span className="text-zinc-400">{state.s}{state.s === 'closed' && state.info ? `: ${state.info}` : ''}</span>
        <span className="flex-1" />
        <button type="button" className="rounded px-2 py-0.5 hover:bg-zinc-800" onClick={() => setGen((g) => g + 1)}>Reconnect</button>
        <button type="button" className="rounded px-2 py-0.5 hover:bg-zinc-800" onClick={() => setTall((t) => !t)}>{tall ? 'Shrink' : 'Expand'}</button>
        <button type="button" className="rounded px-2 py-0.5 hover:bg-zinc-800" onClick={onClose} aria-label="Close terminal">Close</button>
      </div>
      <div className="min-h-0 flex-1">
        <Suspense fallback={<Spinner label="Loading terminal…" />}>
          <Terminal key={`${p}#${gen}`} path={p} onState={(s, info) => setState({ s, info })} />
        </Suspense>
      </div>
    </div>
  )
}
