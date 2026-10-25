import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { imageArch, imagesApi, normArch } from '../api/images'
import { hostsApi } from '../api/hosts'
import type { Arch, Image } from '../api/types'
import { Field } from './ui'
import { fmtBytes } from '../lib/format'

const CUSTOM = '__custom__'

export interface ImageChoice {
  version: string
  arch: Arch
}

/** label shown in the dropdown: "cEOS 4.34.2F · x86_64 · 580 MB · on sandbox" */
export function imageLabel(img: Image, hostNames: Record<string, string> = {}): string {
  const where = img.present_on_hosts.map((h) => hostNames[h] ?? h)
  const parts = [`${img.fmt} ${img.version}`, imageArch(img)]
  if (img.size_bytes) parts.push(fmtBytes(img.size_bytes))
  parts.push(where.length ? `on ${where.join(', ')}` : img.local_path ? 'local store only' : 'not downloaded')
  return parts.join(' · ')
}

/** images that fit a lab host (arch must match when the host arch is known); the rest stay listed but flagged */
export function imageFits(img: Image, hostArch: Arch | null): boolean {
  return !hostArch || imageArch(img) === hostArch
}

interface Props {
  /** current version (free text or from an image) */
  version: string
  /** current arch knob (x86_64 | arm64) */
  arch: Arch
  onChange: (c: ImageChoice) => void
  /** chosen lab host — used to show which images are already present there and to warn on arch mismatch */
  hostId?: string
  label?: string
  help?: string
  disabled?: boolean
}

/**
 * cEOS image dropdown for New lab / Settings: lists the images already downloaded (Images page) instead of a free-text
 * version. Picking one sets both the version and the architecture knob; "Other…" falls back to a text field.
 */
export default function ImagePicker({ version, arch, onChange, hostId, label = 'cEOS image', help, disabled }: Props) {
  const images = useQuery({ queryKey: ['images'], queryFn: imagesApi.list })
  const hosts = useQuery({ queryKey: ['hosts'], queryFn: hostsApi.list })
  const caps = useQuery({ queryKey: ['hosts', hostId, 'caps'], queryFn: () => hostsApi.capabilities(hostId!), staleTime: 60_000, enabled: !!hostId })
  const hostArch = hostId ? normArch(caps.data?.capabilities?.os?.arch) : null
  const hostNames = Object.fromEntries((hosts.data ?? []).map((h) => [h.id, h.name]))

  const list = (images.data ?? []).filter((i) => i.fmt.toLowerCase().startsWith('ceos'))
  const [customMode, setCustomMode] = useState(false)
  const match = list.find((i) => i.version === version && imageArch(i) === arch)
  const selected = customMode ? CUSTOM : match ? match.id : version !== '' ? CUSTOM : ''

  const pick = (value: string) => {
    setCustomMode(value === CUSTOM)
    if (value === CUSTOM) return
    const img = list.find((i) => i.id === value)
    if (img) onChange({ version: img.version, arch: imageArch(img) })
    else onChange({ version: '', arch })
  }
  const mismatch = !customMode && match && hostArch && imageArch(match) !== hostArch
  const chosenOnHost = match && hostId ? match.present_on_hosts.includes(hostId) : null

  return (
    <Field
      label={label}
      help={
        help ?? (
          !list.length ? <>no images downloaded yet — fetch one on the <Link className="link" to="/images">Images</Link> page, or type a version</>
          : mismatch ? <span className="text-amber-600">this image is {imageArch(match!)} but the host is {hostArch} — deploy will refuse it</span>
          : chosenOnHost === false ? <span className="text-amber-600">not on this host yet — deploy pushes it first (or push it from Images)</span>
          : chosenOnHost ? <span className="text-emerald-600">present on the host</span>
          : 'images already downloaded from arista.com; the architecture knob follows the choice'
        )
      }
    >
      <div className="space-y-1">
        <select className="input" data-testid="image-picker" value={selected} disabled={disabled} onChange={(e) => pick(e.target.value)}>
          <option value="">— none —</option>
          {list.map((i) => (
            <option key={i.id} value={i.id}>
              {imageFits(i, hostArch) ? '' : '⚠ '}{imageLabel(i, hostNames)}
            </option>
          ))}
          <option value={CUSTOM}>Other… (type a version)</option>
        </select>
        {selected === CUSTOM && (
          <input className="input mono" data-testid="image-version" value={version} placeholder="4.34.2F" disabled={disabled}
            onChange={(e) => onChange({ version: e.target.value, arch })} />
        )}
      </div>
    </Field>
  )
}
