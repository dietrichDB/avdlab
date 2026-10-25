import { describe, expect, it } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { ValidationResults, TotalsPills } from '../pages/lab/ValidationTab'
import { countByStatus, devicesOf, filterResults } from '../lib/anta'
import { DEFAULT_VALIDATE, antaResults, db } from '../mocks/data'
import type { AntaResult } from '../api/types'

const lab = db.labs.find((l) => l.name === 'evpn-lab')!
const results: AntaResult[] = antaResults(lab, DEFAULT_VALIDATE, true)

describe('mock ANTA report', () => {
  it('is a realistic ~40 row set over 6 devices with failures', () => {
    expect(results.length).toBe(40)
    expect(devicesOf(results)).toHaveLength(6)
    expect(countByStatus(results)).toEqual({ success: 34, failure: 3, error: 1, skipped: 2, unset: 0 })
    expect(results.some((r) => r.test === 'VerifyNTP')).toBe(false) // default skip list applied
  })
})

describe('filterResults', () => {
  it('filters by status set, device and text', () => {
    expect(filterResults(results, { statuses: [], device: '' })).toHaveLength(40)
    expect(filterResults(results, { statuses: ['failure'], device: '' })).toHaveLength(3)
    expect(filterResults(results, { statuses: ['failure', 'error'], device: '' })).toHaveLength(4)
    expect(filterResults(results, { statuses: [], device: 'dc1-leaf2b' })).toHaveLength(7)
    expect(filterResults(results, { statuses: ['failure'], device: 'dc1-leaf1a' })).toHaveLength(0)
    expect(filterResults(results, { statuses: [], device: '', text: 'lldp' }).every((r) => r.test === 'VerifyLLDPNeighbors')).toBe(true)
    expect(filterResults(results, { statuses: [], device: '', text: 'Session state' })).toHaveLength(1)
  })
})

describe('ValidationResults table', () => {
  const rows = () => screen.getAllByTestId('result-row')

  it('shows all rows, failures/errors sorted first', () => {
    render(<ValidationResults results={results} />)
    expect(rows()).toHaveLength(40)
    expect(screen.getByTestId('result-count').textContent).toBe('40 / 40')
    expect(within(rows()[0]).getByText('error')).toBeInTheDocument()
  })

  it('toggles status chips and combines with the device filter', () => {
    render(<ValidationResults results={results} />)
    fireEvent.click(screen.getByTestId('filter-failure'))
    expect(rows()).toHaveLength(3)
    expect(rows().every((r) => r.textContent?.includes('dc1-leaf2b'))).toBe(true)
    fireEvent.click(screen.getByTestId('filter-error'))
    expect(rows()).toHaveLength(4)
    fireEvent.change(screen.getByLabelText('device filter'), { target: { value: 'dc1-spine1' } })
    expect(screen.queryAllByTestId('result-row')).toHaveLength(0)
    expect(screen.getByText('No results match the filters.')).toBeInTheDocument()
    fireEvent.click(screen.getByText(/^all 40$/))
    expect(rows()).toHaveLength(6)
    fireEvent.click(screen.getByTestId('filter-skipped'))
    expect(rows()).toHaveLength(1)
    expect(rows()[0].textContent).toContain('MLAG is disabled')
  })

  it('filters by free text', () => {
    render(<ValidationResults results={results} />)
    fireEvent.change(screen.getByLabelText('text filter'), { target: { value: 'timed out' } })
    expect(rows()).toHaveLength(1)
    expect(rows()[0].textContent).toContain('VerifyMlagStatus')
  })
})

describe('TotalsPills', () => {
  it('renders coloured counts', () => {
    render(<TotalsPills totals={{ success: 34, failure: 3, error: 1, skipped: 2, unset: 0 }} />)
    const pills = screen.getByTestId('totals')
    expect(within(pills).getByText('failure 3').className).toContain('red')
    expect(within(pills).getByText('success 34').className).toContain('emerald')
    expect(within(pills).queryByText(/unset/)).toBeNull()
  })
})
