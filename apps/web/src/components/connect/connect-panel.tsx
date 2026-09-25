'use client';

import { useRef, useState } from 'react';
import { Check, Copy } from '@/components/ui/icons';
import { Spinner } from '@/components/ui/spinner';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import type { ConnectOs } from '@/lib/api/admin';
import { useConnectInfo } from '@/lib/hooks/admin';

/** Duration to show "Copied" feedback in milliseconds */
const COPY_FEEDBACK_DURATION_MS = 2000;

const OS_LABELS: Record<ConnectOs, string> = {
  macos: 'macOS',
  linux: 'Linux',
  windows: 'Windows',
};

export function detectOs(userAgent: string): ConnectOs {
  if (/Mac|iPhone|iPad/i.test(userAgent)) return 'macos';
  if (/Win/i.test(userAgent)) return 'windows';
  return 'linux';
}

/** The sentence a person hands their agent; `/agent` serves the steps as markdown. */
export function agentSentence(origin: string): string {
  return `Set up Sibyl on this machine by following ${origin}/agent`;
}

/**
 * ConnectPanel: the one way to connect a machine to this server.
 *
 * Terminal shows a single line for the visitor's OS that installs the CLI and
 * runs `sibyl setup`. Agent shows a single sentence that points an agent at
 * the same steps. Shared by the setup wizard, onboarding, and the dashboard.
 */
export function ConnectPanel() {
  const { data, isLoading, isError } = useConnectInfo();
  // The panel only renders after client-side data loads, so reading the
  // browser here never races server rendering.
  const [os, setOs] = useState<ConnectOs>(() =>
    typeof navigator === 'undefined' ? 'macos' : detectOs(navigator.userAgent)
  );
  const origin = typeof window === 'undefined' ? '' : window.location.origin;

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-10">
        <Spinner size="lg" color="purple" />
      </div>
    );
  }

  if (isError || !data) {
    return (
      <p className="py-6 text-center text-sm text-sc-fg-muted">
        Couldn't load connection details. Make sure the Sibyl server is running.
      </p>
    );
  }

  return (
    <div className="rounded-xl border border-sc-fg-subtle/10 bg-sc-bg-highlight/40 p-4 text-left">
      <Tabs defaultValue="terminal" variant="pills">
        <TabsList>
          <TabsTrigger value="terminal">Terminal</TabsTrigger>
          <TabsTrigger value="agent">Agent</TabsTrigger>
        </TabsList>

        <TabsContent value="terminal">
          <fieldset className="mb-2 flex justify-end gap-1">
            <legend className="sr-only">Operating system</legend>
            {(Object.keys(OS_LABELS) as ConnectOs[]).map(option => (
              <button
                key={option}
                type="button"
                aria-pressed={os === option}
                onClick={() => setOs(option)}
                className={`rounded-md px-2 py-0.5 text-xs transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan ${
                  os === option
                    ? 'bg-sc-purple/15 text-sc-purple'
                    : 'text-sc-fg-muted hover:text-sc-fg-primary'
                }`}
              >
                {OS_LABELS[option]}
              </button>
            ))}
          </fieldset>
          <CopyBlock value={data.install[os]} label="Copy command" />
          <p className="mt-2 text-xs text-sc-fg-muted">
            Installs the CLI, signs you in, and adds the Sibyl skill and hooks to your agents.
          </p>
        </TabsContent>

        <TabsContent value="agent">
          <CopyBlock value={agentSentence(origin)} label="Copy sentence" />
          <p className="mt-2 text-xs text-sc-fg-muted">
            Paste it into Claude Code, Codex, or any coding agent. It runs the same setup and hands
            you the sign-in.
          </p>
        </TabsContent>
      </Tabs>
    </div>
  );
}

function CopyBlock({ value, label }: { value: string; label: string }) {
  const [feedback, setFeedback] = useState<'idle' | 'copied' | 'selected'>('idle');
  const codeRef = useRef<HTMLElement>(null);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setFeedback('copied');
      setTimeout(() => setFeedback('idle'), COPY_FEEDBACK_DURATION_MS);
    } catch {
      // No clipboard access (insecure origin or a denied permission): select
      // the text so a keyboard copy still works.
      const node = codeRef.current;
      const selection = window.getSelection();
      if (node && selection) {
        const range = document.createRange();
        range.selectNodeContents(node);
        selection.removeAllRanges();
        selection.addRange(range);
      }
      setFeedback('selected');
    }
  };

  return (
    <div>
      <div className="relative">
        <pre className="w-full rounded-lg border border-sc-fg-subtle/10 bg-sc-bg-dark p-3 pr-11">
          <code
            ref={codeRef}
            className="whitespace-pre-wrap font-mono text-xs text-sc-cyan [overflow-wrap:anywhere]"
          >
            {value}
          </code>
        </pre>
        <button
          type="button"
          onClick={handleCopy}
          className="absolute right-2 top-2 rounded-lg bg-sc-bg-elevated/80 p-1.5 text-sc-fg-muted transition-colors duration-200 hover:bg-sc-bg-elevated hover:text-sc-fg-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-dark"
          title={label}
          aria-label={label}
        >
          {feedback === 'copied' ? (
            <Check aria-hidden="true" width={16} height={16} className="text-sc-green" />
          ) : (
            <Copy aria-hidden="true" width={16} height={16} />
          )}
        </button>
      </div>
      <p aria-live="polite" className="sr-only">
        {feedback === 'copied' ? 'Copied' : ''}
      </p>
      {feedback === 'selected' && (
        <p className="mt-1 text-xs text-sc-yellow">Selected. Press Ctrl+C or ⌘C to copy.</p>
      )}
    </div>
  );
}
