import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Icon } from './Brand'

type Probe = 'checking' | 'ok' | 'missing'

/**
 * Does the video exist? HEAD the URL: 404/410 or an HTML answer (SPA fallback of the dev server / Caddy) → missing.
 * Network errors or servers that refuse HEAD fall through to the <video> element, whose onError catches the rest.
 */
export async function probeVideo(src: string, f: typeof fetch = fetch): Promise<'ok' | 'missing'> {
  try {
    const r = await f(src, { method: 'HEAD' })
    if (r.status === 404 || r.status === 410) return 'missing'
    if (r.ok && (r.headers.get('content-type') ?? '').includes('text/html')) return 'missing'
    return 'ok'
  } catch {
    return 'ok'
  }
}

/** Dashboard intro walkthrough (CONTRACT-8 B §3): `/meta.intro_video_url`, graceful fallback when empty or 404. */
export function IntroVideo({ src, poster = '/intro/poster.jpg', title = 'avdlab in 90 seconds' }: { src?: string | null; poster?: string; title?: string }) {
  const url = (src ?? '').trim()
  const [state, setState] = useState<Probe>(url ? 'checking' : 'missing')
  useEffect(() => {
    if (!url) { setState('missing'); return }
    let alive = true
    setState('checking')
    probeVideo(url).then((s) => { if (alive) setState(s) })
    return () => { alive = false }
  }, [url])

  return (
    <section className="card overflow-hidden" data-testid="intro-video-card">
      <div className="card-header">
        <span className="flex items-center gap-2 text-[13px] font-semibold"><Icon name="video" className="h-4 w-4 text-cv-primary" />{title}</span>
        <span className="text-xs text-cv-muted">walkthrough</span>
      </div>
      {state === 'missing' ? (
        <div className="flex aspect-video flex-col items-center justify-center gap-3 bg-cv-surface-2 p-6 text-center" data-testid="intro-video-fallback">
          <span className="flex h-12 w-12 items-center justify-center rounded-full bg-cv-primary/10 text-cv-primary"><Icon name="play" className="h-5 w-5" /></span>
          <div>
            <div className="font-semibold">The intro video isn't available on this server yet</div>
            <div className="mt-1 text-[13px] text-cv-muted">Take a guided tour instead — it walks you through the same steps inside the app.</div>
          </div>
          <div className="flex gap-2">
            <Link to="/learn/tours" className="btn-primary">Start a tour</Link>
            <Link to="/learn/lessons" className="btn-secondary">Browse lessons</Link>
          </div>
        </div>
      ) : state === 'checking' ? (
        <div className="aspect-video animate-pulse bg-cv-surface-2" data-testid="intro-video-loading" />
      ) : (
        <video className="block aspect-video w-full bg-black" controls preload="metadata" poster={poster} src={url}
          data-testid="intro-video" onError={() => setState('missing')}>
          <track kind="captions" />
        </video>
      )}
    </section>
  )
}
