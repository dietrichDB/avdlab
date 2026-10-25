import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

afterEach(() => cleanup())

// jsdom lacks ResizeObserver (React Flow needs it)
if (!('ResizeObserver' in globalThis)) {
  class RO { observe() {} unobserve() {} disconnect() {} }
  ;(globalThis as unknown as { ResizeObserver: typeof RO }).ResizeObserver = RO
}
