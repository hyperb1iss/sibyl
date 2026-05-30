'use client';

import { useState } from 'react';
import { Check, Copy, Download, Network, Page } from '@/components/ui/icons';
import { Spinner } from '@/components/ui/spinner';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import type { McpClientConfig } from '@/lib/api';
import { useIntegration } from '@/lib/hooks';

/** Duration to show "Copied!" feedback in milliseconds */
const COPY_FEEDBACK_DURATION_MS = 2000;

/**
 * ConnectPanel: client-agnostic guide to start using Sibyl.
 *
 * Shared by the setup wizard's final step and the dashboard connect modal.
 * Three paths: install the CLI, wire an MCP client, and paste the agent
 * prompt snippet. Fetches its own data so both consumers just render it.
 */
export function ConnectPanel() {
  const { data, isLoading, isError } = useIntegration();

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
    <div className="space-y-6">
      <Section
        icon={<Download aria-hidden="true" width={18} height={18} />}
        title="Run it yourself"
        description="Install the CLI and Sibyl is yours from any terminal: search, remember, track tasks."
      >
        <CopyBlock value={data.cli_install} />
        <p className="mt-2 text-xs text-sc-fg-subtle">
          Already use uv? <code className="font-mono text-sc-cyan">{data.cli_install_alt}</code>
        </p>
      </Section>

      <Section
        icon={<Network aria-hidden="true" width={18} height={18} />}
        title="Connect your agent"
        description="Wire Sibyl into your AI agent over MCP. Pick your client."
      >
        <McpClientTabs clients={data.mcp_clients} />
      </Section>

      <Section
        icon={<Page aria-hidden="true" width={18} height={18} />}
        title="Teach your agent"
        description="Paste this into your agent's instructions so it actually uses Sibyl."
      >
        <CopyBlock value={data.prompt_snippet} scroll />
        <p className="mt-2 text-xs text-sc-fg-subtle">
          Goes in AGENTS.md, CLAUDE.md, or your agent's system prompt.
        </p>
      </Section>
    </div>
  );
}

function McpClientTabs({ clients }: { clients: McpClientConfig[] }) {
  if (clients.length === 0) {
    return null;
  }

  return (
    <Tabs defaultValue={clients[0].id} variant="pills">
      <TabsList>
        {clients.map(client => (
          <TabsTrigger key={client.id} value={client.id}>
            {client.label}
          </TabsTrigger>
        ))}
      </TabsList>
      {clients.map(client => (
        <TabsContent key={client.id} value={client.id}>
          <CopyBlock value={client.snippet} scroll={client.kind === 'config'} />
          <p className="mt-2 text-xs text-sc-fg-subtle">
            {client.kind === 'command'
              ? 'Run this in your terminal.'
              : `Add this to ${client.target ?? 'your MCP client config'}.`}
          </p>
        </TabsContent>
      ))}
    </Tabs>
  );
}

function Section({
  icon,
  title,
  description,
  children,
}: {
  icon: React.ReactNode;
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="mb-1.5 flex items-center gap-2.5">
        <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-sc-purple/10 text-sc-purple">
          {icon}
        </span>
        <h3 className="font-medium text-sc-fg-primary">{title}</h3>
      </div>
      <p className="mb-3 text-sm text-sc-fg-muted">{description}</p>
      {children}
    </div>
  );
}

function CopyBlock({ value, scroll = false }: { value: string; scroll?: boolean }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    await navigator.clipboard.writeText(value);
    setCopied(true);
    setTimeout(() => setCopied(false), COPY_FEEDBACK_DURATION_MS);
  };

  return (
    <div className="relative">
      <pre
        className={`w-full overflow-x-auto rounded-lg border border-sc-fg-subtle/10 bg-sc-bg-base p-3 pr-11 ${
          scroll ? 'max-h-60 overflow-y-auto' : ''
        }`}
      >
        <code className="whitespace-pre font-mono text-xs text-sc-cyan">{value}</code>
      </pre>
      <button
        type="button"
        onClick={handleCopy}
        className="absolute right-2 top-2 rounded-lg bg-sc-bg-elevated/80 p-1.5 text-sc-fg-muted transition-colors hover:bg-sc-bg-elevated hover:text-sc-fg-primary"
        title="Copy to clipboard"
        aria-label="Copy to clipboard"
      >
        {copied ? (
          <Check aria-hidden="true" width={16} height={16} className="text-sc-green" />
        ) : (
          <Copy aria-hidden="true" width={16} height={16} />
        )}
      </button>
    </div>
  );
}
