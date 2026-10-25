import { createContext, useCallback, useContext, useEffect, useMemo, useReducer, useState, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { labsApi } from '../../api/labs'
import { learnApi, type Tours } from '../../api/phase6'
import { useTours } from '../../hooks/useLearn'
import { LEARN_LAB_KEY, TOUR_ROUTE_HINTS, getLS, resolveTourRoute, tourName, tourReducer, type TourState } from '../../lib/learn'

interface TourCtx {
  tours: Tours | undefined
  state: TourState | null
  start: (id: string, labId?: string | null) => Promise<void>
  next: () => void
  prev: () => void
  end: () => void
}
const Ctx = createContext<TourCtx | null>(null)

/** Lightweight guided tours (spotlight + step card) driven by GET /learn/tours. */
export function TourProvider({ children }: { children: ReactNode }) {
  const [enabled, setEnabled] = useState(false)
  const tours = useTours(enabled)
  const [state, dispatch] = useReducer(tourReducer, null)
  const [labId, setLabId] = useState<string | null>(null)
  const navigate = useNavigate()
  const loc = useLocation()
  const qc = useQueryClient()

  const pickLab = useCallback(async (given?: string | null) => {
    if (given) return given
    const m = /^\/labs\/([^/]+)/.exec(loc.pathname)
    if (m && m[1] !== 'new') return m[1]
    const stored = getLS(LEARN_LAB_KEY)
    if (stored) return stored
    const labs = await qc.fetchQuery({ queryKey: ['labs'], queryFn: labsApi.list }).catch(() => [])
    return labs[0]?.id ?? null
  }, [loc.pathname, qc])

  const start = useCallback(async (id: string, given?: string | null) => {
    setEnabled(true)
    const all = tours.data ?? await qc.fetchQuery({ queryKey: ['learn', 'tours'], queryFn: learnApi.tours, staleTime: Infinity })
    const lab = await pickLab(given)
    setLabId(lab)
    const t = all?.[id]
    if (!t?.steps.length) return
    const route = resolveTourRoute(t.route, lab)
    if (route && route !== loc.pathname) navigate(route)
    dispatch({ type: 'start', id })
  }, [tours.data, qc, pickLab, loc.pathname, navigate])

  const count = state ? tours.data?.[state.id]?.steps.length ?? 0 : 0
  const value = useMemo<TourCtx>(() => ({
    tours: tours.data, state, start,
    next: () => dispatch({ type: 'next', count }), prev: () => dispatch({ type: 'prev' }), end: () => dispatch({ type: 'end' }),
  }), [tours.data, state, start, count])
  return (
    <Ctx.Provider value={value}>
      {children}
      {state && tours.data?.[state.id] && <TourOverlay key={`${state.id}-${state.step}`} ctx={value} labId={labId} />}
    </Ctx.Provider>
  )
}

export function useTour(): TourCtx {
  const c = useContext(Ctx)
  if (!c) throw new Error('useTour outside TourProvider')
  return c
}
/** optional variant for components that may render outside the provider (tests) */
export function useTourOptional(): TourCtx | null {
  return useContext(Ctx)
}

interface Rect { top: number; left: number; width: number; height: number }

function TourOverlay({ ctx, labId }: { ctx: TourCtx; labId: string | null }) {
  const navigate = useNavigate()
  const s = ctx.state!
  const tour = ctx.tours![s.id]
  const step = tour.steps[s.step]
  const [rect, setRect] = useState<Rect | null>(null)
  useEffect(() => {
    let scrolled = false
    const find = () => {
      const el = document.querySelector(step.selector) as HTMLElement | null
      if (!el) { setRect(null); return }
      if (!scrolled) { el.scrollIntoView?.({ block: 'center', behavior: 'smooth' }); scrolled = true }
      const r = el.getBoundingClientRect()
      setRect((old) => (old && old.top === r.top && old.left === r.left && old.width === r.width && old.height === r.height ? old : { top: r.top, left: r.left, width: r.width, height: r.height }))
    }
    find()
    const t = setInterval(find, 250)
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') ctx.end(); else if (e.key === 'ArrowRight') ctx.next(); else if (e.key === 'ArrowLeft') ctx.prev() }
    window.addEventListener('keydown', onKey)
    return () => { clearInterval(t); window.removeEventListener('keydown', onKey) }
  }, [step.selector, ctx])
  const hint = TOUR_ROUTE_HINTS[tourName(step.selector)]
  const hintRoute = hint ? resolveTourRoute(hint, labId) : null
  const pad = 6
  const vw = typeof window !== 'undefined' ? window.innerWidth : 1024
  const vh = typeof window !== 'undefined' ? window.innerHeight : 768
  const cardStyle: React.CSSProperties = rect
    ? (step.placement === 'right' && rect.left + rect.width + 340 < vw
      ? { top: Math.max(8, rect.top), left: rect.left + rect.width + 12 }
      : rect.top + rect.height + 200 < vh
        ? { top: rect.top + rect.height + 12, left: Math.min(Math.max(8, rect.left), vw - 340) }
        : { top: Math.max(8, rect.top - 190), left: Math.min(Math.max(8, rect.left), vw - 340) })
    : { top: '30%', left: '50%', transform: 'translateX(-50%)' }
  const last = s.step + 1 >= tour.steps.length
  return (
    <div className="pointer-events-none fixed inset-0 z-[60]" data-testid="tour-overlay">
      {rect
        ? <div data-testid="tour-spotlight" className="absolute rounded-md ring-2 ring-cyan-400 transition-all"
            style={{ top: rect.top - pad, left: rect.left - pad, width: rect.width + 2 * pad, height: rect.height + 2 * pad, boxShadow: '0 0 0 9999px rgba(9,9,11,0.55)' }} />
        : <div className="absolute inset-0 bg-zinc-950/50" />}
      <div role="dialog" aria-label={tour.title} data-testid="tour-card"
        className="pointer-events-auto absolute w-80 rounded-lg border border-zinc-200 bg-white p-3 text-sm shadow-2xl dark:border-zinc-700 dark:bg-zinc-900" style={cardStyle}>
        <div className="flex items-center gap-2 text-[11px] text-zinc-500">
          <span className="flex-1">{tour.title} · step {s.step + 1} / {tour.steps.length}</span>
          <button aria-label="end tour" className="hover:text-zinc-900 dark:hover:text-zinc-100" onClick={ctx.end}>×</button>
        </div>
        <div className="mt-1 font-semibold" data-testid="tour-step-title">{step.title}</div>
        <p className="mt-1 text-zinc-700 dark:text-zinc-300">{step.text}</p>
        {!rect && (
          <div className="mt-2 rounded bg-amber-50 px-2 py-1 text-xs text-amber-900 dark:bg-amber-950/40 dark:text-amber-100" data-testid="tour-missing">
            This part is not on the current page.
            {hintRoute && <button className="link ml-1" onClick={() => navigate(hintRoute)}>Go there</button>}
          </div>
        )}
        <div className="mt-3 flex items-center gap-2">
          {step.lesson && <Link className="link text-xs" to={`/learn/lessons/${step.lesson}`} onClick={ctx.end}>related lesson</Link>}
          <span className="flex-1" />
          <button className="btn-secondary btn-sm" disabled={s.step === 0} onClick={ctx.prev}>Back</button>
          <button className="btn-primary btn-sm" onClick={ctx.next} data-testid="tour-next">{last ? 'Finish' : 'Next'}</button>
        </div>
      </div>
    </div>
  )
}
