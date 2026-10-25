import { useUiMode, type UiMode } from '../lib/uiMode'
import { cls } from '../lib/format'

const OPTIONS: { id: UiMode; label: string; title: string }[] = [
  { id: 'beginner', label: 'Beginner', title: 'Essentials only, with hints on every page' },
  { id: 'advanced', label: 'Advanced', title: 'Everything: NetBox, MSS, Git, jobs tables, …' },
]

/** Top-bar segmented control "Beginner | Advanced" (persisted to localStorage['avdlab.uiMode']). */
export function ModeToggle({ className }: { className?: string }) {
  const { mode, setMode } = useUiMode()
  return (
    <div className={cls('segmented', className)} role="group" aria-label="Interface mode" data-testid="mode-toggle" data-mode={mode}>
      {OPTIONS.map((o) => (
        <button key={o.id} type="button" aria-pressed={mode === o.id} title={o.title} onClick={() => setMode(o.id)}
          data-testid={`mode-${o.id}`}>
          {o.label}
        </button>
      ))}
    </div>
  )
}
