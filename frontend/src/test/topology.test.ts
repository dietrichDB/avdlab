import { describe, expect, it } from 'vitest'
import { toFlow, roleOf, shortIf } from '../lib/topology'
import type { Topology } from '../api/types'
import { db, topology as mockTopology } from '../mocks/data'

// shape of GET /api/topology/{lab_id} (backend/avdlab/topology/models.py TopologyGraph)
const api: Topology = {
  lab_id: 'l1', name: 'dc1-lab', mgmt_subnet: '172.16.1.0/24',
  nodes: [
    { name: 'dc1-leaf1b', kind: 'arista_ceos', role: 'l3leaf', mgmt_ip: '172.16.1.102' },
    { name: 'dc1-spine1', kind: 'arista_ceos', role: 'spine', mgmt_ip: '172.16.1.11' },
    { name: 'dc1-leaf1a', kind: 'arista_ceos', role: 'l3leaf', mgmt_ip: '172.16.1.101' },
    { name: 'dc1-spine2', kind: 'arista_ceos', role: 'spine', mgmt_ip: '172.16.1.12' },
    { name: 'dc1-host1', kind: 'linux', role: 'server', mgmt_ip: null },
  ],
  links: [
    { a: 'dc1-spine1', a_if: 'Ethernet1', b: 'dc1-leaf1a', b_if: 'Ethernet1', type: 'spine' },
    { a: 'dc1-spine2', a_if: 'Ethernet1', b: 'dc1-leaf1a', b_if: 'Ethernet2', type: 'spine' },
    { a: 'dc1-leaf1a', a_if: 'Ethernet3', b: 'dc1-leaf1b', b_if: 'Ethernet3', type: 'mlag_peer' },
    { a: 'dc1-leaf1a', a_if: 'Ethernet5', b: 'dc1-host1', b_if: 'eth1', type: 'server' },
    { a: 'dc1-leaf1a', a_if: 'Ethernet9', b: 'ghost', b_if: 'Ethernet1' },
  ],
}

describe('topology → React Flow', () => {
  it('creates one node per device, layered by role and sorted by name', () => {
    const { nodes } = toFlow(api)
    expect(nodes.map((n) => n.id)).toEqual(['dc1-spine1', 'dc1-spine2', 'dc1-leaf1a', 'dc1-leaf1b', 'dc1-host1'])
    const y = Object.fromEntries(nodes.map((n) => [n.id, n.position.y]))
    expect(y['dc1-spine1']).toBe(y['dc1-spine2'])
    expect(y['dc1-spine1']).toBeLessThan(y['dc1-leaf1a'])
    expect(y['dc1-leaf1a']).toBeLessThan(y['dc1-host1'])
    expect(nodes.find((n) => n.id === 'dc1-host1')!.data.role).toBe('host')
    expect(nodes.find((n) => n.id === 'dc1-leaf1a')!.data.ifaces).toEqual(['Ethernet1', 'Ethernet2', 'Ethernet3', 'Ethernet5', 'Ethernet9'])
  })

  it('creates one edge per link with interface labels, dropping dangling links', () => {
    const { edges } = toFlow(api)
    expect(edges).toHaveLength(4)
    expect(edges[0]).toMatchObject({ source: 'dc1-spine1', target: 'dc1-leaf1a', label: 'Et1 ↔ Et1' })
    expect(new Set(edges.map((e) => e.id)).size).toBe(edges.length)
  })

  it('infers roles from names when the API omits them', () => {
    expect(roleOf({ name: 'dc1-spine3', kind: 'arista_ceos' })).toBe('spine')
    expect(roleOf({ name: 'dc1-leaf2c', kind: 'arista_ceos' })).toBe('l2leaf')
    expect(roleOf({ name: 'dc1-leaf2a', kind: 'arista_ceos' })).toBe('l3leaf')
    expect(roleOf({ name: 'client', kind: 'linux' })).toBe('host')
    expect(shortIf('Port-Channel10')).toBe('Po10')
  })

  it('mock lab is 2 spines / 4 leaves / 2 hosts', () => {
    const t = mockTopology(db.labs[0])
    const { nodes, edges } = toFlow(t)
    const count = (r: string) => nodes.filter((n) => n.data.role === r).length
    expect([count('spine'), count('l3leaf'), count('host')]).toEqual([2, 4, 2])
    expect(edges).toHaveLength(t.links.length)
    expect(edges.length).toBe(4 * 2 + 2 * 2 + 2 * 2)
  })
})
