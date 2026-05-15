'use client';

import Link from 'next/link';
import { ExternalLink, FileText, Hash, WarningCircle } from '@/components/ui/icons';
import { LoadingState } from '@/components/ui/spinner';
import { ErrorState } from '@/components/ui/tooltip';
import type { MemorySourceInspectResponse } from '@/lib/api';
import { formatDateTime } from '@/lib/constants';
import { useMemorySourceInspect } from '@/lib/hooks';
import { MemoryActivityFeed } from './memory-activity-feed';
import { SourceCorrectionDialog } from './source-correction-dialog';
import { SourceVisibilitySummary } from './source-visibility-summary';

interface SourceInspectPanelProps {
  sourceId: string;
  initialData?: MemorySourceInspectResponse;
}

function valueLabel(value: unknown): string {
  if (value === null || value === undefined) return 'None';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return JSON.stringify(value);
}

function DetailGrid({ source }: { source: MemorySourceInspectResponse }) {
  const details = [
    ['Raw ID', source.id],
    ['Source ID', source.source_id],
    ['Scope key', source.scope_key],
    ['Project', source.project_id],
    ['Agent', source.agent_id],
    ['Captured', source.captured_at ? formatDateTime(source.captured_at) : null],
    ['Created', source.created_at ? formatDateTime(source.created_at) : null],
    ['Audit events', source.audit_event_count],
  ] as const;

  return (
    <section className="rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-base shadow-card">
      <div className="border-b border-sc-fg-subtle/10 px-4 py-3">
        <h2 className="text-sm font-semibold text-sc-fg-primary">Source Metadata</h2>
      </div>
      <dl className="grid gap-px bg-sc-fg-subtle/10 sm:grid-cols-2">
        {details.map(([label, value]) => (
          <div key={label} className="bg-sc-bg-base px-4 py-3">
            <dt className="text-xs uppercase tracking-[0.1em] text-sc-fg-subtle">{label}</dt>
            <dd className="mt-1 truncate text-sm text-sc-fg-primary">{valueLabel(value)}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function RawContentPanel({ source }: { source: MemorySourceInspectResponse }) {
  return (
    <section className="rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-base shadow-card">
      <div className="flex items-center justify-between border-b border-sc-fg-subtle/10 px-4 py-3">
        <h2 className="text-sm font-semibold text-sc-fg-primary">Raw Source</h2>
        <span className="text-xs text-sc-fg-subtle">{source.raw_content_length} bytes</span>
      </div>
      {source.content_redacted || !source.raw_content ? (
        <div className="flex items-start gap-3 px-4 py-5 text-sm text-sc-fg-muted">
          <WarningCircle width={18} height={18} className="mt-0.5 shrink-0 text-sc-yellow" />
          <p>Content hidden by policy or lifecycle state.</p>
        </div>
      ) : (
        <pre className="max-h-[420px] overflow-auto whitespace-pre-wrap p-4 font-mono text-sm leading-6 text-sc-fg-primary">
          {source.raw_content}
        </pre>
      )}
    </section>
  );
}

function DerivedRecords({ source }: { source: MemorySourceInspectResponse }) {
  return (
    <section className="rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-base shadow-card">
      <div className="flex items-center justify-between border-b border-sc-fg-subtle/10 px-4 py-3">
        <h2 className="text-sm font-semibold text-sc-fg-primary">Derived Records</h2>
        <span className="text-xs text-sc-fg-subtle">{source.derived_records.length}</span>
      </div>
      {source.derived_records.length === 0 ? (
        <p className="px-4 py-6 text-sm text-sc-fg-muted">No derived records yet</p>
      ) : (
        <div className="divide-y divide-sc-fg-subtle/10">
          {source.derived_records.map(record => (
            <article
              key={record.id}
              className="grid grid-cols-[auto_minmax(0,1fr)] gap-3 px-4 py-3"
            >
              <FileText width={17} height={17} className="mt-0.5 text-sc-cyan" />
              <div className="min-w-0">
                <p className="truncate text-sm font-medium text-sc-fg-primary">{record.id}</p>
                <p className="mt-1 text-xs text-sc-fg-subtle">
                  {record.record_type} · {record.source_action}
                </p>
              </div>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

function CorrectionHistory({ source }: { source: MemorySourceInspectResponse }) {
  return (
    <section className="rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-base shadow-card">
      <div className="flex items-center justify-between border-b border-sc-fg-subtle/10 px-4 py-3">
        <h2 className="text-sm font-semibold text-sc-fg-primary">Correction History</h2>
        <span className="text-xs text-sc-fg-subtle">{source.correction_history.length}</span>
      </div>
      {source.correction_history.length === 0 ? (
        <p className="px-4 py-6 text-sm text-sc-fg-muted">No lifecycle corrections yet</p>
      ) : (
        <div className="divide-y divide-sc-fg-subtle/10">
          {source.correction_history.map((entry, index) => (
            <article key={`${valueLabel(entry.audit_event_id)}-${index}`} className="px-4 py-3">
              <p className="text-sm font-medium text-sc-fg-primary">
                {valueLabel(entry.action || entry.audit_event_id)}
              </p>
              <p className="mt-1 truncate text-xs text-sc-fg-subtle">
                {valueLabel(entry.policy_reason || entry.created_at)}
              </p>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

function AvailableActions({ source }: { source: MemorySourceInspectResponse }) {
  return (
    <section className="rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-base shadow-card">
      <div className="border-b border-sc-fg-subtle/10 px-4 py-3">
        <h2 className="text-sm font-semibold text-sc-fg-primary">Lifecycle Actions</h2>
      </div>
      <div className="divide-y divide-sc-fg-subtle/10">
        {source.available_actions.map(action => (
          <article
            key={valueLabel(action.action)}
            className="grid grid-cols-[minmax(0,1fr)_auto] gap-3 px-4 py-3"
          >
            <div className="min-w-0">
              <p className="truncate text-sm font-medium text-sc-fg-primary">
                {valueLabel(action.action)}
              </p>
              <p className="mt-1 text-xs text-sc-fg-subtle">
                {action.preview_required ? 'preview required' : 'direct action'}
              </p>
            </div>
            <span
              className={`rounded border px-2 py-0.5 text-xs ${
                action.available
                  ? 'border-sc-green/25 bg-sc-green/10 text-sc-green'
                  : 'border-sc-red/25 bg-sc-red/10 text-sc-red'
              }`}
            >
              {action.available ? 'available' : 'blocked'}
            </span>
          </article>
        ))}
      </div>
    </section>
  );
}

function MetadataPanel({ source }: { source: MemorySourceInspectResponse }) {
  const metadata = Object.entries(source.metadata).slice(0, 10);
  const versions = Object.entries(source.transform_versions);
  return (
    <section className="rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-base shadow-card">
      <div className="border-b border-sc-fg-subtle/10 px-4 py-3">
        <h2 className="text-sm font-semibold text-sc-fg-primary">Metadata</h2>
      </div>
      <div className="grid gap-4 p-4">
        <div className="flex flex-wrap gap-2">
          {source.tags.map(tag => (
            <span
              key={tag}
              className="inline-flex items-center gap-1 rounded border border-sc-cyan/20 bg-sc-cyan/10 px-2 py-0.5 text-xs text-sc-cyan"
            >
              <Hash width={12} height={12} />
              {tag}
            </span>
          ))}
          {source.tags.length === 0 && <span className="text-sm text-sc-fg-muted">No tags</span>}
        </div>
        {metadata.length > 0 && (
          <dl className="grid gap-2 text-sm">
            {metadata.map(([key, value]) => (
              <div key={key} className="grid grid-cols-[140px_minmax(0,1fr)] gap-3">
                <dt className="truncate text-sc-fg-muted">{key}</dt>
                <dd className="truncate text-sc-fg-primary">{valueLabel(value)}</dd>
              </div>
            ))}
          </dl>
        )}
        {versions.length > 0 && (
          <div className="rounded-lg border border-sc-fg-subtle/15 bg-sc-bg-highlight/50 p-3">
            <p className="text-xs font-medium uppercase tracking-[0.1em] text-sc-fg-subtle">
              Transform Versions
            </p>
            <dl className="mt-2 grid gap-1 text-sm">
              {versions.map(([key, value]) => (
                <div key={key} className="flex items-center justify-between gap-3">
                  <dt className="text-sc-fg-muted">{key}</dt>
                  <dd className="truncate text-sc-fg-primary">{valueLabel(value)}</dd>
                </div>
              ))}
            </dl>
          </div>
        )}
      </div>
    </section>
  );
}

export function SourceInspectPanel({ sourceId, initialData }: SourceInspectPanelProps) {
  const {
    data: source,
    isLoading,
    error,
    refetch,
  } = useMemorySourceInspect(sourceId, {
    initialData,
  });

  if (isLoading) {
    return <LoadingState message="Loading source inspection..." />;
  }

  if (error || !source) {
    return (
      <ErrorState
        title="Failed to load source"
        message={error instanceof Error ? error.message : 'Unknown source inspect error'}
      />
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <Link
            href="/memory"
            className="inline-flex items-center gap-1.5 text-sm text-sc-fg-muted transition-colors hover:text-sc-purple"
          >
            <ExternalLink width={14} height={14} />
            Memory Workspace
          </Link>
        </div>
        <SourceCorrectionDialog source={source} onApplied={() => void refetch()} />
      </div>

      <SourceVisibilitySummary source={source} />

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_380px]">
        <div className="space-y-4">
          <RawContentPanel source={source} />
          <DerivedRecords source={source} />
          <MemoryActivityFeed events={source.recent_audit_events} title="Audit Summary" />
        </div>
        <div className="space-y-4">
          <DetailGrid source={source} />
          <MetadataPanel source={source} />
          <CorrectionHistory source={source} />
          <AvailableActions source={source} />
        </div>
      </div>
    </div>
  );
}
