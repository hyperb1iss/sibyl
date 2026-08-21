'use client';

import { EntityBadge } from '@/components/ui/badge';
import { AlertTriangle, Eye, Eye as EyeOpen, Key, WarningCircle } from '@/components/ui/icons';
import type { MemorySourceInspectResponse } from '@/lib/api/memory';

interface SourceVisibilitySummaryProps {
  source: MemorySourceInspectResponse;
}

function statusTone(source: MemorySourceInspectResponse): string {
  if (source.content_redacted || !source.policy_allowed) {
    return 'border-sc-red/30 bg-sc-red/10 text-sc-red';
  }
  if (source.review_state === 'pending') {
    return 'border-sc-yellow/30 bg-sc-yellow/10 text-sc-yellow';
  }
  return 'border-sc-green/30 bg-sc-green/10 text-sc-green';
}

function visibilityLabel(source: MemorySourceInspectResponse): string {
  if (source.content_redacted) return 'Content redacted';
  if (!source.policy_allowed) return 'Policy denied';
  return 'Content visible';
}

export function SourceVisibilitySummary({ source }: SourceVisibilitySummaryProps) {
  return (
    <section className="rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-base p-4 shadow-card">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <EntityBadge type={source.entity_type} />
            <span
              className={`inline-flex items-center gap-1.5 rounded border px-2 py-0.5 text-xs ${statusTone(source)}`}
            >
              {source.content_redacted ? (
                <AlertTriangle width={13} height={13} />
              ) : (
                <EyeOpen width={13} height={13} />
              )}
              {visibilityLabel(source)}
            </span>
          </div>
          <h2 className="mt-3 truncate text-lg font-semibold text-sc-fg-primary">
            {source.title || source.source_id}
          </h2>
          <p className="mt-1 truncate text-sm text-sc-fg-subtle">{source.source_id}</p>
        </div>

        <div className="grid min-w-[220px] gap-2 text-xs text-sc-fg-muted">
          <div className="flex items-center justify-between gap-3">
            <span className="inline-flex items-center gap-1.5">
              <Key width={13} height={13} />
              Scope
            </span>
            <span className="text-sc-fg-primary">{source.memory_scope}</span>
          </div>
          <div className="flex items-center justify-between gap-3">
            <span>Review</span>
            <span className="text-sc-fg-primary">{source.review_state}</span>
          </div>
          <div className="flex items-center justify-between gap-3">
            <span>Policy</span>
            <span className="max-w-[140px] truncate text-sc-fg-primary">
              {source.policy_reason}
            </span>
          </div>
          <div className="flex items-center justify-between gap-3">
            <span className="inline-flex items-center gap-1.5">
              <Eye width={13} height={13} />
              Raw bytes
            </span>
            <span className="text-sc-fg-primary">{source.raw_content_length}</span>
          </div>
          {source.content_redacted && (
            <div className="mt-1 flex items-center gap-2 rounded border border-sc-red/25 bg-sc-red/10 px-2 py-1 text-sc-red">
              <WarningCircle width={13} height={13} />
              Raw text hidden
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
