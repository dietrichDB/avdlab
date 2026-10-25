import type { MssAction, MssApplication, MssPolicy, MssProtocol, MssSegment, MssVrf, Segmentation } from '../api/types'

/** labs/mss.py constants */
export const BUILTIN_POLICIES: Record<string, MssAction> = { 'policy-forward-all': 'forward', 'policy-drop-all': 'drop' }
export const BUILTIN_APP = 'app-match-all'
export const FORWARDING_SEGMENTS = 'forwarding-segments'
export const PROTOCOLS: MssProtocol[] = ['tcp', 'udp', 'icmp', 'any']
export const EMPTY_SEGMENTATION: Segmentation = { enabled: false, applications: [], policies: [], vrfs: [] }

const obj = (v: unknown) => (v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {})
const arr = (v: unknown) => (Array.isArray(v) ? v : [])
const strs = (v: unknown) => (Array.isArray(v) ? v.map(String) : typeof v === 'string' && v ? v.split(',').map((s) => s.trim()).filter(Boolean) : [])
const action = (v: unknown): MssAction => (v === 'drop' ? 'drop' : 'forward')

/** Lenient client-side normalisation of the `segmentation` knob (the server's normalize_segmentation is the authority). */
export function normalizeSegmentation(v: unknown): Segmentation {
  const o = obj(v)
  return {
    enabled: !!o.enabled,
    applications: arr(o.applications).map((a): MssApplication => {
      const x = obj(a)
      const ports = x.ports == null || String(x.ports).trim() === '' ? null : String(x.ports)
      return { name: String(x.name ?? ''), protocol: (PROTOCOLS as string[]).includes(String(x.protocol)) ? (x.protocol as MssProtocol) : 'tcp', ports }
    }),
    policies: arr(o.policies).map((p): MssPolicy => {
      const x = obj(p)
      return { name: String(x.name ?? ''), rules: arr(x.rules).map((r) => { const y = obj(r); return { application: String(y.application ?? ''), action: action(y.action), log: !!y.log } }) }
    }),
    vrfs: arr(o.vrfs).map((vr): MssVrf => {
      const x = obj(vr)
      return {
        name: String(x.name ?? ''),
        segments: arr(x.segments).map((s): MssSegment => {
          const y = obj(s)
          return {
            name: String(y.name ?? ''),
            prefixes: strs(y.prefixes),
            interfaces: strs(y.interfaces),
            policies: arr(y.policies).map((p) => { const z = obj(p); return { from: String(z.from ?? ''), policy: String(z.policy ?? '') } }),
            fallback: y.fallback === 'forward' || y.fallback === 'drop' ? y.fallback : null,
          }
        }),
      }
    }),
  }
}

/** The knob value written back (fallback null → omitted). */
export function segmentationKnob(s: Segmentation): Record<string, unknown> {
  return {
    enabled: s.enabled,
    applications: s.applications.map((a) => ({ name: a.name, protocol: a.protocol, ports: a.ports || null })),
    policies: s.policies.map((p) => ({ name: p.name, rules: p.rules.map((r) => ({ application: r.application, action: r.action, log: r.log })) })),
    vrfs: s.vrfs.map((v) => ({
      name: v.name,
      segments: v.segments.map((g) => ({
        name: g.name, prefixes: g.prefixes, interfaces: g.interfaces, policies: g.policies.filter((p) => p.from && p.policy),
        ...(g.fallback ? { fallback: g.fallback } : {}),
      })),
    })),
  }
}

function segmentPolicy(dst: MssSegment, src: string): string | null {
  const pols = Object.fromEntries(dst.policies.map((p) => [p.from, p.policy]))
  if (src in pols) return pols[src]
  if (FORWARDING_SEGMENTS in pols) return pols[FORWARDING_SEGMENTS]
  if (dst.fallback) return `policy-${dst.fallback}-all`
  return null
}

/** matrix[vrf][src][dst] = governing policy (null = unspecified) — mirrors labs/mss.py policy_matrix. */
export function policyMatrix(s: Segmentation): Record<string, Record<string, Record<string, string | null>>> {
  const out: Record<string, Record<string, Record<string, string | null>>> = {}
  for (const v of s.vrfs) {
    out[v.name] = Object.fromEntries(v.segments.map((src) => [src.name, Object.fromEntries(v.segments.map((dst) => [dst.name, segmentPolicy(dst, src.name)]))]))
  }
  return out
}

/** How a policy acts overall: forward / drop (uniform) or mixed; null = unspecified/unknown. */
export function policyVerdict(s: Pick<Segmentation, 'policies'>, policy: string | null): MssAction | 'mixed' | null {
  if (!policy) return null
  if (policy in BUILTIN_POLICIES) return BUILTIN_POLICIES[policy]
  const p = s.policies.find((x) => x.name === policy)
  if (!p) return null
  const catchAll = p.rules.find((r) => r.application === BUILTIN_APP)
  const acts = new Set(p.rules.map((r) => r.action))
  if (!catchAll) acts.add('drop') // unmatched → drop
  return acts.size === 1 ? [...acts][0] : 'mixed'
}

export const VERDICT_CLASS: Record<string, string> = {
  forward: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/50 dark:text-emerald-200',
  drop: 'bg-red-100 text-red-800 dark:bg-red-900/50 dark:text-red-200',
  mixed: 'bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-200',
  none: 'bg-zinc-100 text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400',
}

/** Client-side sanity checks (names, references) shown before saving; the server validates for real. */
export function segmentationIssues(s: Segmentation): string[] {
  const out: string[] = []
  const dup = (names: string[], what: string) => {
    const seen = new Set<string>()
    for (const n of names) { if (!n.trim()) out.push(`${what}: empty name`); else if (seen.has(n)) out.push(`${what}: duplicate ${n}`); seen.add(n) }
  }
  dup(s.applications.map((a) => a.name), 'application')
  dup(s.policies.map((p) => p.name), 'policy')
  dup(s.vrfs.map((v) => v.name), 'vrf')
  const apps = new Set([BUILTIN_APP, ...s.applications.map((a) => a.name)])
  const pols = new Set([...Object.keys(BUILTIN_POLICIES), ...s.policies.map((p) => p.name)])
  for (const p of s.policies) for (const r of p.rules) if (!apps.has(r.application)) out.push(`policy ${p.name}: unknown application ${r.application || '(empty)'}`)
  for (const v of s.vrfs) {
    dup(v.segments.map((g) => g.name), `vrf ${v.name} segment`)
    const segs = new Set([FORWARDING_SEGMENTS, ...v.segments.map((g) => g.name)])
    for (const g of v.segments) {
      for (const p of g.prefixes) if (!/^(\d{1,3}\.){3}\d{1,3}\/\d{1,2}$/.test(p)) out.push(`${v.name}/${g.name}: bad prefix ${p}`)
      for (const p of g.policies) {
        if (p.from && !segs.has(p.from)) out.push(`${v.name}/${g.name}: unknown source segment ${p.from}`)
        if (p.policy && !pols.has(p.policy)) out.push(`${v.name}/${g.name}: unknown policy ${p.policy}`)
      }
    }
  }
  return out
}

/** forward | drop | null for `application` under `policy` — mirrors labs/mss.py decide(). */
export function decide(s: Pick<Segmentation, 'policies'>, policy: string | null, application: string): MssAction | null {
  if (!policy) return null
  if (policy in BUILTIN_POLICIES) return BUILTIN_POLICIES[policy]
  const p = s.policies.find((x) => x.name === policy)
  if (!p) return null
  for (const r of p.rules) if (r.application === application || r.application === BUILTIN_APP) return r.action
  return 'drop'
}

const ipNum = (ip: string) => ip.split('.').reduce((n, o) => n * 256 + Number(o), 0)
export function inCidr(ip: string, cidr: string): boolean {
  const [net, len] = cidr.split('/')
  const bits = Number(len)
  if (!net || Number.isNaN(bits)) return false
  const mask = bits === 0 ? 0 : (0xffffffff << (32 - bits)) >>> 0
  return ((ipNum(ip.split('/')[0]) & mask) >>> 0) === ((ipNum(net) & mask) >>> 0)
}

/** most specific segment of `vrf` containing `ip` */
export function segmentOf(s: Segmentation, vrf: string | null | undefined, ip: string | null | undefined): string | null {
  if (!vrf || !ip) return null
  let best: [number, string] | null = null
  for (const v of s.vrfs) {
    if (v.name !== vrf) continue
    for (const g of v.segments) for (const p of g.prefixes) {
      const len = Number(p.split('/')[1])
      if (inCidr(ip, p) && (!best || len > best[0])) best = [len, g.name]
    }
  }
  return best ? best[1] : null
}
