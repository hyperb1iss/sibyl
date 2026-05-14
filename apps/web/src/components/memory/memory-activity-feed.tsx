'use client';

import {
  Database,
  Eye,
  Key,
  LightBulb,
  Search,
  Send,
  Users,
  WarningCircle,
} from '@/components/ui/icons';
import type { MemoryAuditEvent } from '@/lib/api';
import { formatDistanceToNow } from '@/lib/constants';

interface MemoryActivityFeedProps {
  events: MemoryAuditEvent[];
  title?: string;
  emptyLabel?: string;
}

function actionLabel(action: string): string {
  return action
    .replace(/^memory\./, '')
    .split('.')
    .filter(Boolean)
    .map(part => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}

function eventIcon(action: string) {
  if (action.includes('recall') || action.includes('context_pack')) return Search;
  if (action.includes('access')) return Key;
  if (action.includes('share')) return Send;
  if (action.includes('reflect')) return LightBulb;
  if (action.includes('inspect')) return Eye;
  if (action.includes('policy_deny') || action.includes('correction')) return WarningCircle;
  if (action.includes('remember')) return Database;
  return Users;
}

function eventTone(event: MemoryAuditEvent): string {
  if (event.policy_allowed === false) return 'text-sc-red bg-sc-red/10 border-sc-red/25';
  if (event.action.includes('access')) return 'text-sc-purple bg-sc-purple/10 border-sc-purple/25';
  if (event.action.includes('recall')) return 'text-sc-cyan bg-sc-cyan/10 border-sc-cyan/25';
  if (event.action.includes('reflect')) return 'text-sc-coral bg-sc-coral/10 border-sc-coral/25';
  return 'text-sc-green bg-sc-green/10 border-sc-green/25';
}

function policyLabel(event: MemoryAuditEvent): string {
  if (event.policy_allowed === true) return event.policy_reason || 'allowed';
  if (event.policy_allowed === false) return event.policy_reason || 'denied';
  return event.policy_reason || 'recorded';
}

export function MemoryActivityFeed({
  events,
  title = 'Activity Feed',
  emptyLabel = 'No memory activity yet',
}: MemoryActivityFeedProps) {
  return (
    <section className="rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-base shadow-card">
      <div className="flex items-center justify-between border-b border-sc-fg-subtle/10 px-4 py-3">
        <h2 className="text-sm font-semibold text-sc-fg-primary">{title}</h2>
        <span className="text-xs text-sc-fg-subtle">{events.length}</span>
      </div>
      <div className="divide-y divide-sc-fg-subtle/10">
        {events.length === 0 ? (
          <p className="px-4 py-6 text-sm text-sc-fg-muted">{emptyLabel}</p>
        ) : (
          events.map(event => {
            const Icon = eventIcon(event.action);
            return (
              <article
                key={event.id}
                className="grid grid-cols-[auto_minmax(0,1fr)] gap-3 px-4 py-3"
              >
                <span
                  className={`mt-0.5 flex h-8 w-8 items-center justify-center rounded-md border ${eventTone(event)}`}
                >
                  <Icon width={15} height={15} />
                </span>
                <div className="min-w-0">
                  <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                    <p className="truncate text-sm font-medium text-sc-fg-primary">
                      {actionLabel(event.action)}
                    </p>
                    <span className="rounded border border-sc-fg-subtle/15 px-1.5 py-0.5 text-[11px] text-sc-fg-muted">
                      {policyLabel(event)}
                    </span>
                  </div>
                  <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-sc-fg-subtle">
                    {event.memory_scope && <span>{event.memory_scope}</span>}
                    {event.project_id && <span>{event.project_id}</span>}
                    {event.source_surface && <span>{event.source_surface}</span>}
                    {event.created_at && <span>{formatDistanceToNow(event.created_at)}</span>}
                  </div>
                </div>
              </article>
            );
          })
        )}
      </div>
    </section>
  );
}
