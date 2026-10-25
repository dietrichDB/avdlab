import { describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { useState } from 'react'
import { KnobForm, defaultKnobValues, validateKnob, violationsByKnob, type KnobValues } from '../components/KnobForm'
import type { Knob } from '../api/types'
import { templates } from '../mocks/data'

const knobs: Knob[] = [
  { key: 'spine_count', type: 'int', default: 2, min: 1, max: 4, label: 'Spines' },
  { key: 'ceos_version', type: 'string', default: '4.34.2F' },
  { key: 'mgmt_subnet', type: 'cidr', default: '172.16.1.0/24' },
  { key: 'mgmt_gateway', type: 'ipv4', default: '172.16.1.1' },
  { key: 'device_password', type: 'secret', default: 'arista' },
  { key: 'pools', type: 'object', default: { loopback: '10.255.0.0/27', vtep: '10.255.1.0/27' } },
  { key: 'tenants', type: 'yaml', default: [{ name: 'TENANT1' }] },
]

function Harness({ onChange, violations }: { onChange?: (v: KnobValues) => void; violations?: { path?: string; message: string }[] }) {
  const [v, setV] = useState(defaultKnobValues(knobs))
  return <KnobForm knobs={knobs} values={v} violations={violations} onChange={(n) => { setV(n); onChange?.(n) }} />
}

describe('knob form generation', () => {
  it('renders one control per knob type', () => {
    const { container } = render(<Harness />)
    expect(container.querySelector('input[name="spine_count"]')).toHaveAttribute('type', 'number')
    expect(container.querySelector('input[name="spine_count"]')).toHaveAttribute('min', '1')
    expect(container.querySelector('input[name="spine_count"]')).toHaveAttribute('max', '4')
    expect(container.querySelector('input[name="ceos_version"]')).toHaveValue('4.34.2F')
    expect(container.querySelector('input[name="mgmt_subnet"]')).toHaveValue('172.16.1.0/24')
    expect(container.querySelector('input[name="mgmt_gateway"]')).toHaveValue('172.16.1.1')
    expect(container.querySelector('input[name="device_password"]')).toHaveAttribute('type', 'password')
    // object knob → one input per key
    expect(container.querySelector('input[name="pools.loopback"]')).toHaveValue('10.255.0.0/27')
    expect(container.querySelector('input[name="pools.vtep"]')).toHaveValue('10.255.1.0/27')
    // yaml knob → CodeMirror editor
    expect(screen.getAllByTestId('yaml-editor')).toHaveLength(1)
    expect(screen.getByText('Spines')).toBeInTheDocument()
  })

  it('emits typed values and flags client-side errors', () => {
    const onChange = vi.fn()
    const { container } = render(<Harness onChange={onChange} />)
    fireEvent.change(container.querySelector('input[name="spine_count"]')!, { target: { value: '3' } })
    expect(onChange.mock.lastCall![0].spine_count).toBe(3)
    fireEvent.change(container.querySelector('input[name="spine_count"]')!, { target: { value: '9' } })
    expect(screen.getByText('max 4')).toBeInTheDocument()
    fireEvent.change(container.querySelector('input[name="mgmt_subnet"]')!, { target: { value: '10.0.0.0' } })
    expect(screen.getByText('expected a.b.c.d/nn')).toBeInTheDocument()
    fireEvent.change(container.querySelector('input[name="pools.vtep"]')!, { target: { value: '10.9.9.0/27' } })
    expect(onChange.mock.lastCall![0].pools).toEqual({ loopback: '10.255.0.0/27', vtep: '10.9.9.0/27' })
  })

  it('maps server violations onto knob fields inline', () => {
    render(<Harness violations={[{ path: 'knobs.spine_count', message: 'spine_count: must be <= 4' }, { path: 'tenants[0].name', message: "Required key 'name' is missing" }, { message: 'something global' }]} />)
    expect(screen.getByText('spine_count: must be <= 4')).toBeInTheDocument()
    expect(screen.getByText("Required key 'name' is missing")).toBeInTheDocument()
    expect(screen.getByText('something global')).toBeInTheDocument()
  })

  it('defaults and validators cover the mock template', () => {
    const t = templates[0]
    const d = defaultKnobValues(t.knobs)
    expect(d.spine_count).toBe(2)
    expect(t.knobs.every((k) => validateKnob(k, d[k.key]) === null)).toBe(true)
    expect(violationsByKnob([{ path: 'leaf_pairs', message: 'x' }], t.knobs)).toEqual({ leaf_pairs: ['x'] })
  })
})
