import { useEffect, useRef, useState } from 'react'
import { Link, NavLink, Outlet, useLocation } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import ErrorBoundary from './ErrorBoundary'
import { useAuth } from '../hooks/useAuth'
import { useMeta } from '../hooks/useMeta'
import { cls } from '../lib/format'
import { useUiMode } from '../lib/uiMode'
import { labsApi } from '../api/labs'
import { hostsApi } from '../api/hosts'
import { Icon, LogoMark, VersionBadge, Wordmark, type IconName } from './Brand'
import { FeedbackProvider, useFeedback } from './Feedback'
import { UpdatePill } from './UpdatePanel'
import { ModeToggle } from './ModeToggle'
import { useLearningMode } from '../hooks/useLearn'

// ---- navigation model (CONTRACT-8 B §2) ---------------------------------------------------------------------------
export interface NavItem {
  to: string
  label: string
  icon: IconName
  end?: boolean
  /** hidden in beginner mode (still reachable by URL) */
  advanced?: boolean
  adminOnly?: boolean
}
export interface NavGroup { id: string; label: string; items: NavItem[] }

export const NAV_GROUPS: NavGroup[] = [
  { id: 'overview', label: 'Overview', items: [{ to: '/', label: 'Dashboard', icon: 'home', end: true }] },
  {
    id: 'provisioning', label: 'Provisioning', items: [
      { to: '/hosts', label: 'Hosts', icon: 'server' },
      { to: '/images', label: 'Images', icon: 'image' },
      { to: '/labs', label: 'Labs', icon: 'labs' },
      { to: '/cloudvision', label: 'CloudVision', icon: 'cloud' },
      { to: '/netbox', label: 'NetBox', icon: 'database', advanced: true },
      { to: '/devcontainer', label: 'Devcontainer', icon: 'code', advanced: true },
    ],
  },
  { id: 'learn', label: 'Learn', items: [{ to: '/getting-started', label: 'Getting started', icon: 'play' }, { to: '/learn', label: 'Learn AVD', icon: 'book' }] },
  {
    id: 'admin', label: 'Admin', items: [
      { to: '/users', label: 'Users', icon: 'users', adminOnly: true },
      { to: '/admin/settings', label: 'Admin settings', icon: 'cog', adminOnly: true },
      { to: '/jobs', label: 'Jobs', icon: 'jobs', advanced: true },
      { to: '/feedback', label: 'Feedback', icon: 'feedback' },
      { to: '/changelog', label: 'Changelog', icon: 'changelog' },
      { to: '/about', label: 'About', icon: 'info' },
    ],
  },
]

/** The groups/items visible for a role and UI mode (empty groups dropped). */
export function visibleNav(role: string | null, beginner: boolean): NavGroup[] {
  return NAV_GROUPS
    .map((g) => ({ ...g, items: g.items.filter((i) => (!i.adminOnly || role === 'admin') && (!i.advanced || !beginner)) }))
    .filter((g) => g.items.length > 0)
}

// ---- shell --------------------------------------------------------------------------------------------------------
export function Layout() {
  const { role } = useAuth()
  const { beginner } = useUiMode()
  const meta = useMeta()
  const groups = visibleNav(role, beginner)
  const pathname = useLocation().pathname
  return (
    <FeedbackProvider>
      <div className="flex min-h-screen bg-cv-bg">
        <aside className="sticky top-0 flex h-screen w-56 shrink-0 flex-col bg-cv-nav text-cv-nav-text" data-testid="sidebar">
          <div className="border-b border-cv-nav-border px-4 py-3" data-testid="sidebar-brand">
            <Link to="/" className="flex items-center gap-2">
              <LogoMark />
              <Wordmark title={meta.data?.app_title} />
              {import.meta.env.VITE_MOCK === '1' && <span className="rounded bg-amber-400/20 px-1 text-[9px] font-semibold uppercase text-amber-300">mock</span>}
            </Link>
            <div className="mt-1.5 flex items-center pl-0.5"><VersionBadge /><UpdatePill /></div>
          </div>
          <nav className="flex-1 space-y-4 overflow-y-auto px-2 py-3" aria-label="Main">
            {groups.map((g) => (
              <div key={g.id} data-testid={`nav-group-${g.id}`}>
                <div className="px-2 pb-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-cv-nav-muted">{g.label}</div>
                {g.items.map((n) => (
                  <NavLink
                    key={n.to}
                    to={n.to}
                    end={n.end}
                    data-testid={`nav-${n.to === '/' ? 'dashboard' : n.to.slice(1).replace('/', '-')}`}
                    className={({ isActive }) => cls(
                      'group relative flex items-center gap-2.5 rounded-md px-2 py-1.5 text-[13px] transition-colors',
                      isActive
                        ? 'bg-cv-nav-active font-medium text-cv-nav-text-active before:absolute before:-left-2 before:top-1 before:bottom-1 before:w-[3px] before:rounded-r before:bg-cv-primary'
                        : 'text-cv-nav-text hover:bg-cv-nav-hover hover:text-white',
                    )}
                  >
                    {({ isActive }) => <>
                      <Icon name={n.icon} className={cls('h-4 w-4', isActive ? 'text-cv-accent' : 'text-cv-nav-muted group-hover:text-cv-nav-text')} />
                      <span className="truncate">{n.label}</span>
                    </>}
                  </NavLink>
                ))}
              </div>
            ))}
          </nav>
          <div className="border-t border-cv-nav-border px-4 py-2.5 text-xs">
            <LearningModeSwitch className="" dark />
          </div>
        </aside>
        <div className="flex min-w-0 flex-1 flex-col">
          <TopBar />
          <main className="min-w-0 flex-1 px-6 py-5">
            <ErrorBoundary name="this page" resetKey={pathname}>
              <Outlet />
            </ErrorBoundary>
          </main>
          <Footer />
        </div>
      </div>
    </FeedbackProvider>
  )
}

// ---- top bar + breadcrumbs ---------------------------------------------------------------------------------------
const SECTION: Record<string, { label: string; group?: string }> = {
  '': { label: 'Dashboard', group: 'Overview' },
  hosts: { label: 'Hosts', group: 'Provisioning' },
  images: { label: 'Images', group: 'Provisioning' },
  labs: { label: 'Labs', group: 'Provisioning' },
  cloudvision: { label: 'CloudVision', group: 'Provisioning' },
  netbox: { label: 'NetBox', group: 'Provisioning' },
  devcontainer: { label: 'Devcontainer', group: 'Provisioning' },
  learn: { label: 'Learn' },
  users: { label: 'Users', group: 'Admin' },
  admin: { label: 'Admin settings', group: 'Admin' },
  jobs: { label: 'Jobs', group: 'Admin' },
  feedback: { label: 'Feedback', group: 'Admin' },
  changelog: { label: 'Changelog', group: 'Admin' },
  about: { label: 'About', group: 'Admin' },
  account: { label: 'Account' },
}
const pretty = (s: string) => {
  const t = decodeURIComponent(s).replace(/[-_]/g, ' ')
  return t === 'git' ? 'Git & VS Code' : t.length <= 4 ? t.toUpperCase() : t.charAt(0).toUpperCase() + t.slice(1)
}
const shortId = (s: string) => (s.length > 12 ? `${s.slice(0, 8)}…` : s)

export interface Crumb { label: string; to?: string }

/** Breadcrumbs from the path; `names` resolves lab/host ids to their names. */
export function crumbsFor(pathname: string, names: { lab?: string; host?: string } = {}): Crumb[] {
  const seg = pathname.split('/').filter(Boolean)
  const head = seg[0] ?? ''
  const sec = SECTION[head]
  if (!sec) return [{ label: pretty(head) }]
  const out: Crumb[] = []
  if (sec.group && sec.group !== sec.label && head !== '') out.push({ label: sec.group })
  if (head === '') return [{ label: 'Overview' }, { label: 'Dashboard' }]
  if (head === 'admin') return [...out, { label: 'Admin settings', to: '/admin/settings' }]
  out.push({ label: sec.label, to: `/${head}` })
  if (head === 'labs' && seg[1]) {
    if (seg[1] === 'new') out.push({ label: 'New lab' })
    else {
      out.push({ label: names.lab ?? shortId(seg[1]), to: `/labs/${seg[1]}` })
      if (seg[2]) out.push({ label: pretty(seg[2]) })
    }
  } else if (head === 'hosts' && seg[1]) out.push({ label: names.host ?? shortId(seg[1]) })
  else if (head === 'learn' && seg[1]) {
    out.push({ label: pretty(seg[1]), to: seg[1] === 'lessons' ? '/learn/lessons' : undefined })
    if (seg[2]) out.push({ label: pretty(seg[2]) })
  } else if (seg[1]) out.push({ label: shortId(seg[1]) })
  return out
}

function TopBar() {
  const { pathname } = useLocation()
  const meta = useMeta()
  const seg = pathname.split('/').filter(Boolean)
  const labId = seg[0] === 'labs' && seg[1] && seg[1] !== 'new' ? seg[1] : ''
  const hostId = seg[0] === 'hosts' && seg[1] ? seg[1] : ''
  // same keys as LabDetail / HostDetail → served from the cache, no extra request
  const lab = useQuery({ queryKey: ['labs', labId], queryFn: () => labsApi.get(labId), enabled: !!labId })
  const host = useQuery({ queryKey: ['hosts', hostId], queryFn: () => hostsApi.get(hostId), enabled: !!hostId })
  const crumbs = crumbsFor(pathname, { lab: lab.data?.name, host: host.data?.name })
  const gh = meta.data?.github_url
  return (
    <header className="sticky top-0 z-20 flex h-12 items-center gap-3 border-b border-cv-border bg-cv-topbar/95 px-6 backdrop-blur" data-testid="topbar">
      <nav aria-label="Breadcrumb" className="min-w-0 flex-1" data-testid="breadcrumbs">
        <ol className="flex min-w-0 items-center gap-1 text-[13px]">
          {crumbs.map((c, i) => {
            const last = i === crumbs.length - 1
            return (
              <li key={i} className={cls('flex min-w-0 items-center gap-1', last ? 'font-semibold text-cv-text' : 'text-cv-muted')}>
                {i > 0 && <Icon name="chevron" className="h-3.5 w-3.5 text-cv-muted/60" />}
                {c.to && !last ? <Link to={c.to} className="truncate hover:text-cv-primary-text">{c.label}</Link> : <span className="truncate">{c.label}</span>}
              </li>
            )
          })}
        </ol>
      </nav>
      <div className="flex items-center gap-2">
        {gh && (
          <a href={gh} target="_blank" rel="noreferrer" className="btn-ghost px-2" title="avdlab on GitHub" data-testid="topbar-github">
            <Icon name="github" className="h-4 w-4" /><span className="hidden lg:inline">GitHub</span>
          </a>
        )}
        <ModeToggle />
        <span className="mx-1 h-5 w-px bg-cv-border" aria-hidden />
        <UserMenu />
      </div>
    </header>
  )
}

const ROLE_BADGE: Record<string, string> = {
  admin: 'bg-violet-100 text-violet-800 dark:bg-violet-900/60 dark:text-violet-200',
  operator: 'bg-blue-100 text-blue-800 dark:bg-blue-900/60 dark:text-blue-200',
  viewer: 'bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300',
}

/** Top-right: avatar + username + role badge → Account / Admin settings (admins) / Sign out. */
export function UserMenu() {
  const { user, role, logout } = useAuth()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const close = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false) }
    window.addEventListener('mousedown', close)
    return () => window.removeEventListener('mousedown', close)
  }, [open])
  const item = 'flex items-center gap-2 px-3 py-1.5 text-[13px] hover:bg-cv-surface-2'
  return (
    <div className="relative text-sm" ref={ref} data-testid="user-menu">
      <button className="flex items-center gap-2 rounded-md px-1.5 py-1 hover:bg-cv-surface-2" onClick={() => setOpen(!open)} aria-haspopup="menu" aria-expanded={open}>
        <span className="flex h-6 w-6 items-center justify-center rounded-full bg-cv-primary text-[11px] font-semibold uppercase text-white" aria-hidden>{(user ?? '?').slice(0, 1)}</span>
        <span className="max-w-[12rem] truncate text-[13px]">{user}</span>
        {role && <span className={cls('rounded px-1.5 py-px text-[10px] font-medium uppercase', ROLE_BADGE[role] ?? ROLE_BADGE.viewer)} data-testid="role-badge">{role}</span>}
        <span className="text-cv-muted">▾</span>
      </button>
      {open && (
        <div className="card absolute right-0 z-30 mt-1 w-48 py-1 shadow-lg" role="menu">
          <Link to="/account" className={item} role="menuitem" onClick={() => setOpen(false)}>Account</Link>
          {role === 'admin' && <>
            <Link to="/users" className={item} role="menuitem" onClick={() => setOpen(false)}>Users</Link>
            <Link to="/admin/settings" className={item} role="menuitem" onClick={() => setOpen(false)} data-testid="menu-admin-settings">Admin settings</Link>
          </>}
          <div className="my-1 border-t border-cv-border" />
          <button className={cls(item, 'w-full text-left')} role="menuitem" onClick={() => { setOpen(false); logout() }}>Sign out</button>
        </div>
      )}
    </div>
  )
}

/** `avdlab v<version> · Changelog · About · Feedback` on every page */
export function Footer() {
  const meta = useMeta()
  const fb = useFeedback()
  return (
    <footer className="border-t border-cv-border px-6 py-2.5 pr-28 text-xs text-cv-muted" data-testid="footer">
      <span>{meta.data?.app_title || 'avdlab'} v{meta.data?.version ?? '…'}</span>
      {' · '}<Link className="hover:text-cv-text hover:underline" to="/changelog">Changelog</Link>
      {' · '}<Link className="hover:text-cv-text hover:underline" to="/about">About</Link>
      {' · '}<button className="hover:text-cv-text hover:underline" onClick={fb.open} data-testid="footer-feedback">Feedback</button>
    </footer>
  )
}

/** "Learning mode" toggle (persisted): (i) popovers, explain panels, glossary hover cards. */
export function LearningModeSwitch({ className = 'mb-1.5', dark = false }: { className?: string; dark?: boolean }) {
  const [on, set] = useLearningMode()
  return (
    <label className={cls('flex cursor-pointer items-center gap-1.5', dark ? 'text-cv-nav-text hover:text-white' : 'text-zinc-600 dark:text-zinc-300', className)} title="(i) knob help, config explain panels and glossary hover cards">
      <input type="checkbox" className="accent-cv-primary" checked={on} onChange={(e) => set(e.target.checked)} data-testid="learning-mode-toggle" />
      Learning mode
    </label>
  )
}
