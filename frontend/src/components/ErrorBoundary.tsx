import React from 'react';
import i18n from '../i18n';

interface ErrorBoundaryState {
  error: Error | null;
}

/**
 * Top-level error boundary: a render crash shows a recoverable screen
 * instead of a blank page. The error detail shown here is a frontend
 * exception (already in the user's browser), not a backend internal —
 * displaying it doesn't leak anything and makes bug reports actionable.
 */
export class ErrorBoundary extends React.Component<{ children: React.ReactNode }, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error('Unhandled render error:', error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="min-h-screen flex items-center justify-center bg-canvas px-4">
        <div className="max-w-md w-full bg-surface border border-border rounded-2xl p-8 text-center space-y-4">
          <h1 className="text-lg font-semibold text-ink">{i18n.t('Something went wrong')}</h1>
          <p className="text-sm text-secondary">
            {i18n.t('The page hit an unexpected error. Your data is safe — try again or reload.')}
          </p>
          <pre className="text-[11px] text-left text-secondary bg-hover rounded-lg p-3 overflow-auto max-h-32 whitespace-pre-wrap break-all">
            {this.state.error.message}
          </pre>
          <div className="flex items-center justify-center gap-3">
            <button
              onClick={() => this.setState({ error: null })}
              className="px-4 py-2 text-sm font-medium text-ink bg-hover rounded-lg hover:bg-border transition-colors"
            >
              {i18n.t('Try again')}
            </button>
            <button
              onClick={() => window.location.reload()}
              className="px-4 py-2 text-sm font-medium text-accent-on bg-accent-strong rounded-lg hover:opacity-90 transition-opacity"
            >
              {i18n.t('Reload page')}
            </button>
          </div>
        </div>
      </div>
    );
  }
}
