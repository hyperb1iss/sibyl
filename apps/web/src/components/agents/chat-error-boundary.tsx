'use client';

/**
 * Error boundary for chat components.
 *
 * Catches rendering errors and provides a user-friendly fallback UI.
 * Allows users to retry without losing the entire page.
 */

import { AlertTriangle, RefreshCcw } from 'lucide-react';
import { Component, type ErrorInfo, type ReactNode } from 'react';

// =============================================================================
// Types
// =============================================================================

interface ChatErrorBoundaryProps {
  children: ReactNode;
  /** Fallback to show on error. If not provided, uses default UI */
  fallback?: ReactNode;
  /** Called when an error is caught */
  onError?: (error: Error, errorInfo: ErrorInfo) => void;
  /** Scope name for error reporting */
  scope?: string;
}

interface ChatErrorBoundaryState {
  hasError: boolean;
  error: Error | null;
  errorInfo: ErrorInfo | null;
}

// =============================================================================
// ChatErrorBoundary
// =============================================================================

/**
 * Error boundary for chat components.
 *
 * Usage:
 * ```tsx
 * <ChatErrorBoundary scope="messages">
 *   <ChatPanel {...props} />
 * </ChatErrorBoundary>
 * ```
 */
export class ChatErrorBoundary extends Component<ChatErrorBoundaryProps, ChatErrorBoundaryState> {
  constructor(props: ChatErrorBoundaryProps) {
    super(props);
    this.state = { hasError: false, error: null, errorInfo: null };
  }

  static getDerivedStateFromError(error: Error): Partial<ChatErrorBoundaryState> {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    // Update state with error info
    this.setState({ errorInfo });

    // Call optional error handler
    this.props.onError?.(error, errorInfo);

    // Log to console in development
    if (process.env.NODE_ENV === 'development') {
      console.error(`[ChatErrorBoundary${this.props.scope ? `:${this.props.scope}` : ''}]`, error);
      console.error('Component stack:', errorInfo.componentStack);
    }
  }

  handleRetry = () => {
    this.setState({ hasError: false, error: null, errorInfo: null });
  };

  render() {
    if (this.state.hasError) {
      // Use custom fallback if provided
      if (this.props.fallback) {
        return this.props.fallback;
      }

      // Default error UI
      return (
        <div className="flex flex-col items-center justify-center p-8 text-center">
          <div className="w-12 h-12 rounded-full bg-sc-red/10 flex items-center justify-center mb-4">
            <AlertTriangle className="w-6 h-6 text-sc-red" />
          </div>
          <h3 className="text-lg font-medium text-sc-fg-primary mb-2">Something went wrong</h3>
          <p className="text-sm text-sc-fg-muted mb-4 max-w-md">
            An error occurred while rendering the chat
            {this.props.scope ? ` (${this.props.scope})` : ''}.
            {process.env.NODE_ENV === 'development' && this.state.error && (
              <span className="block mt-2 font-mono text-xs text-sc-red">
                {this.state.error.message}
              </span>
            )}
          </p>
          <button
            type="button"
            onClick={this.handleRetry}
            className="flex items-center gap-2 px-4 py-2 bg-sc-purple/20 hover:bg-sc-purple/30 text-sc-purple rounded-lg transition-colors"
          >
            <RefreshCcw className="w-4 h-4" />
            Try again
          </button>
        </div>
      );
    }

    return this.props.children;
  }
}

// =============================================================================
// MessageErrorBoundary
// =============================================================================

interface MessageErrorBoundaryProps {
  children: ReactNode;
  messageId?: string;
}

interface MessageErrorBoundaryState {
  hasError: boolean;
}

/**
 * Lightweight error boundary for individual messages.
 * Shows a compact error indicator instead of crashing the entire chat.
 */
export class MessageErrorBoundary extends Component<
  MessageErrorBoundaryProps,
  MessageErrorBoundaryState
> {
  constructor(props: MessageErrorBoundaryProps) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError(): Partial<MessageErrorBoundaryState> {
    return { hasError: true };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    if (process.env.NODE_ENV === 'development') {
      console.error(
        `[MessageErrorBoundary${this.props.messageId ? `:${this.props.messageId}` : ''}]`,
        error
      );
      console.error('Component stack:', errorInfo.componentStack);
    }
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="px-3 py-2 text-xs text-sc-red bg-sc-red/5 rounded-lg border border-sc-red/20 flex items-center gap-2">
          <AlertTriangle className="w-3 h-3 shrink-0" />
          <span>Failed to render message</span>
        </div>
      );
    }

    return this.props.children;
  }
}
