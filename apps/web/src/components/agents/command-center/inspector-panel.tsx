'use client';

import { memo, useMemo } from 'react';
import { ActivityFeed } from '@/components/agents/activity-feed';
import { ApprovalQueue } from '@/components/agents/approval-queue';
import type { Agent } from '@/lib/api';
import {
  AGENT_STATUS_CONFIG,
  AGENT_TYPE_CONFIG,
  type AgentStatusType,
  type AgentTypeValue,
  formatDistanceToNow,
} from '@/lib/constants';
import { usePauseAgent, useResumeAgent, useTerminateAgent } from '@/lib/hooks';

// =============================================================================
// Types
// =============================================================================

interface InspectorPanelProps {
  agent: Agent | null;
  agents?: Agent[];
  projectFilter?: string;
  onSelectAgent: (id: string) => void;
}

// =============================================================================
// Agent Metadata Card
// =============================================================================

const AgentMetadataCard = memo(function AgentMetadataCard({ agent }: { agent: Agent }) {
  const statusConfig =
    AGENT_STATUS_CONFIG[agent.status as AgentStatusType] ?? AGENT_STATUS_CONFIG.working;
  const typeConfig =
    AGENT_TYPE_CONFIG[agent.agent_type as AgentTypeValue] ?? AGENT_TYPE_CONFIG.general;

  const pauseAgent = usePauseAgent();
  const resumeAgent = useResumeAgent();
  const terminateAgent = useTerminateAgent();

  const isActive = ['initializing', 'working', 'resuming'].includes(agent.status);
  const isPaused = agent.status === 'paused';
  const isTerminal = ['completed', 'failed', 'terminated'].includes(agent.status);
  const showControls = !isTerminal && (isActive || isPaused);

  return (
    <div className="space-y-3">
      {/* Agent name + status */}
      <div>
        <h3 className="text-sm font-semibold text-sc-fg-primary truncate">{agent.name}</h3>
        <div className="flex items-center gap-2 mt-1.5">
          <span
            className="inline-flex items-center gap-1 text-[10px] font-bold px-1.5 py-0.5 rounded border"
            style={{
              backgroundColor: `${typeConfig.color}20`,
              color: typeConfig.color,
              borderColor: `${typeConfig.color}40`,
            }}
          >
            {typeConfig.icon} {typeConfig.label}
          </span>
          <span
            className={`text-[10px] px-1.5 py-0.5 rounded ${statusConfig.bgClass} ${statusConfig.textClass}`}
          >
            {statusConfig.icon} {statusConfig.label}
          </span>
        </div>
      </div>

      {/* Current activity */}
      {agent.current_activity && (
        <p className="text-[11px] text-sc-fg-muted leading-relaxed line-clamp-2">
          {agent.current_activity}
        </p>
      )}

      {/* Metadata rows */}
      <div className="space-y-1.5 text-xs">
        {agent.tokens_used > 0 && (
          <div className="flex justify-between">
            <span className="text-sc-fg-subtle">Tokens</span>
            <span className="text-sc-fg-muted tabular-nums">
              {agent.tokens_used.toLocaleString()}
            </span>
          </div>
        )}
        {agent.cost_usd > 0 && (
          <div className="flex justify-between">
            <span className="text-sc-fg-subtle">Cost</span>
            <span className="text-sc-fg-muted tabular-nums">${agent.cost_usd.toFixed(4)}</span>
          </div>
        )}
        {agent.created_at && (
          <div className="flex justify-between">
            <span className="text-sc-fg-subtle">Created</span>
            <span className="text-sc-fg-muted">{formatDistanceToNow(agent.created_at)}</span>
          </div>
        )}
        {agent.last_heartbeat && (
          <div className="flex justify-between">
            <span className="text-sc-fg-subtle">Last active</span>
            <span className="text-sc-fg-muted">{formatDistanceToNow(agent.last_heartbeat)}</span>
          </div>
        )}
      </div>

      {/* Error */}
      {agent.error_message && (
        <div className="text-xs text-sc-red/90 bg-sc-red/10 px-2.5 py-1.5 rounded-lg line-clamp-3">
          {agent.error_message}
        </div>
      )}

      {/* Controls — only for active/paused agents */}
      {showControls && (
        <div className="flex gap-2 pt-1">
          {isActive && (
            <button
              type="button"
              onClick={() => pauseAgent.mutate({ id: agent.id })}
              disabled={pauseAgent.isPending}
              className="flex-1 text-[11px] font-medium py-1.5 rounded-lg bg-sc-yellow/10 text-sc-yellow border border-sc-yellow/20 hover:bg-sc-yellow/20 transition-colors disabled:opacity-50"
            >
              Pause
            </button>
          )}
          {isPaused && (
            <button
              type="button"
              onClick={() => resumeAgent.mutate(agent.id)}
              disabled={resumeAgent.isPending}
              className="flex-1 text-[11px] font-medium py-1.5 rounded-lg bg-sc-green/10 text-sc-green border border-sc-green/20 hover:bg-sc-green/20 transition-colors disabled:opacity-50"
            >
              Resume
            </button>
          )}
          <button
            type="button"
            onClick={() => terminateAgent.mutate({ id: agent.id })}
            disabled={terminateAgent.isPending}
            className="flex-1 text-[11px] font-medium py-1.5 rounded-lg bg-sc-red/10 text-sc-red border border-sc-red/20 hover:bg-sc-red/20 transition-colors disabled:opacity-50"
          >
            Stop
          </button>
        </div>
      )}
    </div>
  );
});

// =============================================================================
// Fleet Summary (no agent selected)
// =============================================================================

const FleetSummary = memo(function FleetSummary({ agents }: { agents: Agent[] }) {
  const statusBreakdown = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const agent of agents) {
      counts[agent.status] = (counts[agent.status] || 0) + 1;
    }
    return Object.entries(counts)
      .map(([status, count]) => ({
        status,
        count,
        config: AGENT_STATUS_CONFIG[status as AgentStatusType] ?? AGENT_STATUS_CONFIG.terminated,
      }))
      .sort((a, b) => b.count - a.count);
  }, [agents]);

  if (agents.length === 0) return null;

  return (
    <div className="space-y-2">
      <h3 className="text-xs font-medium text-sc-fg-subtle uppercase tracking-wider">
        Fleet Status
      </h3>
      <div className="space-y-1">
        {statusBreakdown.map(({ status, count, config }) => (
          <div key={status} className="flex items-center justify-between text-xs">
            <span className={config.textClass}>
              {config.icon} {config.label}
            </span>
            <span className="text-sc-fg-muted tabular-nums">{count}</span>
          </div>
        ))}
      </div>
    </div>
  );
});

// =============================================================================
// Inspector Panel — Context Pane (right)
//
// No selection: Global Activity Feed + fleet status breakdown
// Agent selected: Metadata + controls + agent-scoped approvals + activity
//
// Activity Feed's canonical home is HERE. Approval Queue lives in Dashboard.
// When an agent is selected, we show agent-scoped versions of both.
// =============================================================================

export function InspectorPanel({
  agent,
  agents = [],
  projectFilter,
  onSelectAgent,
}: InspectorPanelProps) {
  return (
    <div className="h-full flex flex-col bg-sc-bg-base overflow-y-auto">
      <div className="p-3 space-y-4">
        {agent ? (
          <>
            {/* Agent detail context */}
            <AgentMetadataCard agent={agent} />

            {/* Agent-scoped approvals (if any) */}
            <div className="border-t border-sc-fg-subtle/10 pt-3">
              <ApprovalQueue
                projectId={projectFilter}
                agentId={agent.id}
                onAgentClick={onSelectAgent}
                maxHeight="250px"
              />
            </div>

            {/* Agent-scoped activity */}
            <div className="border-t border-sc-fg-subtle/10 pt-3">
              <ActivityFeed
                projectId={projectFilter}
                agentId={agent.id}
                onAgentClick={onSelectAgent}
                maxHeight="400px"
              />
            </div>
          </>
        ) : (
          <>
            {/* Global context — no agent selected */}
            <FleetSummary agents={agents} />

            {/* Global Activity Feed — THE canonical home */}
            <div className="border-t border-sc-fg-subtle/10 pt-3">
              <ActivityFeed projectId={projectFilter} onAgentClick={onSelectAgent} />
            </div>
          </>
        )}
      </div>
    </div>
  );
}
