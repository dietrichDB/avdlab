import { useState } from 'react'

const parse = (t: string) => t.split(/[,\s]+/).map((s) => s.trim()).filter(Boolean)

/** Comma/space-separated list input that keeps the typed text (trailing commas etc.) while emitting a clean array. */
export function ListInput({ value, onChange, placeholder, name, 'aria-label': ariaLabel, disabled }: {
  value: string[]; onChange: (v: string[]) => void; placeholder?: string; name?: string; 'aria-label'?: string; disabled?: boolean
}) {
  const [text, setText] = useState(value.join(', '))
  if (JSON.stringify(parse(text)) !== JSON.stringify(value)) setText(value.join(', '))
  return (
    <input className="input mono" name={name} aria-label={ariaLabel} value={text} placeholder={placeholder} spellCheck={false} disabled={disabled}
      onChange={(e) => { setText(e.target.value); onChange(parse(e.target.value)) }} />
  )
}
