'use client';

/**
 * Agent header with status display and control buttons.
 */

import { Pause, Play, Square } from '@/components/ui/icons';
import type { Agent } from '@/lib/api';
import {
  AGENT_STATUS_CONFIG,
  AGENT_TYPE_CONFIG,
  type AgentStatusType,
  type AgentTypeValue,
} from '@/lib/constants';
import { usePauseAgent, useResumeAgent, useTerminateAgent } from '@/lib/hooks';

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

  return (
    <div className="shrink-0 flex items-center justify-between px-3 py-2 border-b border-sc-fg-subtle/20 bg-sc-bg-elevated">
      {/* Left: Icon + Name + Status */}
      <div className="flex items-center gap-2 min-w-0">
        <span
          className="text-sm transition-transform duration-200 hover:scale-110"
          style={{ color: typeConfig.color }}
        >
          {typeConfig.icon}
        </span>
        <span className="text-sm font-medium text-sc-fg-primary truncate">{agent.name}</span>
        <span
          className={`text-[10px] px-1.5 py-0.5 rounded transition-all duration-200 ${statusConfig.bgClass} ${statusConfig.textClass} ${
            isActive ? 'animate-pulse-glow' : ''
          }`}
        >
          {statusConfig.icon} {statusConfig.label}
        </span>
      </div>

      {/* Right: Controls with micro-interactions */}
      <div className="flex items-center gap-1 shrink-0">
        {isActive && (
          <button
            type="button"
            onClick={() => pauseAgent.mutate({ id: agent.id })}
            className="p-1.5 text-sc-yellow hover:bg-sc-yellow/10 rounded transition-all duration-200 hover:scale-110 active:scale-95 disabled:opacity-50 disabled:pointer-events-none"
            disabled={pauseAgent.isPending}
            title="Pause"
          >
            <Pause width={14} height={14} />
          </button>
        )}
        {isPaused && (
          <button
            type="button"
            onClick={() => resumeAgent.mutate(agent.id)}
            className="p-1.5 text-sc-green hover:bg-sc-green/10 rounded transition-all duration-200 hover:scale-110 active:scale-95 disabled:opacity-50 disabled:pointer-events-none"
            disabled={resumeAgent.isPending}
            title="Resume"
          >
            <Play width={14} height={14} />
          </button>
        )}
        {!isTerminal && (
          <button
            type="button"
            onClick={() => terminateAgent.mutate({ id: agent.id })}
            className="p-1.5 text-sc-red hover:bg-sc-red/10 rounded transition-all duration-200 hover:scale-110 active:scale-95 disabled:opacity-50 disabled:pointer-events-none"
            disabled={terminateAgent.isPending}
            title="Stop"
          >
            <Square width={14} height={14} />
          </button>
        )}
      </div>
    </div>
  );
}
