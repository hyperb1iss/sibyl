'use client';

import { useState } from 'react';
import {
  useDestroySandbox,
  useResumeSandbox,
  useSandbox,
  useSandboxLogs,
  useSuspendSandbox,
} from '@/lib/hooks';
import { SandboxTerminal } from './SandboxTerminal';

interface SandboxDetailProps {
  sandboxId: string;
  onClose: () => void;
}

function MetadataRow({ label, value }: { label: string; value: string | null | undefined }) {
  return (
    <div className="flex items-center justify-between py-1.5">
      <span className="text-xs text-sc-fg-subtle">{label}</span>
      <span className="font-mono text-xs text-sc-fg-primary">{value ?? '\u2014'}</span>
    </div>
  );
}

type DetailTab = 'info' | 'terminal' | 'logs';

export function SandboxDetail({ sandboxId, onClose }: SandboxDetailProps) {
  const [tab, setTab] = useState<DetailTab>('info');
  const { data: sandbox, isLoading } = useSandbox(sandboxId);
  const { data: logsData } = useSandboxLogs(sandboxId, {
    enabled: tab === 'logs',
    tail: 200,
  });
  const suspendMutation = useSuspendSandbox();
  const resumeMutation = useResumeSandbox();
  const destroyMutation = useDestroySandbox();

  if (isLoading) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-sc-fg-subtle">
        Loading...
      </div>
    );
  }

  if (!sandbox) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-sc-fg-subtle">
        Sandbox not found
      </div>
    );
  }

  const status = (
    ((sandbox as Record<string, unknown>).status as string) ?? 'unknown'
  ).toLowerCase();
  const podName = (sandbox as Record<string, unknown>).pod_name as string | undefined;
  const runnerId = (sandbox as Record<string, unknown>).runner_id as string | undefined;
  const errorMsg = (sandbox as Record<string, unknown>).error_message as string | undefined;

  const tabs: DetailTab[] = ['info', 'terminal', 'logs'];

  // Extract logs text from response
  const logsText = logsData
    ? typeof logsData === 'string'
      ? logsData
      : (((logsData as Record<string, unknown>).logs as string) ?? '')
    : '';

  return (
    <div className="flex flex-col gap-3 rounded-xl border border-sc-fg-subtle/20 bg-sc-bg-elevated p-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-sc-fg-primary">
          {podName ?? sandboxId.slice(0, 12)}
        </h2>
        <button
          type="button"
          onClick={onClose}
          className="text-xs text-sc-fg-subtle hover:text-sc-fg-primary"
        >
          Close
        </button>
      </div>

      {/* Tab bar */}
      <div className="flex gap-1 border-b border-sc-fg-subtle/10 pb-2">
        {tabs.map(t => (
          <button
            type="button"
            key={t}
            onClick={() => setTab(t)}
            className={`rounded-md px-3 py-1 text-xs font-medium transition-colors ${
              tab === t
                ? 'bg-sc-cyan/15 text-sc-cyan'
                : 'text-sc-fg-subtle hover:text-sc-fg-primary'
            }`}
          >
            {t.charAt(0).toUpperCase() + t.slice(1)}
          </button>
        ))}
      </div>

      {/* Tab content */}
      {tab === 'info' && (
        <div className="flex flex-col gap-3">
          {/* Metadata */}
          <div className="divide-y divide-sc-fg-subtle/10">
            <MetadataRow label="ID" value={sandbox.id} />
            <MetadataRow label="Status" value={status} />
            <MetadataRow label="Pod" value={podName} />
            <MetadataRow label="Runner" value={runnerId?.slice(0, 12)} />
            <MetadataRow label="Image" value={sandbox.image} />
            <MetadataRow label="Created" value={sandbox.created_at} />
            <MetadataRow label="Updated" value={sandbox.updated_at} />
          </div>

          {/* Error message */}
          {errorMsg && (
            <div className="rounded-lg border border-sc-red/30 bg-sc-red/10 px-3 py-2 text-xs text-sc-red">
              {errorMsg}
            </div>
          )}

          {/* Actions */}
          <div className="flex gap-2 pt-2">
            {status === 'running' && (
              <button
                type="button"
                onClick={() => suspendMutation.mutate(sandboxId)}
                disabled={suspendMutation.isPending}
                className="rounded-md bg-sc-yellow/15 px-3 py-1.5 text-xs font-medium text-sc-yellow hover:bg-sc-yellow/25 disabled:opacity-50"
              >
                {suspendMutation.isPending ? 'Suspending...' : 'Suspend'}
              </button>
            )}
            {status === 'suspended' && (
              <button
                type="button"
                onClick={() => resumeMutation.mutate(sandboxId)}
                disabled={resumeMutation.isPending}
                className="rounded-md bg-sc-green/15 px-3 py-1.5 text-xs font-medium text-sc-green hover:bg-sc-green/25 disabled:opacity-50"
              >
                {resumeMutation.isPending ? 'Resuming...' : 'Resume'}
              </button>
            )}
            <button
              type="button"
              onClick={() => {
                if (confirm('Destroy this sandbox? This cannot be undone.')) {
                  destroyMutation.mutate(sandboxId);
                }
              }}
              disabled={destroyMutation.isPending}
              className="rounded-md bg-sc-red/15 px-3 py-1.5 text-xs font-medium text-sc-red hover:bg-sc-red/25 disabled:opacity-50"
            >
              {destroyMutation.isPending ? 'Destroying...' : 'Destroy'}
            </button>
          </div>
        </div>
      )}

      {tab === 'terminal' && (
        <div className="min-h-[400px]">
          {status === 'running' ? (
            <SandboxTerminal sandboxId={sandboxId} />
          ) : (
            <div className="flex h-[400px] items-center justify-center text-sm text-sc-fg-subtle">
              Sandbox must be running to attach terminal
            </div>
          )}
        </div>
      )}

      {tab === 'logs' && (
        <div className="max-h-[500px] overflow-auto rounded-lg bg-black/30 p-3">
          <pre className="whitespace-pre-wrap font-mono text-xs text-sc-fg-primary">
            {logsText || 'No logs available'}
          </pre>
        </div>
      )}
    </div>
  );
}
