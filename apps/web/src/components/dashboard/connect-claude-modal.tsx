'use client';

import { useState } from 'react';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Check, Copy, Sparks } from '@/components/ui/icons';
import { Spinner } from '@/components/ui/spinner';
import { useMcpCommand } from '@/lib/hooks';

/** Duration to show "Copied!" feedback in milliseconds */
const COPY_FEEDBACK_DURATION_MS = 2000;

interface ConnectClaudeModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function ConnectClaudeModal({ open, onOpenChange }: ConnectClaudeModalProps) {
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
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="md">
        <DialogHeader>
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-sc-purple via-sc-magenta to-sc-coral flex items-center justify-center shadow-lg shadow-sc-purple/30">
              <Sparks aria-hidden="true" width={20} height={20} className="text-white" />
            </div>
            <div>
              <DialogTitle>Connect Claude Code</DialogTitle>
              <DialogDescription>
                Add Sibyl as an MCP server to unlock AI-powered knowledge management
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        {/* Steps */}
        <div className="space-y-4 my-6">
          {/* Step 1 */}
          <div className="flex gap-3">
            <div className="flex-shrink-0 w-6 h-6 rounded-full bg-sc-purple/20 flex items-center justify-center text-xs font-bold text-sc-purple">
              1
            </div>
            <div className="flex-1">
              <p className="text-sm font-medium text-sc-fg-primary mb-1">
                Open your terminal
              </p>
              <p className="text-xs text-sc-fg-muted">
                Make sure you have Claude Code CLI installed
              </p>
            </div>
          </div>

          {/* Step 2 */}
          <div className="flex gap-3">
            <div className="flex-shrink-0 w-6 h-6 rounded-full bg-sc-cyan/20 flex items-center justify-center text-xs font-bold text-sc-cyan">
              2
            </div>
            <div className="flex-1">
              <p className="text-sm font-medium text-sc-fg-primary mb-2">
                Run this command
              </p>
              {isLoading ? (
                <div className="flex items-center justify-center py-3 bg-sc-bg-elevated rounded-lg border border-sc-fg-subtle/10">
                  <Spinner size="sm" color="purple" />
                </div>
              ) : (
                <div className="relative group">
                  <code className="block w-full p-3 pr-12 rounded-lg bg-sc-bg-elevated border border-sc-fg-subtle/10 font-mono text-xs text-sc-cyan break-all">
                    {mcpData?.command ||
                      'claude mcp add sibyl --transport http http://localhost:3334/mcp'}
                  </code>
                  <button
                    type="button"
                    onClick={handleCopy}
                    className="absolute right-2 top-1/2 -translate-y-1/2 p-1.5 rounded-md bg-sc-bg-base/80 text-sc-fg-muted hover:text-sc-fg-primary hover:bg-sc-bg-base transition-colors"
                    title="Copy to clipboard"
                  >
                    {copied ? (
                      <Check aria-hidden="true" width={16} height={16} className="text-sc-green" />
                    ) : (
                      <Copy aria-hidden="true" width={16} height={16} />
                    )}
                  </button>
                </div>
              )}
            </div>
          </div>

          {/* Step 3 */}
          <div className="flex gap-3">
            <div className="flex-shrink-0 w-6 h-6 rounded-full bg-sc-green/20 flex items-center justify-center text-xs font-bold text-sc-green">
              3
            </div>
            <div className="flex-1">
              <p className="text-sm font-medium text-sc-fg-primary mb-1">
                Start using Sibyl
              </p>
              <p className="text-xs text-sc-fg-muted">
                Claude Code will now have access to your knowledge graph. Try asking it to search or add knowledge!
              </p>
            </div>
          </div>
        </div>

        {/* Footer */}
        <div className="flex justify-end gap-3">
          <button
            type="button"
            onClick={() => onOpenChange(false)}
            className="px-4 py-2 rounded-lg bg-sc-purple text-white text-sm font-medium transition-all hover:bg-sc-purple/90 hover:shadow-lg hover:shadow-sc-purple/25"
          >
            Done
          </button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
