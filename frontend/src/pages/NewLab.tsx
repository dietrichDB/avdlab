import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { labsApi } from '../api/labs'
import { hostsApi } from '../api/hosts'
import { ApiError } from '../api/client'
import type { Arch, LabTarget, Template, Violation } from '../api/types'
import ImagePicker from '../components/ImagePicker'
import { KnobForm, defaultKnobValues, validateKnob, type KnobValues } from '../components/KnobForm'
import { ErrorBox, Field, Hint, PageHeader, Spinner } from '../components/ui'
import { cls } from '../lib/format'
import { Term } from '../components/learn/Term'
import DesignerPanel, { DesignWithAiButton, SparkleIcon } from '../components/ai/DesignerPanel'
import { aiApi } from '../api/ai'

import type { AiDesignOut } from '../api/types'

type Step = 0 | 1 | 2

const STEPS = ['Template', 'Knobs & target', 'Host & image']

export default function NewLabPage() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const templates = useQuery({ queryKey: ['labs', 'templates'], queryFn: labsApi.templates, staleTime: 60_000 })
  const hosts = useQuery({ queryKey: ['hosts'], queryFn: hostsApi.list })

  const [step, setStep] = useState<Step>(0)
  const [tpl, setTpl] = useState<Template | null>(null)
  const [name, setName] = useState('')
  const [knobs, setKnobs] = useState<KnobValues>({})
  const [hostId, setHostId] = useState<string>('')
  const [ceos, setCeos] = useState<string>('')
  const [target, setTarget] = useState<LabTarget>('clab')
  const [violations, setViolations] = useState<Violation[]>([])
  const [aiNote, setAiNote] = useState<AiDesignOut | null>(null)
  const [aiOpen, setAiOpen] = useState(false)

  const pickTemplate = (t: Template) => {
    setTpl(t)
    setKnobs(defaultKnobValues(t.knobs))
    const ceosKnob = t.knobs.find((k) => k.key === 'ceos_version')
    if (ceosKnob?.default) setCeos(String(ceosKnob.default))
    if (!name) setName(t.name.replace(/-l3ls|-l2ls|-fabric/, '') + '-lab')
    setViolations([])
    setStep(1)
  }

  const create = useMutation({
    mutationFn: () => labsApi.create({ name, template: tpl!.name, host_id: hostId || null, ceos_version: ceos || null, target, knobs: ceos && tpl?.knobs.some((k) => k.key === 'ceos_version') ? { ...knobs, ceos_version: ceos } : knobs }),
    onSuccess: (lab) => {
      qc.invalidateQueries({ queryKey: ['labs'] })
      navigate(`/labs/${lab.id}`)
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 422) {
        setViolations(e.violations)
        setStep(1)
      }
    },
  })

  const localErrors = tpl ? tpl.knobs.filter((k) => validateKnob(k, knobs[k.key])) : []
  const hasArchKnob = !!tpl?.knobs.some((k) => k.key === 'ceos_arch')
  const arch: Arch = knobs.ceos_arch === 'arm64' ? 'arm64' : 'x86_64'
  const pickImage = (c: { version: string; arch: Arch }) => {
    setCeos(c.version)
    if (hasArchKnob && c.arch !== arch) setKnobs((k) => ({ ...k, ceos_arch: c.arch }))
  }

  return (
    <div>
      <PageHeader title="New lab" sub={tpl ? <>template <span className="mono">{tpl.name}</span></> : 'pick a template'} />
      <Hint title="Three steps.">Pick a template (single-DC L3LS is a good first fabric), tune the knobs or keep the defaults, then name the lab — nothing is deployed until you press Deploy on the lab page.</Hint>
      <ol className="mb-4 flex gap-1 text-xs">
        {STEPS.map((s, i) => (
          <li key={s}>
            <button
              disabled={i > 0 && !tpl}
              onClick={() => setStep(i as Step)}
              className={cls('rounded px-2 py-1', step === i ? 'bg-blue-600 text-white' : 'bg-zinc-200 text-zinc-700 disabled:opacity-50 dark:bg-zinc-800 dark:text-zinc-300')}
            >
              {i + 1}. {s}
            </button>
          </li>
        ))}
      </ol>

      {step === 0 && (
        templates.isLoading ? <Spinner /> : <>
          <ErrorBox error={templates.error} />
          <FromScratch templates={templates.data ?? []} onResult={(out) => {
            const t = (templates.data ?? []).find((x) => x.name === out.template)
            if (!t) return
            setTpl(t)
            setKnobs({ ...defaultKnobValues(t.knobs), ...(out.knobs ?? {}) })
            const ceosKnob = t.knobs.find((k) => k.key === 'ceos_version')
            if (ceosKnob?.default) setCeos(String(ceosKnob.default))
            if (out.lab_name) setName(out.lab_name)
            setAiNote(out)
            setViolations([])
            setStep(1)
          }} />
          <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-cv-muted">…or pick a template yourself</div>
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3" data-testid="templates">
            {(templates.data ?? []).map((t) => <TemplateCard key={t.name} t={t} selected={tpl?.name === t.name} onPick={() => pickTemplate(t)} />)}
          </div>
        </>
      )}

      {step === 1 && tpl && (
        <div className="space-y-4">
          <div className="card grid gap-3 p-3 sm:grid-cols-2">
            <Field label="Lab name" help="used for the lab directory slug, containerlab topology name and serial numbers">
              <input className="input max-w-sm" value={name} onChange={(e) => setName(e.target.value)} required />
            </Field>
            <Field label="Target" help={target === 'act' ? 'Arista Cloud Test: the build runs pyavd in digital-twin mode and writes the ACT topology; deploy goes through the ACT tools server (host of kind act)' : 'containerlab on the lab host'}>
              <select className="input" name="target" value={target} onChange={(e) => setTarget(e.target.value as LabTarget)}>
                <option value="clab">containerlab (clab)</option>
                <option value="act">Arista Cloud Test (act)</option>
              </select>
            </Field>
          </div>
          {aiNote && (
            <div className="rounded-lg border border-violet-200 bg-violet-50 px-3 py-2 text-sm text-violet-900 dark:border-violet-900 dark:bg-violet-950/40 dark:text-violet-100" data-testid="ai-from-scratch-note">
              <div className="flex items-start gap-2">
                <SparkleIcon className="mt-0.5 h-4 w-4 shrink-0" />
                <div>
                  <span className="font-semibold">AI picked {aiNote.template}</span>{aiNote.template_reason ? ` — ${aiNote.template_reason}` : ''}.
                  {aiNote.summary && <span> {aiNote.summary}</span>}
                  {Object.keys(aiNote.knobs ?? {}).length > 0 && <span className="mono ml-1 text-xs">({Object.keys(aiNote.knobs).join(', ')} set)</span>}
                  {!!aiNote.assumptions?.length && <ul className="mt-1 list-disc pl-5 text-xs">{aiNote.assumptions.map((a, i) => <li key={i}>{a}</li>)}</ul>}
                  <div className="mt-1 text-xs opacity-80">Review the knobs below — nothing is created until you press Create lab. Use <em>Design with AI</em> to refine.</div>
                </div>
              </div>
            </div>
          )}
          <div className="flex items-center justify-end">
            <DesignWithAiButton onClick={() => setAiOpen(true)} />
          </div>
          <DesignerPanel open={aiOpen} onClose={() => setAiOpen(false)} template={tpl.name} currentKnobs={knobs}
            onUseKnobs={(k) => { setKnobs((cur) => ({ ...cur, ...k })); setViolations([]) }} />
          <div className="card p-3">
            <p className="mb-2 text-xs text-zinc-500">Knobs write the <Term k="AVD Design">AVD design</Term> inputs (<Term k="group_vars">group_vars</Term>) for you; the build runs <Term k="eos_designs">eos_designs</Term> and <Term k="eos_cli_config_gen">eos_cli_config_gen</Term>.</p>
            <KnobForm knobs={tpl.knobs} values={knobs} onChange={setKnobs} violations={violations} labTarget={target} />
          </div>
          <div className="flex justify-between">
            <button className="btn-secondary" onClick={() => setStep(0)}>Back</button>
            <button className="btn-primary" disabled={!name || localErrors.length > 0} onClick={() => setStep(2)}>Next</button>
          </div>
        </div>
      )}

      {step === 2 && tpl && (
        <div className="max-w-2xl space-y-4">
          <div className="card grid gap-3 p-3 sm:grid-cols-2">
            <Field label={target === 'act' ? 'ACT tools server host' : 'Lab host'} help={target === 'act' ? 'optional now: add it once the ACT lab runs' : 'optional now; required to deploy'}>
              <select className="input" value={hostId} onChange={(e) => { setHostId(e.target.value); const hh = (hosts.data ?? []).find((x) => x.id === e.target.value); const a = hh?.summary?.arch; if (hasArchKnob && a && /arm64|aarch64/.test(a) && arch !== 'arm64') { setKnobs((k) => ({ ...k, ceos_arch: 'arm64' })); setCeos('') } }}>
                <option value="">— none —</option>
                {(hosts.data ?? []).filter((h) => target !== 'act' || h.kind === 'act').map((h) => <option key={h.id} value={h.id}>{h.name} ({h.address})</option>)}
              </select>
            </Field>
            <ImagePicker version={ceos} arch={arch} onChange={pickImage} hostId={hostId || undefined}
              label={target === 'act' ? 'cEOS image (unused on ACT: see knob act.os_version)' : 'cEOS image'} />
          </div>
          {violations.length > 0 && (
            <ul className="list-disc rounded border border-red-300 bg-red-50 px-6 py-2 text-xs text-red-800 dark:border-red-800 dark:bg-red-950/40 dark:text-red-200" data-testid="violations">
              {violations.map((v, i) => <li key={i}>{v.path && <span className="mono mr-1">{v.path}:</span>}{v.message}</li>)}
            </ul>
          )}
          {create.error && !(create.error instanceof ApiError && create.error.status === 422) && <ErrorBox error={create.error} />}
          <div className="flex justify-between">
            <button className="btn-secondary" onClick={() => setStep(1)}>Back</button>
            <button className="btn-primary" disabled={create.isPending || !name} onClick={() => create.mutate()}>{create.isPending ? 'Rendering + validating…' : 'Create lab'}</button>
          </div>
        </div>
      )}
    </div>
  )
}

/** CONTRACT-6 §2: tags, node count, notes and the template's `learn` paragraph. */
export function TemplateCard({ t, selected, onPick }: { t: Template; selected: boolean; onPick: () => void }) {
  const [more, setMore] = useState(false)
  const docUrl = /https?:\/\/\S+/.exec(t.learn ?? '')?.[0]?.replace(/[).,]+$/, '')
  const learnText = (t.learn ?? '').replace(/AVD doc:\s*https?:\/\/\S+/, '').trim()
  return (
    <div className={cls('card flex flex-col p-3 text-left hover:border-blue-500', selected && 'border-blue-500 ring-1 ring-blue-500')} data-tour="template-card" data-testid={`template-${t.name}`}>
      <button type="button" className="text-left" onClick={onPick}>
        <div className="font-semibold">{t.title}</div>
        <div className="mono mb-1 text-zinc-500">{t.name}{t.generator ? ` · ${t.generator}` : ''}</div>
        <div className="line-clamp-3 text-xs text-zinc-600 dark:text-zinc-300">{t.description}</div>
      </button>
      {!!t.tags?.length && (
        <div className="mt-2 flex flex-wrap gap-1" data-testid="template-tags">
          {t.tags.map((tag) => <span key={tag} className="rounded bg-zinc-100 px-1.5 py-0.5 text-[10px] font-medium text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300"><Term k={tag}>{tag}</Term></span>)}
        </div>
      )}
      {t.notes && <div className="mt-2 rounded border border-amber-300 bg-amber-50 px-2 py-1 text-[11px] text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-100" data-testid="template-notes">{t.notes}</div>}
      {learnText && (
        <div className="mt-2 text-[11px] text-zinc-600 dark:text-zinc-400" data-testid="template-learn">
          <span className="font-semibold text-cyan-700 dark:text-cyan-300">What it teaches: </span>
          <span className={more ? '' : 'line-clamp-2'}>{learnText}</span>
          <span className="ml-1 whitespace-nowrap">
            <button type="button" className="link" onClick={() => setMore((m) => !m)}>{more ? 'less' : 'more'}</button>
            {docUrl && <a className="link ml-2" href={docUrl} target="_blank" rel="noreferrer">AVD example</a>}
          </span>
        </div>
      )}
      <div className="mt-auto flex items-center gap-2 pt-2 text-[11px] text-zinc-400">
        {t.node_count != null && <span data-testid="template-nodes">{t.node_count} switches</span>}
        <span>{t.knobs.length} knobs</span>
        <span className="flex-1" />
        <button type="button" className="btn-secondary btn-sm" onClick={onPick}>Use</button>
      </div>
    </div>
  )
}

/** 0.10.0: step 0 "from scratch" — one sentence → the model picks the template and fills the knobs; the wizard continues at step 2. */
function FromScratch({ templates, onResult }: { templates: Template[]; onResult: (out: AiDesignOut) => void }) {
  const [prompt, setPrompt] = useState('')
  const link = useQuery({ queryKey: ['ai-link'], queryFn: aiApi.link, retry: false })
  const design = useMutation({
    mutationFn: () => aiApi.design({ prompt: prompt.trim() }),
    onSuccess: (out) => {
      if (out.template && out.kind !== 'question') onResult(out)
    },
  })
  const noLink = link.data && !link.data.effective
  const examples = ['2 spines, 3 leaf pairs with MLAG, 2 servers per pair, tenant ACME with VLANs 110 and 120, CloudVision cvaas',
    'a small L2LS campus-style lab with one spine pair', 'dual data centre EVPN with DCI']
  return (
    <section className="card mb-4 border-violet-200 p-4 dark:border-violet-900" data-testid="ai-from-scratch">
      <div className="flex items-center gap-2">
        <SparkleIcon className="h-5 w-5 text-violet-600 dark:text-violet-400" />
        <div className="h2">Start from a sentence</div>
        <span className="text-xs text-cv-muted">the AI picks the template and fills the knobs; you review before anything is created</span>
      </div>
      {noLink ? (
        <div className="mt-2 text-sm text-cv-muted">Link Claude, OpenAI or a local model on your <Link className="link" to="/account">Account</Link> page to use this.</div>
      ) : (
        <form className="mt-2 space-y-2" onSubmit={(e) => { e.preventDefault(); if (prompt.trim() && !design.isPending) design.mutate() }}>
          <textarea className="input min-h-[4.5rem]" value={prompt} onChange={(e) => setPrompt(e.target.value)} data-testid="ai-scratch-prompt"
            placeholder={examples[0]} disabled={design.isPending} />
          <div className="flex flex-wrap items-center gap-2">
            <button className="btn-primary" disabled={!prompt.trim() || design.isPending || !templates.length} data-testid="ai-scratch-go">
              {design.isPending ? 'Designing…' : 'Design my lab'}
            </button>
            {examples.map((ex) => (
              <button key={ex} type="button" className="btn-ghost btn-sm text-xs" onClick={() => setPrompt(ex)} disabled={design.isPending}>{ex.length > 48 ? ex.slice(0, 46) + '…' : ex}</button>
            ))}
          </div>
          {design.isPending && <div className="text-xs text-cv-muted">two model calls (pick a template, then propose knobs) and a pyavd dry run — up to a minute</div>}
          {design.data?.kind === 'question' && (
            <div className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-100" data-testid="ai-scratch-question">
              The model asks: {design.data.question} — add the answer to your sentence and try again.
            </div>
          )}
          <ErrorBox error={design.error} />
        </form>
      )}
    </section>
  )
}

