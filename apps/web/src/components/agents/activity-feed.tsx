'use client';

/**
 * Activity Feed - Cross-agent activity timeline.
 *
 * Shows recent activity from all agents including status changes,
 * messages, and approval events in chronological order.
 */

import { formatDistanceToNow } from 'date-fns';
import Link from 'next/link';
import { memo, useMemo } from 'react';

import { Section } from '@/components/ui/card';
import {
  Activity,
  AlertTriangle,
  Check,
  Clock,
  InfoCircle,
  Pause,
  Play,
  Sparks,
  WarningCircle,
  Xmark,
} from '@/components/ui/icons';
import { Spinner } from '@/components/ui/spinner';
import type { ActivityEvent, ActivityEventType } from '@/lib/api';
import { useActivityFeed } from '@/lib/hooks';

// =============================================================================
// Event Type Configuration
// =============================================================================

const EVENT_CONFIG: Record<
  ActivityEventType,
  { icon: typeof Activity; label: string; colorClass: string }
> = {
  agent_spawned: { icon: Sparks, label: 'Spawned', colorClass: 'text-sc-purple' },
  agent_started: { icon: Play, label: 'Started', colorClass: 'text-sc-cyan' },
  agent_completed: { icon: Check, label: 'Completed', colorClass: 'text-sc-green' },
  agent_failed: { icon: WarningCircle, label: 'Failed', colorClass: 'text-sc-red' },
  agent_paused: { icon: Pause, label: 'Paused', colorClass: 'text-sc-yellow' },
  agent_terminated: { icon: Xmark, label: 'Terminated', colorClass: 'text-sc-red' },
  agent_message: { icon: InfoCircle, label: 'Message', colorClass: 'text-sc-fg-muted' },
  approval_requested: {
    icon: AlertTriangle,
    label: 'Approval Requested',
    colorClass: 'text-sc-yellow',
  },
  approval_responded: { icon: Check, label: 'Approval Responded', colorClass: 'text-sc-green' },
};

// =============================================================================
// Activity Event Item
// =============================================================================

interface ActivityEventItemProps {
  event: ActivityEvent;
  onAgentClick?: (id: string) => void;
}

const ActivityEventItem = memo(function ActivityEventItem({
  event,
  onAgentClick,
}: ActivityEventItemProps) {
  const config = EVENT_CONFIG[event.event_type] || {
    icon: Activity,
    label: event.event_type,
    colorClass: 'text-sc-fg-muted',
  };
  const EventIcon = config.icon;
  const timestamp = event.timestamp ? new Date(event.timestamp) : null;

  const content = (
    <div className="flex gap-2.5 px-2 py-2 rounded-lg group-hover:bg-sc-bg-elevated/50 transition-colors">
      {/* Timeline icon */}
      <div className="shrink-0 pt-0.5">
        <EventIcon className={`h-3.5 w-3.5 ${config.colorClass}`} />
      </div>

      {/* Content — stacked for narrow panels */}
      <div className="flex-1 min-w-0 space-y-0.5">
        <div className="flex items-baseline justify-between gap-2">
          <span className={`text-[11px] font-medium ${config.colorClass} shrink-0`}>
            {config.label}
          </span>
          {timestamp && (
            <span className="text-[10px] text-sc-fg-subtle shrink-0 tabular-nums">
              {formatDistanceToNow(timestamp, { addSuffix: true })}
            </span>
          )}
        </div>
        {event.summary && (
          <p className="text-[11px] text-sc-fg-muted leading-snug line-clamp-2">{event.summary}</p>
        )}
        {event.agent_name && (
          <p className="text-[10px] text-sc-fg-subtle truncate">{event.agent_name}</p>
        )}
      </div>
    </div>
  );

  // Use callback for in-page selection when provided
  if (event.agent_id && onAgentClick) {
    const agentId = event.agent_id;
    return (
      <button
        type="button"
        onClick={() => onAgentClick(agentId)}
        className="block w-full text-left group"
      >
        {content}
      </button>
    );
  }

  // Fall back to Link navigation
  if (event.agent_id) {
    return (
      <Link href={`/agents?id=${event.agent_id}`} className="block group">
        {content}
      </Link>
    );
  }

  return content;
});

// =============================================================================
// Activity Feed Component
// =============================================================================

interface ActivityFeedProps {
  projectId?: string;
  agentId?: string;
  onAgentClick?: (id: string) => void;
  maxHeight?: string;
  className?: string;
}

export function ActivityFeed({
  projectId,
  agentId,
  onAgentClick,
  maxHeight = '400px',
  className,
}: ActivityFeedProps) {
  const { data, isLoading, error } = useActivityFeed(projectId);

  // Client-side filter by agent when specified
  const events = useMemo(() => {
    const all = data?.events || [];
    if (!agentId) return all;
    return all.filter(e => e.agent_id === agentId);
  }, [data?.events, agentId]);

  if (isLoading) {
    return (
      <Section
        title="Activity Feed"
        icon={<Clock className="h-5 w-5 animate-pulse" />}
        className={className}
      >
        <div className="flex items-center justify-center py-8">
          <Spinner size="lg" />
        </div>
      </Section>
    );
  }

  if (error) {
    return (
      <Section
        title="Activity Feed"
        icon={<WarningCircle className="h-5 w-5 text-sc-red" />}
        className={className}
      >
        <div className="text-sm text-sc-red">Error loading activity: {error.message}</div>
      </Section>
    );
  }

  if (events.length === 0) {
    return (
      <Section
        title="Activity Feed"
        icon={<Activity className="h-5 w-5 text-sc-fg-muted" />}
        description="No recent agent activity."
        className={className}
      >
        <div className="text-center py-4 text-sc-fg-subtle">Agents are quiet</div>
      </Section>
    );
  }

  return (
    <Section
      title="Activity Feed"
      icon={<Activity className="h-5 w-5 text-sc-cyan" />}
      description={agentId ? 'Activity for this agent.' : 'Recent activity across all agents.'}
      actions={
        events.length > 0 ? (
          <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-sc-cyan/20 text-sc-cyan border border-sc-cyan/40">
            {events.length}
          </span>
        ) : null
      }
      className={className}
    >
      <div className="overflow-y-auto" style={{ maxHeight }}>
        {events.map(event => (
          <ActivityEventItem key={event.id} event={event} onAgentClick={onAgentClick} />
        ))}
      </div>
    </Section>
  );
}
