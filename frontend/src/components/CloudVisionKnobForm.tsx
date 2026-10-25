import { useState } from 'react'
import { CVAAS_REGIONS, normalizeCvKnob, type CVMode, type CloudVisionKnob } from '../lib/cloudvision'
import { Field } from './ui'

type Row = [string, string]

const tagsOf = (rows: Row[]) => Object.fromEntries(rows.filter(([k]) => k.trim()).map(([k, v]) => [k.trim(), v]))
const serversOf = (rows: string[]) => rows.map((s) => s.trim()).filter(Boolean)

/**
 * Structured editor for the `cloudvision` object knob. Emits the full knob object
 * `{mode, region, servers, disable_aaa, topology_hints, device_tags}` on every change.
 */
export function CloudVisionKnobForm({ value, onChange, label, help, error, disabled }: {
  value: unknown
  onChange: (v: CloudVisionKnob) => void
  label?: React.ReactNode
  help?: string | null
  error?: string | null
  disabled?: boolean
}) {
  const cv = normalizeCvKnob(value)
  // local row state so half-typed/empty rows survive; resynced when the value changes from outside
  const [tagRows, setTagRows] = useState<Row[]>(() => Object.entries(cv.device_tags))
  const [serverRows, setServerRows] = useState<string[]>(() => cv.servers)
  if (JSON.stringify(tagsOf(tagRows)) !== JSON.stringify(cv.device_tags)) setTagRows(Object.entries(cv.device_tags))
  if (JSON.stringify(serversOf(serverRows)) !== JSON.stringify(cv.servers)) setServerRows(cv.servers)

  const set = (patch: Partial<CloudVisionKnob>) => onChange({ ...cv, ...patch })
  const setTags = (rows: Row[]) => { setTagRows(rows); set({ device_tags: tagsOf(rows) }) }
  const setServers = (rows: string[]) => { setServerRows(rows); set({ servers: serversOf(rows) }) }
  const off = cv.mode === 'none'
  const regionKnown = (CVAAS_REGIONS as readonly string[]).includes(cv.region)

  return (
    <fieldset className="rounded border border-zinc-200 p-3 dark:border-zinc-800" data-testid="cv-knob-form" data-tour="cv-knob" disabled={disabled}>
      <legend className="label px-1">{label ?? 'CloudVision'}</legend>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Field label="Mode">
          <select className="input" name="cloudvision.mode" value={cv.mode} onChange={(e) => set({ mode: e.target.value as CVMode })}>
            <option value="none">none (no TerminAttr)</option>
            <option value="cvaas">CVaaS</option>
            <option value="onprem">on-prem CloudVision</option>
          </select>
        </Field>
        {cv.mode === 'cvaas' && (
          <Field label="CVaaS region" help="auto = resolve from the onboarding token">
            <select className="input" name="cloudvision.region" value={cv.region} onChange={(e) => set({ region: e.target.value })}>
              {!regionKnown && <option value={cv.region}>{cv.region}</option>}
              {CVAAS_REGIONS.map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
          </Field>
        )}
        <label className={`flex items-center gap-2 self-end pb-2 text-sm ${off ? 'opacity-50' : ''}`}>
          <input type="checkbox" name="cloudvision.disable_aaa" checked={cv.disable_aaa} disabled={off} onChange={(e) => set({ disable_aaa: e.target.checked })} />
          <span>disable AAA <span className="mono text-xs text-zinc-500">-disableaaa</span></span>
        </label>
        <label className={`flex items-center gap-2 self-end pb-2 text-sm ${off ? 'opacity-50' : ''}`}>
          <input type="checkbox" name="cloudvision.topology_hints" checked={cv.topology_hints} disabled={off} onChange={(e) => set({ topology_hints: e.target.checked })} />
          <span>topology hints</span>
        </label>
      </div>

      {cv.mode === 'onprem' && (
        <div className="mt-3">
          <span className="label">CVP servers <span className="font-normal text-zinc-400">(IP / hostname, port 9910)</span></span>
          <div className="space-y-1" data-testid="cv-servers">
            {serverRows.map((s, i) => (
              <div key={i} className="flex gap-1">
                <input className="input mono" name={`cloudvision.servers.${i}`} value={s} placeholder="192.0.2.10" spellCheck={false}
                  onChange={(e) => setServers(serverRows.map((x, j) => (j === i ? e.target.value : x)))} />
                <button type="button" className="btn-secondary btn-sm" aria-label={`remove server ${i + 1}`} onClick={() => setServers(serverRows.filter((_, j) => j !== i))}>×</button>
              </div>
            ))}
            <button type="button" className="btn-secondary btn-sm" onClick={() => setServerRows([...serverRows, ''])}>Add server</button>
            {serversOf(serverRows).length === 0 && <div className="text-[11px] text-amber-600">on-prem mode needs at least one server</div>}
          </div>
        </div>
      )}

      {!off && (
        <div className="mt-3">
          <span className="label">Device tags <span className="font-normal text-zinc-400">(applied to every device)</span></span>
          <div className="space-y-1" data-testid="cv-tags">
            {tagRows.map(([k, v], i) => (
              <div key={i} className="flex gap-1">
                <input className="input mono" name={`cloudvision.device_tags.${i}.name`} value={k} placeholder="label" spellCheck={false}
                  onChange={(e) => setTags(tagRows.map((r, j) => (j === i ? [e.target.value, r[1]] : r)))} />
                <input className="input mono" name={`cloudvision.device_tags.${i}.value`} value={v} placeholder="value" spellCheck={false}
                  onChange={(e) => setTags(tagRows.map((r, j) => (j === i ? [r[0], e.target.value] : r)))} />
                <button type="button" className="btn-secondary btn-sm" aria-label={`remove tag ${i + 1}`} onClick={() => setTags(tagRows.filter((_, j) => j !== i))}>×</button>
              </div>
            ))}
            <button type="button" className="btn-secondary btn-sm" onClick={() => setTagRows([...tagRows, ['', '']])}>Add tag</button>
          </div>
        </div>
      )}
      {help && <span className="mt-2 block text-[11px] text-zinc-500 dark:text-zinc-400">{help}</span>}
      {error && <span className="mt-0.5 block text-[11px] text-red-600 dark:text-red-400">{error}</span>}
    </fieldset>
  )
}
