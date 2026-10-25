import { Component, type ErrorInfo, type ReactNode } from 'react'

interface Props {
  children: ReactNode
  /** what the boundary wraps, used in the message ("the CloudVision tab") */
  name?: string
  /** when this changes the boundary resets (e.g. the active tab id / route) */
  resetKey?: unknown
  className?: string
}
interface State { error: Error | null }

/**
 * 0.8.0: a rendering error inside one panel used to unmount the whole SPA (blank page until a reload).
 * Wrap each lab tab and the routed page so the failure stays local and is reportable.
 */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`[avdlab] render error in ${this.props.name ?? 'component'}`, error, info.componentStack)
  }

  componentDidUpdate(prev: Props) {
    if (this.state.error && prev.resetKey !== this.props.resetKey) this.setState({ error: null })
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children
    const what = this.props.name ?? 'this part of the page'
    return (
      <div className={`rounded border border-red-300 bg-red-50 p-3 text-sm text-red-900 dark:border-red-800 dark:bg-red-950/40 dark:text-red-100 ${this.props.className ?? ''}`} role="alert" data-testid="error-boundary">
        <div className="font-semibold">Something went wrong while rendering {what}.</div>
        <div className="mono mt-1 break-all text-xs">{error.name}: {error.message}</div>
        <div className="mt-2 flex flex-wrap gap-2">
          <button type="button" className="btn-secondary btn-sm" onClick={() => this.setState({ error: null })}>Try again</button>
          <button type="button" className="btn-secondary btn-sm" onClick={() => window.location.reload()}>Reload page</button>
        </div>
        <div className="mt-2 text-xs text-red-800/80 dark:text-red-200/80">The rest of the app keeps working and the data behind this panel is intact — please report it with the feedback form at the bottom of the page (include diagnostics).</div>
      </div>
    )
  }
}
