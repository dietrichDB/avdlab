import { cls } from '../lib/format'

/** Renders a unified diff with +/- colouring. Multiple files are split on `diff --git` / `--- a/` headers. */
export function DiffViewer({ diff, className }: { diff: string; className?: string }) {
  if (!diff.trim()) return <div className="text-sm text-zinc-500">No differences.</div>
  const lines = diff.split('\n')
  return (
    <pre className={cls('mono overflow-auto rounded border border-zinc-200 bg-zinc-50 p-2 dark:border-zinc-800 dark:bg-zinc-950', className)} data-testid="diff-viewer">
      {lines.map((l, i) => {
        let c = ''
        if (l.startsWith('diff --git') || l.startsWith('index ')) c = 'font-semibold text-zinc-700 dark:text-zinc-300 bg-zinc-200 dark:bg-zinc-800 block'
        else if (l.startsWith('+++') || l.startsWith('---')) c = 'text-zinc-500'
        else if (l.startsWith('@@')) c = 'text-purple-700 dark:text-purple-300'
        else if (l.startsWith('+')) c = 'bg-emerald-100 text-emerald-900 dark:bg-emerald-950/50 dark:text-emerald-200 block'
        else if (l.startsWith('-')) c = 'bg-red-100 text-red-900 dark:bg-red-950/50 dark:text-red-200 block'
        return <span key={i} className={cls('block', c)}>{l || ' '}</span>
      })}
    </pre>
  )
}
