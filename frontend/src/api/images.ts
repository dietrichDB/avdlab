import { api } from './client'
import type { Arch, Image, ImageCatalog, ImageFormat, ImageToken, JobRef } from './types'

function normaliseCatalog(fmt: string, raw: unknown): ImageCatalog {
  if (Array.isArray(raw)) return { fmt, versions: raw.map(String) }
  const o = (raw ?? {}) as Record<string, unknown>
  const versions = Array.isArray(o.versions) ? o.versions.map(String) : []
  return { fmt: String(o.fmt ?? fmt), versions }
}

export const FALLBACK_FORMATS: ImageFormat[] = [
  { fmt: 'cEOS', arch: 'x86_64', label: 'cEOS (x86_64)' },
  { fmt: 'cEOS64', arch: 'x86_64', label: 'cEOS64 (x86_64, 64-bit)' },
  { fmt: 'cEOSarm', arch: 'arm64', label: 'cEOSarm (arm64)' },
]

/** CONTRACT-3 §6: arch is derived from the format when the backend does not send it */
export function imageArch(img: Pick<Image, 'fmt' | 'arch'>): Arch {
  if (img.arch === 'arm64' || img.arch === 'x86_64') return img.arch
  return /arm/i.test(img.fmt) ? 'arm64' : 'x86_64'
}

/** normalise uname -m style values: aarch64 → arm64, amd64 → x86_64 */
export function normArch(a?: string | null): Arch | null {
  if (!a) return null
  const s = a.toLowerCase()
  if (s === 'aarch64' || s === 'arm64') return 'arm64'
  if (s === 'x86_64' || s === 'amd64') return 'x86_64'
  return null
}

export const imagesApi = {
  formats: async (): Promise<ImageFormat[]> => {
    const r = await api.get<ImageFormat[]>('/images/formats')
    return Array.isArray(r) && r.length ? r : FALLBACK_FORMATS
  },
  list: () => api.get<Image[]>('/images'),
  /** arista.com token status (the value is never returned) */
  token: () => api.get<ImageToken>('/images/token'),
  /** create or replace the arista.com token in place; the backend drops its catalogue cache */
  setToken: (value: string) => api.put<ImageToken>('/images/token', { value }),
  removeToken: () => api.delete<void>('/images/token'),
  catalog: async (fmt = 'cEOS') => normaliseCatalog(fmt, await api.get<unknown>(`/images/catalog?fmt=${encodeURIComponent(fmt)}`)),
  download: (fmt: string, version: string) => api.post<JobRef>('/images/download', { fmt, version }),
  push: (imageId: string, hostId: string, options: { remote_download?: boolean } = {}) =>
    api.post<JobRef>(`/images/${imageId}/push/${hostId}`, { options }),
  upload: (file: File, meta: { fmt: string; version: string }) => {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('fmt', meta.fmt)
    fd.append('version', meta.version)
    return api.post<Image>('/images/upload', fd)
  },
}
