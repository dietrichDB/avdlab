import type { ProvisioningKnob, ProvisioningMode, ZtpNode, ZtpSource } from '../api/types'

export const PROVISIONING_DEFAULT: ProvisioningKnob = { mode: 'startup-config', ztp_source: 'intended', ztp_server_ip: null }

/** Coerce whatever the lab stores into a complete `provisioning` knob value. */
export function normalizeProvisioning(v: unknown): ProvisioningKnob {
  const o = (v && typeof v === 'object' && !Array.isArray(v) ? v : {}) as Record<string, unknown>
  const mode: ProvisioningMode = o.mode === 'ztp' ? 'ztp' : 'startup-config'
  const ztp_source: ZtpSource = o.ztp_source === 'cloudvision' ? 'cloudvision' : 'intended'
  const ip = typeof o.ztp_server_ip === 'string' ? o.ztp_server_ip.trim() : ''
  return { mode, ztp_source, ztp_server_ip: ip || null }
}

export const provisioningMode = (knobs: Record<string, unknown> | null | undefined): ProvisioningMode =>
  normalizeProvisioning(knobs?.provisioning).mode

export type ZtpStepId = 'dhcp' | 'bootfile' | 'eapi'
/** done = observed; current = the next step the node is waiting for; pending = not reached yet */
export type ZtpStepState = 'done' | 'current' | 'pending'
export const ZTP_STEPS: { id: ZtpStepId; label: string }[] = [
  { id: 'dhcp', label: 'DHCP' },
  { id: 'bootfile', label: 'bootfile' },
  { id: 'eapi', label: 'eAPI' },
]

/** Per-node timeline DHCP → bootfile → eAPI. A later step observed implies the earlier ones happened. */
export function ztpSteps(n: ZtpNode): Record<ZtpStepId, ZtpStepState> {
  const flags = [n.lease, n.bootfile_fetched, n.eapi]
  const last = flags.lastIndexOf(true)
  const out = {} as Record<ZtpStepId, ZtpStepState>
  ZTP_STEPS.forEach((s, i) => { out[s.id] = i <= last ? 'done' : i === last + 1 ? 'current' : 'pending' })
  return out
}

export const ztpComplete = (n: ZtpNode) => n.eapi

const IPV4_RE = /^(\d{1,3}\.){3}\d{1,3}$/
export const isIpv4 = (s: string) => IPV4_RE.test(s) && s.split('.').every((o) => Number(o) <= 255)
