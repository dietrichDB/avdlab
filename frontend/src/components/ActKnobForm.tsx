import { useState } from 'react'
import type { ActKnob } from '../api/types'
import { normalizeAct } from '../lib/act'
import { isIpv4 } from '../lib/provisioning'
import { Field } from './ui'

/** Structured editor for the `act` object knob (CONTRACT-4 D). */
export function ActKnobForm({ value, onChange, label, help, error, disabled, active = true }: {
  value: unknown
  onChange: (v: ActKnob) => void
  label?: React.ReactNode
  help?: string | null
  error?: string | null
  disabled?: boolean
  /** false when the lab target is clab: the knob is kept but unused */
  active?: boolean
}) {
  const a = normalizeAct(value)
  const [show, setShow] = useState(false)
  const set = (patch: Partial<ActKnob>) => onChange({ ...a, ...patch })
  const ipErr = a.tools_server_ip && !isIpv4(a.tools_server_ip) ? 'expected a.b.c.d' : null
  return (
    <fieldset className="rounded border border-zinc-200 p-3 dark:border-zinc-800" data-testid="act-knob-form" disabled={disabled}>
      <legend className="label px-1">{label ?? 'Arista Cloud Test'}</legend>
      {!active && <div className="mb-2 text-[11px] text-zinc-500">Only used when the lab target is <span className="mono">act</span>.</div>}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <Field label="vEOS version"><input className="input mono" name="act.os_version" value={a.os_version} onChange={(e) => set({ os_version: e.target.value })} /></Field>
        <Field label="Node username"><input className="input mono" name="act.username" value={a.username} onChange={(e) => set({ username: e.target.value })} /></Field>
        <Field label="Node password">
          <div className="flex gap-1">
            <input className="input mono" name="act.password" type={show ? 'text' : 'password'} autoComplete="off" value={a.password} onChange={(e) => set({ password: e.target.value })} />
            <button type="button" className="btn-secondary btn-sm" onClick={() => setShow((s) => !s)}>{show ? 'Hide' : 'Show'}</button>
          </div>
        </Field>
        <Field label="Tools server IP" help="mgmt subnet; empty = highest free" error={ipErr}>
          <input className="input mono" name="act.tools_server_ip" value={a.tools_server_ip} placeholder="auto" spellCheck={false} onChange={(e) => set({ tools_server_ip: e.target.value.trim() })} />
        </Field>
        <label className="flex items-center gap-2 self-end pb-2 text-sm">
          <input type="checkbox" name="act.internet_access" checked={a.internet_access} onChange={(e) => set({ internet_access: e.target.checked })} />
          <span>internet access</span>
        </label>
      </div>
      {help && <span className="mt-2 block text-[11px] text-zinc-500 dark:text-zinc-400">{help}</span>}
      {error && <span className="mt-0.5 block text-[11px] text-red-600 dark:text-red-400">{error}</span>}
    </fieldset>
  )
}
