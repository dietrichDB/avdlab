import { useEffect, useRef, useState } from 'react'
import { cls } from '../lib/format'

function lineClass(l: string): string {
  if (/\[FAIL\]|error|traceback|failed/i.test(l)) return 'text-red-400'
  if (/\[ok\]|success|succeeded|✓/i.test(l)) return 'text-emerald-400'
  if (/\[skip\]/i.test(l)) return 'text-zinc-500'
  if (/\[apply\]|warn/i.test(l)) return 'text-amber-300'
  return ''
}

export function LogPane({ lines, className, autoScroll = true }: { lines: string[]; className?: string; autoScroll?: boolean }) {
  const ref = useRef<HTMLDivElement>(null)
  const [stick, setStick] = useState(true)
  useEffect(() => {
    if (autoScroll && stick && ref.current) ref.current.scrollTop = ref.current.scrollHeight
  }, [lines, stick, autoScroll])
  return (
    <div
      ref={ref}
      data-testid="log-pane"
      onScroll={(e) => {
        const el = e.currentTarget
        setStick(el.scrollHeight - el.scrollTop - el.clientHeight < 24)
      }}
      className={cls('overflow-auto whitespace-pre-wrap break-words bg-zinc-950 p-2 font-mono text-[12px] leading-[1.35] text-zinc-200', className)}
    >
      {lines.length === 0 && <div className="text-zinc-500">(no output yet)</div>}
      {lines.map((l, i) => (
        <div key={i} className={lineClass(l)}>
          <span className="mr-2 select-none text-zinc-600">{String(i + 1).padStart(4, ' ')}</span>
          {l}
        </div>
      ))}
    </div>
  )
}
