import { useState, type FormEvent } from 'react'
import { useMutation } from '@tanstack/react-query'
import { hostsApi } from '../api/hosts'
import type { DevcontainerIn } from '../api/types'
import { ErrorBox, Field, PageHeader } from '../components/ui'

export function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

const FILES = [
  ['.devcontainer/devcontainer.json', 'container definition (+ docker-in-docker feature in local-clab mode)'],
  ['.devcontainer/Dockerfile', 'FROM the avdlab agent image, adds containerlab CLI, eos-downloader, docker CLI'],
  ['ansible.cfg', 'collections / inventory defaults'],
  ['requirements.txt', 'pinned pyavd / anta'],
  ['Makefile', 'build · deploy · validate · clab-up · clab-down'],
  ['README.md', 'how to open it in VS Code / Codespaces'],
]

export default function DevcontainerPage() {
  const [form, setForm] = useState<DevcontainerIn>({ mode: 'local-clab', avd_version: '', include_vscode_extensions: true })
  const [done, setDone] = useState<string | null>(null)
  const gen = useMutation({
    mutationFn: async () => {
      const body: DevcontainerIn = { ...form, avd_version: form.avd_version?.trim() || undefined }
      const blob = await hostsApi.devcontainer(body)
      const name = `avdlab-devcontainer-${form.mode}.zip`
      saveBlob(blob, name)
      return `${name} (${blob.size} bytes)`
    },
    onSuccess: setDone,
  })
  const submit = (e: FormEvent) => {
    e.preventDefault()
    setDone(null)
    gen.mutate()
  }
  return (
    <div className="max-w-3xl">
      <PageHeader title="Devcontainer" sub="Generate a VS Code devcontainer bundle to run AVD + containerlab outside the web UI" />
      <form onSubmit={submit} className="card space-y-4 p-4">
        <fieldset>
          <span className="label">Mode</span>
          <div className="space-y-1 text-sm">
            <label className="flex items-start gap-2">
              <input type="radio" name="mode" checked={form.mode === 'local-clab'} onChange={() => setForm({ ...form, mode: 'local-clab' })} className="mt-1" />
              <span><b>local-clab</b> — run containerlab inside the devcontainer (docker-in-docker). Needs a beefy laptop / Codespace.</span>
            </label>
            <label className="flex items-start gap-2">
              <input type="radio" name="mode" checked={form.mode === 'remote-host'} onChange={() => setForm({ ...form, mode: 'remote-host' })} className="mt-1" />
              <span><b>remote-host</b> — tooling only; deploy to a lab host over SSH.</span>
            </label>
          </div>
        </fieldset>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="AVD version" help="blank = server default">
            <input className="input mono" value={form.avd_version ?? ''} placeholder="e.g. 5.7.2" onChange={(e) => setForm({ ...form, avd_version: e.target.value })} />
          </Field>
          <label className="flex items-center gap-2 self-end pb-1 text-sm">
            <input type="checkbox" checked={form.include_vscode_extensions} onChange={(e) => setForm({ ...form, include_vscode_extensions: e.target.checked })} />
            Include recommended VS Code extensions
          </label>
        </div>
        <ErrorBox error={gen.error} />
        {done && <div className="text-sm text-emerald-600">Downloaded {done}</div>}
        <div className="flex justify-end">
          <button className="btn-primary" disabled={gen.isPending}>{gen.isPending ? 'Generating…' : 'Download zip'}</button>
        </div>
      </form>
      <section className="card mt-4 p-4">
        <div className="h2 mb-2">Bundle contents</div>
        <table className="table">
          <tbody>
            {FILES.map(([f, d]) => (
              <tr key={f}><td className="mono whitespace-nowrap">{f}</td><td className="text-zinc-500">{d}</td></tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  )
}
