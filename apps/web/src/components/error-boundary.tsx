'use client';

import { Component, type ErrorInfo, type ReactNode } from 'react';
import { Button } from '@/components/ui/button';
import { AlertTriangle } from '@/components/ui/icons';

// =============================================================================
// Error Boundary Types
// =============================================================================

interface ErrorBoundaryProps {
  children: ReactNode;
  /** Fallback UI to render on error. If not provided, uses default ErrorFallback */
  fallback?: ReactNode | ((error: Error, reset: () => void) => ReactNode);
  /** Called when an error is caught */
  onError?: (error: Error, errorInfo: ErrorInfo) => void;
  /** Boundary level affects styling and recovery options */
  level?: 'page' | 'section' | 'component';
}

interface ErrorBoundaryState {
  hasError: boolean;
  error: Error | null;
}

// =============================================================================
// Error Boundary Class Component
// =============================================================================

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    console.error('[ErrorBoundary] Caught error:', error, errorInfo);
    this.props.onError?.(error, errorInfo);
  }

  reset = (): void => {
    this.setState({ hasError: false, error: null });
  };

  render(): ReactNode {
    if (this.state.hasError && this.state.error) {
      // Custom fallback function
      if (typeof this.props.fallback === 'function') {
        return this.props.fallback(this.state.error, this.reset);
      }

      // Custom fallback node
      if (this.props.fallback) {
        return this.props.fallback;
      }

      // Default fallback
      return (
        <ErrorFallback
          error={this.state.error}
          reset={this.reset}
          level={this.props.level ?? 'section'}
        />
      );
    }

    return this.props.children;
  }
}

// =============================================================================
// Error Fallback UI
// =============================================================================

interface ErrorFallbackProps {
  error: Error;
  reset: () => void;
  level?: 'page' | 'section' | 'component';
}

const LEVEL_STYLES = {
  page: {
    container: 'min-h-[60vh] flex items-center justify-center',
    icon: 'text-7xl',
    title: 'text-2xl',
    message: 'text-base max-w-lg',
  },
  section: {
    container: 'py-16',
    icon: 'text-5xl',
    title: 'text-xl',
    message: 'text-sm max-w-md',
  },
  component: {
    container: 'py-8',
    icon: 'text-3xl',
    title: 'text-lg',
    message: 'text-xs max-w-sm',
  },
};

export function ErrorFallback({ error, reset, level = 'section' }: ErrorFallbackProps) {
  const styles = LEVEL_STYLES[level];

  // Friendly error messages for common scenarios
  const friendlyMessage = getFriendlyErrorMessage(error);

  // Icon sizes based on level
  const iconSizes = { page: 56, section: 40, component: 28 };
  const iconSize = iconSizes[level];

  return (
    <div className={`text-center animate-fade-in ${styles.container}`}>
      <div className="mb-4 flex justify-center animate-wiggle">
        <AlertTriangle width={iconSize} height={iconSize} className="text-sc-red" />
      </div>
      <h2 className={`font-semibold text-sc-red ${styles.title}`}>Something went wrong</h2>
      <p className={`text-sc-fg-muted mt-2 mx-auto ${styles.message}`}>{friendlyMessage}</p>

      {/* Error details (dev only) */}
      {process.env.NODE_ENV === 'development' && (
        <details className="mt-4 text-left max-w-lg mx-auto">
          <summary className="text-sc-fg-subtle text-xs cursor-pointer hover:text-sc-fg-muted">
            Technical details
          </summary>
          <pre className="mt-2 p-3 bg-sc-bg-elevated rounded-lg text-xs text-sc-coral overflow-auto max-h-48">
            {error.message}
            {error.stack && (
              <>
                {'\n\n'}
                {error.stack}
              </>
            )}
          </pre>
        </details>
      )}

      <div className="mt-6 flex items-center justify-center gap-3">
        <Button onClick={reset} variant="primary">
          Try again
        </Button>
        {level === 'page' && (
          <Button
            onClick={() => {
              window.location.href = '/';
            }}
            variant="secondary"
          >
            Go home
          </Button>
        )}
      </div>
    </div>
  );
}

// =============================================================================
// Helper: Friendly Error Messages
// =============================================================================

function getFriendlyErrorMessage(error: Error): string {
  const message = error.message.toLowerCase();

  if (message.includes('fetch') || message.includes('network')) {
    return "Couldn't reach the server. Check your connection and try again.";
  }

  if (message.includes('timeout')) {
    return 'The request took too long. The server might be busy.';
  }

  if (message.includes('404') || message.includes('not found')) {
    return "We couldn't find what you're looking for.";
  }

  if (message.includes('401') || message.includes('unauthorized')) {
    return "You don't have permission to access this.";
  }

  if (message.includes('500') || message.includes('server error')) {
    return 'The server encountered an error. Try again in a moment.';
  }

  // Default
  return "Something unexpected happened. We're looking into it.";
}

// =============================================================================
// Async Error Boundary (for async server components)
// =============================================================================

interface AsyncBoundaryProps {
  children: ReactNode;
  fallback?: ReactNode;
  level?: 'page' | 'section' | 'component';
}

/**
 * Wrapper that combines ErrorBoundary with proper typing for async children.
 * Use this when wrapping async Server Components.
 */
export function AsyncBoundary({ children, fallback, level = 'section' }: AsyncBoundaryProps) {
  return (
    <ErrorBoundary fallback={fallback} level={level}>
      {children}
    </ErrorBoundary>
  );
}
