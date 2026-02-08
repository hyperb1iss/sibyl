'use client';

import type { Sandbox } from '@/lib/hooks';

function statusBadge(status?: string) {
  const s = (status ?? 'unknown').toLowerCase();
  const colors: Record<string, string> = {
    running: 'bg-sc-green/15 text-sc-green border-sc-green/30',
    suspended: 'bg-sc-yellow/15 text-sc-yellow border-sc-yellow/30',
    failed: 'bg-sc-red/15 text-sc-red border-sc-red/30',
    pending: 'bg-sc-purple/15 text-sc-purple border-sc-purple/30',
    starting: 'bg-sc-cyan/15 text-sc-cyan border-sc-cyan/30',
    deleted: 'bg-sc-fg-subtle/15 text-sc-fg-subtle border-sc-fg-subtle/30',
  };
  const cls = colors[s] ?? 'bg-sc-fg-subtle/15 text-sc-fg-subtle border-sc-fg-subtle/30';

  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider ${cls}`}
    >
      {s}
    </span>
  );
}

function formatTime(iso?: string) {
  if (!iso) return '\u2014';
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return '\u2014';
  }
}

interface SandboxListProps {
  sandboxes: Sandbox[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}

export function SandboxList({ sandboxes, selectedId, onSelect }: SandboxListProps) {
  if (sandboxes.length === 0) {
    return (
      <div className="flex flex-1 items-center justify-center text-sm text-sc-fg-subtle">
        No sandboxes found
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-1 overflow-y-auto">
      {sandboxes.map(sandbox => (
        <button
          type="button"
          key={sandbox.id}
          onClick={() => onSelect(sandbox.id)}
          className={`flex items-center justify-between rounded-lg border px-4 py-3 text-left transition-colors ${
            selectedId === sandbox.id
              ? 'border-sc-purple/40 bg-sc-purple/10'
              : 'border-sc-fg-subtle/10 bg-sc-bg-elevated hover:border-sc-fg-subtle/30'
          }`}
        >
          <div className="flex flex-col gap-1">
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium text-sc-fg-primary">
                {((sandbox as Record<string, unknown>).pod_name as string) ??
                  sandbox.id.slice(0, 12)}
              </span>
              {statusBadge(sandbox.status)}
            </div>
            <span className="text-xs text-sc-fg-subtle">{formatTime(sandbox.created_at)}</span>
          </div>
          <span className="font-mono text-xs text-sc-fg-subtle">{sandbox.id.slice(0, 8)}</span>
        </button>
      ))}
    </div>
  );
}
