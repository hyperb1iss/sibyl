'use client';

import Link from 'next/link';
import type { ReactNode } from 'react';
import { useMemo, useState } from 'react';
import { EntityBadge } from '@/components/ui/badge';
import {
  ArrowRight,
  Database,
  FileText,
  type IconComponent,
  Key,
  LightBulb,
  Search,
  Upload,
  Users,
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

function scopeDot(scope: MemoryScope | null): string {
  switch (scope) {
    case 'private':
      return 'bg-sc-purple shadow-[0_0_8px_color-mix(in_oklch,var(--sc-purple)_60%,transparent)]';
    case 'delegated':
      return 'bg-sc-magenta shadow-[0_0_8px_color-mix(in_oklch,var(--sc-magenta)_60%,transparent)]';
    case 'project':
      return 'bg-sc-cyan shadow-[0_0_8px_color-mix(in_oklch,var(--sc-cyan)_60%,transparent)]';
    case 'team':
      return 'bg-sc-coral shadow-[0_0_8px_color-mix(in_oklch,var(--sc-coral)_60%,transparent)]';
    case 'organization':
      return 'bg-sc-yellow shadow-[0_0_8px_color-mix(in_oklch,var(--sc-yellow)_60%,transparent)]';
    case 'shared':
      return 'bg-sc-green shadow-[0_0_8px_color-mix(in_oklch,var(--sc-green)_60%,transparent)]';
    case 'public':
      return 'bg-sc-red shadow-[0_0_8px_color-mix(in_oklch,var(--sc-red)_60%,transparent)]';
    default:
      return 'bg-sc-fg-subtle/50';
  }
}

interface HeroProps {
  captureCount: number;
  pendingCount: number;
  recallCount: number;
  agentReaders: number;
  scopeChip: string | null;
}

function MemoryHero({
  captureCount,
  pendingCount,
  recallCount,
  agentReaders,
  scopeChip,
}: HeroProps) {
  return (
    <div className="relative overflow-hidden rounded-xl border border-sc-purple/25 bg-gradient-to-br from-sc-bg-base via-sc-bg-elevated to-sc-purple/8 p-5 sm:p-6 shadow-xl shadow-black/10">
      <div className="pointer-events-none absolute -top-24 -right-24 h-72 w-72 rounded-full bg-sc-purple/20 blur-3xl" />
      <div className="pointer-events-none absolute -bottom-16 -left-16 h-56 w-56 rounded-full bg-sc-cyan/12 blur-3xl" />
      <div className="pointer-events-none absolute top-1/2 right-1/3 h-32 w-32 rounded-full bg-sc-coral/8 blur-3xl" />

      <div className="relative flex flex-col gap-5 lg:flex-row lg:items-center lg:justify-between">
        <div className="flex items-start gap-3 sm:gap-4 min-w-0">
          <div className="flex h-11 w-11 sm:h-12 sm:w-12 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-sc-purple via-sc-magenta to-sc-coral shadow-lg shadow-sc-purple/30">
            <Database width={22} height={22} className="text-white" />
          </div>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-xl sm:text-2xl font-bold text-sc-fg-primary">Memory Workspace</h1>
              {scopeChip && (
                <span className="rounded-full border border-sc-purple/30 bg-sc-purple/10 px-2.5 py-0.5 text-[11px] font-medium uppercase tracking-wider text-sc-purple">
                  {scopeChip}
                </span>
              )}
            </div>
            <p className="mt-1.5 max-w-2xl text-sm text-sc-fg-muted">
              Operate the trust pipeline behind your second brain. Raw captures, imports, reviews,
              recalls, and source-grounded synthesis all live here.
            </p>
            <div className="mt-3 flex flex-wrap gap-x-5 gap-y-2 text-xs sm:text-sm">
              <HeroStat icon={Database} tone="cyan" value={captureCount} label="captures in view" />
              <HeroStat
                icon={WarningCircle}
                tone="yellow"
                value={pendingCount}
                label="pending review"
                pulse={pendingCount > 0}
              />
              <HeroStat icon={Search} tone="coral" value={recallCount} label="recent recalls" />
              <HeroStat icon={Key} tone="purple" value={agentReaders} label="agent readers" />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function HeroStat({
  icon: Icon,
  tone,
  value,
  label,
  pulse = false,
}: {
  icon: IconComponent;
  tone: 'cyan' | 'purple' | 'coral' | 'yellow' | 'green';
  value: number;
  label: string;
  pulse?: boolean;
}) {
  const toneClass = {
    cyan: 'text-sc-cyan',
    purple: 'text-sc-purple',
    coral: 'text-sc-coral',
    yellow: 'text-sc-yellow',
    green: 'text-sc-green',
  }[tone];

  return (
    <div className="flex items-center gap-2">
      <Icon width={14} height={14} className={`${toneClass} shrink-0`} />
      <span className="text-sc-fg-muted">
        <span
          className={`font-semibold text-sc-fg-primary ${pulse ? 'animate-pulse' : ''}`}
          suppressHydrationWarning
        >
          {value}
        </span>{' '}
        {label}
      </span>
    </div>
  );
}

function PrimaryActionTile({
  href,
  icon: Icon,
  label,
  description,
  tone,
  badge,
}: {
  href: string;
  icon: IconComponent;
  label: string;
  description: string;
  tone: 'purple' | 'cyan' | 'coral';
  badge?: string;
}) {
  const toneClasses = {
    purple: {
      ring: 'border-sc-purple/25 hover:border-sc-purple/60',
      tint: 'from-sc-purple/12 to-transparent',
      icon: 'bg-sc-purple/15 text-sc-purple border-sc-purple/25',
      arrow: 'text-sc-purple',
      badge: 'border-sc-purple/30 bg-sc-purple/10 text-sc-purple',
    },
    cyan: {
      ring: 'border-sc-cyan/25 hover:border-sc-cyan/60',
      tint: 'from-sc-cyan/12 to-transparent',
      icon: 'bg-sc-cyan/15 text-sc-cyan border-sc-cyan/25',
      arrow: 'text-sc-cyan',
      badge: 'border-sc-cyan/30 bg-sc-cyan/10 text-sc-cyan',
    },
    coral: {
      ring: 'border-sc-coral/25 hover:border-sc-coral/60',
      tint: 'from-sc-coral/12 to-transparent',
      icon: 'bg-sc-coral/15 text-sc-coral border-sc-coral/25',
      arrow: 'text-sc-coral',
      badge: 'border-sc-coral/30 bg-sc-coral/10 text-sc-coral',
    },
  }[tone];

  return (
    <Link
      href={href}
      className={`group relative overflow-hidden rounded-xl border bg-sc-bg-base p-4 shadow-card transition-all hover:shadow-card-hover ${toneClasses.ring}`}
    >
      <div
        className={`pointer-events-none absolute inset-0 bg-gradient-to-br ${toneClasses.tint} opacity-60 transition-opacity group-hover:opacity-100`}
      />
      <div className="relative flex items-start gap-3">
        <div
          className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border ${toneClasses.icon}`}
        >
          <Icon width={18} height={18} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-semibold text-sc-fg-primary">{label}</h3>
            {badge && (
              <span
                className={`rounded-full border px-1.5 py-0.5 text-[10px] font-medium ${toneClasses.badge}`}
              >
                {badge}
              </span>
            )}
          </div>
          <p className="mt-1 text-xs leading-relaxed text-sc-fg-muted">{description}</p>
        </div>
        <ArrowRight
          width={16}
          height={16}
          className={`mt-1 shrink-0 transition-transform group-hover:translate-x-1 ${toneClasses.arrow}`}
        />
      </div>
    </Link>
  );
}

function Panel({
  title,
  count,
  icon: Icon,
  iconTone,
  children,
  action,
}: {
  title: string;
  count?: number;
  icon?: IconComponent;
  iconTone?: 'cyan' | 'purple' | 'coral' | 'yellow' | 'green';
  children: ReactNode;
  action?: ReactNode;
}) {
  const iconColor = {
    cyan: 'text-sc-cyan',
    purple: 'text-sc-purple',
    coral: 'text-sc-coral',
    yellow: 'text-sc-yellow',
    green: 'text-sc-green',
  }[iconTone ?? 'cyan'];

  return (
    <section className="rounded-xl border border-sc-fg-subtle/20 bg-sc-bg-base shadow-card overflow-hidden">
      <header className="flex items-center justify-between gap-3 border-b border-sc-fg-subtle/10 px-4 py-2.5">
        <div className="flex items-center gap-2 min-w-0">
          {Icon && <Icon width={14} height={14} className={`${iconColor} shrink-0`} />}
          <h2 className="text-sm font-semibold text-sc-fg-primary truncate">{title}</h2>
          {typeof count === 'number' && (
            <span className="rounded-full bg-sc-bg-highlight px-2 py-0.5 text-[11px] font-medium text-sc-fg-muted">
              {count}
            </span>
          )}
        </div>
        {action}
      </header>
      {children}
    </section>
  );
}

function CaptureRows({
  captures,
  emptyLabel,
  linkToReview = false,
}: {
  captures: RawCaptureSummary[];
  emptyLabel: string;
  linkToReview?: boolean;
}) {
  if (captures.length === 0) {
    return (
      <div className="px-4 py-6 text-center">
        <p className="text-sm text-sc-fg-muted">{emptyLabel}</p>
      </div>
    );
  }

  return (
    <div className="divide-y divide-sc-fg-subtle/10">
      {captures.map(capture => {
        const scope = captureScope(capture);
        const body = (
          <>
            <div className="flex items-center gap-2 min-w-0">
              <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${scopeDot(scope)}`} />
              <div className="min-w-0">
                <p className="truncate text-sm font-medium text-sc-fg-primary group-hover:text-sc-cyan transition-colors">
                  {capture.title}
                </p>
                <p className="mt-0.5 truncate text-[11px] text-sc-fg-subtle">
                  {captureMeta(capture)}
                </p>
              </div>
            </div>
            <EntityBadge type={capture.entity_type} />
          </>
        );

        const href = linkToReview
          ? `/memory/captures?id=${encodeURIComponent(capture.id)}`
          : `/memory/sources/${encodeURIComponent(capture.id)}`;

        return (
          <Link
            key={capture.id}
            href={href}
            className="group grid grid-cols-[minmax(0,1fr)_auto] items-center gap-3 px-4 py-2.5 transition-colors hover:bg-sc-bg-highlight/50"
          >
            {body}
          </Link>
        );
      })}
    </div>
  );
}

function MemorySpacesPanel({ spaces }: { spaces: MemorySpace[] }) {
  if (spaces.length === 0) {
    return (
      <Panel title="Memory Spaces" icon={Users} iconTone="cyan">
        <p className="px-4 py-6 text-center text-sm text-sc-fg-muted">
          No memory spaces in this scope
        </p>
      </Panel>
    );
  }

  return (
    <Panel title="Memory Spaces" icon={Users} iconTone="cyan" count={spaces.length}>
      <div className="divide-y divide-sc-fg-subtle/10">
        {spaces.slice(0, 5).map(space => {
          const memberCount = space.members.length;
          return (
            <Link
              key={space.id}
              href={`/memory?space=${encodeURIComponent(space.id)}`}
              className="group flex items-center justify-between gap-3 px-4 py-2.5 transition-colors hover:bg-sc-bg-highlight/50"
            >
              <div className="flex items-center gap-2 min-w-0">
                <span
                  className={`h-1.5 w-1.5 shrink-0 rounded-full ${scopeDot(space.memory_scope)}`}
                />
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-sc-fg-primary group-hover:text-sc-cyan transition-colors">
                    {space.name}
                  </p>
                  <p className="mt-0.5 truncate text-[11px] text-sc-fg-subtle">
                    {space.memory_scope}
                    {space.scope_key ? ` · ${space.scope_key}` : ''} · {memberCount}{' '}
                    {memberCount === 1 ? 'reader' : 'readers'}
                  </p>
                </div>
              </div>
              {space.state === 'disabled' && (
                <span className="rounded border border-sc-yellow/30 bg-sc-yellow/10 px-1.5 py-0.5 text-[10px] font-medium text-sc-yellow">
                  disabled
                </span>
              )}
            </Link>
          );
        })}
      </div>
    </Panel>
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

  if (events.length === 0 && members.length === 0) {
    return (
      <Panel title="Agent Access" icon={Key} iconTone="purple">
        <div className="px-4 py-6 text-center">
          <Key width={20} height={20} className="mx-auto mb-2 text-sc-fg-subtle/40" />
          <p className="text-sm text-sc-fg-muted">No agent access previews yet</p>
          <p className="mt-1 text-[11px] text-sc-fg-subtle">Delegated readers will appear here</p>
        </div>
      </Panel>
    );
  }

  return (
    <Panel title="Agent Access" icon={Key} iconTone="purple" count={events.length + members.length}>
      <div className="divide-y divide-sc-fg-subtle/10">
        {events.slice(0, 3).map(event => (
          <article key={event.id} className="px-4 py-2.5">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="truncate text-sm font-medium text-sc-fg-primary">
                  {event.policy_reason || 'access preview'}
                </p>
                <p className="mt-0.5 truncate text-[11px] text-sc-fg-subtle">
                  {event.source_surface || 'memory access'} · {event.memory_scope || 'scope'}
                </p>
              </div>
              <Key width={13} height={13} className="mt-1 shrink-0 text-sc-purple" />
            </div>
          </article>
        ))}
        {members.slice(0, 4).map(member => (
          <article key={member.id} className="px-4 py-2.5">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="truncate text-sm font-medium text-sc-fg-primary">
                  {member.principal_type}:{member.principal_id}
                </p>
                <p className="mt-0.5 truncate text-[11px] text-sc-fg-subtle">
                  {member.spaceName} · {member.scope}
                </p>
              </div>
              <span className="rounded border border-sc-purple/20 bg-sc-purple/10 px-1.5 py-0.5 text-[10px] font-medium text-sc-purple shrink-0">
                {member.role}
              </span>
            </div>
          </article>
        ))}
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
  const synthesisEvents = events.filter(event => event.action.includes('synthesis'));
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
    return <LoadingState message="Loading memory workspace..." />;
  }

  const agentReaders = spaces.reduce((acc, space) => acc + space.members.length, 0);
  const scopeChip = scope === 'all' ? null : scope;

  return (
    <div className="space-y-4">
      <MemoryHero
        captureCount={captures.length}
        pendingCount={pending.length}
        recallCount={recalls.length}
        agentReaders={agentReaders}
        scopeChip={scopeChip}
      />

      {panelErrors.length > 0 && (
        <div className="flex items-start gap-2 rounded-lg border border-sc-yellow/30 bg-sc-yellow/10 px-4 py-3 text-sm text-sc-yellow">
          <WarningCircle width={16} height={16} className="mt-0.5 shrink-0" />
          <span>Some memory panels are unavailable from the current role or backend state.</span>
        </div>
      )}

      <div className="grid gap-3 md:grid-cols-3">
        <PrimaryActionTile
          href="/memory/synthesize"
          icon={FileText}
          label="Synthesize"
          description="Draft a source-grounded artifact from authorized memory"
          tone="purple"
          badge="NEW"
        />
        <PrimaryActionTile
          href="/memory/imports"
          icon={Upload}
          label="Import Sources"
          description="Ingest a mailbox or archive into private memory"
          tone="cyan"
        />
        <PrimaryActionTile
          href="/memory/captures?link=unlinked"
          icon={WarningCircle}
          label="Review Queue"
          description={
            pending.length > 0
              ? `${pending.length} captures waiting on your review`
              : 'Triage pending captures and reflections'
          }
          tone="coral"
          badge={pending.length > 0 ? `${pending.length}` : undefined}
        />
      </div>

      <div className="flex items-center justify-between gap-3">
        <MemoryScopeSwitcher
          value={scope}
          onChange={setScope}
          counts={scopeCounts(
            capturesQuery.data?.captures ?? [],
            auditQuery.data?.events ?? [],
            spacesQuery.data?.spaces ?? []
          )}
        />
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.5fr)_minmax(320px,1fr)]">
        <div className="space-y-4">
          <Panel
            title="Recent Captures"
            icon={Database}
            iconTone="cyan"
            count={captures.length}
            action={
              captures.length > 6 && (
                <Link
                  href="/memory/captures"
                  className="text-[11px] font-medium text-sc-cyan hover:text-sc-cyan/80 transition-colors"
                >
                  View all →
                </Link>
              )
            }
          >
            <CaptureRows captures={captures.slice(0, 6)} emptyLabel="No captures in this scope" />
          </Panel>

          <Panel
            title="Review Actions"
            icon={WarningCircle}
            iconTone="yellow"
            count={pending.length}
            action={
              pending.length > 0 && (
                <Link
                  href="/memory/captures?link=unlinked"
                  className="text-[11px] font-medium text-sc-yellow hover:text-sc-yellow/80 transition-colors"
                >
                  Open Queue →
                </Link>
              )
            }
          >
            <CaptureRows
              captures={pending.slice(0, 5)}
              emptyLabel="Inbox zero. No pending reviews."
              linkToReview
            />
          </Panel>

          <div className="grid gap-4 lg:grid-cols-2">
            <Panel title="Recent Imports" icon={Upload} iconTone="cyan" count={imports.length}>
              <CaptureRows captures={imports.slice(0, 4)} emptyLabel="No source imports yet" />
            </Panel>
            <Panel
              title="Reflection Queue"
              icon={LightBulb}
              iconTone="coral"
              count={reflections.length}
            >
              <CaptureRows
                captures={reflections.slice(0, 4)}
                emptyLabel="No reflection candidates waiting"
                linkToReview
              />
            </Panel>
          </div>
        </div>

        <div className="space-y-4">
          <MemoryActivityFeed events={events.slice(0, 8)} />
          <MemorySpacesPanel spaces={spaces} />
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
          events={synthesisEvents.slice(0, 5)}
          title="Synthesis Activity"
          emptyLabel="No synthesis events yet"
        />
      </div>
    </div>
  );
}
