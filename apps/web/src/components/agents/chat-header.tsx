'use client';

/**
 * Agent header with status display and control buttons.
 */

import { Pause, Play, Square, WarningCircle } from '@/components/ui/icons';
import type { Agent } from '@/lib/api';
import {
  AGENT_STATUS_CONFIG,
  AGENT_TYPE_CONFIG,
  type AgentStatusType,
  type AgentTypeValue,
} from '@/lib/constants';
import { usePauseAgent, useResumeAgent, useTerminateAgent } from '@/lib/hooks';

// Heartbeat thresholds (matching backend)
const STALE_THRESHOLD_SECONDS = 120; // 2 minutes
const UNRESPONSIVE_THRESHOLD_SECONDS = 600; // 10 minutes

/** Check if agent is a zombie (says working but no heartbeat) */
function getHeartbeatStatus(agent: Agent): 'healthy' | 'stale' | 'unresponsive' | null {
  // waiting_approval is expected to not heartbeat - worker is blocked on Redis pubsub
  // Only check heartbeat health for actively running states
  const isSupposedlyActive = ['initializing', 'working', 'resuming'].includes(agent.status);
  if (!isSupposedlyActive) return null;

  if (!agent.last_heartbeat) {
    // Grace period for new agents
    if (agent.started_at) {
      const startedAt = new Date(agent.started_at);
      const secondsSinceStart = (Date.now() - startedAt.getTime()) / 1000;
      if (secondsSinceStart < STALE_THRESHOLD_SECONDS) return 'healthy';
    }
    // Never heartbeated and past grace period
    return 'unresponsive';
  }

  const lastHeartbeat = new Date(agent.last_heartbeat);
  const secondsSince = (Date.now() - lastHeartbeat.getTime()) / 1000;

  if (secondsSince > UNRESPONSIVE_THRESHOLD_SECONDS) return 'unresponsive';
  if (secondsSince > STALE_THRESHOLD_SECONDS) return 'stale';
  return 'healthy';
}

// =============================================================================
// AgentHeader
// =============================================================================

export interface AgentHeaderProps {
  agent: Agent;
}

export function AgentHeader({ agent }: AgentHeaderProps) {
  const pauseAgent = usePauseAgent();
  const resumeAgent = useResumeAgent();
  const terminateAgent = useTerminateAgent();

  const statusConfig =
    AGENT_STATUS_CONFIG[agent.status as AgentStatusType] ?? AGENT_STATUS_CONFIG.working;
  const typeConfig =
    AGENT_TYPE_CONFIG[agent.agent_type as AgentTypeValue] ?? AGENT_TYPE_CONFIG.general;

  const isActive = ['initializing', 'working', 'resuming'].includes(agent.status);
  const isPaused = agent.status === 'paused';
  const isTerminal = ['completed', 'failed', 'terminated'].includes(agent.status);

  // Check for zombie state
  const heartbeatStatus = getHeartbeatStatus(agent);
  const isZombie = heartbeatStatus === 'unresponsive' || heartbeatStatus === 'stale';

  return (
    <div className="shrink-0 flex flex-col border-b border-sc-fg-subtle/20 bg-sc-bg-elevated">
      <div className="flex items-center justify-between px-3 py-2">
        {/* Left: Icon + Name + Status */}
        <div className="flex items-center gap-2 min-w-0">
          <span
            className="text-sm transition-transform duration-200 hover:scale-110"
            style={{ color: typeConfig.color }}
          >
            {typeConfig.icon}
          </span>
          <span className="text-sm font-medium text-sc-fg-primary truncate">{agent.name}</span>
          {isZombie ? (
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-sc-red/20 text-sc-red flex items-center gap-1" role="status">
              <WarningCircle width={10} height={10} aria-hidden="true" />
              {heartbeatStatus === 'unresponsive' ? 'Unresponsive' : 'Stale'}
            </span>
          ) : (
            <span
              role="status"
              className={`text-[10px] px-1.5 py-0.5 rounded transition-all duration-200 ${statusConfig.bgClass} ${statusConfig.textClass} ${
                isActive ? 'animate-pulse-glow' : ''
              }`}
            >
              {statusConfig.icon} {statusConfig.label}
            </span>
          )}
        </div>

        {/* Right: Controls with micro-interactions */}
        <div className="flex items-center gap-1 shrink-0">
          {isActive && !isZombie && (
            <button
              type="button"
              onClick={() => pauseAgent.mutate({ id: agent.id })}
              className="p-1.5 text-sc-yellow hover:bg-sc-yellow/10 rounded transition-all duration-200 hover:scale-110 active:scale-95 disabled:opacity-50 disabled:pointer-events-none"
              disabled={pauseAgent.isPending}
              aria-label="Pause agent"
            >
              <Pause width={14} height={14} aria-hidden="true" />
            </button>
          )}
          {(isPaused || isTerminal) && (
            <button
              type="button"
              onClick={() => resumeAgent.mutate(agent.id)}
              className="p-1.5 text-sc-green hover:bg-sc-green/10 rounded transition-all duration-200 hover:scale-110 active:scale-95 disabled:opacity-50 disabled:pointer-events-none"
              disabled={resumeAgent.isPending}
              aria-label={isTerminal ? 'Continue session' : 'Resume agent'}
            >
              <Play width={14} height={14} aria-hidden="true" />
            </button>
          )}
          {!isTerminal && (
            <button
              type="button"
              onClick={() => terminateAgent.mutate({ id: agent.id })}
              className="p-1.5 text-sc-red hover:bg-sc-red/10 rounded transition-all duration-200 hover:scale-110 active:scale-95 disabled:opacity-50 disabled:pointer-events-none"
              disabled={terminateAgent.isPending}
              aria-label="Stop agent"
            >
              <Square width={14} height={14} aria-hidden="true" />
            </button>
          )}
        </div>
      </div>

      {/* Zombie warning banner */}
      {isZombie && (
        <div role="alert" className="px-3 py-2 bg-sc-red/10 border-t border-sc-red/20 flex items-center justify-between gap-2">
          <span className="text-xs text-sc-red">
            This agent appears to be dead. No heartbeat for{' '}
            {agent.last_heartbeat
              ? `${Math.round((Date.now() - new Date(agent.last_heartbeat).getTime()) / 60000)} min`
              : 'unknown time'}
            .
          </span>
          <button
            type="button"
            onClick={() => terminateAgent.mutate({ id: agent.id, reason: 'zombie_cleanup' })}
            disabled={terminateAgent.isPending}
            aria-label="Mark agent as failed"
            className="text-xs px-2 py-1 bg-sc-red/20 hover:bg-sc-red/30 text-sc-red rounded transition-colors disabled:opacity-50"
          >
            {terminateAgent.isPending ? 'Terminating...' : 'Mark as Failed'}
          </button>
        </div>
      )}
    </div>
  );
}
