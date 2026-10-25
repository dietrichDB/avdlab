import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { cvApi } from '../../api/cloudvision'
import { labKnobValues } from '../../api/labs'
import type { CVDeployParams, CVLastDeploy, CVOnboarding, Lab } from '../../api/types'
import { useJobDrawer } from '../../components/JobDrawer'
import { Badge, ErrorBox, KV, Spinner } from '../../components/ui'
import { cvActions, cvMode } from '../../lib/cloudvision'
import { fmtAgo, fmtDate } from '../../lib/format'
import { KindBadge, OnboardingTokenMark, TestConnection } from '../CloudVision'
import { Term } from '../../components/learn/Term'

const Yes = ({ label }: { label: string }) => <span className="font-semibold text-emerald-600 dark:text-emerald-400" aria-label={label} title={label}>✓</span>
const No = ({ label }: { label: string }) => <span className="font-semibold text-red-600 dark:text-red-400" aria-label={label} title={label}>✗</span>

function wsStateColour(state?: string | null) {
  const s = (state ?? '').toLowerCase()
  if (/(submitted|completed|success|approved)/.test(s)) return 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/60 dark:text-emerald-200'
  if (/(fail|abandon|error|cancel)/.test(s)) return 'bg-red-100 text-red-800 dark:bg-red-900/60 dark:text-red-200'
  return 'bg-amber-100 text-amber-800 dark:bg-amber-900/60 dark:text-amber-200'
}

/** per-node TerminAttr / streaming status from the last lab.cv_onboard */
export function CvOnboardingTable({ onboarding }: { onboarding: CVOnboarding }) {
  const nodes = onboarding.nodes
  const running = nodes.filter((n) => n.terminattr_running).length
  const streaming = nodes.filter((n) => n.streaming).length
  return (
    <div>
      <div className="mb-1 flex gap-3 px-2 text-xs text-zinc-500 dark:text-zinc-400" data-testid="cv-onboard-summary">
        <span>{nodes.length} nodes</span>
        <span className={running < nodes.length ? 'text-amber-600 dark:text-amber-400' : ''}>TerminAttr {running}/{nodes.length}</span>
        <span className={streaming < nodes.length ? 'text-amber-600 dark:text-amber-400' : ''}>streaming {streaming}/{nodes.length}</span>
        {onboarding.checked_at && <span title={fmtDate(onboarding.checked_at)}>checked {fmtAgo(onboarding.checked_at)}</span>}
      </div>
      <table className="table" data-testid="cv-onboard-table">
        <thead><tr><th>Node</th><th>Serial</th><th className="text-center">TerminAttr</th><th className="text-center">Streaming</th></tr></thead>
        <tbody>
          {nodes.map((n) => (
            <tr key={n.name} data-testid="cv-onboard-row">
              <td className="mono">{n.name}</td>
              <td className="mono text-zinc-500">{n.serial ?? '—'}</td>
              <td className="text-center">{n.terminattr_running ? <Yes label="TerminAttr running" /> : <No label="TerminAttr not running" />}</td>
              <td className="text-center">{n.streaming ? <Yes label="streaming" /> : <No label="not streaming" />}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** CloudVision's wording when the workspace built but had nothing to push */
export function noChangesDetected(result: CVLastDeploy): boolean {
  const state = (result.workspace?.state ?? '').toLowerCase()
  return !!result.workspace && !result.change_control && /submitted|completed|success/.test(state) && !(result.errors?.length)
}

/** result of the last lab.cv_deploy — null-safe: a failed run may have no workspace / change control at all (0.8.0) */
export function CvLastDeployPanel({ result, jobHref }: { result: CVLastDeploy; jobHref?: string }) {
  const ws = result.workspace
  const cc = result.change_control
  const errors = result.errors ?? []
  const warnings = result.warnings ?? []
  const devices = result.devices ?? []
  const failed = result.ok === false || errors.length > 0 || !ws
  const [showWarnings, setShowWarnings] = useState(false)
  return (
    <div className="space-y-3" data-testid="cv-last-deploy">
      {failed && (
        <div className="rounded border border-red-300 bg-red-50 px-2 py-1 text-sm text-red-900 dark:border-red-800 dark:bg-red-950/40 dark:text-red-100" data-testid="cv-deploy-failed">
          <span className="font-semibold">Deploy failed</span>{!ws && ' before CloudVision created a workspace'} —
          {errors.length ? ' see the errors below' : ' see the job log'}{jobHref && <> (<Link className="link" to={jobHref}>open job</Link>)</>}.
          {!ws && <div className="mt-1 text-xs">Typical causes: expired or wrong service-account token on the target (test the connection on the right), the lab host cannot reach {`the CloudVision server`} on 443, or the playbook aborted — the job log has the ansible output.</div>}
        </div>
      )}
      {!failed && noChangesDetected(result) && (
        <div className="rounded border border-emerald-300 bg-emerald-50 px-2 py-1 text-sm text-emerald-900 dark:border-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-100" data-testid="cv-no-changes">
          <span className="font-semibold">Workspace submitted, nothing to push.</span> CloudVision computed the designed config from the AVD configlets and it already
          equals what the devices run (they were deployed with the same intended configs), so no change control was needed. Change a knob, build and deploy again to see one.
        </div>
      )}
      <KV items={[
        ['Workspace', ws ? <span className="flex flex-wrap items-center gap-2">
          <span className="mono">{ws.id ?? '—'}</span>
          {ws.state && <Badge className={wsStateColour(ws.state)}>{ws.state}</Badge>}
          {ws.url && <a className="link text-xs" href={ws.url} target="_blank" rel="noreferrer">open in CloudVision ↗</a>}
        </span> : <span className="text-zinc-400">not created</span>],
        ['Change control', cc ? <span className="flex items-center gap-2"><span className="mono">{cc.id ?? '—'}</span>{cc.state && <Badge className={wsStateColour(cc.state)}>{cc.state}</Badge>}</span>
          : <span className="text-zinc-400">{noChangesDetected(result) ? 'not needed (no config changes)' : 'not created'}</span>],
        ['Finished', result.finished_at ? <span title={fmtDate(result.finished_at)}>{fmtAgo(result.finished_at)}</span> : result.at ? <span title={fmtDate(new Date(result.at * 1000).toISOString())}>{fmtAgo(new Date(result.at * 1000).toISOString())}</span> : '—'],
      ]} />
      {errors.length > 0 && (
        <ul className="space-y-1 text-sm" data-testid="cv-deploy-errors">
          {errors.map((e, i) => <li key={i} className="rounded border border-red-300 bg-red-50 px-2 py-1 text-red-800 dark:border-red-800 dark:bg-red-950/40 dark:text-red-200">{e}</li>)}
        </ul>
      )}
      {warnings.length > 0 && (
        <div data-testid="cv-deploy-warnings">
          <button type="button" className="link text-xs" onClick={() => setShowWarnings((v) => !v)}>
            {showWarnings ? 'Hide' : 'Show'} {warnings.length} workspace build warning(s)
          </button>
          {showWarnings && (
            <ul className="mt-1 space-y-1 text-sm">
              {warnings.map((w, i) => <li key={i} className="rounded border border-amber-300 bg-amber-50 px-2 py-1 text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-100">{w}</li>)}
            </ul>
          )}
          {!showWarnings && <div className="text-xs text-zinc-500">CloudVision lints the designed config before applying it (e.g. VLAN not yet present, portfast on peer-links); these are informational.</div>}
        </div>
      )}
      {devices.length > 0 && (
        <table className="table" data-testid="cv-deploy-devices">
          <thead><tr><th>Device</th><th>Serial</th><th className="text-center">On CloudVision</th><th className="text-center">Streaming</th></tr></thead>
          <tbody>
            {devices.map((d) => (
              <tr key={d.hostname} data-testid="cv-deploy-row">
                <td className="mono">{d.hostname}</td>
                <td className="mono text-zinc-500">{d.serial ?? '—'}</td>
                <td className="text-center">{d.exists_on_cv ? <Yes label="exists on CloudVision" /> : <No label="missing on CloudVision" />}</td>
                <td className="text-center">{d.streaming ? <Yes label="streaming" /> : <No label="not streaming" />}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

const DEFAULT_DEPLOY: CVDeployParams = { submit_workspace: true, run_change_control: false, skip_missing_devices: true, strict_tags: false, workspace_name: '' }

export default function CloudVisionTab({ lab }: { lab: Lab }) {
  const drawer = useJobDrawer()
  const status = useQuery({ queryKey: ['labs', lab.id, 'cv'], queryFn: () => cvApi.status(lab.id) })
  const [opts, setOpts] = useState<CVDeployParams>(DEFAULT_DEPLOY)
  const onboard = useMutation({ mutationFn: () => drawer.track(cvApi.onboard(lab.id)) })
  const deploy = useMutation({
    mutationFn: () => {
      const { workspace_name, ...rest } = opts
      return drawer.track(cvApi.deploy(lab.id, { ...rest, run_change_control: rest.submit_workspace && rest.run_change_control, ...(workspace_name?.trim() ? { workspace_name: workspace_name.trim() } : {}) }))
    },
  })
  const mode = cvMode(labKnobValues(lab))
  const target = status.data?.target ?? null
  const can = cvActions(lab.state, target, mode)
  const flag = (k: keyof Omit<CVDeployParams, 'workspace_name'>, label: React.ReactNode, disabled = false) => (
    <label className={`flex items-center gap-2 text-sm ${disabled ? 'opacity-50' : ''}`}>
      <input type="checkbox" name={k} checked={!!opts[k]} disabled={disabled} onChange={(e) => setOpts({ ...opts, [k]: e.target.checked })} />
      <span>{label}</span>
    </label>
  )

  if (status.isLoading) return <Spinner label="CloudVision status…" />
  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_22rem]">
      <div className="min-w-0 space-y-4">
        <ErrorBox error={status.error} />
        <section className="card overflow-x-auto p-2">
          <div className="mb-2 flex flex-wrap items-center gap-2 px-1">
            <span className="h2 mr-auto">Onboarding <span className="text-xs font-normal text-zinc-500">(<Term k="TerminAttr">TerminAttr</Term>)</span></span>
            <button className="btn-primary btn-sm" data-testid="cv-onboard" data-tour="cv-onboard" disabled={!can.onboard.enabled || onboard.isPending} title={can.onboard.reason} onClick={() => onboard.mutate()}>
              {onboard.isPending ? 'Starting…' : 'Onboard'}
            </button>
          </div>
          <ErrorBox error={onboard.error} />
          {!can.onboard.enabled && <div className="px-2 pb-1 text-xs text-zinc-500">{can.onboard.reason}</div>}
          {status.data?.onboarding?.nodes?.length
            ? <CvOnboardingTable onboarding={status.data.onboarding} />
            : <div className="p-4 text-center text-sm text-zinc-500 dark:text-zinc-400">Not onboarded yet. Onboard copies the onboarding token into every cEOS node and restarts TerminAttr.</div>}
        </section>
        <section className="card overflow-x-auto p-2">
          <div className="h2 mb-2 px-1">Last deploy</div>
          {status.data?.last_deploy
            ? <div className="px-1"><CvLastDeployPanel result={status.data.last_deploy} jobHref={status.data.last_deploy.job_id ? `/jobs/${status.data.last_deploy.job_id}` : undefined} /></div>
            : <div className="p-4 text-center text-sm text-zinc-500 dark:text-zinc-400">No CloudVision deploy yet.</div>}
        </section>
      </div>

      <div className="space-y-4">
        <section className="card space-y-3 p-3" data-testid="cv-target-summary">
          <div className="h2">Target</div>
          {target ? (<>
            <KV items={[
              ['Name', <span className="flex items-center gap-2">{target.name} <KindBadge kind={target.kind} /></span>],
              ['Server', <span className="mono">{target.server}</span>],
              ['Certs', target.verify_certs ? 'verify' : <span className="text-amber-600">no verify</span>],
              ['Token', target.has_token ? 'set' : <span className="text-red-600 dark:text-red-400">missing</span>],
              ['Onboarding token', <OnboardingTokenMark target={target} />],
              ['Lab mode', <span className="mono">{mode}</span>],
            ]} />
            <TestConnection target={target} />
          </>) : (
            <div className="text-sm text-zinc-500">
              No target bound. {mode === 'none' ? 'Set cloudvision.mode and pick a target' : 'Pick a target'} in <Link className="link" to={`/labs/${lab.id}/settings`}>Settings</Link>
              {' '}(targets live on the <Link className="link" to="/cloudvision">CloudVision</Link> page).
            </div>
          )}
        </section>

        <section className="card space-y-2 p-3" data-testid="cv-deploy-form" data-tour="cv-deploy">
          <div className="h2">Deploy to CloudVision</div>
          <div className="text-xs text-zinc-500"><Term k="cv_deploy">arista.avd.cv_deploy</Term>: pushes the built configs and tags into a CloudVision workspace.</div>
          {flag('submit_workspace', <>submit workspace <span className="text-xs text-zinc-500">(unchecked: build only, nothing is pushed)</span></>)}
          {flag('run_change_control', <>approve &amp; execute the change control <span className="text-xs text-zinc-500">(unchecked: it is created and waits for approval in CloudVision)</span></>, !opts.submit_workspace)}
          <div className="rounded bg-zinc-50 px-2 py-1 text-[11px] text-zinc-600 dark:bg-zinc-800/60 dark:text-zinc-300" data-testid="cv-cc-help">
            Submitting a workspace that changes configs always creates a change control — that is how CloudVision works. This box decides what happens to it:
            <span className="mono"> pending approval</span> (you approve and run it in CloudVision) or <span className="mono">completed</span> (AVD approves and executes it for you).
          </div>
          {flag('skip_missing_devices', 'skip devices missing on CloudVision')}
          {flag('strict_tags', 'strict tags (remove other tags)')}
          <label className="block text-sm">
            <span className="label">Workspace name <span className="font-normal text-zinc-400">(optional)</span></span>
            <input className="input" name="workspace_name" value={opts.workspace_name ?? ''} placeholder={`AVD ${lab.slug} …`} onChange={(e) => setOpts({ ...opts, workspace_name: e.target.value })} />
          </label>
          <ErrorBox error={deploy.error} />
          <button className="btn-primary w-full justify-center" data-testid="cv-deploy" disabled={!can.deploy.enabled || deploy.isPending} title={can.deploy.reason} onClick={() => deploy.mutate()}>
            {deploy.isPending ? 'Starting…' : 'Deploy to CloudVision'}
          </button>
          {!can.deploy.enabled && <div className="text-xs text-zinc-500">{can.deploy.reason}</div>}
        </section>
      </div>
    </div>
  )
}
