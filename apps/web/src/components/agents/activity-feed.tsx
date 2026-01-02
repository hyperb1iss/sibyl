'use client';

/**
 * Activity Feed - Cross-agent activity timeline.
 *
 * Shows recent activity from all agents including status changes,
 * messages, and approval events in chronological order.
 */

import { formatDistanceToNow } from 'date-fns';
import Link from 'next/link';
import { memo } from 'react';

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
  { icon: typeof Activity; label: string; colorClass: string; bgClass: string }
> = {
  agent_spawned: {
    icon: Sparks,
    label: 'Spawned',
    colorClass: 'text-sc-purple',
    bgClass: 'bg-sc-purple/20',
  },
  agent_started: {
    icon: Play,
    label: 'Started',
    colorClass: 'text-sc-cyan',
    bgClass: 'bg-sc-cyan/20',
  },
  agent_completed: {
    icon: Check,
    label: 'Completed',
    colorClass: 'text-sc-green',
    bgClass: 'bg-sc-green/20',
  },
  agent_failed: {
    icon: WarningCircle,
    label: 'Failed',
    colorClass: 'text-sc-red',
    bgClass: 'bg-sc-red/20',
  },
  agent_paused: {
    icon: Pause,
    label: 'Paused',
    colorClass: 'text-sc-yellow',
    bgClass: 'bg-sc-yellow/20',
  },
  agent_terminated: {
    icon: Xmark,
    label: 'Terminated',
    colorClass: 'text-sc-red',
    bgClass: 'bg-sc-red/20',
  },
  agent_message: {
    icon: InfoCircle,
    label: 'Message',
    colorClass: 'text-sc-fg-muted',
    bgClass: 'bg-sc-fg-subtle/10',
  },
  approval_requested: {
    icon: AlertTriangle,
    label: 'Approval Requested',
    colorClass: 'text-sc-yellow',
    bgClass: 'bg-sc-yellow/20',
  },
  approval_responded: {
    icon: Check,
    label: 'Approval Responded',
    colorClass: 'text-sc-green',
    bgClass: 'bg-sc-green/20',
  },
};

// =============================================================================
// Activity Event Item
// =============================================================================

interface ActivityEventItemProps {
  event: ActivityEvent;
}

const ActivityEventItem = memo(function ActivityEventItem({ event }: ActivityEventItemProps) {
  const config = EVENT_CONFIG[event.event_type] || {
    icon: Activity,
    label: event.event_type,
    colorClass: 'text-sc-fg-muted',
    bgClass: 'bg-sc-fg-subtle/10',
  };
  const EventIcon = config.icon;
  const timestamp = event.timestamp ? new Date(event.timestamp) : null;

  const content = (
    <div className="flex items-start gap-3 py-3 border-b border-sc-fg-subtle/10 last:border-0 group-hover:bg-sc-bg-elevated/50 transition-colors">
      {/* Icon */}
      <div className={`p-2 rounded-lg ${config.bgClass}`}>
        <EventIcon className={`h-4 w-4 ${config.colorClass}`} />
      </div>

      {/* Content */}
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 mb-0.5">
          <span className={`text-xs font-medium ${config.colorClass}`}>{config.label}</span>
          {event.agent_name && (
            <span className="text-xs text-sc-fg-subtle">• {event.agent_name}</span>
          )}
        </div>
        <p className="text-sm text-sc-fg-primary truncate">{event.summary}</p>
      </div>

      {/* Timestamp */}
      {timestamp && (
        <span className="text-xs text-sc-fg-subtle flex-shrink-0">
          {formatDistanceToNow(timestamp, { addSuffix: true })}
        </span>
      )}
    </div>
  );

  // Wrap in Link if we have an agent_id
  if (event.agent_id) {
    return (
      <Link href={`/agents/${event.agent_id}`} className="block group">
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
  maxHeight?: string;
  className?: string;
}

export function ActivityFeed({ projectId, maxHeight = '400px', className }: ActivityFeedProps) {
  const { data, isLoading, error } = useActivityFeed(projectId);

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

  const events = data?.events || [];

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
      description="Recent activity across all agents."
      actions={
        data?.total && data.total > 0 ? (
          <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-sc-cyan/20 text-sc-cyan border border-sc-cyan/40">
            {data.total}
          </span>
        ) : null
      }
      className={className}
    >
      <div className="overflow-y-auto" style={{ maxHeight }}>
        {events.map(event => (
          <ActivityEventItem key={event.id} event={event} />
        ))}
      </div>
    </Section>
  );
}
