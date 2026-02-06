'use client';

import { memo, useMemo, useState } from 'react';
import { SpawnAgentDialog } from '@/components/agents/spawn-agent-dialog';
import { Plus, Search } from '@/components/ui/icons';
import type { Agent } from '@/lib/api';
import {
  AGENT_STATUS_CONFIG,
  AGENT_TYPE_CONFIG,
  type AgentStatusType,
  type AgentTypeValue,
} from '@/lib/constants';

// =============================================================================
// Types
// =============================================================================

interface AgentNavigatorProps {
  agents: Agent[];
  projects: Array<{ id: string; name: string }>;
  selectedAgentId: string | null;
  onSelectAgent: (id: string | null) => void;
  onSpawned: (id: string) => void;
}

// =============================================================================
// Status dot component
// =============================================================================

function StatusDot({ status, isActive }: { status: string; isActive: boolean }) {
  const config = AGENT_STATUS_CONFIG[status as AgentStatusType] ?? AGENT_STATUS_CONFIG.terminated;

  if (isActive) {
    return (
      <span className="relative flex h-2.5 w-2.5 shrink-0" title={config.label}>
        <span
          className="absolute inline-flex h-full w-full rounded-full opacity-75 animate-ping"
          style={{ backgroundColor: config.color }}
        />
        <span
          className="relative inline-flex h-2.5 w-2.5 rounded-full"
          style={{ backgroundColor: config.color }}
        />
      </span>
    );
  }

  if (status === 'waiting_approval') {
    return (
      <span className="relative flex h-2.5 w-2.5 shrink-0" title={config.label}>
        <span
          className="absolute inline-flex h-full w-full rounded-full opacity-50 animate-pulse"
          style={{ backgroundColor: config.color }}
        />
        <span
          className="relative inline-flex h-2.5 w-2.5 rounded-full"
          style={{ backgroundColor: config.color }}
        />
      </span>
    );
  }

  const isTerminal = ['completed', 'failed', 'terminated'].includes(status);

  return (
    <span
      className={`inline-flex h-2.5 w-2.5 rounded-full shrink-0 ${isTerminal ? 'opacity-50' : ''}`}
      style={{ backgroundColor: config.color }}
      title={config.label}
    />
  );
}

// =============================================================================
// Agent Row
// =============================================================================

const AgentRow = memo(function AgentRow({
  agent,
  isSelected,
  isActive,
  onSelect,
}: {
  agent: Agent;
  isSelected: boolean;
  isActive: boolean;
  onSelect: () => void;
}) {
  const typeConfig =
    AGENT_TYPE_CONFIG[agent.agent_type as AgentTypeValue] ?? AGENT_TYPE_CONFIG.general;

  return (
    <button
      type="button"
      onClick={onSelect}
      className={`
        w-full flex items-center gap-2.5 px-3 py-2 text-left text-sm rounded-lg
        transition-all duration-150 group
        ${
          isSelected
            ? 'bg-sc-purple/15 text-sc-fg-primary border-l-2 border-sc-purple'
            : 'text-sc-fg-muted hover:bg-sc-bg-elevated hover:text-sc-fg-primary border-l-2 border-transparent'
        }
      `}
    >
      <StatusDot status={agent.status} isActive={isActive} />
      <span className="flex-1 truncate text-xs font-medium">{agent.name}</span>
      {agent.tokens_used > 0 && (
        <span
          className="text-[10px] text-sc-fg-subtle shrink-0 tabular-nums opacity-0 group-hover:opacity-100 transition-opacity"
          title="Tokens used"
        >
          {agent.tokens_used >= 1000
            ? `${(agent.tokens_used / 1000).toFixed(0)}k`
            : agent.tokens_used}
        </span>
      )}
      <span
        className="text-[10px] shrink-0"
        style={{ color: typeConfig.color }}
        title={typeConfig.label}
      >
        {typeConfig.icon}
      </span>
    </button>
  );
});

// =============================================================================
// Project Group (collapsible)
// =============================================================================

const ProjectGroupNav = memo(function ProjectGroupNav({
  projectName,
  agents,
  selectedAgentId,
  onSelectAgent,
  isAgentActive,
}: {
  projectName: string;
  agents: Agent[];
  selectedAgentId: string | null;
  onSelectAgent: (id: string | null) => void;
  isAgentActive: (agent: Agent) => boolean;
}) {
  const [collapsed, setCollapsed] = useState(false);
  const activeCount = useMemo(() => agents.filter(isAgentActive).length, [agents, isAgentActive]);

  return (
    <div>
      <button
        type="button"
        onClick={() => setCollapsed(c => !c)}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-[11px] font-semibold uppercase tracking-wider text-sc-fg-subtle hover:text-sc-fg-muted transition-colors"
      >
        <span className={`transition-transform duration-150 ${collapsed ? '-rotate-90' : ''}`}>
          ▾
        </span>
        <span className="flex-1 truncate text-left">{projectName}</span>
        <span className="text-[10px] font-normal text-sc-fg-subtle tabular-nums">
          {agents.length}
        </span>
        {activeCount > 0 && (
          <span className="text-[10px] font-normal text-sc-purple tabular-nums">
            {activeCount}●
          </span>
        )}
      </button>
      {!collapsed && (
        <div className="space-y-0.5 mt-0.5">
          {agents.map(agent => (
            <AgentRow
              key={agent.id}
              agent={agent}
              isSelected={agent.id === selectedAgentId}
              isActive={isAgentActive(agent)}
              onSelect={() => onSelectAgent(agent.id === selectedAgentId ? null : agent.id)}
            />
          ))}
        </div>
      )}
    </div>
  );
});

// =============================================================================
// Agent Navigator
// =============================================================================

export function AgentNavigator({
  agents,
  projects,
  selectedAgentId,
  onSelectAgent,
  onSpawned,
}: AgentNavigatorProps) {
  const [searchQuery, setSearchQuery] = useState('');

  const isAgentActive = useMemo(() => {
    const twoMinutesAgo = Date.now() - 2 * 60 * 1000;
    return (agent: Agent) => {
      if (!agent.last_heartbeat) return false;
      return new Date(agent.last_heartbeat).getTime() >= twoMinutesAgo;
    };
  }, []);

  // Filter agents by search query
  const filteredAgents = useMemo(() => {
    if (!searchQuery.trim()) return agents;
    const q = searchQuery.toLowerCase();
    return agents.filter(
      a =>
        a.name.toLowerCase().includes(q) ||
        a.tags?.some(t => t.toLowerCase().includes(q)) ||
        a.current_activity?.toLowerCase().includes(q)
    );
  }, [agents, searchQuery]);

  // Sort: active first, then by recency
  const sortedAgents = useMemo(() => {
    return [...filteredAgents].sort((a, b) => {
      const aActive = isAgentActive(a);
      const bActive = isAgentActive(b);
      if (aActive && !bActive) return -1;
      if (!aActive && bActive) return 1;
      const aTime = a.last_heartbeat ? new Date(a.last_heartbeat).getTime() : 0;
      const bTime = b.last_heartbeat ? new Date(b.last_heartbeat).getTime() : 0;
      return bTime - aTime;
    });
  }, [filteredAgents, isAgentActive]);

  // Group by project
  const groupedByProject = useMemo(() => {
    const groups: Record<string, { name: string; agents: Agent[] }> = {};

    for (const agent of sortedAgents) {
      const projectId = agent.project_id || 'no-project';
      if (!groups[projectId]) {
        const project = projects.find(p => p.id === projectId);
        groups[projectId] = {
          name: project?.name ?? (projectId === 'no-project' ? 'No Project' : 'Unknown'),
          agents: [],
        };
      }
      groups[projectId].agents.push(agent);
    }

    // Sort groups: groups with active agents first
    return Object.entries(groups).sort((a, b) => {
      const aActive = a[1].agents.filter(isAgentActive).length;
      const bActive = b[1].agents.filter(isAgentActive).length;
      return bActive - aActive;
    });
  }, [sortedAgents, projects, isAgentActive]);

  return (
    <div className="h-full flex flex-col bg-sc-bg-base">
      {/* Header */}
      <div className="shrink-0 px-3 pt-3 pb-2 space-y-2">
        {/* Search */}
        <div className="relative">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-sc-fg-subtle" />
          <input
            type="text"
            placeholder="Search agents..."
            value={searchQuery}
            onChange={e => setSearchQuery(e.target.value)}
            className="w-full pl-8 pr-3 py-1.5 text-xs bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-lg text-sc-fg-primary placeholder:text-sc-fg-subtle outline-none focus:border-sc-purple/40 transition-colors"
          />
        </div>

        {/* Spawn button */}
        <SpawnAgentDialog
          trigger={
            <button
              type="button"
              className="w-full flex items-center justify-center gap-1.5 px-3 py-1.5 text-xs font-medium bg-sc-purple/10 hover:bg-sc-purple/20 text-sc-purple border border-sc-purple/20 rounded-lg transition-colors"
            >
              <Plus width={12} height={12} />
              Spawn Agent
            </button>
          }
          onSpawned={onSpawned}
        />
      </div>

      {/* Agent list */}
      <div className="flex-1 overflow-y-auto px-1 pb-2 space-y-1">
        {sortedAgents.length === 0 && (
          <div className="px-3 py-8 text-center text-xs text-sc-fg-subtle">
            {searchQuery ? 'No matches' : 'No agents'}
          </div>
        )}

        {groupedByProject.length === 1 ? (
          // Single project — skip the group header
          <div className="space-y-0.5">
            {groupedByProject[0][1].agents.map(agent => (
              <AgentRow
                key={agent.id}
                agent={agent}
                isSelected={agent.id === selectedAgentId}
                isActive={isAgentActive(agent)}
                onSelect={() => onSelectAgent(agent.id === selectedAgentId ? null : agent.id)}
              />
            ))}
          </div>
        ) : (
          groupedByProject.map(([projectId, { name, agents: groupAgents }]) => (
            <ProjectGroupNav
              key={projectId}
              projectName={name}
              agents={groupAgents}
              selectedAgentId={selectedAgentId}
              onSelectAgent={onSelectAgent}
              isAgentActive={isAgentActive}
            />
          ))
        )}
      </div>
    </div>
  );
}
