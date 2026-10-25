import { useMemo, useState, type ReactNode } from 'react'
import { Field } from './ui'

/** Value of the `device_tags` knob: {hostname: {label: value}} (CONTRACT-3b §1). */
export type DeviceTags = Record<string, Record<string, string>>

export function asDeviceTags(v: unknown): DeviceTags {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return {}
  const out: DeviceTags = {}
  for (const [host, tags] of Object.entries(v as Record<string, unknown>)) {
    if (!tags || typeof tags !== 'object' || Array.isArray(tags)) continue
    const t: Record<string, string> = {}
    for (const [k, val] of Object.entries(tags as Record<string, unknown>)) if (val !== null && val !== undefined && String(val) !== '') t[k] = String(val)
    if (Object.keys(t).length) out[host] = t
  }
  return out
}

/** Set one cell; an empty value removes the tag, a host without tags is dropped. */
export function setTag(tags: DeviceTags, host: string, label: string, value: string): DeviceTags {
  const next: DeviceTags = { ...tags, [host]: { ...(tags[host] ?? {}) } }
  if (value === '') delete next[host][label]
  else next[host][label] = value
  if (!Object.keys(next[host]).length) delete next[host]
  return next
}

export function removeLabel(tags: DeviceTags, label: string): DeviceTags {
  const out: DeviceTags = {}
  for (const [h, t] of Object.entries(tags)) {
    const { [label]: _drop, ...rest } = t
    void _drop
    if (Object.keys(rest).length) out[h] = rest
  }
  return out
}

/**
 * Settings → "Device tags": a hostname × tag-label table backed by the `device_tags` knob. Rows are the lab's devices
 * (`hostnames`, from GET /labs/{id}/devices) plus any host already in the value; columns are the labels in use plus
 * the ones added with "Add tag column". Empty cells mean "no tag".
 */
export function DeviceTagsEditor({ value, onChange, hostnames = [], label, help, error, disabled }: {
  value: unknown; onChange: (v: DeviceTags) => void; hostnames?: string[]; label?: ReactNode; help?: string | null
  error?: string | null; disabled?: boolean
}) {
  const tags = useMemo(() => asDeviceTags(value), [value])
  const [extraCols, setExtraCols] = useState<string[]>([])
  const [extraHosts, setExtraHosts] = useState<string[]>([])
  const [newCol, setNewCol] = useState('')
  const [newHost, setNewHost] = useState('')

  const cols = useMemo(() => {
    const seen: string[] = []
    for (const t of Object.values(tags)) for (const k of Object.keys(t)) if (!seen.includes(k)) seen.push(k)
    for (const c of extraCols) if (!seen.includes(c)) seen.push(c)
    return seen
  }, [tags, extraCols])
  const hosts = useMemo(() => {
    const all = [...hostnames]
    for (const h of [...Object.keys(tags), ...extraHosts]) if (!all.includes(h)) all.push(h)
    return all
  }, [hostnames, tags, extraHosts])
  const known = new Set(hostnames)

  const addCol = () => {
    const c = newCol.trim()
    if (c && !cols.includes(c)) setExtraCols((x) => [...x, c])
    setNewCol('')
  }
  const addHost = () => {
    const h = newHost.trim()
    if (h && !hosts.includes(h)) setExtraHosts((x) => [...x, h])
    setNewHost('')
  }
  const dropCol = (c: string) => {
    setExtraCols((x) => x.filter((y) => y !== c))
    onChange(removeLabel(tags, c))
  }

  return (
    <Field label={label ?? 'Device tags'} help={help ?? undefined} error={error}>
      <div className="space-y-2" data-testid="device-tags-editor">
        <div className="flex flex-wrap items-center gap-2">
          <input className="input mono w-44 py-0.5 text-xs" placeholder="new tag label" aria-label="New tag label" value={newCol}
            disabled={disabled} onChange={(e) => setNewCol(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addCol() } }} />
          <button type="button" className="btn-secondary btn-sm" disabled={disabled || !newCol.trim()} onClick={addCol}>Add tag column</button>
          {hostnames.length === 0 && (
            <>
              <input className="input mono w-44 py-0.5 text-xs" placeholder="hostname" aria-label="New hostname" value={newHost}
                disabled={disabled} onChange={(e) => setNewHost(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addHost() } }} />
              <button type="button" className="btn-secondary btn-sm" disabled={disabled || !newHost.trim()} onClick={addHost}>Add device</button>
              <span className="text-xs text-zinc-500">build the lab to list its devices here</span>
            </>
          )}
        </div>
        {hosts.length > 0 && (
          <div className="overflow-x-auto">
            <table className="table text-xs">
              <thead>
                <tr>
                  <th>Hostname</th>
                  {cols.map((c) => (
                    <th key={c} className="mono">
                      {c}
                      {!disabled && <button type="button" className="ml-1 text-zinc-400 hover:text-red-600" aria-label={`Remove tag ${c}`} onClick={() => dropCol(c)}>×</button>}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {hosts.map((h) => (
                  <tr key={h}>
                    <td className={`mono ${hostnames.length && !known.has(h) ? 'text-red-600' : ''}`} title={hostnames.length && !known.has(h) ? 'not a device of this lab' : undefined}>{h}</td>
                    {cols.map((c) => (
                      <td key={c} className="p-0.5">
                        <input className="input mono py-0.5 text-xs" aria-label={`${h} ${c}`} value={tags[h]?.[c] ?? ''} disabled={disabled}
                          onChange={(e) => onChange(setTag(tags, h, c, e.target.value))} spellCheck={false} />
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
            {cols.length === 0 && <div className="px-1 text-xs text-zinc-400">No tags yet: add a tag column.</div>}
          </div>
        )}
      </div>
    </Field>
  )
}
