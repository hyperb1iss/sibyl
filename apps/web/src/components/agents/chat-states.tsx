'use client';

/**
 * Loading and empty state components for agent chat.
 */

import { useEffect, useState } from 'react';
import { Spinner } from '@/components/ui/spinner';
import { pickRandomPhrase } from './chat-constants';

// =============================================================================
// ThinkingIndicator
// =============================================================================

/** Animated thinking indicator with playful phrases */
export function ThinkingIndicator() {
  // Pick a random phrase once when component mounts - no cycling
  const [phrase] = useState(() => pickRandomPhrase());
  const [dotCount, setDotCount] = useState(1);

  // Animate dots
  useEffect(() => {
    const dotInterval = setInterval(() => {
      setDotCount(prev => (prev % 3) + 1);
    }, 400);
    return () => clearInterval(dotInterval);
  }, []);

  const dots = '.'.repeat(dotCount).padEnd(3, '\u00A0');

  return (
    <div className="flex items-center gap-3 px-4 py-3 rounded-lg bg-gradient-to-r from-sc-purple/10 via-sc-cyan/5 to-sc-purple/10 border border-sc-purple/30 animate-slide-up relative overflow-hidden">
      {/* Subtle shimmer overlay */}
      <div className="absolute inset-0 bg-gradient-to-r from-transparent via-sc-purple/5 to-transparent animate-shimmer-slow pointer-events-none" />

      {/* Subtle glow */}
      <div className="relative">
        <div className="absolute inset-0 bg-sc-purple/15 rounded-full blur-sm" />
        <Spinner size="sm" className="text-sc-purple relative z-10" />
      </div>

      <span className="text-sm text-sc-fg-muted font-medium">
        <span className="text-sc-purple">{phrase}</span>
        <span className="text-sc-cyan font-mono">{dots}</span>
      </span>
    </div>
  );
}

// =============================================================================
// EmptyChatState
// =============================================================================

export interface EmptyChatStateProps {
  agentName: string;
}

/** Empty chat state with personality - shown when no messages yet */
export function EmptyChatState({ agentName }: EmptyChatStateProps) {
  return (
    <div className="flex flex-col items-center justify-center h-full py-12 px-4 animate-fade-in">
      <div className="relative mb-6">
        {/* Floating glow behind icon */}
        <div className="absolute inset-0 bg-gradient-to-br from-sc-purple/20 to-sc-cyan/20 rounded-full blur-xl animate-glow-pulse" />
        <div className="relative bg-gradient-to-br from-sc-purple/10 to-sc-cyan/10 p-6 rounded-2xl border border-sc-purple/20">
          <div className="text-4xl animate-float">
            <svg
              width="48"
              height="48"
              viewBox="0 0 24 24"
              fill="none"
              className="text-sc-purple"
              aria-hidden="true"
            >
              <path
                d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"
                fill="currentColor"
                opacity="0.2"
              />
              <circle cx="12" cy="12" r="3" fill="currentColor" className="animate-pulse" />
              <path
                d="M12 2v2m0 16v2M2 12h2m16 0h2"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
              />
            </svg>
          </div>
        </div>
      </div>

      <h3 className="text-lg font-semibold text-sc-fg-primary mb-2">{agentName} is ready</h3>
      <p className="text-sm text-sc-fg-muted text-center max-w-xs">
        This agent is standing by. Watch the magic unfold as it works through its task.
      </p>
    </div>
  );
}
