import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { setupServer } from 'msw/node'
import { handlers } from '../mocks/handlers'
import { db } from '../mocks/data'
import { labsApi } from '../api/labs'
import { hostsApi } from '../api/hosts'
import { ApiError } from '../api/client'

const server = setupServer(...handlers)
const origin = globalThis.location?.origin ?? 'http://localhost'
beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' })
  // client uses relative /api/... URLs; give fetch a base in node
  const f = globalThis.fetch
  globalThis.fetch = ((u: RequestInfo | URL, init?: RequestInit) => f(typeof u === 'string' && u.startsWith('/') ? origin + u : u, init)) as typeof fetch
  db.authed = true
})
afterAll(() => server.close())

describe('MSW mock API', () => {
  it('serves hosts in HostOut shape', async () => {
    const hosts = await hostsApi.list()
    expect(hosts).toHaveLength(2)
    expect(hosts[0].summary.docker).toBe('27.3.1')
    expect('capabilities' in hosts[0]).toBe(false)
    const caps = await hostsApi.capabilities(hosts[1].id)
    expect(caps.capabilities.recommendations!.length).toBeGreaterThan(0)
  })

  it('rejects invalid knobs with 422 violations', async () => {
    const err = (await labsApi.create({ name: 'x', template: 'single-dc-l3ls', knobs: { spine_count: 9 } }).catch((e: ApiError) => e)) as ApiError
    expect(err).toBeInstanceOf(ApiError)
    expect(err.status).toBe(422)
    expect(err.violations[0]).toMatchObject({ path: 'knobs.spine_count' })
  })

  it('validates file edits and commits', async () => {
    const lab = (await labsApi.list())[0]
    const bad = (await labsApi.saveFile(lab.id, 'group_vars/FABRIC.yml', 'foo: [').catch((e: ApiError) => e)) as ApiError
    expect(bad.status).toBe(422)
    await labsApi.saveFile(lab.id, 'group_vars/FABRIC.yml', 'fabric_name: FABRIC\nx: 1\n')
    const hist = await labsApi.history(lab.id)
    expect(hist[0].message).toBe('edit group_vars/FABRIC.yml')
    const diff = await labsApi.diff(lab.id, hist[1].sha, hist[0].sha)
    expect(String(diff)).toContain('+x: 1')
  })
})

describe('errorMessage (0.10.0)', async () => {
  const { errorMessage } = await import('../api/client')
  it('surfaces message + hint, string details and violations, else HTTP <status>', () => {
    expect(errorMessage(502, { message: 'anthropic HTTP 400: bad tool schema', violations: [] })).toBe('anthropic HTTP 400: bad tool schema')
    expect(errorMessage(409, { message: 'no token', hint: 'create one' })).toBe('no token — create one')
    expect(errorMessage(404, 'lab not found')).toBe('lab not found')
    expect(errorMessage(422, [{ loc: ['body', 'x'], msg: 'required' }])).toBe('HTTP 422: body.x: required')
    expect(errorMessage(500, null)).toBe('HTTP 500')
  })
})
