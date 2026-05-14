'use client';

import Link from 'next/link';
import type { ReactNode } from 'react';
import { useMemo, useState } from 'react';
import { EntityBadge } from '@/components/ui/badge';
import {
  Database,
  type IconComponent,
  Key,
  Search,
  Upload,
  WarningCircle,
} from '@/components/ui/icons';
import { LoadingState } from '@/components/ui/spinner';
import type { MemoryAuditEvent, MemoryScope, MemorySpace, RawCaptureSummary } from '@/lib/api';
import { formatDistanceToNow } from '@/lib/constants';
import { useMemoryAudit, useMemorySpaces, useRawCaptures } from '@/lib/hooks';
import { MemoryActivityFeed } from './memory-activity-feed';
import { type MemoryScopeFilter, MemoryScopeSwitcher } from './memory-scope-switcher';

const MEMORY_SCOPE_VALUES = new Set<MemoryScope>([
  'private',
  'delegated',
  'project',
  'team',
  'organization',
  'shared',
  'public',
]);

function stringFromMetadata(metadata: Record<string, unknown>, keys: string[]): string | null {
  for (const key of keys) {
    const value = metadata[key];
    if (typeof value === 'string' && value.trim()) {
      return value;
    }
  }
  return null;
}

function normalizeMemoryScope(value: string | null | undefined): MemoryScope | null {
  return value && MEMORY_SCOPE_VALUES.has(value as MemoryScope) ? (value as MemoryScope) : null;
}

function captureScope(capture: RawCaptureSummary): MemoryScope | null {
  return normalizeMemoryScope(
    stringFromMetadata(capture.metadata, ['memory_scope', 'target_memory_scope', 'scope'])
  );
}

function matchesScope(scope: MemoryScopeFilter, value: string | null | undefined): boolean {
  return scope === 'all' || normalizeMemoryScope(value) === scope;
}

function matchesCaptureScope(scope: MemoryScopeFilter, capture: RawCaptureSummary): boolean {
  return scope === 'all' || captureScope(capture) === scope;
}

function eventIsRecall(event: MemoryAuditEvent): boolean {
  return event.action.includes('recall') || event.action.includes('context_pack');
}

function eventIsAgentAccess(event: MemoryAuditEvent): boolean {
  return event.action.includes('access') || event.source_surface === 'mcp_context';
}

function surfaceLabel(surface: string | null): string {
  if (!surface) return 'Unknown';
  if (surface === 'cli') return 'CLI';
  if (surface === 'mcp') return 'MCP';
  return surface
    .split(/[_\-\s]+/)
    .filter(Boolean)
    .map(part => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}

function captureMeta(capture: RawCaptureSummary): string {
  const scope = captureScope(capture);
  const pieces = [
    scope,
    surfaceLabel(capture.capture_surface),
    capture.created_at ? formatDistanceToNow(capture.created_at) : null,
  ].filter(Boolean);
  return pieces.join(' · ');
}

function scopeCounts(
  captures: RawCaptureSummary[],
  events: MemoryAuditEvent[],
  spaces: MemorySpace[]
): Partial<Record<MemoryScopeFilter, number>> {
  const counts: Partial<Record<MemoryScopeFilter, number>> = {
    all: captures.length + events.length + spaces.length,
  };

  for (const scope of MEMORY_SCOPE_VALUES) {
    counts[scope] =
      captures.filter(capture => captureScope(capture) === scope).length +
      events.filter(event => normalizeMemoryScope(event.memory_scope) === scope).length +
      spaces.filter(space => space.memory_scope === scope).length;
  }

  return counts;
}

function MetricCard({
  icon: Icon,
  label,
  value,
  hint,
}: {
  icon: IconComponent;
  label: string;
  value: number | string;
  hint: string;
}) {
  return (
    <div className="rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-base p-4 shadow-card">
      <div className="flex items-center justify-between gap-3">
        <p className="text-xs font-medium uppercase tracking-[0.12em] text-sc-fg-subtle">{label}</p>
        <Icon width={18} height={18} className="text-sc-cyan" />
      </div>
      <p className="mt-2 text-2xl font-semibold text-sc-fg-primary">{value}</p>
      <p className="mt-1 text-sm text-sc-fg-muted">{hint}</p>
    </div>
  );
}

function Panel({ title, count, children }: { title: string; count?: number; children: ReactNode }) {
  return (
    <section className="rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-base shadow-card">
      <div className="flex items-center justify-between border-b border-sc-fg-subtle/10 px-4 py-3">
        <h2 className="text-sm font-semibold text-sc-fg-primary">{title}</h2>
        {typeof count === 'number' && <span className="text-xs text-sc-fg-subtle">{count}</span>}
      </div>
      {children}
    </section>
  );
}

function CaptureRows({
  captures,
  emptyLabel,
  linkToArchive = false,
}: {
  captures: RawCaptureSummary[];
  emptyLabel: string;
  linkToArchive?: boolean;
}) {
  if (captures.length === 0) {
    return <p className="px-4 py-6 text-sm text-sc-fg-muted">{emptyLabel}</p>;
  }

  return (
    <div className="divide-y divide-sc-fg-subtle/10">
      {captures.map(capture => {
        const body = (
          <>
            <div className="min-w-0">
              <p className="truncate text-sm font-medium text-sc-fg-primary">{capture.title}</p>
              <p className="mt-1 truncate text-xs text-sc-fg-subtle">{captureMeta(capture)}</p>
            </div>
            <EntityBadge type={capture.entity_type} />
          </>
        );

        if (linkToArchive) {
          return (
            <Link
              key={capture.id}
              href={`/archive?id=${encodeURIComponent(capture.id)}`}
              className="grid grid-cols-[minmax(0,1fr)_auto] gap-3 px-4 py-3 transition-colors hover:bg-sc-bg-highlight/60"
            >
              {body}
            </Link>
          );
        }

        return (
          <article key={capture.id} className="grid grid-cols-[minmax(0,1fr)_auto] gap-3 px-4 py-3">
            {body}
          </article>
        );
      })}
    </div>
  );
}

function AgentAccessPanel({
  events,
  spaces,
}: {
  events: MemoryAuditEvent[];
  spaces: MemorySpace[];
}) {
  const members = spaces.flatMap(space =>
    space.members.map(member => ({
      ...member,
      spaceName: space.name,
      scope: space.memory_scope,
    }))
  );

  return (
    <Panel title="Agent Access" count={events.length + members.length}>
      <div className="divide-y divide-sc-fg-subtle/10">
        {events.slice(0, 4).map(event => (
          <article key={event.id} className="px-4 py-3">
            <div className="flex items-center justify-between gap-3">
              <p className="truncate text-sm font-medium text-sc-fg-primary">
                {event.policy_reason || 'access preview'}
              </p>
              <Key width={15} height={15} className="shrink-0 text-sc-purple" />
            </div>
            <p className="mt-1 truncate text-xs text-sc-fg-subtle">
              {event.source_surface || 'memory access'} · {event.memory_scope || 'scope'}
            </p>
          </article>
        ))}
        {members.slice(0, 4).map(member => (
          <article key={member.id} className="px-4 py-3">
            <div className="flex items-center justify-between gap-3">
              <p className="truncate text-sm font-medium text-sc-fg-primary">
                {member.principal_type}:{member.principal_id}
              </p>
              <span className="rounded border border-sc-purple/20 bg-sc-purple/10 px-2 py-0.5 text-xs text-sc-purple">
                {member.role}
              </span>
            </div>
            <p className="mt-1 truncate text-xs text-sc-fg-subtle">
              {member.spaceName} · {member.scope}
            </p>
          </article>
        ))}
        {events.length === 0 && members.length === 0 && (
          <p className="px-4 py-6 text-sm text-sc-fg-muted">No agent access previews yet</p>
        )}
      </div>
    </Panel>
  );
}

export function MemoryHome() {
  const [scope, setScope] = useState<MemoryScopeFilter>('all');

  const capturesQuery = useRawCaptures({ limit: 24 });
  const pendingQuery = useRawCaptures({ review_state: 'pending', limit: 24 });
  const importsQuery = useRawCaptures({ capture_surface: 'source_import', limit: 8 });
  const reflectionsQuery = useRawCaptures({
    capture_surface: 'reflection_candidate',
    review_state: 'pending',
    limit: 8,
  });
  const auditQuery = useMemoryAudit({ limit: 50 });
  const spacesQuery = useMemorySpaces();

  const captures = useMemo(
    () =>
      (capturesQuery.data?.captures ?? []).filter(capture => matchesCaptureScope(scope, capture)),
    [capturesQuery.data?.captures, scope]
  );
  const pending = useMemo(
    () =>
      (pendingQuery.data?.captures ?? []).filter(capture => matchesCaptureScope(scope, capture)),
    [pendingQuery.data?.captures, scope]
  );
  const imports = useMemo(
    () =>
      (importsQuery.data?.captures ?? []).filter(capture => matchesCaptureScope(scope, capture)),
    [importsQuery.data?.captures, scope]
  );
  const reflections = useMemo(
    () =>
      (reflectionsQuery.data?.captures ?? []).filter(capture =>
        matchesCaptureScope(scope, capture)
      ),
    [reflectionsQuery.data?.captures, scope]
  );
  const events = useMemo(
    () => (auditQuery.data?.events ?? []).filter(event => matchesScope(scope, event.memory_scope)),
    [auditQuery.data?.events, scope]
  );
  const spaces = useMemo(
    () => (spacesQuery.data?.spaces ?? []).filter(space => matchesScope(scope, space.memory_scope)),
    [scope, spacesQuery.data?.spaces]
  );

  const recalls = events.filter(eventIsRecall);
  const agentAccess = events.filter(eventIsAgentAccess);
  const isLoading =
    capturesQuery.isLoading &&
    pendingQuery.isLoading &&
    auditQuery.isLoading &&
    spacesQuery.isLoading;
  const panelErrors = [
    capturesQuery.error,
    pendingQuery.error,
    importsQuery.error,
    reflectionsQuery.error,
    auditQuery.error,
    spacesQuery.error,
  ].filter(Boolean);

  if (isLoading) {
    return <LoadingState message="Loading memory cockpit..." />;
  }

  return (
    <div className="space-y-4">
      {panelErrors.length > 0 && (
        <div className="rounded-lg border border-sc-yellow/30 bg-sc-yellow/10 px-4 py-3 text-sm text-sc-yellow">
          Some memory panels are unavailable from the current role or backend state.
        </div>
      )}

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-5">
        <MetricCard
          icon={Database}
          label="Captures"
          value={captures.length}
          hint="recent raw memory"
        />
        <MetricCard
          icon={WarningCircle}
          label="Review"
          value={pending.length}
          hint="pending actions"
        />
        <MetricCard
          icon={Upload}
          label="Imports"
          value={imports.length}
          hint="source-import records"
        />
        <MetricCard icon={Search} label="Recalls" value={recalls.length} hint="recent retrievals" />
        <MetricCard
          icon={Key}
          label="Access"
          value={agentAccess.length + spaces.length}
          hint="agent scope signals"
        />
      </div>

      <MemoryScopeSwitcher
        value={scope}
        onChange={setScope}
        counts={scopeCounts(
          capturesQuery.data?.captures ?? [],
          auditQuery.data?.events ?? [],
          spacesQuery.data?.spaces ?? []
        )}
      />

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(360px,0.8fr)]">
        <div className="space-y-4">
          <div className="grid gap-4 lg:grid-cols-2">
            <Panel title="Recent Captures" count={captures.length}>
              <CaptureRows captures={captures.slice(0, 6)} emptyLabel="No captures in this scope" />
            </Panel>
            <Panel title="Review Actions" count={pending.length}>
              <CaptureRows
                captures={pending.slice(0, 6)}
                emptyLabel="No pending review actions"
                linkToArchive
              />
              {pending.length > 0 && (
                <div className="border-t border-sc-fg-subtle/10 px-4 py-3">
                  <Link
                    href="/archive?link=unlinked"
                    className="inline-flex items-center rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-highlight px-3 py-1.5 text-sm font-medium text-sc-fg-primary transition-colors hover:border-sc-purple/50 hover:text-sc-purple"
                  >
                    Open Review Queue
                  </Link>
                </div>
              )}
            </Panel>
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            <Panel title="Recent Imports" count={imports.length}>
              <CaptureRows captures={imports.slice(0, 5)} emptyLabel="No source imports yet" />
            </Panel>
            <Panel title="Reflection Queue" count={reflections.length}>
              <CaptureRows
                captures={reflections.slice(0, 5)}
                emptyLabel="No reflection candidates waiting"
                linkToArchive
              />
            </Panel>
          </div>
        </div>

        <div className="space-y-4">
          <MemoryActivityFeed events={events.slice(0, 8)} />
          <AgentAccessPanel events={agentAccess} spaces={spaces} />
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <MemoryActivityFeed
          events={recalls.slice(0, 5)}
          title="Recent Recalls"
          emptyLabel="No recall events in this scope"
        />
        <MemoryActivityFeed
          events={events.filter(event => event.action.includes('inspect')).slice(0, 5)}
          title="Source Inspect"
          emptyLabel="No source inspections yet"
        />
        <MemoryActivityFeed
          events={events.filter(event => event.action.includes('synthesis')).slice(0, 5)}
          title="Synthesis Activity"
          emptyLabel="No synthesis events yet"
        />
      </div>
    </div>
  );
}
