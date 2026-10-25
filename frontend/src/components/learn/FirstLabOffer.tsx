import { useState } from 'react'
import { FIRST_LAB_DISMISSED_KEY, getLS, setLS } from '../../lib/learn'
import { useTourOptional } from './Tour'

/** Offered on an empty labs page: start the `first-lab` tour, or dismiss for good. */
export function FirstLabOffer() {
  const tour = useTourOptional()
  const [dismissed, setDismissed] = useState(() => getLS(FIRST_LAB_DISMISSED_KEY) === '1')
  if (dismissed || !tour) return null
  const dismiss = () => { setLS(FIRST_LAB_DISMISSED_KEY, '1'); setDismissed(true) }
  return (
    <div className="mb-3 flex flex-wrap items-center gap-3 rounded border border-cyan-300 bg-cyan-50 px-3 py-2 text-sm text-cyan-900 dark:border-cyan-800 dark:bg-cyan-950/40 dark:text-cyan-100" data-testid="first-lab-offer">
      <span className="flex-1"><span className="font-semibold">New to avdlab?</span> Take the 2-minute tour: create a lab from an AVD example, build it, read the generated config, deploy and validate.</span>
      <button className="btn-primary btn-sm" onClick={() => { void tour.start('first-lab') }} data-testid="first-lab-start">Start the tour</button>
      <button className="btn-secondary btn-sm" onClick={dismiss} data-testid="first-lab-dismiss">Dismiss</button>
    </div>
  )
}
