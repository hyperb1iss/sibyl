'use client';

import { ArrowRight, Terminal } from 'lucide-react';
import { ConnectPanel } from '@/components/connect';

interface ConnectStepProps {
  onBack: () => void;
  onNext: () => void;
  onSkip: () => void;
}

export function ConnectStep({ onBack, onNext, onSkip }: ConnectStepProps) {
  return (
    <div className="p-5">
      <div className="text-center mb-5">
        <div className="relative inline-flex items-center justify-center mb-4">
          <div className="absolute w-16 h-16 rounded-full bg-sc-purple/15 animate-pulse" />
          <div className="relative inline-flex items-center justify-center w-14 h-14 rounded-full bg-sc-purple/20 text-sc-purple ring-1 ring-sc-purple/30">
            <Terminal className="w-7 h-7" />
          </div>
        </div>
        <h2 className="text-xl font-semibold text-sc-fg-primary mb-2">Connect your tools</h2>
        <p className="text-sc-fg-muted text-sm">Sibyl lives in your terminal and your agents.</p>
      </div>

      <ConnectPanel />

      <div className="flex items-center justify-between pt-4 mt-5 border-t border-sc-fg-subtle/10">
        <div className="flex items-center gap-4">
          <button
            type="button"
            onClick={onBack}
            className="rounded text-sm text-sc-fg-muted transition-colors duration-200 hover:text-sc-fg-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated"
          >
            Back
          </button>
          <button
            type="button"
            onClick={onSkip}
            className="rounded text-sm text-sc-fg-muted transition-colors duration-200 hover:text-sc-fg-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated"
          >
            Skip for now
          </button>
        </div>
        <button
          type="button"
          onClick={onNext}
          className="flex items-center gap-2 px-5 py-2.5 bg-sc-purple hover:bg-sc-purple/80 text-sc-on-accent rounded-lg font-medium transition-colors duration-200 shadow-glow-purple focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated"
        >
          Continue
          <ArrowRight className="w-4 h-4" />
        </button>
      </div>
    </div>
  );
}
