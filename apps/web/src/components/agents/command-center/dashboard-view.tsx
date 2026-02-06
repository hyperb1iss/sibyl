'use client';

import { memo, useMemo } from 'react';
import { ApprovalQueue } from '@/components/agents/approval-queue';
import type { Agent } from '@/lib/api';
import {
  AGENT_STATUS_CONFIG,
  AGENT_TYPE_CONFIG,
  type AgentStatusType,
  type AgentTypeValue,
  formatDistanceToNow,
} from '@/lib/constants';

// =============================================================================
// Types
// =============================================================================

interface DashboardViewProps {
  agents: Agent[];
  projectFilter?: string;
  onSelectAgent: (id: string) => void;
}

// =============================================================================
// Fleet Status Strip
// =============================================================================

const FleetStatusStrip = memo(function FleetStatusStrip({ agents }: { agents: Agent[] }) {
  const counts = useMemo(() => {
    const twoMinutesAgo = Date.now() - 2 * 60 * 1000;
    let active = 0;
    let approval = 0;
    let paused = 0;
    let terminal = 0;
    for (const agent of agents) {
      if (agent.status === 'waiting_approval') {
        approval++;
      } else if (agent.status === 'paused') {
        paused++;
      } else if (['completed', 'failed', 'terminated'].includes(agent.status)) {
        terminal++;
      } else if (
        agent.last_heartbeat &&
        new Date(agent.last_heartbeat).getTime() >= twoMinutesAgo
      ) {
        active++;
      }
    }
    return { total: agents.length, active, approval, paused, terminal };
  }, [agents]);

  return (
    <div className="flex items-center gap-3 px-4 py-3 bg-sc-bg-elevated/60 border border-sc-fg-subtle/10 rounded-lg backdrop-blur-sm">
      <span className="text-lg font-bold text-sc-fg-primary tabular-nums">{counts.total}</span>
      <span className="text-xs text-sc-fg-subtle uppercase tracking-wider">agents</span>
      <div className="h-4 w-px bg-sc-fg-subtle/15" />
      <div className="flex items-center gap-2.5 text-[11px] font-medium">
        {counts.active > 0 && (
          <span className="flex items-center gap-1.5 text-sc-purple">
            <span className="w-1.5 h-1.5 rounded-full bg-sc-purple animate-pulse" />
            {counts.active}
          </span>
        )}
        {counts.approval > 0 && (
          <span className="flex items-center gap-1.5 text-sc-coral">
            <span className="w-1.5 h-1.5 rounded-full bg-sc-coral" />
            {counts.approval}
          </span>
        )}
        {counts.paused > 0 && (
          <span className="flex items-center gap-1.5 text-sc-yellow">
            <span className="w-1.5 h-1.5 rounded-full bg-sc-yellow" />
            {counts.paused}
          </span>
        )}
        {counts.terminal > 0 && (
          <span className="flex items-center gap-1.5 text-sc-fg-subtle">
            <span className="w-1.5 h-1.5 rounded-full bg-sc-fg-subtle/50" />
            {counts.terminal}
          </span>
        )}
      </div>
    </div>
  );
});

// =============================================================================
// Compact Agent Card
// =============================================================================

const CompactAgentCard = memo(function CompactAgentCard({
  agent,
  onSelect,
}: {
  agent: Agent;
  onSelect: () => void;
}) {
  const statusConfig =
    AGENT_STATUS_CONFIG[agent.status as AgentStatusType] ?? AGENT_STATUS_CONFIG.terminated;
  const typeConfig =
    AGENT_TYPE_CONFIG[agent.agent_type as AgentTypeValue] ?? AGENT_TYPE_CONFIG.general;

  const isActive = (() => {
    if (!agent.last_heartbeat) return false;
    return new Date(agent.last_heartbeat).getTime() >= Date.now() - 2 * 60 * 1000;
  })();

  return (
    <button
      type="button"
      onClick={onSelect}
      className="group block w-full text-left rounded-lg overflow-hidden border border-sc-fg-subtle/15 hover:border-sc-purple/30 bg-sc-bg-elevated/40 hover:bg-sc-bg-elevated transition-all duration-200"
    >
      <div className="px-3 py-2.5">
        <div className="flex items-center gap-2 mb-1.5">
          <span
            className="text-[10px] font-bold px-1.5 py-0.5 rounded border"
            style={{
              backgroundColor: `${typeConfig.color}15`,
              color: typeConfig.color,
              borderColor: `${typeConfig.color}30`,
            }}
          >
            {typeConfig.icon} {typeConfig.label}
          </span>
          {isActive && (
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-sc-purple/15 text-sc-purple font-medium">
              Active
            </span>
          )}
        </div>

        <h3 className="text-xs font-medium text-sc-fg-primary line-clamp-1 leading-snug group-hover:text-white transition-colors">
          {agent.name}
        </h3>

        {isActive && agent.current_activity && (
          <p className="text-[10px] text-sc-fg-muted line-clamp-1 mt-1">{agent.current_activity}</p>
        )}

        <div className="flex items-center justify-between mt-2 pt-1.5 border-t border-sc-fg-subtle/10">
          <span className={`text-[10px] ${statusConfig.textClass}`}>
            {statusConfig.icon} {statusConfig.label}
          </span>
          {agent.last_heartbeat && (
            <span className="text-[10px] text-sc-fg-subtle">
              {formatDistanceToNow(agent.last_heartbeat)}
            </span>
          )}
        </div>
      </div>
    </button>
  );
});

// =============================================================================
// Dashboard View — Center Stage (no agent selected)
//
// Role: Action center — approvals to act on + fleet overview
// Activity Feed lives ONLY in the Inspector (right panel)
// =============================================================================

export function DashboardView({ agents, projectFilter, onSelectAgent }: DashboardViewProps) {
  const recentAgents = useMemo(() => {
    const twoMinutesAgo = Date.now() - 2 * 60 * 1000;
    return [...agents]
      .sort((a, b) => {
        const aActive = a.last_heartbeat && new Date(a.last_heartbeat).getTime() >= twoMinutesAgo;
        const bActive = b.last_heartbeat && new Date(b.last_heartbeat).getTime() >= twoMinutesAgo;
        if (aActive && !bActive) return -1;
        if (!aActive && bActive) return 1;
        const aTime = a.last_heartbeat ? new Date(a.last_heartbeat).getTime() : 0;
        const bTime = b.last_heartbeat ? new Date(b.last_heartbeat).getTime() : 0;
        return bTime - aTime;
      })
      .slice(0, 9);
  }, [agents]);

  return (
    <div className="h-full overflow-y-auto p-4 space-y-5">
      {/* Fleet status */}
      {agents.length > 0 && <FleetStatusStrip agents={agents} />}

      {/* Approval Queue — THE canonical home for approvals */}
      <ApprovalQueue projectId={projectFilter} onAgentClick={onSelectAgent} maxHeight="400px" />

      {/* Recent agents grid */}
      {recentAgents.length > 0 && (
        <div className="space-y-2.5">
          <h2 className="text-xs font-medium text-sc-fg-subtle uppercase tracking-wider">
            Recent Agents
          </h2>
          <div className="grid gap-2.5 sm:grid-cols-2 xl:grid-cols-3">
            {recentAgents.map(agent => (
              <CompactAgentCard
                key={agent.id}
                agent={agent}
                onSelect={() => onSelectAgent(agent.id)}
              />
            ))}
          </div>
        </div>
      )}

      {agents.length === 0 && (
        <div className="flex flex-col items-center justify-center py-16 text-sc-fg-subtle">
          <p className="text-sm">No agents yet. Spawn one to get started.</p>
        </div>
      )}
    </div>
  );
}
