'use client';

/**
 * Agent Health Monitor - Real-time agent liveness tracking.
 *
 * Displays health status of all active agents based on heartbeat data.
 * Agents are classified as healthy, stale, or unresponsive based on
 * time since last heartbeat.
 */

import { formatDistanceToNow } from 'date-fns';
import { memo } from 'react';

import { Section } from '@/components/ui/card';
import { Activity, Check, Clock, Pause, WarningCircle } from '@/components/ui/icons';
import { Spinner } from '@/components/ui/spinner';
import type { AgentHealth, AgentHealthStatus } from '@/lib/api';
import { useHealthOverview } from '@/lib/hooks';

// =============================================================================
// Health Status Configuration
// =============================================================================

const STATUS_CONFIG: Record<
  AgentHealthStatus,
  { icon: typeof Activity; label: string; colorClass: string; bgClass: string }
> = {
  healthy: {
    icon: Activity,
    label: 'Healthy',
    colorClass: 'text-sc-green',
    bgClass: 'bg-sc-green/20',
  },
  stale: {
    icon: Clock,
    label: 'Stale',
    colorClass: 'text-sc-yellow',
    bgClass: 'bg-sc-yellow/20',
  },
  unresponsive: {
    icon: WarningCircle,
    label: 'Unresponsive',
    colorClass: 'text-sc-red',
    bgClass: 'bg-sc-red/20',
  },
};

// =============================================================================
// Health Stats Summary
// =============================================================================

interface HealthStatsProps {
  total: number;
  healthy: number;
  stale: number;
  unresponsive: number;
}

function HealthStats({ total, healthy, stale, unresponsive }: HealthStatsProps) {
  return (
    <div className="flex items-center gap-4 mb-4 p-3 rounded-lg bg-sc-bg-secondary">
      <div className="flex items-center gap-2">
        <span className="text-xs text-sc-fg-subtle">Total</span>
        <span className="text-sm font-semibold text-sc-fg-primary">{total}</span>
      </div>
      <div className="h-4 w-px bg-sc-fg-subtle/20" />
      <div className="flex items-center gap-2">
        <div className="w-2 h-2 rounded-full bg-sc-green" />
        <span className="text-xs text-sc-fg-subtle">Healthy</span>
        <span className="text-sm font-semibold text-sc-green">{healthy}</span>
      </div>
      <div className="flex items-center gap-2">
        <div className="w-2 h-2 rounded-full bg-sc-yellow" />
        <span className="text-xs text-sc-fg-subtle">Stale</span>
        <span className="text-sm font-semibold text-sc-yellow">{stale}</span>
      </div>
      <div className="flex items-center gap-2">
        <div className="w-2 h-2 rounded-full bg-sc-red" />
        <span className="text-xs text-sc-fg-subtle">Unresponsive</span>
        <span className="text-sm font-semibold text-sc-red">{unresponsive}</span>
      </div>
    </div>
  );
}

// =============================================================================
// Agent Health Item
// =============================================================================

interface AgentHealthItemProps {
  agent: AgentHealth;
}

const AgentHealthItem = memo(function AgentHealthItem({ agent }: AgentHealthItemProps) {
  const config = STATUS_CONFIG[agent.status];
  const StatusIcon = config.icon;
  const lastHeartbeat = agent.last_heartbeat ? new Date(agent.last_heartbeat) : null;

  return (
    <div className="flex items-center gap-3 py-3 border-b border-sc-fg-subtle/10 last:border-0">
      {/* Status Icon */}
      <div className={`p-2 rounded-lg ${config.bgClass}`}>
        <StatusIcon className={`h-4 w-4 ${config.colorClass}`} />
      </div>

      {/* Agent Info */}
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 mb-0.5">
          <span className="text-sm font-medium text-sc-fg-primary truncate">
            {agent.agent_name}
          </span>
          <span
            className={`px-1.5 py-0.5 text-xs font-medium rounded ${config.bgClass} ${config.colorClass}`}
          >
            {config.label}
          </span>
        </div>
        <div className="flex items-center gap-2 text-xs text-sc-fg-subtle">
          <span className="capitalize">{agent.agent_status.replace('_', ' ')}</span>
          {lastHeartbeat && (
            <>
              <span>•</span>
              <span>Last seen {formatDistanceToNow(lastHeartbeat, { addSuffix: true })}</span>
            </>
          )}
        </div>
      </div>

      {/* Heartbeat Indicator */}
      <div className="flex-shrink-0">
        {agent.status === 'healthy' && (
          <div className="relative">
            <Activity className="h-4 w-4 text-sc-green" />
            <div className="absolute inset-0 animate-ping">
              <Activity className="h-4 w-4 text-sc-green opacity-40" />
            </div>
          </div>
        )}
        {agent.status === 'stale' && <Pause className="h-4 w-4 text-sc-yellow" />}
        {agent.status === 'unresponsive' && <WarningCircle className="h-4 w-4 text-sc-red" />}
      </div>
    </div>
  );
});

// =============================================================================
// Health Monitor Component
// =============================================================================

interface HealthMonitorProps {
  projectId?: string;
  maxHeight?: string;
  className?: string;
}

export function HealthMonitor({ projectId, maxHeight = '400px', className }: HealthMonitorProps) {
  const { data, isLoading, error } = useHealthOverview(projectId);

  if (isLoading) {
    return (
      <Section
        title="Agent Health"
        icon={<Activity className="h-5 w-5 animate-pulse" />}
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
        title="Agent Health"
        icon={<Activity className="h-5 w-5 text-sc-red" />}
        className={className}
      >
        <div className="text-sm text-sc-red">Error loading health data: {error.message}</div>
      </Section>
    );
  }

  const agents = data?.agents || [];
  const total = data?.total || 0;
  const healthy = data?.healthy || 0;
  const stale = data?.stale || 0;
  const unresponsive = data?.unresponsive || 0;

  if (agents.length === 0) {
    return (
      <Section
        title="Agent Health"
        icon={<Activity className="h-5 w-5 text-sc-fg-muted" />}
        description="No active agents to monitor."
        className={className}
      >
        <div className="text-center py-4 text-sc-fg-subtle">No agents running</div>
      </Section>
    );
  }

  const allHealthy = stale === 0 && unresponsive === 0;

  return (
    <Section
      title="Agent Health"
      icon={<Activity className={`h-5 w-5 ${allHealthy ? 'text-sc-green' : 'text-sc-yellow'}`} />}
      description="Real-time agent liveness monitoring."
      actions={
        allHealthy ? (
          <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-sc-green/20 text-sc-green border border-sc-green/40 flex items-center gap-1">
            <Check className="h-3 w-3" />
            All Healthy
          </span>
        ) : (
          <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-sc-yellow/20 text-sc-yellow border border-sc-yellow/40">
            {stale + unresponsive} Issue{stale + unresponsive !== 1 ? 's' : ''}
          </span>
        )
      }
      className={className}
    >
      <HealthStats total={total} healthy={healthy} stale={stale} unresponsive={unresponsive} />
      <div className="overflow-y-auto" style={{ maxHeight }}>
        {agents.map(agent => (
          <AgentHealthItem key={agent.agent_id} agent={agent} />
        ))}
      </div>
    </Section>
  );
}
