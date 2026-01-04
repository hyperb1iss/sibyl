'use client';

import { useState } from 'react';
import {
  Check,
  CheckCircle,
  ClipboardCheck,
  Copy,
  Plus,
  Search,
} from '@/components/ui/icons';
import { Spinner } from '@/components/ui/spinner';
import { useMcpCommand } from '@/lib/hooks';

/** Duration to show "Copied!" feedback in milliseconds */
const COPY_FEEDBACK_DURATION_MS = 2000;

interface ConnectClaudeStepProps {
  onFinish: () => void;
}

export function ConnectClaudeStep({ onFinish }: ConnectClaudeStepProps) {
  const { data: mcpData, isLoading } = useMcpCommand();
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    if (mcpData?.command) {
      await navigator.clipboard.writeText(mcpData.command);
      setCopied(true);
      setTimeout(() => setCopied(false), COPY_FEEDBACK_DURATION_MS);
    }
  };

  return (
    <div className="p-8">
      {/* Success Icon */}
      <div className="w-20 h-20 mx-auto mb-6 rounded-full bg-gradient-to-br from-sc-green/20 to-sc-cyan/20 flex items-center justify-center">
        <CheckCircle aria-hidden="true" width={40} height={40} className="text-sc-green" />
      </div>

      {/* Content */}
      <div className="text-center mb-8">
        <h2 className="text-2xl font-semibold text-sc-fg-primary mb-3">Setup Complete!</h2>
        <p className="text-sc-fg-muted leading-relaxed max-w-md mx-auto">
          Sibyl is ready. Connect Claude Code to start building your knowledge graph.
        </p>
      </div>

      {/* MCP Command */}
      <div className="mb-8">
        <div className="flex items-center justify-between mb-2">
          <label className="text-sm font-medium text-sc-fg-secondary">Connect Claude Code</label>
          <span className="text-xs text-sc-fg-subtle">Run this in your terminal</span>
        </div>

        {isLoading ? (
          <div className="flex items-center justify-center py-4 bg-sc-bg-base rounded-lg border border-sc-fg-subtle/10">
            <Spinner size="sm" color="purple" />
          </div>
        ) : (
          <div className="relative group">
            <code className="block w-full p-4 pr-12 rounded-lg bg-sc-bg-base border border-sc-fg-subtle/10 font-mono text-sm text-sc-cyan break-all">
              {mcpData?.command ||
                'claude mcp add sibyl --transport http http://localhost:3334/mcp'}
            </code>
            <button
              type="button"
              onClick={handleCopy}
              className="absolute right-2 top-1/2 -translate-y-1/2 p-2 rounded-md bg-sc-bg-elevated/80 text-sc-fg-muted hover:text-sc-fg-primary hover:bg-sc-bg-elevated transition-colors"
              title="Copy to clipboard"
            >
              {copied ? (
                <Check aria-hidden="true" width={20} height={20} className="text-sc-green" />
              ) : (
                <Copy aria-hidden="true" width={20} height={20} />
              )}
            </button>
          </div>
        )}
      </div>

      {/* Quick Tips */}
      <div className="mb-8 space-y-3">
        <h3 className="text-sm font-medium text-sc-fg-secondary mb-2">Quick Tips</h3>
        <Tip
          icon={<Search aria-hidden="true" width={16} height={16} />}
          text="Search your knowledge with 'sibyl search <query>'"
        />
        <Tip
          icon={<Plus aria-hidden="true" width={16} height={16} />}
          text='Add learnings with: sibyl add "Title" "What you learned"'
        />
        <Tip
          icon={<ClipboardCheck aria-hidden="true" width={16} height={16} />}
          text='Track tasks with: sibyl task create --title "My task"'
        />
      </div>

      {/* CTA */}
      <button
        type="button"
        onClick={onFinish}
        className="w-full py-3 px-4 rounded-lg bg-sc-purple text-white font-medium transition-all duration-200 hover:bg-sc-purple/90 hover:shadow-lg hover:shadow-sc-purple/25 active:scale-[0.98]"
      >
        Start Using Sibyl
      </button>
    </div>
  );
}

function Tip({ icon, text }: { icon: React.ReactNode; text: string }) {
  return (
    <div className="flex items-center gap-3 text-sm">
      <span className="text-sc-purple">{icon}</span>
      <code className="text-sc-fg-muted font-mono text-xs">{text}</code>
    </div>
  );
}
