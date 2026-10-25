import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { setupServer } from 'msw/node'
import { ClientPlanTable, PingMatrix } from '../pages/lab/ClientsTab'
import { ConfigDiffView } from '../pages/lab/DevicesTab'
import { GitRemoteForm, GitStatusSummary } from '../pages/lab/GitTab'
import { archMismatch } from '../pages/Images'
import { lossLevel, matrixFromResults, asIperfResults, fmtMbps } from '../lib/traffic'
import { containerRows, diskMounts, hostRows, topContainers, GB } from '../lib/metrics'
import { gitConfigBody, gitPanelState } from '../lib/git'
import { cmdSlug } from '../api/nodes'
import { imageArch, normArch } from '../api/images'
import { handlers } from '../mocks/handlers'
import { db } from '../mocks/data'
import { hostMetrics, importLab } from '../mocks/phase3'
import { labsApi } from '../api/labs'
import { trafficApi } from '../api/traffic'
import { nodesApi } from '../api/nodes'
import { gitApi } from '../api/git'
import { hostsApi } from '../api/hosts'
import { jobsApi } from '../api/jobs'
import { ApiError } from '../api/client'
import type { GitStatus, HostMetrics, LabClient, PingResult } from '../api/types'

const clients: LabClient[] = [
  {
    name: 'dc1-host1', image: 'ghcr.io/srl-labs/network-multitool:latest',
    links: [{ eth: 'eth1', switch: 'dc1-leaf1a', switch_port: 'Ethernet5' }, { eth: 'eth2', switch: 'dc1-leaf1b', switch_port: 'Ethernet5' }],
    bond: { name: 'bond0', members: ['eth1', 'eth2'] },
    vlans: [
      { vid: 11, iface: 'bond0.11', ip: '10.10.11.100/24', gateway: '10.10.11.1', default_route: true, tenant: 'TENANT1', vrf: 'VRF10' },
      { vid: 12, iface: 'bond0.12', ip: '10.10.12.100/24', gateway: '10.10.12.1', default_route: false, tenant: 'TENANT1', vrf: 'VRF10' },
      { vid: 3401, iface: 'bond0.3401', ip: null, gateway: null, default_route: false, tenant: 'TENANT1', vrf: null },
    ],
    container_state: 'running', mgmt_ip: '172.16.1.200',
  },
  {
    name: 'dc1-host3', image: 'ghcr.io/srl-labs/network-multitool:latest',
    links: [{ eth: 'eth1', switch: 'dc1-leaf2a', switch_port: 'Ethernet6' }], bond: null,
    vlans: [{ vid: 12, iface: 'eth1', ip: '10.10.12.102/24', gateway: '10.10.12.1', default_route: true }],
    container_state: 'exited',
  },
]

describe('client plan table', () => {
  it('renders one block per client with a row per VLAN', () => {
    render(<ClientPlanTable clients={clients} />)
    const first = screen.getAllByTestId('client-row')
    expect(first).toHaveLength(2)
    expect(screen.getAllByTestId('client-vlan-row')).toHaveLength(2) // vlans 12 + 3401 of host1
    const h1 = within(first[0])
    expect(h1.getByText('dc1-host1')).toBeInTheDocument()
    expect(h1.getByText('eth1 → dc1-leaf1a:Ethernet5')).toBeInTheDocument()
    expect(first[0].textContent).toContain('bond0')
    expect(first[0].textContent).toContain('(eth1, eth2)')
    expect(h1.getByText('bond0.11')).toBeInTheDocument()
    expect(h1.getByText('default')).toBeInTheDocument()
    expect(first[0].querySelector('td')!.getAttribute('rowspan')).toBe('3')
    const l2 = screen.getAllByTestId('client-vlan-row')[1]
    expect(within(l2).getByText('L2 only')).toBeInTheDocument()
    const h3 = within(first[1])
    expect(h3.getByText('exited').className).toContain('red')
    expect(first[1].textContent).toContain('—') // no bond
  })
})

describe('ping matrix', () => {
  it('maps loss to levels', () => {
    expect(lossLevel(0)).toBe('ok')
    expect(lossLevel(33.3)).toBe('partial')
    expect(lossLevel(100)).toBe('down')
    expect(lossLevel(null)).toBe('none')
    expect(lossLevel(undefined)).toBe('none')
  })

  it('colours cells green/amber/red by loss and leaves the diagonal empty', () => {
    const rows = [
      { src: 'a', dst: 'b', target_ip: '10.0.0.2', sent: 3, received: 3, loss_pct: 0, rtt_avg_ms: 0.5, ok: true },
      { src: 'b', dst: 'a', target_ip: '10.0.0.1', sent: 3, received: 2, loss_pct: 33.3, rtt_avg_ms: 0.7, ok: false },
      { src: 'a', dst: 'c', target_ip: '10.0.0.3', sent: 3, received: 0, loss_pct: 100, rtt_avg_ms: null, ok: false },
    ]
    const result: PingResult = { results: rows, matrix: matrixFromResults(rows) }
    render(<PingMatrix result={result} />)
    const cell = (s: string, d: string) => screen.getAllByTestId('ping-cell').find((c) => c.dataset.src === s && c.dataset.dst === d)!
    expect(screen.getAllByTestId('ping-cell')).toHaveLength(6) // 3×3 minus diagonal
    expect(cell('a', 'b').className).toContain('emerald')
    expect(cell('a', 'b').textContent).toContain('0%')
    expect(cell('b', 'a').className).toContain('amber')
    expect(cell('b', 'a').textContent).toContain('33%')
    expect(cell('a', 'c').className).toContain('red')
    expect(cell('a', 'c').dataset.level).toBe('down')
    expect(cell('c', 'a').dataset.level).toBe('none')
    expect(cell('c', 'a').textContent).toBe('—')
  })

  it('normalises iperf results', () => {
    expect(asIperfResults({ results: [{ src: 'a' }] })).toHaveLength(1)
    expect(asIperfResults([{ src: 'a' }, { src: 'b' }])).toHaveLength(2)
    expect(asIperfResults({})).toBeNull()
    expect(fmtMbps(9.41e8)).toBe('941.0 Mbps')
    expect(fmtMbps(4.2e9)).toBe('4.20 Gbps')
  })
})

describe('config diff rendering', () => {
  it('colours + and - lines and summarises drift', () => {
    const diff = ['--- intended/configs/leaf.cfg', '+++ running-config (leaf)', '@@ -1,3 +1,4 @@', ' hostname leaf', '-interface Loopback0', '+interface Ethernet1', '+   shutdown'].join('\n')
    render(<ConfigDiffView diff={{ node: 'leaf', intended: '', running: '', diff, identical: false }} />)
    const viewer = screen.getByTestId('diff-viewer')
    expect(within(viewer).getByText('+interface Ethernet1').className).toContain('emerald')
    expect(within(viewer).getByText('-interface Loopback0').className).toContain('red')
    expect(within(viewer).getByText('+++ running-config (leaf)').className).not.toContain('emerald')
    expect(within(viewer).getByText('@@ -1,3 +1,4 @@').className).toContain('purple')
    const summary = screen.getByTestId('config-diff-summary').textContent
    expect(summary).toContain('+2')
    expect(summary).toContain('−1')
  })

  it('shows an identical badge when there is no drift', () => {
    render(<ConfigDiffView diff={{ node: 'x', intended: 'a', running: 'a', diff: '', identical: true }} />)
    expect(screen.getByTestId('config-identical')).toBeInTheDocument()
    expect(screen.queryByTestId('diff-viewer')).toBeNull()
  })

  it('slugs snapshot commands', () => {
    expect(cmdSlug('show ip bgp summary')).toBe('show-ip-bgp-summary')
    expect(cmdSlug(' show running-config ')).toBe('show-running-config')
  })
})

describe('metrics chart data mapping', () => {
  const t0 = Date.parse('2026-09-22T10:00:00Z')
  const at = (i: number) => new Date(t0 + i * 20_000).toISOString()
  const m: HostMetrics = {
    host_id: 'h', sampled_at: at(2), watched: true,
    points: [2, 0, 1].map((i) => ({
      at: at(i), cpu_pct: 10.26 + i, load1: 1, load5: 0.5, load15: 0.25,
      mem_total: 16 * GB, mem_used: 4 * GB, mem_available: 10 * GB,
      disk: { '/': { size: 100, used: 25 + i }, ...(i === 2 ? { '/var/lib/docker': { size: 200, used: 50 } } : {}) },
    })),
    containers: {
      small: [{ at: at(0), cpu_pct: 1, mem_used: 10 * 1024 ** 2, mem_limit: null }, { at: at(1), cpu_pct: 1, mem_used: 20 * 1024 ** 2, mem_limit: null }],
      big: [{ at: at(0), cpu_pct: 1, mem_used: 1024 ** 3, mem_limit: null }, { at: at(1), cpu_pct: 1, mem_used: 1.5 * 1024 ** 3, mem_limit: null }],
      mid: [{ at: at(1), cpu_pct: 1, mem_used: 300 * 1024 ** 2, mem_limit: null }],
    },
  }

  it('sorts points, converts memory to GB and disk to %', () => {
    const rows = hostRows(m.points)
    expect(rows.map((r) => r.t)).toEqual([t0, t0 + 20_000, t0 + 40_000])
    expect(rows[0]).toMatchObject({ cpu: 10.3, memUsed: 4, memAvail: 10, memTotal: 16, load1: 1 })
    expect(rows[0]['disk:/']).toBe(25)
    expect(rows[2]['disk:/var/lib/docker']).toBe(25)
    expect(diskMounts(m.points)).toEqual(['/', '/var/lib/docker'])
  })

  it('keeps only the 2 h window before the newest point', () => {
    const pts = [{ ...m.points[0], at: new Date(t0 - 3 * 3600_000).toISOString() }, ...m.points]
    expect(hostRows(pts)).toHaveLength(3)
  })

  it('picks top containers by latest memory and merges series on time (MB)', () => {
    expect(topContainers(m.containers, 2)).toEqual(['big', 'mid'])
    const rows = containerRows(m.containers, ['big', 'small'])
    expect(rows).toEqual([{ t: t0, big: 1024, small: 10 }, { t: t0 + 20_000, big: 1536, small: 20 }])
  })

  it('mock generator produces 360 points / 20 s and a top-10 container set', () => {
    const mm = hostMetrics('host-lab1')
    expect(mm.points).toHaveLength(360)
    expect(Date.parse(mm.points[1].at) - Date.parse(mm.points[0].at)).toBe(20_000)
    expect(hostRows(mm.points)).toHaveLength(360)
    const top = topContainers(mm.containers, 10)
    expect(top).toHaveLength(10)
    expect(containerRows(mm.containers, top)).toHaveLength(360)
  })
})

describe('git panel state', () => {
  const base: GitStatus = { configured: true, url: 'https://github.com/x/y.git', branch: 'main', auth_kind: 'token', has_secret: true, ahead: 0, behind: 0, last_sync: null }

  it('derives buttons and the sync label', () => {
    expect(gitPanelState({ configured: false })).toMatchObject({ configured: false, canPush: false, canPull: false, syncLabel: 'no remote' })
    expect(gitPanelState(base)).toMatchObject({ canPush: true, canPull: true, syncLabel: 'in sync', tone: 'ok' })
    expect(gitPanelState({ ...base, ahead: 2 }).syncLabel).toBe('2 ahead')
    expect(gitPanelState({ ...base, ahead: 2, behind: 1 })).toMatchObject({ syncLabel: '2 ahead, 1 behind', tone: 'warn' })
    expect(gitPanelState({ ...base, ahead: null, behind: null }).syncLabel).toBe('not fetched')
    expect(gitPanelState({ ...base, has_secret: false })).toMatchObject({ canPush: false, canPull: false, tone: 'warn' })
    expect(gitPanelState({ ...base, last_sync: { at: '2026-01-01T00:00:00Z', direction: 'push', ok: false, message: 'rejected' } })).toMatchObject({ tone: 'error', lastSync: 'push failed' })
  })

  it('builds a write-only PUT body', () => {
    const f = { url: ' https://github.com/x/y.git ', branch: '', auth_kind: 'token' as const, token: '', secret_id: '' }
    expect(gitConfigBody(f)).toEqual({ url: 'https://github.com/x/y.git', branch: 'main', auth_kind: 'token' }) // empty token = keep
    expect(gitConfigBody({ ...f, token: 'ghp_x' })).toMatchObject({ token: 'ghp_x', secret_id: null })
    expect(gitConfigBody({ ...f, auth_kind: 'ssh_key', secret_id: 'k1' })).toMatchObject({ auth_kind: 'ssh_key', secret_id: 'k1' })
    expect(gitConfigBody({ ...f, auth_kind: 'none', token: 'ignored' })).toMatchObject({ secret_id: null, token: null })
  })

  it('renders status and never shows the stored token', () => {
    render(<GitStatusSummary status={{ ...base, ahead: 3 }} />)
    expect(screen.getByTestId('git-sync-label').textContent).toBe('3 ahead')
    expect(screen.getByText('✓ stored')).toBeInTheDocument()
    const onSubmit = vi.fn()
    const { container } = render(<GitRemoteForm status={base} secrets={[]} onSubmit={onSubmit} />)
    const tok = container.querySelector('[name="git.token"]') as HTMLInputElement
    expect(tok.type).toBe('password')
    expect(tok.value).toBe('')
    expect(tok.placeholder).toContain('stored')
    fireEvent.click(screen.getByTestId('git-save'))
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ url: base.url, auth_kind: 'token', token: '' }))
  })

  it('requires a token or key when none is stored', () => {
    const { container } = render(<GitRemoteForm status={{ configured: false }} secrets={[{ id: 'k1', name: 'deploy', kind: 'ssh_key', meta: {}, created_at: '' }]} onSubmit={() => undefined} />)
    fireEvent.change(container.querySelector('[name="git.url"]')!, { target: { value: 'git@github.com:x/y.git' } })
    fireEvent.change(container.querySelector('[name="git.auth_kind"]')!, { target: { value: 'ssh_key' } })
    expect(screen.getByTestId('git-save')).toBeDisabled()
    fireEvent.change(container.querySelector('[name="git.secret_id"]')!, { target: { value: 'k1' } })
    expect(screen.getByTestId('git-save')).not.toBeDisabled()
  })
})

describe('image arch', () => {
  it('derives arch and flags mismatches', () => {
    expect(imageArch({ fmt: 'cEOSarm' })).toBe('arm64')
    expect(imageArch({ fmt: 'cEOS64' })).toBe('x86_64')
    expect(normArch('aarch64')).toBe('arm64')
    expect(archMismatch('x86_64', { fmt: 'cEOSarm', arch: 'arm64' })).toBe('host is x86_64, image is arm64')
    expect(archMismatch('x86_64', { fmt: 'cEOS' })).toBeNull()
    expect(archMismatch(null, { fmt: 'cEOSarm' })).toBeNull()
  })
})

describe('phase 3 mock API', () => {
  const server = setupServer(...handlers)
  const origin = globalThis.location?.origin ?? 'http://localhost'
  const f0 = globalThis.fetch
  beforeAll(() => {
    server.listen({ onUnhandledRequest: 'error' })
    const f = globalThis.fetch // patched by msw
    globalThis.fetch = ((u: RequestInfo | URL, init?: RequestInit) => f(typeof u === 'string' && u.startsWith('/') ? origin + u : u, init)) as typeof fetch
    db.authed = true
  })
  afterAll(() => { server.close(); globalThis.fetch = f0 })
  const evpn = () => db.labs.find((l) => l.name === 'evpn-lab')!
  const waitJob = async (id: string) => {
    for (let i = 0; i < 100; i++) {
      const j = await jobsApi.get(id)
      if (['succeeded', 'failed', 'cancelled'].includes(j.status)) return j
      await new Promise((r) => setTimeout(r, 50))
    }
    throw new Error('job timeout')
  }

  it('derives clients and runs a ping mesh job', async () => {
    const cs = await trafficApi.clients(evpn().id)
    expect(cs.map((c) => c.name)).toEqual(['dc1-host1', 'dc1-host2', 'dc1-host3'])
    expect(cs[0].bond?.members).toEqual(['eth1', 'eth2'])
    expect(cs[0].vlans[0]).toMatchObject({ vid: 11, iface: 'bond0.11', ip: '10.10.11.100/24', gateway: '10.10.11.1', default_route: true })
    expect(cs[2].vlans[0]).toMatchObject({ iface: 'eth1', ip: '10.10.12.102/24' })
    const { job_id } = await trafficApi.ping(evpn().id, { pairs: null, count: 3, size: 56 })
    const j = await waitJob(job_id)
    const r = j.result as unknown as PingResult
    expect(r.results).toHaveLength(6)
    expect(r.matrix['dc1-host2']['dc1-host3']).toBe(100)
    const runs = await trafficApi.runs(evpn().id)
    expect(runs[0].job_id).toBe(job_id)
    expect((await trafficApi.run(evpn().id, runs[0].id)).results).toBeTruthy()
  }, 15_000)

  it('serves running config and drift diff', async () => {
    const rc = await nodesApi.runningConfig(evpn().id, 'dc1-leaf1a')
    expect(rc.config).toContain('hostname dc1-leaf1a')
    expect((await nodesApi.configDiff(evpn().id, 'dc1-leaf1a')).identical).toBe(true)
    const d = await nodesApi.configDiff(evpn().id, 'dc1-leaf2b')
    expect(d.identical).toBe(false)
    expect(d.diff).toContain('+   description added-by-hand')
    const snaps = await nodesApi.snapshots(evpn().id)
    expect(await nodesApi.snapshotFile(evpn().id, snaps[0].id, 'dc1-spine1', 'show-version')).toContain('Serial number')
  })

  it('git status, import archive, open-links and host monitoring', async () => {
    expect((await gitApi.status(evpn().id)).ahead).toBe(2)
    // multipart uploads hang under jsdom+undici, so the archive import is exercised through the mock helper
    const imported = importLab('imported', 'host-lab1', ['x'])
    const lab = await labsApi.get(imported.id)
    expect(lab.template).toBe('custom')
    expect(lab.settings?.import_warnings).toEqual(['x'])
    const { job_id } = await gitApi.importGit({ name: 'from-git', url: 'https://gitlab.com/x/y.git', branch: 'main', auth_kind: 'token', token: 't' })
    const j = await waitJob(job_id)
    expect(j.status).toBe('succeeded')
    expect((await gitApi.status(String(j.result.lab_id))).configured).toBe(true)
    const e = (await labsApi.get('nope').catch((x: ApiError) => x)) as ApiError
    expect(e.status).toBe(404)
    const links = await gitApi.openLinks(evpn().id)
    expect(links.remote_ssh).toMatch(/^vscode:\/\/vscode-remote\/ssh-remote\+ubuntu@10\.20\.0\.11\/opt\/avdlab\/labs\/evpn-lab$/)
    expect(links.hints.join(' ')).toContain('srl-labs.vscode-containerlab')
    const m = await hostsApi.metrics('host-lab1')
    expect(m.points).toHaveLength(360)
    expect((await hostsApi.processes('host-lab1')).processes.length).toBeGreaterThan(3)
    expect((await hostsApi.containers('host-lab1')).some((c) => c.labels['avdlab.lab'] === 'evpn-lab')).toBe(true)
  }, 15_000)
})
