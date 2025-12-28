'use client';

import { ArrowRight, Command, Sparkles } from 'lucide-react';

interface CompletionStepProps {
  onFinish: () => void;
}

export function CompletionStep({ onFinish }: CompletionStepProps) {
  return (
    <div className="p-5">
      {/* Celebration */}
      <div className="text-center">
        {/* Animated glow effect */}
        <div className="relative inline-flex items-center justify-center mb-5">
          <div className="absolute w-20 h-20 rounded-full bg-sc-green/10 animate-pulse" />
          <div className="absolute w-16 h-16 rounded-full bg-sc-green/15 animate-pulse delay-75" />
          <div className="relative inline-flex items-center justify-center w-14 h-14 rounded-full bg-gradient-to-br from-sc-green to-sc-cyan text-sc-bg-dark">
            <Sparkles className="w-7 h-7" />
          </div>
        </div>

        <h2 className="text-xl font-semibold text-sc-fg-primary mb-2">You're All Set!</h2>
        <p className="text-sc-fg-muted text-sm mb-6 max-w-sm mx-auto">
          Your knowledge graph is ready. Start capturing patterns, tracking tasks, and building your
          team's wisdom.
        </p>

        {/* Quick tips */}
        <div className="bg-sc-bg-dark border border-sc-fg-subtle/10 rounded-xl p-4 mb-6 text-left">
          <h3 className="font-medium text-sc-fg-primary mb-3 text-sm">Quick Tips</h3>
          <ul className="space-y-2.5 text-sm text-sc-fg-muted">
            <li className="flex items-start gap-2.5">
              <span className="flex items-center justify-center w-5 h-5 rounded bg-sc-purple/15 text-sc-purple shrink-0 mt-0.5">
                <Command className="w-3 h-3" />
              </span>
              <span>
                Press{' '}
                <kbd className="px-1.5 py-0.5 text-xs bg-sc-bg-elevated rounded border border-sc-fg-subtle/20 font-mono">
                  ⌘K
                </kbd>{' '}
                to quickly search your knowledge
              </span>
            </li>
            <li className="flex items-start gap-2.5">
              <span className="w-5 h-5 rounded bg-sc-cyan/15 text-sc-cyan flex items-center justify-center text-xs font-bold shrink-0 mt-0.5">
                +
              </span>
              <span>Add learnings after completing tasks to build memory</span>
            </li>
            <li className="flex items-start gap-2.5">
              <span className="w-5 h-5 rounded bg-sc-green/15 text-sc-green flex items-center justify-center text-xs font-bold shrink-0 mt-0.5">
                ⚡
              </span>
              <span>Connect Sibyl to your IDE via MCP for inline assistance</span>
            </li>
          </ul>
        </div>

        <button
          type="button"
          onClick={onFinish}
          className="flex items-center justify-center gap-2 w-full px-5 py-3 bg-sc-purple hover:bg-sc-purple/80 text-white rounded-xl font-medium transition-colors shadow-lg shadow-sc-purple/25"
        >
          Go to Dashboard
          <ArrowRight className="w-4 h-4" />
        </button>
      </div>
    </div>
  );
}
