import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { hostsApi } from '../api/hosts'
import type { ContainerKillResult, HostContainer } from '../api/types'
import { ErrorBox, Modal } from './ui'

export interface ContainerAction { container: HostContainer; force: boolean }

/** Lab containers (containerlab / avdlab label) need the name typed before Stop/Kill is enabled. */
export const isLabContainer = (c: HostContainer) => !!(c.labels?.['avdlab.lab'] || c.labels?.['containerlab'])

/** Confirm dialog for `docker stop` (force=false) / `docker rm -f` (force=true) of one container on a host. */
export function ContainerActionDialog({ hostId, action, onClose, onDone }: {
  hostId: string; action: ContainerAction | null; onClose: () => void; onDone?: (r: ContainerKillResult) => void
}) {
  const qc = useQueryClient()
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  if (!action) return null
  const { container: c, force } = action
  const lab = isLabContainer(c)
  const ok = !lab || typed === c.name
  const close = () => { setTyped(''); setError(null); onClose() }
  const run = async () => {
    setBusy(true)
    setError(null)
    try {
      const r = await hostsApi.killContainer(hostId, c.name, { force, confirm: lab ? typed : undefined })
      await qc.invalidateQueries({ queryKey: ['hosts', hostId, 'containers'] })
      onDone?.(r)
      close()
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }
  return (
    <Modal open onClose={close} title={`${force ? 'Kill' : 'Stop'} container`}>
      <div className="space-y-3 text-sm" data-testid="container-action">
        <p>
          {force ? <>Remove <span className="mono font-semibold">{c.name}</span> with <span className="mono">docker rm -f</span>.</>
            : <>Stop <span className="mono font-semibold">{c.name}</span> with <span className="mono">docker stop</span>.</>}
        </p>
        {lab && (
          <>
            <p className="text-amber-700 dark:text-amber-300">
              This container belongs to lab <span className="mono">{c.labels['avdlab.lab'] ?? c.labels['containerlab']}</span>.
              {force ? ' The node is gone until the lab is redeployed.' : ''} Type the container name to confirm.
            </p>
            <input className="input mono w-full" aria-label="Container name" placeholder={c.name} value={typed}
              onChange={(e) => setTyped(e.target.value)} autoFocus />
          </>
        )}
        <ErrorBox error={error} />
        <div className="flex justify-end gap-2">
          <button className="btn-secondary" onClick={close}>Cancel</button>
          <button className="btn-danger" disabled={!ok || busy} onClick={run}>
            {busy ? 'Working…' : force ? 'Kill (rm -f)' : 'Stop'}
          </button>
        </div>
      </div>
    </Modal>
  )
}
