import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import { labsApi } from '../../api/labs'
import { useJobDrawer } from '../../components/JobDrawer'
import { ConfirmButton, ErrorBox, LabStateBadge, PageHeader, Spinner, Tabs } from '../../components/ui'
import ErrorBoundary from '../../components/ErrorBoundary'
import OverviewTab from './OverviewTab'
import SettingsTab from './SettingsTab'
import FilesTab from './FilesTab'
import BuildTab from './BuildTab'
import TopologyTab from './TopologyTab'
import LabJobsTab from './LabJobsTab'
import RuntimeTab from './RuntimeTab'
import ValidationTab from './ValidationTab'
import CloudVisionTab from './CloudVisionTab'
import ClientsTab from './ClientsTab'
import DevicesTab from './DevicesTab'
import GitTab from './GitTab'
import NetBoxTab from './NetBoxTab'
import MssTab from './MssTab'
import ActTab from './ActTab'
import { gitApi } from '../../api/git'
import { useUiMode } from '../../lib/uiMode'
import { isRunning, labActions } from '../../lib/labActions'
import type { Lab } from '../../api/types'

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'settings', label: 'Settings' },
  { id: 'files', label: 'Files' },
  { id: 'build', label: 'Build' },
  { id: 'topology', label: 'Topology' },
  { id: 'runtime', label: 'Runtime' },
  { id: 'devices', label: 'Devices' },
  { id: 'clients', label: 'Clients' },
  { id: 'validation', label: 'Validation' },
  { id: 'cloudvision', label: 'CloudVision' },
  { id: 'mss', label: 'MSS' },
  { id: 'netbox', label: 'NetBox' },
  { id: 'act', label: 'ACT' },
  { id: 'git', label: 'Git & VS Code' },
  { id: 'jobs', label: 'Jobs' },
] as const
type TabId = (typeof TABS)[number]['id']
/** CONTRACT-8 B §2: hidden in beginner mode (the current tab stays visible when opened by URL) */
export const ADVANCED_TABS: ReadonlySet<string> = new Set(['mss', 'netbox', 'act', 'git'])

export default function LabDetailPage() {
  const { id = '', tab = 'overview' } = useParams()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const drawer = useJobDrawer()
  const ui = useUiMode()
  const lab = useQuery({ queryKey: ['labs', id], queryFn: () => labsApi.get(id) })
  const build = useMutation({ mutationFn: () => drawer.track(labsApi.build(id)) })
  const deploy = useMutation({ mutationFn: () => drawer.track(labsApi.deploy(id, { reconfigure: true, wait_eapi: true, wait_timeout: 600 })) })
  const validate = useMutation({ mutationFn: () => drawer.track(labsApi.validate(id, { extra_fabric_validation: true, skip_tests: ['VerifyNTP', 'VerifyReloadCause'], user_catalogs: false, tags: [] })) })
  const destroy = useMutation({ mutationFn: () => drawer.track(labsApi.destroy(id)) })
  const remove = useMutation({
    mutationFn: () => labsApi.remove(id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['labs'] }); navigate('/labs') },
  })
  const current = (TABS.some((t) => t.id === tab) ? tab : 'overview') as TabId

  if (lab.isLoading) return <Spinner />
  if (!lab.data) return <ErrorBox error={lab.error ?? 'lab not found'} />
  const l = lab.data
  const can = labActions(l.state)
  const noHost = !l.host_id
  // the ACT tab only exists for target act (CONTRACT-4 D)
  const tabs = TABS.filter((t) => t.id !== 'act' || l.target === 'act')
    .filter((t) => ui.advanced || !ADVANCED_TABS.has(t.id) || t.id === current)
  const hiddenTabs = ui.beginner && TABS.some((t) => ADVANCED_TABS.has(t.id) && t.id !== current && (t.id !== 'act' || l.target === 'act'))
  const busy = build.isPending || deploy.isPending || validate.isPending || destroy.isPending
  return (
    <div>
      <PageHeader
        title={<span className="flex items-center gap-2">{l.name} <LabStateBadge state={l.state} /></span>}
        sub={<><span className="mono">{l.slug}</span> · template <span className="mono">{l.template}</span> · target <span className={l.target === 'act' ? 'font-medium text-violet-600 dark:text-violet-400' : ''}>{l.target}</span></>}
        actions={<>
          <button className="btn-secondary" data-testid="action-build" data-tour="build-button" onClick={() => build.mutate()} disabled={!can.build || busy}>Build</button>
          <button className="btn-primary" data-testid="action-deploy" data-tour="deploy-button" onClick={() => deploy.mutate()} disabled={!can.deploy || noHost || busy}
            title={noHost ? 'assign a host in Settings' : undefined}>Deploy</button>
          <button className="btn-primary" data-testid="action-validate" data-tour="validate-button" onClick={() => validate.mutate()} disabled={!can.validate || busy}>Validate</button>
          {can.destroy
            ? <ConfirmButton className="btn-danger" onConfirm={() => destroy.mutate()} confirmText="Destroy lab?">Destroy</ConfirmButton>
            : <button className="btn-danger" data-testid="action-destroy" disabled>Destroy</button>}
          <a className="btn-secondary" href={gitApi.exportUrl(id)} download data-testid="action-export" title="zip of the lab directory (without .git and ANTA reports)">Export zip</a>
          {!isRunning(l.state) && <ConfirmButton className="btn-secondary" onConfirm={() => remove.mutate()} confirmText="Delete lab?">Delete</ConfirmButton>}
        </>}
      />
      <ErrorBox error={build.error ?? deploy.error ?? validate.error ?? destroy.error ?? remove.error} />
      <ImportWarnings lab={l} />
      <Tabs tabs={[...tabs]} value={current} onChange={(t) => navigate(`/labs/${id}/${t}`)}
        extra={hiddenTabs && <button type="button" className="text-xs font-medium text-cv-primary-text hover:underline" data-testid="show-advanced"
          title="MSS, NetBox, Git & VS Code, ACT" onClick={() => ui.setMode('advanced')}>Show advanced</button>} />
      <ErrorBoundary name={`the ${current} tab`} resetKey={current}>
        {current === 'overview' && <OverviewTab lab={l} />}
        {current === 'settings' && <SettingsTab lab={l} />}
        {current === 'files' && <FilesTab lab={l} />}
        {current === 'build' && <BuildTab lab={l} />}
        {current === 'topology' && <TopologyTab lab={l} />}
        {current === 'runtime' && <RuntimeTab lab={l} />}
        {current === 'devices' && <DevicesTab lab={l} />}
        {current === 'clients' && <ClientsTab lab={l} />}
        {current === 'git' && <GitTab lab={l} />}
        {current === 'validation' && <ValidationTab lab={l} />}
        {current === 'cloudvision' && <CloudVisionTab lab={l} />}
        {current === 'mss' && <MssTab lab={l} />}
        {current === 'netbox' && <NetBoxTab lab={l} />}
        {current === 'act' && (l.target === 'act' ? <ActTab lab={l} /> : <div className="card p-4 text-sm text-zinc-500">This lab targets containerlab. Set the target to act in Settings to use Arista Cloud Test.</div>)}
        {current === 'jobs' && <LabJobsTab lab={l} />}
      </ErrorBoundary>
    </div>
  )
}

/** CONTRACT-3 §4: an imported lab that failed pyavd validation is still created (draft) with warnings */
function ImportWarnings({ lab }: { lab: Lab }) {
  const w = (lab.settings as Record<string, unknown> | undefined)?.import_warnings
  const list = Array.isArray(w) ? w.map((x) => (typeof x === 'string' ? x : (x as { message?: string })?.message ?? JSON.stringify(x))) : []
  if (!list.length) return null
  return (
    <div className="mb-3 rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-100" data-testid="import-warnings">
      <div className="font-medium">Imported with {list.length} validation warning(s): fix them in Files, then build.</div>
      <ul className="mono mt-1 list-disc pl-5 text-xs">{list.slice(0, 20).map((m, i) => <li key={i}>{m}</li>)}</ul>
    </div>
  )
}
