'use client';

import Link from 'next/link';
import type { ReactNode } from 'react';
import type { SourceImportStatus, SourceImportStatusResponse } from '@/lib/api';
import { formatDateTime, formatDistanceToNow } from '@/lib/constants';

const STATUS_STYLES: Record<SourceImportStatus, string> = {
  pending: 'border-sc-fg-subtle/20 bg-sc-fg-subtle/10 text-sc-fg-muted',
  running: 'border-sc-yellow/30 bg-sc-yellow/10 text-sc-yellow',
  paused: 'border-sc-purple/30 bg-sc-purple/10 text-sc-purple',
  completed: 'border-sc-green/30 bg-sc-green/10 text-sc-green',
  failed: 'border-sc-red/30 bg-sc-red/10 text-sc-red',
  canceled: 'border-sc-fg-subtle/20 bg-sc-bg-highlight text-sc-fg-muted',
};

const SAFE_RECORD_KEYS = new Set([
  'adapter_record_id',
  'source_uri',
  'reason',
  'error',
  'message',
  'code',
  'type',
]);

function titleCase(value: string): string {
  return value
    .split(/[_\-\s]+/)
    .filter(Boolean)
    .map(part => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}

function numberLabel(value: number): string {
  return Intl.NumberFormat().format(value);
}

function compactValue(value: unknown): string {
  if (value === null || value === undefined) return 'null';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return JSON.stringify(value);
}

function safeRecordEntries(item: Record<string, unknown>): [string, string][] {
  return Object.entries(item)
    .filter(([key, value]) => SAFE_RECORD_KEYS.has(key) && typeof value !== 'object')
    .map(([key, value]) => [key, compactValue(value)]);
}

function StatusBadge({ status }: { status: SourceImportStatus }) {
  return (
    <span
      className={`inline-flex items-center rounded border px-2 py-0.5 text-xs font-medium ${STATUS_STYLES[status]}`}
    >
      {titleCase(status)}
    </span>
  );
}

function LabelChip({ children }: { children: ReactNode }) {
  return (
    <span className="inline-flex items-center rounded-full bg-sc-bg-highlight px-2 py-0.5 text-xs font-medium text-sc-fg-muted">
      {children}
    </span>
  );
}

function CounterCard({ label, value, hint }: { label: string; value: number; hint: string }) {
  return (
    <div className="rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-elevated p-3">
      <p className="text-xs uppercase tracking-[0.12em] text-sc-fg-subtle">{label}</p>
      <p className="mt-2 text-xl font-semibold text-sc-fg-primary">{numberLabel(value)}</p>
      <p className="mt-1 text-xs text-sc-fg-muted">{hint}</p>
    </div>
  );
}

function DetailRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid gap-1 rounded-lg border border-sc-fg-subtle/10 bg-sc-bg-highlight/40 px-3 py-2 sm:grid-cols-[150px_minmax(0,1fr)]">
      <p className="text-xs uppercase tracking-[0.12em] text-sc-fg-subtle">{label}</p>
      <div className="min-w-0 text-sm text-sc-fg-primary">{children}</div>
    </div>
  );
}

function ObjectRows({
  items,
  emptyLabel,
}: {
  items: Record<string, unknown>[];
  emptyLabel: string;
}) {
  if (items.length === 0) {
    return <p className="text-sm text-sc-fg-muted">{emptyLabel}</p>;
  }

  return (
    <div className="space-y-2">
      {items.slice(0, 6).map((item, index) => {
        const entries = safeRecordEntries(item);

        return (
          <div
            key={`${item.adapter_record_id ?? index}`}
            className="rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-highlight/40 p-3"
          >
            {entries.length > 0 ? (
              <dl className="grid gap-2 text-xs sm:grid-cols-2">
                {entries.map(([key, value]) => (
                  <div key={key} className="min-w-0">
                    <dt className="font-medium text-sc-fg-subtle">{titleCase(key)}</dt>
                    <dd className="mt-1 truncate text-sc-fg-primary">{value}</dd>
                  </div>
                ))}
              </dl>
            ) : (
              <p className="text-xs text-sc-fg-muted">No source-safe fields available</p>
            )}
          </div>
        );
      })}
    </div>
  );
}

export function SourceImportProgress({ status }: { status: SourceImportStatusResponse }) {
  const progress = status.progress;

  return (
    <section className="space-y-4 rounded-xl border border-sc-fg-subtle/20 bg-sc-bg-elevated p-4 shadow-card">
      <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-lg font-semibold text-sc-fg-primary">Import Progress</h2>
            <StatusBadge status={status.status} />
          </div>
          <p className="mt-1 truncate text-sm text-sc-fg-muted">
            {status.source_identity ?? status.import_id}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <LabelChip>{status.adapter_name}</LabelChip>
          {status.target_memory_scope && <LabelChip>{status.target_memory_scope}</LabelChip>}
        </div>
      </div>

      <div className="grid gap-3 md:grid-cols-3 xl:grid-cols-6">
        <CounterCard label="Imported" value={progress.imported_count} hint="raw memory records" />
        <CounterCard label="Skipped" value={progress.skipped_count} hint="filtered records" />
        <CounterCard label="Dedupe" value={progress.dedupe_count} hint="deduped records" />
        <CounterCard label="Errors" value={progress.error_count} hint="failed records" />
        <CounterCard
          label="Attachments"
          value={progress.attachment_count}
          hint="preserved references"
        />
        <CounterCard
          label="Extract"
          value={progress.extraction_pending_count}
          hint="pending graph extraction"
        />
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        <div className="space-y-2">
          <DetailRow label="Import Id">
            <code className="break-all text-xs text-sc-cyan">{status.import_id}</code>
          </DetailRow>
          <DetailRow label="Adapter">
            {status.adapter_name}
            {status.adapter_version ? ` ${status.adapter_version}` : ''}
          </DetailRow>
          <DetailRow label="Target Scope">
            {status.target_memory_scope ?? 'private'}
            {status.target_scope_key ? ` · ${status.target_scope_key}` : ''}
          </DetailRow>
          <DetailRow label="Source Version">{status.source_version ?? 'unversioned'}</DetailRow>
        </div>
        <div className="space-y-2">
          <DetailRow label="Created">{formatDateTime(status.created_at)}</DetailRow>
          <DetailRow label="Updated">{formatDistanceToNow(status.updated_at)}</DetailRow>
          <DetailRow label="Completed">
            {status.completed_at ? formatDateTime(status.completed_at) : 'not completed'}
          </DetailRow>
          <DetailRow label="Privacy">{status.privacy_class ?? 'default'}</DetailRow>
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <section className="rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-highlight/30 p-4">
          <h3 className="text-sm font-semibold text-sc-fg-primary">Raw Memory Records</h3>
          {status.raw_memory_ids.length === 0 ? (
            <p className="mt-3 text-sm text-sc-fg-muted">No raw records have been published yet</p>
          ) : (
            <div className="mt-3 flex flex-wrap gap-2">
              {status.raw_memory_ids.slice(0, 18).map(id => (
                <Link
                  key={id}
                  href={`/memory/sources/${encodeURIComponent(id)}`}
                  title={id}
                  className="rounded border border-sc-cyan/20 bg-sc-cyan/10 px-2 py-1 font-mono text-xs text-sc-cyan transition-colors hover:border-sc-cyan/50 hover:bg-sc-cyan/20"
                >
                  {id.length > 16 ? `…${id.slice(-12)}` : id}
                </Link>
              ))}
            </div>
          )}
        </section>

        <section className="rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-highlight/30 p-4">
          <h3 className="text-sm font-semibold text-sc-fg-primary">Dedupe Keys</h3>
          {status.duplicate_dedupe_keys.length === 0 ? (
            <p className="mt-3 text-sm text-sc-fg-muted">No duplicate records reported</p>
          ) : (
            <div className="mt-3 flex flex-wrap gap-2">
              {status.duplicate_dedupe_keys.slice(0, 18).map(key => (
                <code
                  key={key}
                  className="rounded border border-sc-yellow/20 bg-sc-yellow/10 px-2 py-1 text-xs text-sc-yellow"
                >
                  {key}
                </code>
              ))}
            </div>
          )}
        </section>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <section className="rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-highlight/30 p-4">
          <h3 className="text-sm font-semibold text-sc-fg-primary">Skipped Records</h3>
          <div className="mt-3">
            <ObjectRows items={status.skipped_records} emptyLabel="No skipped records reported" />
          </div>
        </section>

        <section className="rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-highlight/30 p-4">
          <h3 className="text-sm font-semibold text-sc-fg-primary">Errors</h3>
          <div className="mt-3">
            <ObjectRows items={status.errors} emptyLabel="No import errors reported" />
          </div>
        </section>
      </div>
    </section>
  );
}
