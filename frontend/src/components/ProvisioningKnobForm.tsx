import type { ProvisioningKnob } from '../api/types'
import { isIpv4, normalizeProvisioning } from '../lib/provisioning'
import { Field } from './ui'

/** Structured editor for the `provisioning` object knob (CONTRACT-4 A). Emits `{mode, ztp_source, ztp_server_ip}`. */
export function ProvisioningKnobForm({ value, onChange, label, help, error, disabled }: {
  value: unknown
  onChange: (v: ProvisioningKnob) => void
  label?: React.ReactNode
  help?: string | null
  error?: string | null
  disabled?: boolean
}) {
  const p = normalizeProvisioning(value)
  const set = (patch: Partial<ProvisioningKnob>) => onChange({ ...p, ...patch })
  const ztp = p.mode === 'ztp'
  const ip = p.ztp_server_ip ?? ''
  const ipErr = ip && !isIpv4(ip) ? 'expected a.b.c.d' : null
  return (
    <fieldset className="rounded border border-zinc-200 p-3 dark:border-zinc-800" data-testid="provisioning-knob-form" disabled={disabled}>
      <legend className="label px-1">{label ?? 'Provisioning'}</legend>
      <div className="grid gap-3 sm:grid-cols-3">
        <Field label="Mode">
          <select className="input" name="provisioning.mode" value={p.mode} onChange={(e) => set({ mode: e.target.value as ProvisioningKnob['mode'] })}>
            <option value="startup-config">startup-config (containerlab mounts the intended config)</option>
            <option value="ztp">ZTP (DHCP + HTTP bootfile)</option>
          </select>
        </Field>
        <Field label="ZTP source" help={ztp ? (p.ztp_source === 'cloudvision' ? "bootfile runs the CloudVision target's ZTP script (or writes the onboarding token)" : 'bootfile = the intended <node>.cfg') : undefined}>
          <select className="input" name="provisioning.ztp_source" value={p.ztp_source} disabled={!ztp} onChange={(e) => set({ ztp_source: e.target.value as ProvisioningKnob['ztp_source'] })}>
            <option value="intended">intended config</option>
            <option value="cloudvision">CloudVision bootstrap</option>
          </select>
        </Field>
        <Field label="ZTP server IP" help="empty = highest free IP of the management subnet" error={ipErr}>
          <input className="input mono" name="provisioning.ztp_server_ip" value={ip} disabled={!ztp} placeholder="auto" spellCheck={false}
            onChange={(e) => set({ ztp_server_ip: e.target.value.trim() || null })} />
        </Field>
      </div>
      {help && <span className="mt-2 block text-[11px] text-zinc-500 dark:text-zinc-400">{help}</span>}
      {error && <span className="mt-0.5 block text-[11px] text-red-600 dark:text-red-400">{error}</span>}
    </fieldset>
  )
}
