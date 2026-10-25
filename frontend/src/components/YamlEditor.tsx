import CodeMirror from '@uiw/react-codemirror'
import { yaml } from '@codemirror/lang-yaml'
import { EditorView } from '@codemirror/view'
import { useEffect, useMemo, useRef, useState } from 'react'

const base = [yaml(), EditorView.lineWrapping]

function usePrefersDark(): boolean {
  const [dark, setDark] = useState(() => typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-color-scheme: dark)').matches)
  useEffect(() => {
    const mq = window.matchMedia?.('(prefers-color-scheme: dark)')
    if (!mq) return
    const fn = (e: MediaQueryListEvent) => setDark(e.matches)
    mq.addEventListener?.('change', fn)
    return () => mq.removeEventListener?.('change', fn)
  }, [])
  return dark
}

/**
 * `onKeyHelp(lineNo)`: CONTRACT-6 §3 — Ctrl/Cmd-click a key, or the "?" button (key on the cursor line), opens the
 * key's schema entry.
 */
export function YamlEditor({ value, onChange, readOnly, height = '100%', id, onKeyHelp }: {
  value: string; onChange?: (v: string) => void; readOnly?: boolean; height?: string; id?: string; onKeyHelp?: (lineNo: number) => void
}) {
  const dark = usePrefersDark()
  const help = useRef(onKeyHelp)
  help.current = onKeyHelp
  const [cursorLine, setCursorLine] = useState(1)
  const withHelp = !!onKeyHelp
  const extensions = useMemo(() => (withHelp ? [
    ...base,
    EditorView.domEventHandlers({
      mousedown(e, view) {
        if (!(e.ctrlKey || e.metaKey) || !help.current) return false
        const pos = view.posAtCoords({ x: e.clientX, y: e.clientY })
        if (pos == null) return false
        help.current(view.state.doc.lineAt(pos).number)
        e.preventDefault()
        return true
      },
    }),
    EditorView.updateListener.of((u) => {
      if (u.selectionSet || u.docChanged) setCursorLine(u.state.doc.lineAt(u.state.selection.main.head).number)
    }),
  ] : base), [withHelp])
  return (
    <div id={id} className="relative overflow-hidden rounded border border-zinc-300 text-[12px] dark:border-zinc-700" data-testid="yaml-editor">
      {onKeyHelp && (
        <button type="button" className="absolute right-2 top-1 z-10 rounded-full border border-cyan-500 bg-white px-1.5 text-[11px] font-semibold text-cyan-700 shadow-sm hover:bg-cyan-50 dark:bg-zinc-900 dark:text-cyan-300"
          title="Schema of the key on the cursor line (or Ctrl/Cmd-click a key)" data-testid="yaml-key-help" onClick={() => onKeyHelp(cursorLine)}>?</button>
      )}
      <CodeMirror
        value={value}
        height={height}
        theme={dark ? 'dark' : 'light'}
        extensions={extensions}
        readOnly={readOnly}
        editable={!readOnly}
        basicSetup={{ foldGutter: true, lineNumbers: true, highlightActiveLine: !readOnly }}
        onChange={(v) => onChange?.(v)}
      />
    </div>
  )
}
