import { useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useLocation } from 'react-router-dom'
import { metaApi } from '../api/phase5'
import type { ChangelogEntry } from '../api/types'
import { useMeta } from '../hooks/useMeta'
import { Badge, ErrorBox, PageHeader, Spinner } from '../components/ui'
import { renderInline } from '../lib/markdown'
import { UpdatePanel } from '../components/UpdatePanel'
import { latestVersion, markSeen } from '../lib/whatsNew'

const SECTION_TONE: Record<string, string> = {
  Added: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/60 dark:text-emerald-200',
  Changed: 'bg-blue-100 text-blue-800 dark:bg-blue-900/60 dark:text-blue-200',
  Fixed: 'bg-amber-100 text-amber-800 dark:bg-amber-900/60 dark:text-amber-200',
  Removed: 'bg-red-100 text-red-800 dark:bg-red-900/60 dark:text-red-200',
  Security: 'bg-violet-100 text-violet-800 dark:bg-violet-900/60 dark:text-violet-200',
  Deprecated: 'bg-zinc-200 text-zinc-700 dark:bg-zinc-700 dark:text-zinc-200',
}
const ORDER = ['Added', 'Changed', 'Fixed', 'Removed', 'Deprecated', 'Security']
export const versionAnchor = (v: string) => `v${v.replace(/[^0-9A-Za-z.-]/g, '')}`

export default function ChangelogPage() {
  const cl = useQuery({ queryKey: ['meta', 'changelog'], queryFn: metaApi.changelog })
  const meta = useMeta()
  const loc = useLocation()
  const latest = cl.data?.entries[0]?.version ?? latestVersion(meta.data)
  // visiting the changelog marks the latest version as seen (clears the dot on the version badge)
  useEffect(() => { if (latest) markSeen(latest) }, [latest])
  useEffect(() => {
    if (!cl.data || !loc.hash) return
    document.getElementById(loc.hash.slice(1))?.scrollIntoView?.()
  }, [cl.data, loc.hash])

  if (cl.isLoading) return <Spinner />
  return (
    <div className="max-w-3xl">
      <PageHeader title="Changelog" sub={meta.data ? <>running v{meta.data.version}</> : null} />
      <div className="mb-4"><UpdatePanel /></div>
      <ErrorBox error={cl.error} />
      {cl.data && (
        <>
          <nav className="mb-3 flex flex-wrap gap-2 text-xs">
            {cl.data.entries.map((e) => <a key={e.version} className="link mono" href={`#${versionAnchor(e.version)}`}>{e.version}</a>)}
          </nav>
          <div className="space-y-4" data-testid="changelog">
            {cl.data.entries.map((e) => <Entry key={e.version} e={e} current={e.version === meta.data?.version} />)}
          </div>
        </>
      )}
    </div>
  )
}

function Entry({ e, current }: { e: ChangelogEntry; current: boolean }) {
  const keys = Object.keys(e.sections).sort((a, b) => (ORDER.indexOf(a) + 1 || 99) - (ORDER.indexOf(b) + 1 || 99))
  return (
    <section id={versionAnchor(e.version)} className="card scroll-mt-4 p-4" data-testid={`changelog-${e.version}`}>
      <h2 className="mb-2 flex items-baseline gap-2">
        <a href={`#${versionAnchor(e.version)}`} className="text-base font-semibold hover:underline">{e.version}</a>
        {e.date && <span className="text-xs text-zinc-500">{e.date}</span>}
        {current && <Badge className="bg-cyan-100 text-cyan-800 dark:bg-cyan-900/60 dark:text-cyan-200">running</Badge>}
      </h2>
      <div className="space-y-2">
        {keys.filter((k) => e.sections[k]?.length).map((k) => (
          <div key={k}>
            <Badge className={SECTION_TONE[k] ?? SECTION_TONE.Deprecated}>{k}</Badge>
            <ul className="mt-1 list-disc space-y-0.5 pl-5 text-sm">
              {e.sections[k].map((item, i) => <li key={i}>{renderInline(item, `${e.version}-${k}-${i}`)}</li>)}
            </ul>
          </div>
        ))}
      </div>
    </section>
  )
}
