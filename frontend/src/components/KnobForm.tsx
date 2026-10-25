import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import type { Knob, Violation } from '../api/types'
import { Field } from './ui'
import { YamlEditor } from './YamlEditor'
import { toYaml, parseYaml } from '../lib/yaml'
import { CloudVisionKnobForm } from './CloudVisionKnobForm'
import { DeviceTagsEditor } from './DeviceTagsEditor'
import { ProvisioningKnobForm } from './ProvisioningKnobForm'
import { ActKnobForm } from './ActKnobForm'
import { normalizeSegmentation } from '../lib/mss'
import { KnobInfo } from './learn/Term'

export type KnobValues = Record<string, unknown>

export function defaultKnobValues(knobs: Knob[]): KnobValues {
  const out: KnobValues = {}
  for (const k of knobs) out[k.key] = k.default ?? (k.type === 'int' ? 0 : k.type === 'bool' ? false : k.type === 'object' ? {} : k.type === 'yaml' ? [] : '')
  return out
}

const CIDR_RE = /^(\d{1,3}\.){3}\d{1,3}\/\d{1,2}$/
const IPV4_RE = /^(\d{1,3}\.){3}\d{1,3}$/

/** Client-side validation mirroring the knob types (the server is the authority). */
export function validateKnob(k: Knob, v: unknown): string | null {
  switch (k.type) {
    case 'int': {
      if (v === '' || v === null || v === undefined || Number.isNaN(Number(v))) return 'must be an integer'
      const n = Number(v)
      if (!Number.isInteger(n)) return 'must be an integer'
      if (k.min != null && n < k.min) return `min ${k.min}`
      if (k.max != null && n > k.max) return `max ${k.max}`
      return null
    }
    case 'cidr':
      return typeof v === 'string' && CIDR_RE.test(v) ? null : 'expected a.b.c.d/nn'
    case 'ipv4':
      return typeof v === 'string' && IPV4_RE.test(v) ? null : 'expected a.b.c.d'
    default:
      return null
  }
}

/** Map server violations (path like "knobs.spine_count" / "spine_count" / loc arrays) onto knob keys. */
export function violationsByKnob(violations: Violation[], knobs: Knob[]): Record<string, string[]> {
  const out: Record<string, string[]> = {}
  for (const v of violations) {
    const p = v.path ?? ''
    const hit = knobs.find((k) => p === k.key || p.endsWith(`.${k.key}`) || p.includes(`${k.key}.`) || p.includes(`${k.key}[`) || v.message.includes(k.key))
    const key = hit?.key ?? '_'
    ;(out[key] ??= []).push(v.message)
  }
  return out
}

export function KnobForm({ knobs, values, onChange, violations = [], customKeys = [], disabled, hostnames, segmentationHref, labTarget }: {
  knobs: Knob[]
  values: KnobValues
  onChange: (next: KnobValues) => void
  violations?: Violation[]
  /** knobs whose file was hand-edited — shown with a hint */
  customKeys?: string[]
  disabled?: boolean
  /** the lab's device hostnames (rows of the device_tags editor) */
  hostnames?: string[]
  /** link to the lab's MSS tab (the `segmentation` knob is edited there) */
  segmentationHref?: string
  /** clab | act: the `act` knob is only used for act labs */
  labTarget?: string
}) {
  const serverErrors = useMemo(() => violationsByKnob(violations, knobs), [violations, knobs])
  const set = (key: string, v: unknown) => onChange({ ...values, [key]: v })

  const simple = knobs.filter((k) => k.type !== 'yaml' && k.type !== 'object')
  const complex = knobs.filter((k) => k.type === 'yaml' || k.type === 'object')

  return (
    <div className="space-y-4" data-testid="knob-form" data-tour="knobs-form">
      {serverErrors._ && (
        <ul className="list-disc rounded border border-red-300 bg-red-50 px-6 py-2 text-xs text-red-800 dark:border-red-800 dark:bg-red-950/40 dark:text-red-200">
          {serverErrors._.map((m, i) => <li key={i}>{m}</li>)}
        </ul>
      )}
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3">
        {simple.map((k) => (
          <KnobField key={k.key} knob={k} value={values[k.key]} onChange={(v) => set(k.key, v)}
            errors={serverErrors[k.key]} custom={customKeys.includes(k.key)} disabled={disabled} />
        ))}
      </div>
      {complex.map((k) => (
        <KnobField key={k.key} knob={k} value={values[k.key]} onChange={(v) => set(k.key, v)}
          errors={serverErrors[k.key]} custom={customKeys.includes(k.key)} disabled={disabled} hostnames={hostnames}
          segmentationHref={segmentationHref} labTarget={labTarget} />
      ))}
    </div>
  )
}

function KnobField({ knob: k, value, onChange, errors, custom, disabled, hostnames, segmentationHref, labTarget }: {
  knob: Knob; value: unknown; onChange: (v: unknown) => void; errors?: string[]; custom?: boolean; disabled?: boolean; hostnames?: string[]
  segmentationHref?: string; labTarget?: string
}) {
  const local = validateKnob(k, value)
  const err = errors?.join('; ') ?? local
  const label = (
    <span>
      {k.label ?? k.key}
      <span className="ml-1 font-normal text-zinc-400">{k.type}</span>
      <KnobInfo learn={k.learn} knobKey={k.key} />
      {custom && <span className="ml-1 rounded bg-amber-100 px-1 text-[10px] text-amber-800 dark:bg-amber-900/50 dark:text-amber-200" title="file edited by hand; saving this knob overwrites it">custom</span>}
    </span>
  )
  const inputId = `knob-${k.key}`
  switch (k.type) {
    case 'int':
      return (
        <Field label={label} help={k.help ?? undefined} error={err}>
          <input id={inputId} name={k.key} type="number" className="input" value={value === undefined || value === null ? '' : String(value)}
            min={k.min ?? undefined} max={k.max ?? undefined} step={1} disabled={disabled}
            onChange={(e) => onChange(e.target.value === '' ? '' : Number(e.target.value))} />
        </Field>
      )
    case 'bool':
      return (
        <Field label={label} help={k.help ?? undefined} error={err}>
          <input id={inputId} name={k.key} type="checkbox" className="h-4 w-4" checked={!!value} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
        </Field>
      )
    case 'secret':
      return <SecretInput id={inputId} knob={k} label={label} value={String(value ?? '')} onChange={onChange} error={err} disabled={disabled} />
    case 'cidr':
    case 'ipv4':
    case 'pool':
    case 'string':
      if (k.choices?.length) return (
        <Field label={label} help={k.help ?? undefined} error={err}>
          <select id={inputId} name={k.key} className="input mono" value={String(value ?? '')} disabled={disabled} onChange={(e) => onChange(e.target.value)}>
            {!k.choices.some((c) => String(c) === String(value ?? '')) && <option value={String(value ?? '')}>{String(value ?? '') || '—'}</option>}
            {k.choices.map((c) => <option key={String(c)} value={String(c)}>{String(c)}</option>)}
          </select>
        </Field>
      )
      return (
        <Field label={label} help={k.help ?? undefined} error={err}>
          <input id={inputId} name={k.key} type="text" className="input mono" value={String(value ?? '')} disabled={disabled}
            placeholder={k.type === 'cidr' ? '10.0.0.0/24' : k.type === 'pool' ? '10.0.0.0/24, 10.1.0.1-10.1.0.9' : k.type === 'ipv4' ? '10.0.0.1' : undefined}
            onChange={(e) => onChange(e.target.value)} spellCheck={false} />
        </Field>
      )
    case 'object':
      // CONTRACT-2c: the `cloudvision` knob gets a structured sub-form; other object knobs stay generic
      if (k.key === 'cloudvision') return <CloudVisionKnobForm value={value} onChange={onChange} label={label} help={k.help} error={err} disabled={disabled} />
      // CONTRACT-4: provisioning (ZTP) and act get structured sub-forms too
      if (k.key === 'provisioning') return <ProvisioningKnobForm value={value} onChange={onChange} label={label} help={k.help} error={err} disabled={disabled} />
      if (k.key === 'act') return <ActKnobForm value={value} onChange={onChange} label={label} help={k.help} error={err} disabled={disabled} active={labTarget === undefined || labTarget === 'act'} />
      return <ObjectInput id={inputId} knob={k} label={label} value={value} onChange={onChange} error={err} disabled={disabled} />
    case 'yaml':
      // CONTRACT-3b: per-device CloudVision tags get a hostname × label table
      if (k.key === 'device_tags') return <DeviceTagsEditor value={value} onChange={onChange} hostnames={hostnames} label={label} help={k.help} error={err} disabled={disabled} />
      // CONTRACT-4 C: segmentation is edited in the lab's MSS tab; here it is a read-only summary
      if (k.key === 'segmentation') return <SegmentationSummary label={label} value={value} href={segmentationHref} error={err} />
      return <YamlKnob id={inputId} knob={k} label={label} value={value} onChange={onChange} error={err} disabled={disabled} />
    default:
      return (
        <Field label={label} help={k.help ?? undefined} error={err}>
          <input id={inputId} name={k.key} type="text" className="input" value={String(value ?? '')} disabled={disabled} onChange={(e) => onChange(e.target.value)} />
        </Field>
      )
  }
}

function SecretInput({ id, knob, label, value, onChange, error, disabled }: {
  id: string; knob: Knob; label: React.ReactNode; value: string; onChange: (v: unknown) => void; error: string | null; disabled?: boolean
}) {
  const [show, setShow] = useState(false)
  return (
    <Field label={label} help={knob.help ?? undefined} error={error}>
      <div className="flex gap-1">
        <input id={id} name={knob.key} type={show ? 'text' : 'password'} className="input mono" value={value} disabled={disabled}
          autoComplete="off" onChange={(e) => onChange(e.target.value)} />
        <button type="button" className="btn-secondary btn-sm" onClick={() => setShow((s) => !s)}>{show ? 'Hide' : 'Show'}</button>
      </div>
    </Field>
  )
}

/** `object` knobs: a flat key→value table (pools etc.). Falls back to YAML when nested. */
function ObjectInput({ id, knob, label, value, onChange, error, disabled }: {
  id: string; knob: Knob; label: React.ReactNode; value: unknown; onChange: (v: unknown) => void; error: string | null; disabled?: boolean
}) {
  const obj = (value && typeof value === 'object' && !Array.isArray(value) ? value : {}) as Record<string, unknown>
  const flat = Object.values(obj).every((v) => v === null || ['string', 'number', 'boolean'].includes(typeof v))
  if (!flat) return <YamlKnob id={id} knob={knob} label={label} value={value} onChange={onChange} error={error} disabled={disabled} />
  const entries = Object.entries(obj)
  return (
    <Field label={label} help={knob.help ?? undefined} error={error}>
      <div className="grid grid-cols-1 gap-1 sm:grid-cols-2 lg:grid-cols-3">
        {entries.map(([k, v]) => (
          <div key={k} className="flex items-center gap-1">
            <span className="w-28 shrink-0 truncate text-xs text-zinc-500" title={k}>{k}</span>
            <input name={`${knob.key}.${k}`} className="input mono" value={v == null ? '' : String(v)} disabled={disabled}
              onChange={(e) => onChange({ ...obj, [k]: e.target.value })} spellCheck={false} />
          </div>
        ))}
        {entries.length === 0 && <span className="text-xs text-zinc-400">(empty)</span>}
      </div>
    </Field>
  )
}

/** `yaml` knobs: CodeMirror YAML editor; the knob value is the parsed document. */
function YamlKnob({ id, knob, label, value, onChange, error, disabled }: {
  id: string; knob: Knob; label: React.ReactNode; value: unknown; onChange: (v: unknown) => void; error: string | null; disabled?: boolean
}) {
  const [text, setText] = useState(() => toYaml(value))
  const [parseErr, setParseErr] = useState<string | null>(null)
  return (
    <Field label={label} help={knob.help ?? undefined} error={parseErr ?? error}>
      <YamlEditor id={id} value={text} readOnly={disabled} height="220px" onChange={(t) => {
        setText(t)
        const r = parseYaml(t)
        if (r.ok) { setParseErr(null); onChange(r.value) } else setParseErr(r.error)
      }} />
    </Field>
  )
}

function SegmentationSummary({ label, value, href, error }: { label: React.ReactNode; value: unknown; href?: string; error: string | null }) {
  const s = normalizeSegmentation(value)
  const segs = s.vrfs.reduce((n, v) => n + v.segments.length, 0)
  return (
    <div className="rounded border border-zinc-200 p-3 text-sm dark:border-zinc-800" data-testid="segmentation-summary">
      <span className="label">{label}</span>
      <div className="flex flex-wrap items-center gap-3 text-xs text-zinc-600 dark:text-zinc-300">
        <span>{s.enabled ? 'enabled' : 'disabled'}</span>
        <span>{s.applications.length} application(s)</span>
        <span>{s.policies.length} policy(ies)</span>
        <span>{s.vrfs.length} VRF(s) / {segs} segment(s)</span>
        {href ? <Link className="link" to={href}>edit in the MSS tab</Link> : <span className="text-zinc-400">edit in the lab&apos;s MSS tab after creating the lab</span>}
      </div>
      {error && <span className="mt-0.5 block text-[11px] text-red-600 dark:text-red-400">{error}</span>}
    </div>
  )
}
