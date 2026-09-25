'use client';

import { Button } from '@/components/ui';
import { ClipboardCheck, Code, LightBulb, Page } from '@/components/ui/icons';

interface WelcomeStepProps {
  onNext: () => void;
  /** Model providers the server already configured, so no keys are needed. */
  configuredProviders?: string[];
}

export function WelcomeStep({ onNext, configuredProviders = [] }: WelcomeStepProps) {
  return (
    <div className="p-8">
      {/* Icon */}
      <div className="w-16 h-16 mx-auto mb-6 rounded-xl bg-gradient-to-br from-sc-purple/20 to-sc-cyan/20 flex items-center justify-center">
        <LightBulb width={32} height={32} className="text-sc-purple" />
      </div>

      {/* Content */}
      <div className="text-center mb-8">
        <h2 className="text-2xl font-semibold text-sc-fg-primary mb-3">Welcome to Sibyl</h2>
        <p className="text-sc-fg-muted leading-relaxed max-w-md mx-auto">
          Sibyl is a memory system for you and your AI agents. Knowledge, context, and tasks that
          persist across every session.
        </p>
      </div>

      {/* Features */}
      <div className="grid gap-4 mb-8">
        <Feature
          icon={<Page aria-hidden="true" width={20} height={20} />}
          title="Knowledge Graph"
          description="Capture patterns, decisions, and learnings in a persistent knowledge base"
        />
        <Feature
          icon={<ClipboardCheck aria-hidden="true" width={20} height={20} />}
          title="Task Tracking"
          description="Manage development tasks with full lifecycle tracking and learnings capture"
        />
        <Feature
          icon={<Code aria-hidden="true" width={20} height={20} />}
          title="Works With Any Agent"
          description="Use the sibyl CLI or connect any MCP client: Claude Code, Codex, opencode, OpenClaw, and more"
        />
      </div>

      {configuredProviders.length > 0 && (
        <p className="mb-6 text-center text-sm text-sc-fg-muted">
          Models are configured on the server:{' '}
          <span className="text-sc-green">{configuredProviders.join(', ')}</span>
        </p>
      )}

      {/* CTA */}
      <Button
        type="button"
        variant="primary"
        size="lg"
        onClick={onNext}
        className="w-full focus-visible:ring-offset-sc-bg-elevated"
      >
        Let's Get Started
      </Button>
    </div>
  );
}

function Feature({
  icon,
  title,
  description,
}: {
  icon: React.ReactNode;
  title: string;
  description: string;
}) {
  return (
    <div className="flex gap-4 p-4 rounded-xl bg-sc-bg-highlight border border-sc-fg-subtle/10">
      <div className="flex-shrink-0 w-10 h-10 rounded-lg bg-sc-purple/10 flex items-center justify-center text-sc-purple">
        {icon}
      </div>
      <div>
        <h3 className="font-medium text-sc-fg-primary mb-0.5">{title}</h3>
        <p className="text-sm text-sc-fg-muted">{description}</p>
      </div>
    </div>
  );
}
