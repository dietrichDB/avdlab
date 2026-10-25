import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { actApi } from '../../api/phase4'
import { labsApi } from '../../api/labs'
import type { ActInfo, Lab } from '../../api/types'
import { useJobDrawer } from '../../components/JobDrawer'
import { Badge, CopyButton, ErrorBox, KV, Spinner } from '../../components/ui'
import { ACT_ROLE_CLASS } from '../../lib/act'
import { labActions } from '../../lib/labActions'

export function ActNodesTable({ info }: { info: ActInfo }) {
  return (
    <table className="table" data-testid="act-nodes">
      <thead><tr><th>Node</th><th>Role</th><th>Node type</th><th>IP</th><th>Version</th></tr></thead>
      <tbody>
        {info.nodes.map((n) => (
          <tr key={n.name} data-testid="act-node-row">
            <td className="mono">{n.name}</td>
            <td><Badge className={ACT_ROLE_CLASS[n.role] ?? ACT_ROLE_CLASS.client}>{n.role}</Badge></td>
            <td className="mono text-zinc-500">{n.node_type}</td>
            <td className="mono">{n.ip_addr ?? '—'}</td>
            <td className="mono text-zinc-500">{n.version ?? '—'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function ClientScripts({ info }: { info: ActInfo }) {
  const [sel, setSel] = useState<string | null>(null)
  const cur = info.clients.find((c) => c.name === sel) ?? info.clients[0]
  if (!info.clients.length) return <div className="p-3 text-sm text-zinc-500">No clients in this lab.</div>
  return (
    <div className="space-y-2" data-testid="act-clients">
      <div className="flex flex-wrap gap-1">
        {info.clients.map((c) => (
          <button key={c.name} className={c.name === cur.name ? 'btn-primary btn-sm' : 'btn-secondary btn-sm'} onClick={() => setSel(c.name)}>{c.name}</button>
        ))}
      </div>
      <KV items={[
        ['IP', <span className="mono">{cur.ip_addr ?? '—'}</span>],
        ['Links', cur.links.length ? <span className="mono text-xs">{cur.links.map((l) => l.join(' ↔ ')).join(' · ')}</span> : '—'],
        ['Script', cur.script_path ? <span className="mono">{cur.script_path}</span> : <span className="text-zinc-400">not generated</span>],
      ]} />
      {cur.script && (
        <div className="relative">
          <div className="absolute right-1 top-1"><CopyButton text={cur.script} /></div>
          <pre className="mono max-h-80 overflow-auto rounded border border-zinc-200 bg-zinc-50 p-2 text-xs dark:border-zinc-800 dark:bg-zinc-950" data-testid="act-client-script">{cur.script}</pre>
        </div>
      )}
      {info.apply_script_path && <div className="text-xs text-zinc-500">Apply all from the tools server: <span className="mono">sh {info.apply_script_path}</span></div>}
    </div>
  )
}

export default function ActTab({ lab }: { lab: Lab }) {
  const qc = useQueryClient()
  const drawer = useJobDrawer()
  const info = useQuery({ queryKey: ['labs', lab.id, 'act'], queryFn: () => actApi.info(lab.id) })
  const prepare = useMutation({
    mutationFn: () => actApi.prepare(lab.id),
    onSuccess: (d) => qc.setQueryData(['labs', lab.id, 'act'], d),
  })
  const deploy = useMutation({ mutationFn: () => drawer.track(labsApi.deploy(lab.id, { reconfigure: false, wait_eapi: true, wait_timeout: 900 })) })
  const validate = useMutation({ mutationFn: () => drawer.track(labsApi.validate(lab.id, { extra_fabric_validation: true, skip_tests: ['VerifyNTP', 'VerifyReloadCause'], user_catalogs: false, tags: [] })) })
  const d = info.data
  const can = labActions(lab.state)
  const hostKind = lab.host?.kind
  const deployReason = !d?.prepared ? 'build the lab with target act first'
    : !lab.host_id ? 'set the ACT tools server (host of kind act) as the lab host in Settings'
    : hostKind && hostKind !== 'act' ? `host ${lab.host?.name} is kind ${hostKind}: an ACT lab deploys through a host of kind act`
    : !can.deploy ? `not deployable in state ${lab.state}` : undefined

  if (info.isLoading) return <Spinner label="ACT topology…" />
  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_24rem]" data-testid="act-tab">
      <div className="min-w-0 space-y-4">
        <ErrorBox error={info.error} />
        <section className="card overflow-x-auto p-2">
          <div className="mb-2 flex flex-wrap items-center gap-2 px-1">
            <span className="h2 mr-auto">ACT topology</span>
            {d && (d.prepared
              ? <Badge className="bg-emerald-100 text-emerald-800 dark:bg-emerald-900/60 dark:text-emerald-200">prepared</Badge>
              : <Badge className="bg-amber-100 text-amber-800 dark:bg-amber-900/60 dark:text-amber-200">not prepared</Badge>)}
            <a className={d?.prepared ? 'btn-secondary btn-sm' : 'btn-secondary btn-sm pointer-events-none opacity-50'} data-testid="act-download"
              href={actApi.topologyUrl(lab.id)} download aria-disabled={!d?.prepared}>Download topology YAML</a>
            <button className="btn-secondary btn-sm" data-testid="act-prepare" disabled={lab.target !== 'act' || prepare.isPending} onClick={() => prepare.mutate()}
              title={lab.target !== 'act' ? 'set the lab target to act first' : 're-run the post-processing (tools server + clients) after changing the act knob'}>
              {prepare.isPending ? 'Preparing…' : 'Prepare'}
            </button>
          </div>
          <ErrorBox error={prepare.error} />
          {d && (
            <div className="px-1 pb-2">
              <KV items={[
                ['Topology file', <span className="mono">{d.topology_path}</span>],
                ['Tools server', d.tools_server_ip ? <span className="mono">{d.tools_server_ip}</span> : '—'],
                ['Nodes', `${d.nodes.filter((n) => n.role === 'switch').length} switches, ${d.clients.length} clients`],
              ]} />
            </div>
          )}
          {d?.nodes.length ? <ActNodesTable info={d} /> : <div className="p-4 text-center text-sm text-zinc-500">No ACT topology yet: build the lab with target act.</div>}
        </section>
        {d && (
          <section className="card p-3">
            <div className="h2 mb-2">Client scripts</div>
            <ClientScripts info={d} />
          </section>
        )}
      </div>

      <div className="space-y-4">
        <section className="card space-y-2 p-3" data-testid="act-hints">
          <div className="h2">Next steps</div>
          <ol className="list-decimal space-y-1 pl-5 text-sm">
            {(d?.hints ?? []).map((h, i) => <li key={i}>{h}</li>)}
          </ol>
          <div className="text-xs text-zinc-500">Hosts of kind act are added on the <Link className="link" to="/hosts">Hosts</Link> page (Bootstrap → recipe <span className="mono">act-tools</span>).</div>
        </section>
        <section className="card space-y-2 p-3">
          <div className="h2">Run</div>
          <KV items={[['Lab host', lab.host ? <span>{lab.host.name} <Badge className="bg-zinc-200 dark:bg-zinc-700">{lab.host.kind}</Badge></span> : <span className="text-red-600 dark:text-red-400">none</span>]]} />
          <ErrorBox error={deploy.error ?? validate.error} />
          <div className="flex gap-2">
            <button className="btn-primary flex-1 justify-center" data-testid="act-deploy" disabled={!!deployReason || deploy.isPending} title={deployReason} onClick={() => deploy.mutate()}>Deploy</button>
            <button className="btn-primary flex-1 justify-center" data-testid="act-validate" disabled={!can.validate || validate.isPending} title={!can.validate ? 'deploy first' : undefined} onClick={() => validate.mutate()}>Validate</button>
          </div>
          {deployReason && <div className="text-xs text-zinc-500">{deployReason}</div>}
        </section>
      </div>
    </div>
  )
}
