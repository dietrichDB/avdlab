import { Link } from 'react-router-dom'
import logoRaw from '../../public/logo.svg?raw'
import { useWhatsNew } from '../hooks/useMeta'
import { shortSha } from '../lib/whatsNew'
import { cls } from '../lib/format'

// inline so the wordmark's currentColor follows light/dark; ids namespaced so they can't clash with other inline SVGs
const logoSvg = logoRaw.replace(/id="([^"]+)"/g, 'id="avdlab-logo-$1"').replace(/url\(#([^)]+)\)/g, 'url(#avdlab-logo-$1)')

/** Full logo (mark + wordmark), used on Login and About. */
export function Logo({ className = 'h-12 w-auto' }: { className?: string }) {
  return <span className={cls('inline-block text-zinc-900 dark:text-zinc-100 [&>svg]:h-full [&>svg]:w-auto', className)} data-testid="logo" role="img" aria-label="avdlab"
    dangerouslySetInnerHTML={{ __html: logoSvg }} />
}

export function LogoMark({ className = 'h-7 w-7' }: { className?: string }) {
  return <img src="/logo-mark.svg" alt="" className={className} data-testid="logo-mark" />
}

/** v<version> under the logo in the sidebar: tooltip git sha / build date, click → changelog, dot when unseen. */
export function VersionBadge() {
  const { meta, unseen } = useWhatsNew()
  const m = meta.data
  if (!m) return <span className="text-[10px] text-cv-nav-muted" data-testid="version-badge">v…</span>
  const tip = [`avdlab v${m.version}`, m.git_sha ? `git ${shortSha(m.git_sha)}` : null, m.build_date ? `built ${m.build_date}` : null, unseen ? "what's new: see changelog" : null].filter(Boolean).join(' · ')
  return (
    <Link to="/changelog" title={tip} data-testid="version-badge"
      className="relative inline-flex items-center rounded bg-white/10 px-1.5 py-0 font-mono text-[10px] text-cv-nav-text hover:bg-white/20 hover:text-white">
      v{m.version}
      {unseen && <span className="absolute -right-1 -top-1 h-2 w-2 rounded-full bg-cv-accent ring-2 ring-cv-nav" data-testid="whats-new-dot" aria-label="new version" />}
    </Link>
  )
}

// ---- icons (CONTRACT-8 B: inline SVG set, no icon dependency) --------------------------------------------------
// 24×24, 1.75 stroke, currentColor — lucide/feather-style outlines; `github` is the filled mark.
const ICONS = {
  home: 'M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z',
  server: 'M4 3h16a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2zM4 13h16a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-4a2 2 0 0 1 2-2zM6 7h.01M6 17h.01',
  image: 'M21 8 12 3 3 8v8l9 5 9-5zM3 8l9 5 9-5M12 13v8',
  labs: 'M9 3h6M10 3v6l-5.5 9.5A1.7 1.7 0 0 0 6 21h12a1.7 1.7 0 0 0 1.5-2.5L14 9V3M7 15h10',
  cloud: 'M17.5 19H8a5 5 0 1 1 .9-9.92A6 6 0 0 1 20 11.2 4 4 0 0 1 17.5 19z',
  book: 'M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5zM4 19.5A2.5 2.5 0 0 0 6.5 22H20v-5',
  users: 'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75',
  cog: 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z',
  github: 'M12 .5C5.65.5.5 5.65.5 12a11.5 11.5 0 0 0 7.86 10.92c.58.1.79-.25.79-.56v-2c-3.2.7-3.87-1.37-3.87-1.37-.52-1.33-1.28-1.69-1.28-1.69-1.04-.71.08-.7.08-.7 1.15.08 1.76 1.19 1.76 1.19 1.03 1.76 2.69 1.25 3.35.96.1-.75.4-1.25.73-1.54-2.55-.29-5.24-1.28-5.24-5.69 0-1.26.45-2.29 1.19-3.1-.12-.29-.52-1.46.11-3.05 0 0 .97-.31 3.17 1.18a11 11 0 0 1 5.77 0c2.2-1.49 3.17-1.18 3.17-1.18.63 1.59.23 2.76.11 3.05.74.81 1.19 1.84 1.19 3.1 0 4.42-2.69 5.39-5.25 5.68.41.36.78 1.06.78 2.14v3.17c0 .31.21.67.8.56A11.5 11.5 0 0 0 23.5 12C23.5 5.65 18.35.5 12 .5z',
  jobs: 'M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01',
  feedback: 'M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z',
  changelog: 'M3 12a9 9 0 1 0 18 0 9 9 0 0 0-18 0M12 7v5l3 2',
  info: 'M3 12a9 9 0 1 0 18 0 9 9 0 0 0-18 0M12 16v-4M12 8h.01',
  database: 'M12 8c4.97 0 9-1.34 9-3s-4.03-3-9-3-9 1.34-9 3 4.03 3 9 3zM3 5v14c0 1.66 4.03 3 9 3s9-1.34 9-3V5M3 12c0 1.66 4.03 3 9 3s9-1.34 9-3',
  code: 'M16 18l6-6-6-6M8 6l-6 6 6 6',
  play: 'M7 4.5v15l12.5-7.5z',
  check: 'M20 6 9 17l-5-5',
  plus: 'M12 5v14M5 12h14',
  chevron: 'M9 18l6-6-6-6',
  external: 'M15 3h6v6M10 14 21 3M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6',
  video: 'M23 7l-7 5 7 5zM3 5h11a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2z',
} as const
export type IconName = keyof typeof ICONS

export function Icon({ name, className = 'h-4 w-4', title }: { name: IconName; className?: string; title?: string }) {
  const filled = name === 'github'
  return (
    <svg viewBox="0 0 24 24" className={cls('shrink-0', className)} aria-hidden={title ? undefined : true} role={title ? 'img' : undefined}
      fill={filled ? 'currentColor' : 'none'} stroke={filled ? 'none' : 'currentColor'} strokeWidth={1.75} strokeLinecap="round" strokeLinejoin="round"
      data-icon={name}>
      {title && <title>{title}</title>}
      <path d={ICONS[name]} />
    </svg>
  )
}

/** "avdlab" wordmark text for dark (navy) backgrounds. */
export function Wordmark({ title, className }: { title?: string; className?: string }) {
  const t = (title || 'avdlab').trim()
  const isDefault = t.toLowerCase() === 'avdlab'
  return (
    <span className={cls('text-[15px] font-bold tracking-tight text-white', className)}>
      {isDefault ? <>avd<span className="text-cv-accent">lab</span></> : t}
    </span>
  )
}
