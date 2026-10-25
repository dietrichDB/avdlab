import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { labsApi, labCustomKnobs, labKnobValues } from '../../api/labs'
import { hostsApi } from '../../api/hosts'
import { cvApi } from '../../api/cloudvision'
import { cvMode } from '../../lib/cloudvision'
import { ApiError } from '../../api/client'
import type { Lab, LabTarget, Violation } from '../../api/types'
import ImagePicker from '../../components/ImagePicker'
import { KnobForm, defaultKnobValues, validateKnob, type KnobValues } from '../../components/KnobForm'
import { ErrorBox, Field, Spinner } from '../../components/ui'
import DesignerPanel, { DesignWithAiButton } from '../../components/ai/DesignerPanel'

/** Only send knobs that differ from what the lab has now. */
export function changedKnobs(before: KnobValues, after: KnobValues): KnobValues {
  const out: KnobValues = {}
  for (const [k, v] of Object.entries(after)) if (JSON.stringify(before[k]) !== JSON.stringify(v)) out[k] = v
  return out
}

export default function SettingsTab({ lab }: { lab: Lab }) {
  const qc = useQueryClient()
  const templates = useQuery({ queryKey: ['labs', 'templates'], queryFn: labsApi.templates, staleTime: 60_000 })
  const hosts = useQuery({ queryKey: ['hosts'], queryFn: hostsApi.list })
  const tpl = templates.data?.find((t) => t.name === lab.template)
  const initial = useMemo(() => ({ ...(tpl ? defaultKnobValues(tpl.knobs) : {}), ...labKnobValues(lab) }), [tpl, lab])
  const [values, setValues] = useState<KnobValues>(initial)
  const [hostId, setHostId] = useState(lab.host_id ?? '')
  const [ceos, setCeos] = useState(lab.ceos_version ?? '')
  const [target, setTarget] = useState<LabTarget>(lab.target ?? 'clab')
  const cvTargets = useQuery({ queryKey: ['cv', 'targets'], queryFn: cvApi.targets })
  // rows of the device tags editor (absent until the lab is built: the editor then takes free-form hostnames)
  const devices = useQuery({ queryKey: ['labs', lab.id, 'devices'], queryFn: () => labsApi.devices(lab.id), retry: false })
  const hostnames = useMemo(() => (devices.data ?? []).map((d) => d.hostname), [devices.data])
  const [cvTarget, setCvTarget] = useState(lab.cv_target_id ?? '')
  const [violations, setViolations] = useState<Violation[]>([])
  const [saved, setSaved] = useState(false)
  const [aiOpen, setAiOpen] = useState(false)
  useEffect(() => setValues(initial), [initial])

  const diff = changedKnobs(initial, values)
  const mode = cvMode(values)
  // a target can only be bound while cloudvision.mode != none (backend 409s otherwise)
  const effTarget = mode === 'none' ? '' : cvTarget
  const targetChanged = effTarget !== (lab.cv_target_id ?? '')
  const labTargetChanged = target !== (lab.target ?? 'clab')
  const dirty = Object.keys(diff).length > 0 || hostId !== (lab.host_id ?? '') || ceos !== (lab.ceos_version ?? '') || targetChanged || labTargetChanged
  const running = lab.state === 'deployed' || lab.state === 'validated'

  const save = useMutation({
    mutationFn: async () => {
      // unbinding goes first (mode may be switching to none); binding goes last (mode must already be != none)
      if (targetChanged && !effTarget) await labsApi.update(lab.id, { cv_target_id: null })
      const body = {
        ...(Object.keys(diff).length ? { knobs: diff } : {}),
        ...(hostId !== (lab.host_id ?? '') ? { host_id: hostId || null } : {}),
        ...(ceos !== (lab.ceos_version ?? '') ? { ceos_version: ceos || null } : {}),
        ...(labTargetChanged ? { target } : {}),
      }
      let l = Object.keys(body).length ? await labsApi.update(lab.id, body) : lab
      if (targetChanged && effTarget) l = await labsApi.update(lab.id, { cv_target_id: effTarget })
      else if (targetChanged && !Object.keys(body).length) l = await labsApi.get(lab.id)
      return l
    },
    onSuccess: (l) => {
      setViolations([])
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
      qc.setQueryData(['labs', lab.id], l)
      qc.invalidateQueries({ queryKey: ['labs'] })
    },
    onError: (e) => { if (e instanceof ApiError && e.status === 422) setViolations(e.violations) },
  })

  if (templates.isLoading) return <Spinner />
  if (!tpl) return <ErrorBox error={templates.error ?? `template ${lab.template} not found`} />
  const invalid = tpl.knobs.some((k) => validateKnob(k, values[k.key]))
  return (
    <div className="space-y-4">
      {/* 0.10.1: the action bar sits at the top-left of the tab (sticky) — the bottom-right spot was covered by the Feedback button */}
      <div className="sticky top-0 z-20 flex flex-wrap items-center gap-2 rounded-md border border-cv-border bg-cv-surface/95 px-3 py-2 shadow-sm backdrop-blur" data-testid="settings-actions">
        <button className="btn-primary" disabled={!dirty || invalid || save.isPending} onClick={() => save.mutate()} data-testid="settings-save">{save.isPending ? 'Validating…' : 'Save'}</button>
        <button className="btn-secondary" disabled={!dirty} onClick={() => { setValues(initial); setHostId(lab.host_id ?? ''); setCeos(lab.ceos_version ?? ''); setCvTarget(lab.cv_target_id ?? ''); setTarget(lab.target ?? 'clab'); setViolations([]) }}>Reset</button>
        {dirty && <span className="rounded bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-800 dark:bg-amber-900/50 dark:text-amber-200">{Object.keys(diff).length} knob(s) changed — unsaved</span>}
        {saved && !dirty && <span className="text-sm text-emerald-600">Saved, re-rendered and committed.</span>}
        {invalid && <span className="text-xs text-red-600">fix the highlighted knobs first</span>}
        <span className="ml-auto"><DesignWithAiButton onClick={() => setAiOpen(true)} /></span>
      </div>
      <DesignerPanel open={aiOpen} onClose={() => setAiOpen(false)} labId={lab.id} />
      <div className="card grid gap-3 p-3 sm:grid-cols-2 lg:grid-cols-4">
        <Field label="Target" help={running ? 'destroy the lab before changing the target' : target === 'act' ? 'Arista Cloud Test: digital-twin build, deploy through a host of kind act' : 'containerlab on the lab host'}>
          <select className="input" name="target" value={target} disabled={running} onChange={(e) => setTarget(e.target.value as LabTarget)}>
            <option value="clab">containerlab (clab)</option>
            <option value="act">Arista Cloud Test (act)</option>
          </select>
        </Field>
        <Field label="Host" help={target === 'act' && hostId && hosts.data?.find((h) => h.id === hostId)?.kind !== 'act' ? <span className="text-amber-600">an ACT lab deploys through a host of kind act (the tools server)</span> : undefined}>
          <select className="input" value={hostId} onChange={(e) => setHostId(e.target.value)}>
            <option value="">— none —</option>
            {(hosts.data ?? []).map((h) => <option key={h.id} value={h.id}>{h.name}{h.kind === 'act' ? ' (act)' : ''}</option>)}
          </select>
        </Field>
        <ImagePicker version={ceos} arch={values.ceos_arch === 'arm64' ? 'arm64' : 'x86_64'} hostId={hostId || undefined}
          onChange={(c) => { setCeos(c.version); if (tpl.knobs.some((k) => k.key === 'ceos_arch') && c.arch !== (values.ceos_arch ?? 'x86_64')) setValues((v) => ({ ...v, ceos_arch: c.arch })) }} />
        <Field label="CloudVision target" help={mode === 'none' ? 'set cloudvision.mode below to bind a target' : cvTargets.data?.length === 0 ? 'no targets yet: add one on the CloudVision page' : undefined}>
          <select className="input" name="cv_target_id" value={effTarget} disabled={mode === 'none'} onChange={(e) => setCvTarget(e.target.value)}>
            <option value="">— none —</option>
            {(cvTargets.data ?? []).map((t) => <option key={t.id} value={t.id}>{t.name} ({t.kind}, {t.server})</option>)}
          </select>
        </Field>
      </div>
      <div className="card p-3">
        <KnobForm knobs={tpl.knobs} values={values} onChange={setValues} violations={violations} customKeys={labCustomKnobs(lab)} hostnames={hostnames}
          segmentationHref={`/labs/${lab.id}/mss`} labTarget={target} />
      </div>
      {save.error && !(save.error instanceof ApiError && save.error.status === 422) && <ErrorBox error={save.error} />}
    </div>
  )
}
