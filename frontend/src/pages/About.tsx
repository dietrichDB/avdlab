import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { metaApi } from '../api/phase5'
import { useMeta } from '../hooks/useMeta'
import { Logo } from '../components/Brand'
import { UpdatePanel } from '../components/UpdatePanel'
import { ErrorBox, KV, Spinner } from '../components/ui'
import { Markdown } from '../lib/markdown'
import { shortSha } from '../lib/whatsNew'

export default function AboutPage() {
  const meta = useMeta()
  const notices = useQuery({ queryKey: ['meta', 'notices'], queryFn: metaApi.notices, staleTime: Infinity })
  const m = meta.data
  return (
    <div className="max-w-3xl space-y-4" data-testid="about">
      <div className="card flex flex-wrap items-center gap-6 p-5">
        <Logo className="h-14" />
        <div className="space-y-1 text-sm text-zinc-600 dark:text-zinc-400">
          <div>AVD lab orchestrator — build, deploy and validate Arista AVD fabrics on containerlab and Arista Cloud Test.</div>
          <div className="text-xs" data-testid="about-author">Created by Dietrich De Bruyne, Systems Engineer at Arista Networks.</div>
          {(m?.support_email || m?.github_url) && (
            <div className="flex flex-wrap gap-3 text-xs" data-testid="about-contact">
              {m?.support_email && <a className="link" href={`mailto:${m.support_email}`}>{m.support_email}</a>}
              {m?.github_url && <a className="link" href={m.github_url} target="_blank" rel="noreferrer">GitHub</a>}
            </div>
          )}
        </div>
      </div>
      <section className="card p-4">
        <div className="h2 mb-2">Version</div>
        {meta.isLoading ? <Spinner /> : <ErrorBox error={meta.error} />}
        {m && (
          <KV items={[
            ['avdlab', <span data-testid="about-version">v{m.version} <Link className="link ml-2 text-xs" to="/changelog">changelog</Link></span>],
            ['git', <span className="mono" title={m.git_sha ?? undefined}>{shortSha(m.git_sha) ?? '—'}</span>],
            ['built', m.build_date ?? '—'],
            ['pyavd', m.avd_version ?? '—'],
            ['ANTA', m.anta_version ?? '—'],
            ['Python', m.python ?? '—'],
          ]} />
        )}
      </section>
      <UpdatePanel />
      <ErrorBox error={notices.error} />
      {notices.data && (
        <>
          <section className="card p-4">
            <div className="h2 mb-2">Copyright &amp; licence</div>
            <p className="text-sm font-medium" data-testid="about-copyright">{m?.copyright_notice || notices.data.copyright}</p>
            <Markdown source={notices.data.license} className="md mt-2 space-y-2 text-xs text-zinc-600 dark:text-zinc-400" />
          </section>
          <section className="card p-4" data-testid="about-notices">
            <div className="h2 mb-2">Third-party notices</div>
            <Markdown source={notices.data.markdown} />
          </section>
        </>
      )}
      <p className="text-xs text-zinc-500" data-testid="about-trademark">
        Arista, EOS and CloudVision are trademarks of Arista Networks, Inc. avdlab is not an Arista product and is not endorsed by Arista Networks.
      </p>
    </div>
  )
}
