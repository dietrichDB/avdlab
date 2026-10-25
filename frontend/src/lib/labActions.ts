import type { LabState } from '../api/types'

export interface LabActions { build: boolean; deploy: boolean; validate: boolean; destroy: boolean }

/** Which header actions are enabled for a lab state (CONTRACT-2b, frontend section). */
export function labActions(state: LabState | string): LabActions {
  switch (state) {
    case 'draft': return { build: true, deploy: false, validate: false, destroy: false }
    case 'built': return { build: true, deploy: true, validate: false, destroy: false }
    case 'deployed':
    case 'validated': return { build: true, deploy: false, validate: true, destroy: true }
    case 'destroyed': return { build: true, deploy: true, validate: false, destroy: false }
    default: return { build: true, deploy: false, validate: false, destroy: false }
  }
}

/** executors/clab.py deploy precondition: redeploy (with --reconfigure) is allowed from these states */
export const DEPLOYABLE_STATES: ReadonlySet<string> = new Set(['built', 'deployed', 'validated', 'destroyed'])

export const isRunning = (state: LabState | string) => state === 'deployed' || state === 'validated'
