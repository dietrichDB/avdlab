import { useRef, useState, type FormEvent } from 'react'
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import { FALLBACK_FORMATS, imageArch, imagesApi, normArch } from '../api/images'
import { hostsApi } from '../api/hosts'
import { ApiError } from '../api/client'
import type { Arch, Image, ImageFormat } from '../api/types'
import { useJobDrawer } from '../components/JobDrawer'
import { Badge, Empty, ErrorBox, Field, Hint, PageHeader, Spinner } from '../components/ui'
import { cls, fmtAgo, fmtBytes } from '../lib/format'

export function ArchBadge({ arch }: { arch: Arch | string }) {
  return <Badge className={arch === 'arm64' ? 'bg-violet-100 text-violet-800 dark:bg-violet-900/60 dark:text-violet-200' : 'bg-zinc-200 text-zinc-700 dark:bg-zinc-700 dark:text-zinc-200'}>{arch}</Badge>
}

/** CONTRACT-3 §6 wording: "host is x86_64, image is arm64" */
export function archMismatch(hostArch: Arch | null, img: Pick<Image, 'fmt' | 'arch'>): string | null {
  const ia = imageArch(img)
  return hostArch && hostArch !== ia ? `host is ${hostArch}, image is ${ia}` : null
}

function useFormats(): ImageFormat[] {
  const q = useQuery({ queryKey: ['images', 'formats'], queryFn: imagesApi.formats, staleTime: 3_600_000, retry: false })
  return q.data ?? FALLBACK_FORMATS
}

export default function ImagesPage() {
  const qc = useQueryClient()
  const drawer = useJobDrawer()
  const images = useQuery({ queryKey: ['images'], queryFn: imagesApi.list })
  const hosts = useQuery({ queryKey: ['hosts'], queryFn: hostsApi.list })
  const formats = useFormats()
  const caps = useQueries({ queries: (hosts.data ?? []).map((h) => ({ queryKey: ['hosts', h.id, 'caps'], queryFn: () => hostsApi.capabilities(h.id), staleTime: 60_000 })) })
  const hostArch = (hostId: string): Arch | null => {
    const i = (hosts.data ?? []).findIndex((h) => h.id === hostId)
    return i < 0 ? null : normArch(caps[i]?.data?.capabilities?.os?.arch)
  }
  const [fmt, setFmt] = useState('cEOS')
  const fmtArch = formats.find((f) => f.fmt === fmt)?.arch ?? imageArch({ fmt })
  const [filter, setFilter] = useState('')
  const catalog = useQuery({ queryKey: ['images', 'catalog', fmt], queryFn: () => imagesApi.catalog(fmt), retry: false, staleTime: 3_600_000 })
  const token = useQuery({ queryKey: ['images', 'token'], queryFn: imagesApi.token, retry: false })
  const [editToken, setEditToken] = useState(false)
  const tokenSaved = () => {
    setEditToken(false)
    qc.invalidateQueries({ queryKey: ['images', 'token'] })
    qc.invalidateQueries({ queryKey: ['images', 'catalog'] })
  }

  const download = useMutation({ mutationFn: (version: string) => drawer.track(imagesApi.download(fmt, version)) })
  const push = useMutation({
    mutationFn: ({ image, hostId, remote }: { image: Image; hostId: string; remote: boolean }) =>
      drawer.track(imagesApi.push(image.id, hostId, { remote_download: remote })),
  })

  const have = new Set((images.data ?? []).filter((i) => i.fmt === fmt).map((i) => i.version))
  const versions = (catalog.data?.versions ?? []).filter((v) => !filter || v.toLowerCase().includes(filter.toLowerCase()))
  const noToken = (catalog.error instanceof ApiError && catalog.error.status === 409) || token.data?.configured === false
  const badToken = catalog.error instanceof ApiError && catalog.error.status === 502

  return (
    <div>
      <PageHeader title="Images" sub="cEOS images: Arista catalogue, local store and presence on lab hosts" />
      <Hint title="Images are the switch software.">Download a cEOS-lab version from arista.com (needs your Arista token) or upload one; avdlab copies it to your hosts when a lab deploys.</Hint>
      <div className="grid gap-4 xl:grid-cols-[1fr_22rem]">
        <section>
          <div className="h2 mb-2">Local images</div>
          <ErrorBox error={images.error ?? push.error} />
          {images.isLoading ? <Spinner /> : !images.data?.length ? <Empty>No images yet — download one from the catalogue or upload a .tar.xz.</Empty> : (
            <div className="card overflow-x-auto">
              <table className="table">
                <thead>
                  <tr>
                    <th>Format</th><th>Arch</th><th>Version</th><th>Docker tag</th><th>Size</th><th>Added</th>
                    {(hosts.data ?? []).map((h) => <th key={h.id} title={h.address}>{h.name}{hostArch(h.id) && <span className="ml-1 font-normal normal-case text-zinc-400">{hostArch(h.id)}</span>}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {images.data.map((img) => (
                    <tr key={img.id}>
                      <td>{img.fmt}</td>
                      <td><ArchBadge arch={imageArch(img)} /></td>
                      <td className="mono font-medium">{img.version}</td>
                      <td className="mono">{img.docker_tag}</td>
                      <td>{fmtBytes(img.size_bytes)}</td>
                      <td className="text-zinc-500">{fmtAgo(img.created_at)}</td>
                      {(hosts.data ?? []).map((h) => {
                        const mismatch = archMismatch(hostArch(h.id), img)
                        return (
                        <td key={h.id}>
                          {mismatch && <div className="mb-0.5 text-[11px] font-medium text-amber-700 dark:text-amber-300" data-testid="arch-mismatch" title="deploy will refuse this combination">⚠ {mismatch}</div>}
                          {img.present_on_hosts.includes(h.id) ? (
                            <Badge className="bg-emerald-100 text-emerald-800 dark:bg-emerald-900/60 dark:text-emerald-200">present</Badge>
                          ) : (
                            <div className="flex gap-1">
                              <button className="btn-secondary btn-sm" disabled={!img.local_path} title={img.local_path ? 'docker save | ssh docker load' : 'no local file'}
                                onClick={() => push.mutate({ image: img, hostId: h.id, remote: false })}>Push</button>
                              <button className="btn-secondary btn-sm" title="download on the host with ardl"
                                onClick={() => push.mutate({ image: img, hostId: h.id, remote: true })}>Fetch</button>
                            </div>
                          )}
                        </td>
                        )
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <UploadCard formats={formats} onDone={() => qc.invalidateQueries({ queryKey: ['images'] })} />
        </section>

        <section className="card flex max-h-[80vh] flex-col">
          <div className="flex items-center gap-2 border-b border-zinc-200 px-3 py-2 dark:border-zinc-800">
            <span className="h2 flex-1">Arista catalogue</span>
            <select className="input w-auto py-0.5 text-xs" aria-label="catalogue format" value={fmt} onChange={(e) => setFmt(e.target.value)}>
              {formats.map((f) => <option key={f.fmt} value={f.fmt}>{f.label || f.fmt}</option>)}
            </select>
            <ArchBadge arch={fmtArch} />
            <button type="button" className={cls('btn-secondary btn-sm', badToken && 'border-amber-400')} data-testid="token-button"
              title={token.data?.configured ? `token saved ${fmtAgo(token.data.created_at ?? '')} — replace or remove it` : 'save your arista.com API token'}
              onClick={() => setEditToken((v) => !v)}>
              {token.data?.configured ? 'Token…' : 'Set token'}
            </button>
          </div>
          {editToken || noToken ? (
            <TokenForm configured={!!token.data?.configured} hint={noToken ? (catalog.error instanceof ApiError ? catalog.error.message : token.data?.hint ?? 'no arista.com token configured')
              : badToken ? `The saved token was rejected: ${(catalog.error as ApiError).message}` : null}
              onSaved={tokenSaved} onCancel={noToken ? undefined : () => setEditToken(false)} />
          ) : catalog.isLoading ? <Spinner label="Querying arista.com…" /> : catalog.error ? (
            <div className="space-y-2 p-3">
              <ErrorBox error={catalog.error} />
              {badToken && (
                <div className="flex items-center gap-2 text-xs text-zinc-600 dark:text-zinc-300" data-testid="bad-token">
                  <span>arista.com rejected the request — most often a mistyped or expired token.</span>
                  <button type="button" className="btn-primary btn-sm" onClick={() => setEditToken(true)}>Replace token</button>
                  <button type="button" className="btn-secondary btn-sm" onClick={() => catalog.refetch()}>Retry</button>
                </div>
              )}
            </div>
          ) : (
            <>
              <div className="border-b border-zinc-200 p-2 dark:border-zinc-800">
                <input className="input py-0.5 text-xs" placeholder="filter e.g. 4.34" value={filter} onChange={(e) => setFilter(e.target.value)} />
              </div>
              <ul className="min-h-0 flex-1 overflow-auto text-sm" data-testid="catalog">
                {versions.map((v) => (
                  <li key={v} className="flex items-center justify-between border-b border-zinc-100 px-3 py-1 dark:border-zinc-800/60">
                    <span className="mono">{v}</span>
                    {have.has(v) ? <span className="text-xs text-emerald-600">downloaded</span> : (
                      <button className="btn-secondary btn-sm" disabled={download.isPending} onClick={() => download.mutate(v)}>Download</button>
                    )}
                  </li>
                ))}
                {!versions.length && <li className="p-3 text-xs text-zinc-500">No versions.</li>}
              </ul>
              <ErrorBox error={download.error} />
            </>
          )}
        </section>
      </div>
    </div>
  )
}

function TokenForm({ hint, configured, onSaved, onCancel }: { hint: string | null; configured: boolean; onSaved: () => void; onCancel?: () => void }) {
  const [token, setToken] = useState('')
  const [show, setShow] = useState(false)
  const save = useMutation({ mutationFn: () => imagesApi.setToken(token.trim()), onSuccess: onSaved })
  const remove = useMutation({ mutationFn: imagesApi.removeToken, onSuccess: onSaved })
  return (
    <form className="space-y-2 p-3 text-sm" data-testid="token-form" onSubmit={(e) => { e.preventDefault(); save.mutate() }}>
      {hint && <div className="rounded border border-amber-300 bg-amber-50 px-2 py-1 text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-100">{hint}</div>}
      <Field label={configured ? 'New arista.com API token (replaces the saved one)' : 'arista.com API token'}
        help="arista.com → My Profile → Access Token. Stored encrypted as secret “arista_token”; never shown again or written to logs.">
        <div className="flex gap-1">
          <input className="input mono flex-1" type={show ? 'text' : 'password'} autoComplete="off" spellCheck={false} value={token}
            onChange={(e) => setToken(e.target.value)} required minLength={8} placeholder="paste the token" />
          <button type="button" className="btn-secondary btn-sm" onClick={() => setShow((v) => !v)} title="show/hide while typing">{show ? 'Hide' : 'Show'}</button>
        </div>
      </Field>
      <ErrorBox error={save.error ?? remove.error} />
      <div className="flex flex-wrap items-center gap-2">
        <button className="btn-primary" disabled={save.isPending || token.trim().length < 8}>{save.isPending ? 'Saving…' : configured ? 'Replace token' : 'Save token'}</button>
        {configured && (
          <button type="button" className="btn-secondary" disabled={remove.isPending} title="forget the saved token (catalogue and downloads stop working until a new one is saved)"
            onClick={() => { if (window.confirm('Remove the saved arista.com token?')) remove.mutate() }}>Remove</button>
        )}
        {onCancel && <button type="button" className="btn-secondary" onClick={onCancel}>Cancel</button>}
      </div>
    </form>
  )
}

function UploadCard({ onDone, formats }: { onDone: () => void; formats: ImageFormat[] }) {
  const fileRef = useRef<HTMLInputElement>(null)
  const [meta, setMeta] = useState({ fmt: 'cEOS', version: '' })
  const [file, setFile] = useState<File | null>(null)
  const up = useMutation({
    mutationFn: () => imagesApi.upload(file!, meta),
    onSuccess: () => {
      setFile(null)
      if (fileRef.current) fileRef.current.value = ''
      onDone()
    },
  })
  const pick = (f: File | null) => {
    setFile(f)
    const m = f?.name.match(/(\d+\.\d+\.\d+[A-Z]?)/)
    if (m && !meta.version) setMeta((x) => ({ ...x, version: m[1] }))
  }
  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (file) up.mutate()
  }
  return (
    <form onSubmit={submit} className="card mt-4 p-3">
      <div className="h2 mb-2">Manual upload</div>
      <div className="grid items-end gap-2 sm:grid-cols-[1fr_8rem_8rem_auto]">
        <Field label="Image file (.tar / .tar.xz)">
          <input ref={fileRef} type="file" accept=".tar,.xz,.tar.xz" className="block w-full text-xs" onChange={(e) => pick(e.target.files?.[0] ?? null)} />
        </Field>
        <Field label="Format">
          <select className="input" value={meta.fmt} onChange={(e) => setMeta({ ...meta, fmt: e.target.value })}>
            {formats.map((f) => <option key={f.fmt} value={f.fmt}>{f.fmt} ({f.arch})</option>)}
          </select>
        </Field>
        <Field label="Version"><input className="input mono" required value={meta.version} onChange={(e) => setMeta({ ...meta, version: e.target.value })} placeholder="4.34.2F" /></Field>
        <button className="btn-primary" disabled={!file || up.isPending}>{up.isPending ? 'Uploading…' : 'Upload'}</button>
      </div>
      <ErrorBox error={up.error} />
    </form>
  )
}
