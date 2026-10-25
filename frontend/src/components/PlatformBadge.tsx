import type { HostCapabilities } from '../api/types'

/** 1.0: "WSL2 on Windows · beta" / "OrbStack VM on macOS · beta" next to a host name; nothing for plain Linux. */
export default function PlatformBadge({ caps, className = '' }: { caps?: HostCapabilities | null; className?: string }) {
  const os = caps?.os
  if (!os?.platform || os.platform === 'linux') return null
  return (
    <span className={`badge bg-violet-100 text-violet-800 dark:bg-violet-900/50 dark:text-violet-200 ${className}`} data-testid="platform-badge"
      title={os.beta ? 'Linux VM on a desktop OS — supported as a beta feature: not yet field-tested; please report problems' : undefined}>
      {os.platform_label ?? os.platform}{os.beta ? ' · beta' : ''}
    </span>
  )
}
