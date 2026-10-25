import { useEffect, useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AI_PROVIDERS, aiApi } from '../../api/ai'
import type { AiProbeResult, AiProvider } from '../../api/types'
import { useAuth } from '../../hooks/useAuth'
import { ConfirmButton, ErrorBox, Field, Spinner } from '../ui'
import { SparkleIcon } from './DesignerPanel'

export function ProbeResult({ r, testId = 'ai-test-result' }: { r: AiProbeResult; testId?: string }) {
  return (
    <span className={`text-xs ${r.ok ? 'text-emerald-700 dark:text-emerald-400' : 'text-red-600 dark:text-red-400'}`} data-testid={testId}>
      {r.ok
        ? `connected${r.models.length ? ` · ${r.models.length} model(s)` : ''}${r.model_listed === false ? ' · model not in the list (check the name)' : ''}`
        : `failed: ${r.error}`}
    </span>
  )
}

/** Account page: link your own Claude / OpenAI / local model. The API key is write-only. */
export function AiLinkSection() {
  const qc = useQueryClient()
  const { role } = useAuth()
  const q = useQuery({ queryKey: ['ai-link'], queryFn: aiApi.link })
  const [provider, setProvider] = useState<AiProvider>('anthropic')
  const [baseUrl, setBaseUrl] = useState('')
  const [model, setModel] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [saved, setSaved] = useState(false)
  useEffect(() => {
    const l = q.data?.link
    if (l) { setProvider(l.provider); setBaseUrl(l.base_url); setModel(l.model) }
  }, [q.data])

  const save = useMutation({
    mutationFn: () => aiApi.saveLink({ provider, base_url: baseUrl.trim(), model: model.trim(), ...(apiKey ? { api_key: apiKey } : {}) }),
    onSuccess: (v) => { qc.setQueryData(['ai-link'], v); setApiKey(''); setSaved(true) },
  })
  const remove = useMutation({
    mutationFn: aiApi.removeLink,
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['ai-link'] }); setModel(''); setBaseUrl(''); setApiKey(''); test.reset() },
  })
  const test = useMutation({ mutationFn: () => aiApi.testLink({ provider, base_url: baseUrl.trim(), model: model.trim(), ...(apiKey ? { api_key: apiKey } : {}) }) })
  const p = AI_PROVIDERS.find((x) => x.id === provider)!
  const submit = (e: FormEvent) => { e.preventDefault(); setSaved(false); save.mutate() }
  const link = q.data?.link
  const dirty = !!apiKey || (link ? provider !== link.provider || baseUrl.trim() !== link.base_url || model.trim() !== link.model : !!model.trim())

  return (
    <section className="card space-y-3 p-4" data-testid="ai-link-section">
      <div className="flex items-center gap-2">
        <SparkleIcon className="h-4 w-4 text-violet-600 dark:text-violet-400" />
        <h2 className="font-semibold">AI assistant</h2>
        {q.data && (
          <span className="text-xs text-zinc-500" data-testid="ai-link-status">
            {q.data.linked ? `linked: ${link?.provider} · ${link?.model}` : q.data.shared.available ? `using the shared model (${q.data.shared.model})` : 'not linked'}
          </span>
        )}
      </div>
      <p className="text-sm text-zinc-600 dark:text-zinc-400">
        Link Claude, OpenAI or a local model (Ollama, LM Studio, vLLM) to design labs from a sentence. The model only proposes AVD inputs
        (knob values and topology operations) — the server validates them with pyavd and nothing changes until you press Apply.
        Sent to the provider: the template's knobs, your lab's knob values and a topology summary — never passwords or other secrets.
      </p>
      {role === 'viewer' && <div className="text-xs text-amber-700 dark:text-amber-400">Viewers cannot use the designer (operator or admin role required).</div>}
      {q.isLoading ? <Spinner /> : (
        <form className="space-y-3" onSubmit={submit}>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Provider">
              <select className="input" value={provider} onChange={(e) => { setProvider(e.target.value as AiProvider); setSaved(false) }} data-testid="ai-provider">
                {AI_PROVIDERS.map((x) => <option key={x.id} value={x.id}>{x.label}</option>)}
              </select>
            </Field>
            <Field label="Model" help={test.data?.ok && test.data.models.length ? `pick one of the ${test.data.models.length} models the provider lists (from Test connection)` : `${p.modelHint} — press Test connection to list the models your key can use`}>
              <input className="input" list="ai-models" value={model} onChange={(e) => { setModel(e.target.value); setSaved(false) }} data-testid="ai-model" required />
              <datalist id="ai-models">{(test.data?.models ?? []).map((m) => <option key={m} value={m} />)}</datalist>
            </Field>
            <Field label="Base URL" help={provider === 'openai' ? 'empty = OpenAI; Ollama http://host:11434/v1; Azure https://<res>.openai.azure.com/openai/deployments/<name>' : 'empty = Anthropic API'}>
              <input className="input" value={baseUrl} onChange={(e) => { setBaseUrl(e.target.value); setSaved(false) }} placeholder={p.placeholder} data-testid="ai-base-url" />
            </Field>
            <Field label="API key" help={link?.has_api_key ? 'a key is stored (encrypted) — type to replace it' : provider === 'openai' ? 'optional for local servers (Ollama, LM Studio)' : 'stored encrypted; never shown again'}>
              <input className="input" type="password" value={apiKey} onChange={(e) => { setApiKey(e.target.value); setSaved(false) }}
                placeholder={link?.has_api_key ? '•••••••• (set)' : ''} autoComplete="off" data-testid="ai-api-key" />
            </Field>
          </div>
          <ErrorBox error={save.error ?? remove.error ?? test.error} />
          <div className="flex flex-wrap items-center gap-2">
            <button className="btn-primary" disabled={!model.trim() || save.isPending} data-testid="ai-save">{save.isPending ? 'Saving…' : 'Save'}</button>
            <button type="button" className="btn-secondary" disabled={!model.trim() || test.isPending} onClick={() => test.mutate()} data-testid="ai-test">
              {test.isPending ? 'Testing…' : 'Test connection'}
            </button>
            {q.data?.linked && <ConfirmButton className="btn-secondary" onConfirm={() => remove.mutate()} confirmText="Remove link and key?">Remove</ConfirmButton>}
            {saved && !dirty && <span className="text-xs text-emerald-700 dark:text-emerald-400" data-testid="ai-saved">saved</span>}
            {dirty && <span className="rounded bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-800 dark:bg-amber-900/50 dark:text-amber-200" data-testid="ai-unsaved">unsaved changes — press Save to keep them</span>}
            {test.data && <ProbeResult r={test.data} />}
          </div>
        </form>
      )}
    </section>
  )
}

/** Admin settings: the shared default model for users without their own link. */
export function AiSharedSection() {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: ['admin-ai'], queryFn: aiApi.shared })
  const [provider, setProvider] = useState<AiProvider | ''>('')
  const [baseUrl, setBaseUrl] = useState('')
  const [model, setModel] = useState('')
  const [allowed, setAllowed] = useState(false)
  const [apiKey, setApiKey] = useState('')
  const [clearKey, setClearKey] = useState(false)
  const [saved, setSaved] = useState(false)
  useEffect(() => {
    if (!q.data) return
    setProvider(q.data.ai_default_provider); setBaseUrl(q.data.ai_default_base_url); setModel(q.data.ai_default_model); setAllowed(q.data.ai_shared_allowed)
  }, [q.data])
  const save = useMutation({
    mutationFn: () => aiApi.saveShared({
      ai_default_provider: provider, ai_default_base_url: baseUrl.trim(), ai_default_model: model.trim(), ai_shared_allowed: allowed,
      ...(clearKey ? { ai_default_api_key: '' } : apiKey ? { ai_default_api_key: apiKey } : {}),
    }),
    onSuccess: (s) => { qc.setQueryData(['admin-ai'], s); qc.invalidateQueries({ queryKey: ['ai-link'] }); setApiKey(''); setClearKey(false); setSaved(true) },
  })
  const test = useMutation({ mutationFn: aiApi.testShared })
  if (q.isLoading) return <Spinner />
  const s = q.data
  const dirty = !!apiKey || clearKey || (!!s && (provider !== s.ai_default_provider || baseUrl.trim() !== s.ai_default_base_url
    || model.trim() !== s.ai_default_model || allowed !== s.ai_shared_allowed))
  return (
    <section className="card space-y-3 p-4" data-testid="ai-shared-section">
      <div>
        <div className="h2">AI assistant</div>
        <div className="text-xs text-zinc-500 dark:text-zinc-400">shared default model for users who did not link their own (Account page); the key is stored encrypted and never shown</div>
      </div>
      <ErrorBox error={q.error} />
      <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); setSaved(false); save.mutate() }}>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Provider">
            <select className="input" value={provider} onChange={(e) => { setProvider(e.target.value as AiProvider | ''); setSaved(false) }} data-testid="ai-shared-provider">
              <option value="">— none —</option>
              {AI_PROVIDERS.map((x) => <option key={x.id} value={x.id}>{x.label}</option>)}
            </select>
          </Field>
          <Field label="Model">
            <input className="input" value={model} onChange={(e) => { setModel(e.target.value); setSaved(false) }} data-testid="ai-shared-model" />
          </Field>
          <Field label="Base URL" help="empty = the provider's public API">
            <input className="input" value={baseUrl} onChange={(e) => { setBaseUrl(e.target.value); setSaved(false) }} data-testid="ai-shared-base-url" />
          </Field>
          <Field label="API key" help={s?.has_ai_default_api_key ? 'a key is stored — type to replace it' : 'optional for local servers'}>
            <input className="input" type="password" value={apiKey} autoComplete="off" onChange={(e) => { setApiKey(e.target.value); setClearKey(false); setSaved(false) }}
              placeholder={s?.has_ai_default_api_key ? '•••••••• (set)' : ''} data-testid="ai-shared-key" />
            {s?.has_ai_default_api_key && (
              <label className="mt-1 flex items-center gap-1 text-[11px] text-zinc-500">
                <input type="checkbox" checked={clearKey} onChange={(e) => { setClearKey(e.target.checked); setApiKey(''); setSaved(false) }} /> clear the stored key
              </label>
            )}
          </Field>
        </div>
        <label className="flex items-start gap-2 text-sm">
          <input type="checkbox" className="mt-0.5" checked={allowed} onChange={(e) => { setAllowed(e.target.checked); setSaved(false) }} data-testid="ai-shared-allowed" />
          <span>Allow users without their own link to use this model
            <span className="block text-[11px] text-zinc-500 dark:text-zinc-400">lab data (knob values, topology summary — never secrets) of every user is then sent to this provider</span>
          </span>
        </label>
        <ErrorBox error={save.error ?? test.error} />
        <div className="flex flex-wrap items-center gap-2">
          <button className="btn-primary" disabled={save.isPending} data-testid="ai-shared-save">{save.isPending ? 'Saving…' : 'Save AI settings'}</button>
          <button type="button" className="btn-secondary" disabled={!s?.configured || test.isPending} onClick={() => test.mutate()} data-testid="ai-shared-test">Test connection</button>
          {saved && !dirty && <span className="text-xs text-emerald-700 dark:text-emerald-400" data-testid="ai-shared-saved">saved</span>}
          {dirty && <span className="rounded bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-800 dark:bg-amber-900/50 dark:text-amber-200" data-testid="ai-shared-unsaved">unsaved changes — Test connection uses the saved values</span>}
          {test.data && <ProbeResult r={test.data} testId="ai-shared-test-result" />}
        </div>
      </form>
    </section>
  )
}
