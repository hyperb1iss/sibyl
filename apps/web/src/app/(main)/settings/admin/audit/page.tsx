'use client';

import { useMemo, useState } from 'react';
import { toast } from 'sonner';
import {
  SettingsPageHeader,
  SettingsSection,
  SettingsSectionSkeleton,
} from '@/components/settings/primitives';
import { StatusBadge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  ArrowLeft,
  ArrowRight,
  ClipboardCheck,
  Download,
  Filter,
  RefreshDouble,
  Xmark,
} from '@/components/ui/icons';
import { Input } from '@/components/ui/input';
import { Spinner } from '@/components/ui/spinner';
import type { AdminAuditEvent, AdminAuditExportFormat, AdminAuditParams } from '@/lib/api';
import { api } from '@/lib/api';
import { formatDateTime } from '@/lib/constants/formatting';
import { useAdminAudit } from '@/lib/hooks';

const DEFAULT_LIMIT = 50;

function isoFromLocal(value: string): string | undefined {
  if (!value) return undefined;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return undefined;
  return date.toISOString();
}

function compactDetails(details: Record<string, unknown>): string {
  const text = JSON.stringify(details);
  if (text.length <= 180) return text;
  return `${text.slice(0, 177)}...`;
}

function eventTone(action: string): 'healthy' | 'warning' | 'running' | 'unknown' {
  if (action.includes('denied') || action.includes('revoke') || action.includes('failed')) {
    return 'warning';
  }
  if (action.includes('login') || action.includes('create')) return 'healthy';
  if (action.startsWith('memory.')) return 'running';
  return 'unknown';
}

function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function AuditRow({ event }: { event: AdminAuditEvent }) {
  return (
    <tr className="border-b border-sc-fg-subtle/10 last:border-0">
      <td className="min-w-48 px-4 py-3 align-top text-sm text-sc-fg-secondary">
        {event.created_at ? formatDateTime(event.created_at) : 'Unknown'}
      </td>
      <td className="min-w-44 px-4 py-3 align-top">
        <StatusBadge status={eventTone(event.action)} label={event.action} variant="chip" />
      </td>
      <td className="min-w-52 px-4 py-3 align-top text-sm text-sc-fg-muted">
        <div className="max-w-56 truncate" title={event.user_id ?? ''}>
          {event.user_id ?? 'System'}
        </div>
      </td>
      <td className="min-w-44 px-4 py-3 align-top text-sm text-sc-fg-secondary">
        <div className="max-w-48 truncate" title={event.resource ?? ''}>
          {event.resource ?? 'Unscoped'}
        </div>
      </td>
      <td className="min-w-56 px-4 py-3 align-top text-sm text-sc-fg-muted">
        <div className="max-w-64 truncate" title={event.ip_address ?? event.user_agent ?? ''}>
          {event.ip_address ?? event.user_agent ?? 'No client metadata'}
        </div>
      </td>
      <td className="min-w-72 px-4 py-3 align-top">
        <code className="block max-w-96 overflow-hidden text-ellipsis whitespace-nowrap rounded bg-sc-bg-highlight px-2 py-1 text-xs text-sc-fg-muted">
          {compactDetails(event.details)}
        </code>
      </td>
    </tr>
  );
}

export default function AdminAuditPage() {
  const [action, setAction] = useState('');
  const [userId, setUserId] = useState('');
  const [resource, setResource] = useState('');
  const [startTime, setStartTime] = useState('');
  const [endTime, setEndTime] = useState('');
  const [offset, setOffset] = useState(0);
  const [exporting, setExporting] = useState<AdminAuditExportFormat | null>(null);

  const params = useMemo<AdminAuditParams>(
    () => ({
      action: action.trim() || undefined,
      user_id: userId.trim() || undefined,
      resource: resource.trim() || undefined,
      start_time: isoFromLocal(startTime),
      end_time: isoFromLocal(endTime),
      limit: DEFAULT_LIMIT,
      offset,
    }),
    [action, endTime, offset, resource, startTime, userId]
  );

  const { data, isLoading, isFetching, error, refetch } = useAdminAudit(params);
  const events = data?.events ?? [];
  const total = data?.total ?? 0;
  const hasPrevious = offset > 0;
  const hasNext = Boolean(data?.has_more);
  const rangeStart = total === 0 ? 0 : offset + 1;
  const rangeEnd = Math.min(offset + DEFAULT_LIMIT, Math.max(total, offset + events.length));

  function resetFilters() {
    setAction('');
    setUserId('');
    setResource('');
    setStartTime('');
    setEndTime('');
    setOffset(0);
  }

  async function handleExport(format: AdminAuditExportFormat) {
    try {
      setExporting(format);
      const blob = await api.admin.audit.export({ ...params, offset: undefined, format });
      downloadBlob(blob, `sibyl-audit.${format}`);
    } catch {
      toast.error(`Failed to export ${format.toUpperCase()} audit log`);
    } finally {
      setExporting(null);
    }
  }

  if (isLoading) {
    return (
      <div className="space-y-6">
        <SettingsPageHeader
          icon={ClipboardCheck}
          iconColor="text-sc-cyan"
          title="Audit Log"
          description="Security and access events for this organization."
        />
        <SettingsSectionSkeleton rows={3} rowHeight={56} />
        <SettingsSectionSkeleton rows={6} rowHeight={48} />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <SettingsPageHeader
        icon={ClipboardCheck}
        iconColor="text-sc-cyan"
        title="Audit Log"
        description="Security and access events for this organization."
        actions={
          <div className="flex flex-wrap gap-2">
            <Button
              variant="secondary"
              size="sm"
              onClick={() => refetch()}
              icon={<RefreshDouble width={14} height={14} />}
            >
              Refresh
            </Button>
            <Button
              variant="secondary"
              size="sm"
              loading={exporting === 'csv'}
              onClick={() => handleExport('csv')}
              icon={<Download width={14} height={14} />}
            >
              CSV
            </Button>
            <Button
              variant="secondary"
              size="sm"
              loading={exporting === 'json'}
              onClick={() => handleExport('json')}
              icon={<Download width={14} height={14} />}
            >
              JSON
            </Button>
          </div>
        }
      />

      <SettingsSection title="Filters" icon={Filter} iconColor="text-sc-purple">
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-5">
          <label className="block" htmlFor="audit-filter-action">
            <span className="mb-1.5 block text-[11px] font-medium uppercase tracking-[0.06em] text-sc-fg-subtle">
              Action
            </span>
            <Input
              id="audit-filter-action"
              value={action}
              onChange={event => {
                setAction(event.target.value);
                setOffset(0);
              }}
              className="text-sm"
              placeholder="memory.recall"
            />
          </label>
          <label className="block" htmlFor="audit-filter-user">
            <span className="mb-1.5 block text-[11px] font-medium uppercase tracking-[0.06em] text-sc-fg-subtle">
              User
            </span>
            <Input
              id="audit-filter-user"
              value={userId}
              onChange={event => {
                setUserId(event.target.value);
                setOffset(0);
              }}
              className="text-sm"
              placeholder="user UUID"
            />
          </label>
          <label className="block" htmlFor="audit-filter-resource">
            <span className="mb-1.5 block text-[11px] font-medium uppercase tracking-[0.06em] text-sc-fg-subtle">
              Resource
            </span>
            <Input
              id="audit-filter-resource"
              value={resource}
              onChange={event => {
                setResource(event.target.value);
                setOffset(0);
              }}
              className="text-sm"
              placeholder="project or source"
            />
          </label>
          <label className="block" htmlFor="audit-filter-start">
            <span className="mb-1.5 block text-[11px] font-medium uppercase tracking-[0.06em] text-sc-fg-subtle">
              Start
            </span>
            <Input
              id="audit-filter-start"
              type="datetime-local"
              value={startTime}
              onChange={event => {
                setStartTime(event.target.value);
                setOffset(0);
              }}
              className="text-sm"
            />
          </label>
          <label className="block" htmlFor="audit-filter-end">
            <span className="mb-1.5 block text-[11px] font-medium uppercase tracking-[0.06em] text-sc-fg-subtle">
              End
            </span>
            <Input
              id="audit-filter-end"
              type="datetime-local"
              value={endTime}
              onChange={event => {
                setEndTime(event.target.value);
                setOffset(0);
              }}
              className="text-sm"
            />
          </label>
        </div>
        <div className="mt-4 flex items-center justify-between gap-3">
          <p className="text-sm text-sc-fg-muted">
            Showing {events.length.toLocaleString()} of {total.toLocaleString()} events
          </p>
          <Button
            variant="ghost"
            size="sm"
            onClick={resetFilters}
            icon={<Xmark width={14} height={14} />}
          >
            Clear
          </Button>
        </div>
      </SettingsSection>

      <SettingsSection flush>
        {error ? (
          <div className="m-6 rounded-lg border border-sc-red/20 bg-sc-red/5 p-4 text-sm text-sc-red">
            Failed to load audit events.
          </div>
        ) : events.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse">
              <thead>
                <tr className="border-b border-sc-fg-subtle/10 bg-sc-bg-highlight/50 text-left text-[11px] font-semibold uppercase tracking-[0.08em] text-sc-fg-subtle">
                  <th className="px-4 py-3">Time</th>
                  <th className="px-4 py-3">Action</th>
                  <th className="px-4 py-3">User</th>
                  <th className="px-4 py-3">Resource</th>
                  <th className="px-4 py-3">Client</th>
                  <th className="px-4 py-3">Details</th>
                </tr>
              </thead>
              <tbody>
                {events.map(event => (
                  <AuditRow key={event.id} event={event} />
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="flex min-h-56 items-center justify-center p-6 text-sm text-sc-fg-muted">
            {isFetching ? <Spinner size="sm" color="purple" /> : 'No audit events match.'}
          </div>
        )}
      </SettingsSection>

      <div className="flex items-center justify-between">
        <Button
          variant="secondary"
          size="sm"
          disabled={!hasPrevious}
          onClick={() => setOffset(Math.max(0, offset - DEFAULT_LIMIT))}
          icon={<ArrowLeft width={14} height={14} />}
        >
          Previous
        </Button>
        <span className="text-sm text-sc-fg-muted">
          {rangeStart}-{rangeEnd}
        </span>
        <Button
          variant="secondary"
          size="sm"
          disabled={!hasNext}
          onClick={() => setOffset(offset + DEFAULT_LIMIT)}
          icon={<ArrowRight width={14} height={14} />}
        >
          Next
        </Button>
      </div>
    </div>
  );
}
