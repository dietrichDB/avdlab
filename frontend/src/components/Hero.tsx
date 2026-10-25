import { Link } from 'react-router-dom'
import { Icon, LogoMark } from './Brand'

export const PITCH = 'Build, deploy and validate Arista AVD labs — and learn AVD while you do it'

/** Dashboard hero (CONTRACT-8 B §3): logo mark, pitch, Start learning / New lab, GitHub when configured. */
export function Hero({ title = 'avdlab', githubUrl, user }: { title?: string; githubUrl?: string | null; user?: string | null }) {
  return (
    <section className="relative overflow-hidden rounded-lg bg-cv-nav px-6 py-6 text-white shadow-sm ring-1 ring-white/5 sm:px-8" data-testid="hero">
      {/* subtle fabric motif: spine/leaf dots, purely decorative */}
      <svg className="pointer-events-none absolute -right-6 -top-6 h-48 w-80 text-white/[0.07]" viewBox="0 0 320 190" aria-hidden>
        <g fill="currentColor" stroke="currentColor" strokeWidth="1.5">
          {[90, 230].map((x) => <circle key={x} cx={x} cy={40} r={9} />)}
          {[40, 120, 200, 280].map((x) => <circle key={x} cx={x} cy={140} r={9} />)}
          {[90, 230].flatMap((s) => [40, 120, 200, 280].map((l) => <line key={`${s}-${l}`} x1={s} y1={40} x2={l} y2={140} />))}
        </g>
      </svg>
      <div className="relative flex items-start gap-4">
        <LogoMark className="hidden h-12 w-12 shrink-0 sm:block" />
        <div className="min-w-0">
          <div className="text-xs font-medium uppercase tracking-[0.14em] text-cv-accent">{user ? `Welcome back, ${user}` : 'Welcome'}</div>
          <h1 className="mt-1 text-2xl font-semibold tracking-tight" data-testid="hero-title">{title}</h1>
          <p className="mt-1.5 max-w-2xl text-[15px] leading-relaxed text-cv-nav-text" data-testid="hero-pitch">{PITCH}.</p>
          <div className="mt-4 flex flex-wrap gap-2">
            <Link to="/getting-started" className="btn-primary px-4 py-2" data-testid="hero-getting-started"><Icon name="play" />Getting started</Link>
            <Link to="/learn" className="btn-primary px-4 py-2" data-testid="hero-learn"><Icon name="book" />Start learning</Link>
            <Link to="/labs/new" className="btn-primary px-4 py-2" data-testid="hero-new-lab"><Icon name="plus" />New lab</Link>
            {githubUrl && (
              <a href={githubUrl} target="_blank" rel="noreferrer" data-testid="hero-github"
                className="btn border-white/25 bg-white/10 px-4 py-2 text-white hover:bg-white/20">
                <Icon name="github" />GitHub
              </a>
            )}
          </div>
        </div>
      </div>
    </section>
  )
}
