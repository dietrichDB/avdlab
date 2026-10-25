import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { setupServer } from 'msw/node'
import { RuntimeStatusTable } from '../pages/lab/RuntimeTab'
import { labActions } from '../lib/labActions'
import { handlers } from '../mocks/handlers'
import { db } from '../mocks/data'
import { labsApi } from '../api/labs'
import { ApiError } from '../api/client'
import type { LabNodeStatus } from '../api/types'

const nodes: LabNodeStatus[] = [
  { name: 'clab-dc1-dc1-spine1', kind: 'arista_ceos', state: 'running', mgmt_ip: '172.16.1.11/24', image: 'arista/ceos:4.34.2F', eapi: true },
  { name: 'clab-dc1-dc1-leaf1a', kind: 'arista_ceos', state: 'running', mgmt_ip: '172.16.1.101/24', image: 'arista/ceos:4.34.2F', eapi: false },
  { name: 'clab-dc1-dc1-leaf1b', kind: 'arista_ceos', state: 'exited', mgmt_ip: null, image: 'arista/ceos:4.34.2F', eapi: false },
  { name: 'clab-dc1-dc1-host1', kind: 'linux', state: 'running', mgmt_ip: '172.16.1.200/24', image: 'ghcr.io/hellt/network-multitool', eapi: false },
]

describe('RuntimeStatusTable', () => {
  it('renders one row per node with state, mgmt ip and eAPI marks', () => {
    render(<RuntimeStatusTable nodes={nodes} />)
    const rows = screen.getAllByTestId('runtime-row')
    expect(rows).toHaveLength(4)
    expect(within(rows[0]).getByLabelText('eAPI reachable')).toBeInTheDocument()
    expect(within(rows[1]).getByLabelText('eAPI unreachable')).toBeInTheDocument()
    expect(within(rows[2]).getByText('exited').className).toContain('red')
    expect(within(rows[2]).getByText('—')).toBeInTheDocument() // no mgmt ip
    expect(within(rows[3]).getByTitle('not an EOS node')).toBeInTheDocument() // linux: no eAPI
    expect(screen.getAllByText('✓')).toHaveLength(1)
    expect(screen.getAllByText('✗')).toHaveLength(2)
    const summary = screen.getByTestId('runtime-summary').textContent
    expect(summary).toContain('4 nodes')
    expect(summary).toContain('3 running')
    expect(summary).toContain('eAPI 1/3')
  })
})

describe('labActions', () => {
  it('enables actions by state', () => {
    const on = (s: string) => Object.entries(labActions(s)).filter(([, v]) => v).map(([k]) => k).sort()
    expect(on('draft')).toEqual(['build'])
    expect(on('built')).toEqual(['build', 'deploy'])
    expect(on('deployed')).toEqual(['build', 'destroy', 'validate'])
    expect(on('validated')).toEqual(['build', 'destroy', 'validate'])
    expect(on('destroyed')).toEqual(['build', 'deploy'])
  })
})

describe('runtime mock API', () => {
  const server = setupServer(...handlers)
  const origin = globalThis.location?.origin ?? 'http://localhost'
  const f0 = globalThis.fetch
  beforeAll(() => {
    server.listen({ onUnhandledRequest: 'error' })
    const f = globalThis.fetch
    globalThis.fetch = ((u: RequestInfo | URL, init?: RequestInit) => f(typeof u === 'string' && u.startsWith('/') ? origin + u : u, init)) as typeof fetch
    db.authed = true
  })
  afterAll(() => { server.close(); globalThis.fetch = f0 })

  it('serves status, validations and reports for the seeded deployed lab', async () => {
    const lab = (await labsApi.list()).find((l) => l.name === 'evpn-lab')!
    expect(lab.state).toBe('deployed')
    const st = await labsApi.status(lab.id)
    expect(st.deployed).toBe(true)
    expect(st.nodes.filter((n) => n.kind === 'arista_ceos')).toHaveLength(6)
    const runs = await labsApi.validations(lab.id)
    expect(runs[0].totals.failure).toBe(3)
    const rows = await labsApi.reportJson(lab.id, runs[0].id)
    expect(rows).toHaveLength(40)
    expect(await labsApi.reportText(lab.id, runs[0].id, 'md')).toContain('# ANTA Report')
    expect(await labsApi.reportText(lab.id, runs[0].id, 'csv')).toMatch(/^Device,Test Name,Test Status/)
  })

  it('refuses deploy/validate out of state and reports undeployed status', async () => {
    const draft = await labsApi.create({ name: 'fresh', template: 'l2ls-fabric', host_id: 'host-lab1', knobs: {} })
    expect(await labsApi.status(draft.id)).toEqual({ deployed: false, nodes: [] })
    const e1 = (await labsApi.deploy(draft.id).catch((e: ApiError) => e)) as ApiError
    expect(e1.status).toBe(409)
    const e2 = (await labsApi.validate(draft.id).catch((e: ApiError) => e)) as ApiError
    expect(e2.status).toBe(409)
  })
})
