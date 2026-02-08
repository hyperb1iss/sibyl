'use client';

import { Suspense, useState } from 'react';
import { SandboxDetail } from '@/components/sandbox/sandbox-detail';
import { SandboxList } from '@/components/sandbox/sandbox-list';
import { LoadingState } from '@/components/ui/spinner';
import { useSandboxes } from '@/lib/hooks';

type StatusFilter = 'all' | 'running' | 'suspended' | 'failed';

function SandboxesPageContent() {
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const params = statusFilter !== 'all' ? { status: statusFilter } : undefined;
  const { data, isLoading, error } = useSandboxes(params);
  const sandboxes = data?.sandboxes ?? [];

  const filters: StatusFilter[] = ['all', 'running', 'suspended', 'failed'];

  return (
    <div className="flex h-[calc(100vh-8rem)] gap-4 p-4">
      {/* Main list panel */}
      <div className="flex flex-1 flex-col gap-4 overflow-hidden">
        {/* Header + filter tabs */}
        <div className="flex items-center justify-between">
          <h1 className="text-xl font-semibold text-sc-fg-primary">Sandboxes</h1>
          <div className="flex gap-1 rounded-lg bg-sc-bg-elevated p-1">
            {filters.map(f => (
              <button
                type="button"
                key={f}
                onClick={() => setStatusFilter(f)}
                className={`rounded-md px-3 py-1 text-xs font-medium transition-colors ${
                  statusFilter === f
                    ? 'bg-sc-purple/20 text-sc-purple'
                    : 'text-sc-fg-subtle hover:text-sc-fg-primary'
                }`}
              >
                {f.charAt(0).toUpperCase() + f.slice(1)}
              </button>
            ))}
          </div>
        </div>

        {/* Error state */}
        {error && (
          <div className="rounded-lg border border-sc-red/30 bg-sc-red/10 px-4 py-3 text-sm text-sc-red">
            {error instanceof Error ? error.message : 'Failed to load sandboxes'}
          </div>
        )}

        {/* List */}
        {isLoading ? (
          <LoadingState />
        ) : (
          <SandboxList sandboxes={sandboxes} selectedId={selectedId} onSelect={setSelectedId} />
        )}
      </div>

      {/* Detail panel */}
      {selectedId && (
        <div className="w-[480px] shrink-0 overflow-y-auto">
          <SandboxDetail sandboxId={selectedId} onClose={() => setSelectedId(null)} />
        </div>
      )}
    </div>
  );
}

export default function SandboxesPage() {
  return (
    <Suspense fallback={<LoadingState />}>
      <SandboxesPageContent />
    </Suspense>
  );
}
