import type { CVTarget, LabState } from '../api/types'

export type CVMode = 'none' | 'cvaas' | 'onprem'

/** The `cloudvision` template knob (CONTRACT-2c). */
export interface CloudVisionKnob {
  mode: CVMode
  region: string
  servers: string[]
  disable_aaa: boolean
  topology_hints: boolean
  device_tags: Record<string, string>
}

export const CV_KNOB_DEFAULT: CloudVisionKnob = { mode: 'none', region: 'auto', servers: [], disable_aaa: true, topology_hints: true, device_tags: {} }

/** AVD 6.4 `cv_settings.cvaas.clusters[].region` valid values (schema_fragments/cv_settings.schema.yml) — must match backend CV_REGIONS */
export const CVAAS_REGIONS = [
  'auto', 'us-central1-a', 'us-central1-b', 'us-central1-c', 'us-4', 'apnortheast-1', 'euwest-2', 'eu-3', 'ausoutheast-1', 'na-northeast1-b', 'uk-1', 'india-1',
  'staging', 'dev', 'play',
] as const

/** CVaaS regional API servers (target.server presets) */
export const CVAAS_SERVERS: { value: string; label: string }[] = [
  { value: 'www.arista.io', label: 'www.arista.io (US central, default)' },
  { value: 'www.cv-prod-us-central1-b.arista.io', label: 'US central 1b' },
  { value: 'www.cv-prod-us-central1-c.arista.io', label: 'US central 1c' },
  { value: 'www.cv-prod-us-east4-a.arista.io', label: 'US east 4a' },
  { value: 'www.cv-prod-euwest-2.arista.io', label: 'Europe west 2' },
  { value: 'www.cv-prod-ap-northeast-1.arista.io', label: 'Japan' },
  { value: 'www.cv-prod-ap-southeast-2.arista.io', label: 'Australia' },
  { value: 'www.cv-prod-na-northeast1-b.arista.io', label: 'Canada' },
  { value: 'www.cv-prod-me-central2-b.arista.io', label: 'Middle East' },
]

const MODES: CVMode[] = ['none', 'cvaas', 'onprem']

/** Coerce whatever the lab stores into a complete, typed knob value. */
export function normalizeCvKnob(v: unknown): CloudVisionKnob {
  const o = (v && typeof v === 'object' && !Array.isArray(v) ? v : {}) as Record<string, unknown>
  const tags = o.device_tags
  let device_tags: Record<string, string> = {}
  if (Array.isArray(tags)) {
    // tolerate AVD's list form [{name, value}]
    for (const t of tags) if (t && typeof t === 'object' && 'name' in t) device_tags[String((t as { name: unknown }).name)] = String((t as { value?: unknown }).value ?? '')
  } else if (tags && typeof tags === 'object') {
    device_tags = Object.fromEntries(Object.entries(tags as Record<string, unknown>).map(([k, x]) => [k, x == null ? '' : String(x)]))
  }
  return {
    mode: MODES.includes(o.mode as CVMode) ? (o.mode as CVMode) : CV_KNOB_DEFAULT.mode,
    region: typeof o.region === 'string' && o.region ? o.region : CV_KNOB_DEFAULT.region,
    servers: Array.isArray(o.servers) ? o.servers.map(String) : typeof o.servers === 'string' && o.servers ? [o.servers] : [],
    disable_aaa: typeof o.disable_aaa === 'boolean' ? o.disable_aaa : CV_KNOB_DEFAULT.disable_aaa,
    topology_hints: typeof o.topology_hints === 'boolean' ? o.topology_hints : CV_KNOB_DEFAULT.topology_hints,
    device_tags,
  }
}

export function cvMode(knobs: Record<string, unknown> | undefined | null): CVMode {
  return normalizeCvKnob(knobs?.cloudvision).mode
}

export interface CvAction { enabled: boolean; reason?: string }

/** CloudVision tab enabling rules (CONTRACT-2c frontend). */
export function cvActions(state: LabState | string, target: CVTarget | null | undefined, mode: CVMode = 'cvaas'): { onboard: CvAction; deploy: CvAction } {
  const running = state === 'deployed' || state === 'validated'
  const built = ['built', 'deployed', 'validated', 'destroyed'].includes(state)
  const onboard: CvAction = !target ? { enabled: false, reason: 'bind a CloudVision target in Settings' }
    : mode === 'none' ? { enabled: false, reason: 'cloudvision.mode is none: no TerminAttr configured' }
    : !running ? { enabled: false, reason: `lab must be deployed (state: ${state})` }
    : !target.has_onboarding_token ? { enabled: false, reason: `target ${target.name} has no onboarding token` }
    : { enabled: true }
  const deploy: CvAction = !target ? { enabled: false, reason: 'bind a CloudVision target in Settings' }
    : !built ? { enabled: false, reason: `lab must be built (state: ${state})` }
    : !target.has_token ? { enabled: false, reason: `target ${target.name} has no service-account token` }
    : { enabled: true }
  return { onboard, deploy }
}

export function isExpired(iso: string | null | undefined, now = Date.now()): boolean {
  if (!iso) return false
  const t = Date.parse(iso)
  return !Number.isNaN(t) && t < now
}
