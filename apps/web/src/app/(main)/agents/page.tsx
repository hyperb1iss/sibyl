'use client';

import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { memo, Suspense, useCallback, useMemo, useState } from 'react';
import { ActivityFeed } from '@/components/agents/activity-feed';
import { ApprovalQueue } from '@/components/agents/approval-queue';
import { HealthMonitor } from '@/components/agents/health-monitor';
import { SpawnAgentDialog } from '@/components/agents/spawn-agent-dialog';
import { Breadcrumb } from '@/components/layout/breadcrumb';
import { Dashboard, List, Plus } from '@/components/ui/icons';
import { LoadingState } from '@/components/ui/spinner';
import { FilterChip } from '@/components/ui/toggle';
import { ErrorState } from '@/components/ui/tooltip';
import type { Agent, AgentStatus } from '@/lib/api';
import {
  AGENT_STATUS_CONFIG,
  AGENT_TYPE_CONFIG,
  type AgentStatusType,
  type AgentTypeValue,
  formatDistanceToNow,
} from '@/lib/constants';
import {
  useAgents,
  usePauseAgent,
  useProjects,
  useResumeAgent,
  useTerminateAgent,
} from '@/lib/hooks';
import { useProjectFilter } from '@/lib/project-context';

// =============================================================================
// Agent Card Component
// =============================================================================

const AgentCard = memo(function AgentCard({
  agent,
  onPause,
  onResume,
  onTerminate,
}: {
  agent: Agent;
  onPause: (id: string) => void;
  onResume: (id: string) => void;
  onTerminate: (id: string) => void;
}) {
  const statusConfig =
    AGENT_STATUS_CONFIG[agent.status as AgentStatusType] ?? AGENT_STATUS_CONFIG.working;
  const typeConfig =
    AGENT_TYPE_CONFIG[agent.agent_type as AgentTypeValue] ?? AGENT_TYPE_CONFIG.general;

  const isActive = ['initializing', 'working', 'resuming'].includes(agent.status);
  const isPaused = agent.status === 'paused';
  const isWaiting = ['waiting_approval', 'waiting_dependency'].includes(agent.status);
  const isTerminal = ['completed', 'failed', 'terminated'].includes(agent.status);

  return (
    <Link
      href={`/agents/${agent.id}`}
      className="block bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-lg p-4 hover:border-sc-purple/30 transition-colors"
    >
      {/* Header: Name + Status */}
      <div className="flex items-start justify-between gap-3 mb-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-sm font-medium" style={{ color: typeConfig.color }}>
              {typeConfig.icon}
            </span>
            <h3 className="text-sm font-medium text-sc-fg-primary truncate">{agent.name}</h3>
          </div>
          <div className="flex items-center gap-2 text-xs text-sc-fg-muted">
            <span
              className={`px-1.5 py-0.5 rounded ${statusConfig.bgClass} ${statusConfig.textClass}`}
            >
              {statusConfig.icon} {statusConfig.label}
            </span>
            <span className="px-1.5 py-0.5 rounded bg-sc-bg-highlight text-sc-fg-muted">
              {typeConfig.label}
            </span>
          </div>
        </div>

        {/* Action Buttons - stop propagation to prevent navigation */}
        <div className="flex items-center gap-1 shrink-0" onClick={e => e.preventDefault()}>
          {isActive && (
            <button
              type="button"
              onClick={e => {
                e.stopPropagation();
                onPause(agent.id);
              }}
              className="p-1.5 text-sc-fg-muted hover:text-sc-yellow hover:bg-sc-yellow/10 rounded transition-colors"
              title="Pause agent"
            >
              <span className="text-xs">‖</span>
            </button>
          )}
          {isPaused && (
            <button
              type="button"
              onClick={e => {
                e.stopPropagation();
                onResume(agent.id);
              }}
              className="p-1.5 text-sc-fg-muted hover:text-sc-green hover:bg-sc-green/10 rounded transition-colors"
              title="Resume agent"
            >
              <span className="text-xs">▶</span>
            </button>
          )}
          {!isTerminal && (
            <button
              type="button"
              onClick={e => {
                e.stopPropagation();
                onTerminate(agent.id);
              }}
              className="p-1.5 text-sc-fg-muted hover:text-sc-red hover:bg-sc-red/10 rounded transition-colors"
              title="Terminate agent"
            >
              <span className="text-xs">✕</span>
            </button>
          )}
        </div>
      </div>

      {/* Metrics Row */}
      <div className="flex items-center gap-4 text-xs text-sc-fg-muted">
        {agent.tokens_used > 0 && (
          <span title="Tokens used">{agent.tokens_used.toLocaleString()} tokens</span>
        )}
        {agent.cost_usd > 0 && <span title="Cost">${agent.cost_usd.toFixed(4)}</span>}
        {agent.last_heartbeat && (
          <span title="Last heartbeat">{formatDistanceToNow(agent.last_heartbeat)}</span>
        )}
      </div>

      {/* Error message */}
      {agent.error_message && (
        <div className="mt-2 text-xs text-sc-red bg-sc-red/10 px-2 py-1 rounded">
          {agent.error_message}
        </div>
      )}

      {/* Working indicator */}
      {isActive && (
        <div className="mt-2 flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-sc-purple animate-pulse" />
          <span className="text-xs text-sc-fg-muted">Working...</span>
        </div>
      )}

      {/* Waiting indicator */}
      {isWaiting && (
        <div className="mt-2 flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-sc-coral" />
          <span className="text-xs text-sc-fg-muted">
            {agent.status === 'waiting_approval' ? 'Needs approval' : 'Waiting on dependency'}
          </span>
        </div>
      )}
    </Link>
  );
});

// =============================================================================
// Project Group Component
// =============================================================================

const ProjectGroup = memo(function ProjectGroup({
  projectName,
  agents,
  onPause,
  onResume,
  onTerminate,
}: {
  projectName: string;
  agents: Agent[];
  onPause: (id: string) => void;
  onResume: (id: string) => void;
  onTerminate: (id: string) => void;
}) {
  const activeCount = agents.filter(a =>
    ['initializing', 'working', 'resuming', 'waiting_approval', 'waiting_dependency'].includes(
      a.status
    )
  ).length;

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <h2 className="text-sm font-medium text-sc-fg-primary">{projectName}</h2>
        <span className="text-xs text-sc-fg-muted">({agents.length} agents)</span>
        {activeCount > 0 && (
          <span className="text-xs px-1.5 py-0.5 rounded bg-sc-purple/20 text-sc-purple">
            {activeCount} active
          </span>
        )}
      </div>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {agents.map(agent => (
          <AgentCard
            key={agent.id}
            agent={agent}
            onPause={onPause}
            onResume={onResume}
            onTerminate={onTerminate}
          />
        ))}
      </div>
    </div>
  );
});

// =============================================================================
// Summary Bar Component
// =============================================================================

const SummaryBar = memo(function SummaryBar({ agents }: { agents: Agent[] }) {
  const counts = useMemo(() => {
    const result: Record<string, number> = {};
    for (const agent of agents) {
      result[agent.status] = (result[agent.status] || 0) + 1;
    }
    return result;
  }, [agents]);

  const totalActive = (counts.initializing || 0) + (counts.working || 0) + (counts.resuming || 0);
  const totalWaiting = (counts.waiting_approval || 0) + (counts.waiting_dependency || 0);
  const totalPaused = counts.paused || 0;
  const totalCompleted = counts.completed || 0;
  const totalFailed = (counts.failed || 0) + (counts.terminated || 0);

  return (
    <div className="flex flex-wrap items-center gap-4 p-4 bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-lg">
      <div className="flex items-center gap-2">
        <span className="text-2xl font-bold text-sc-fg-primary">{agents.length}</span>
        <span className="text-sm text-sc-fg-muted">Total Agents</span>
      </div>
      <div className="h-6 w-px bg-sc-fg-subtle/20" />
      <div className="flex flex-wrap items-center gap-3 text-sm">
        {totalActive > 0 && (
          <span className="flex items-center gap-1.5 px-2 py-1 rounded bg-sc-purple/20 text-sc-purple">
            <span className="w-2 h-2 rounded-full bg-sc-purple animate-pulse" />
            {totalActive} active
          </span>
        )}
        {totalWaiting > 0 && (
          <span className="flex items-center gap-1.5 px-2 py-1 rounded bg-sc-coral/20 text-sc-coral">
            {totalWaiting} waiting
          </span>
        )}
        {totalPaused > 0 && (
          <span className="flex items-center gap-1.5 px-2 py-1 rounded bg-sc-yellow/20 text-sc-yellow">
            {totalPaused} paused
          </span>
        )}
        {totalCompleted > 0 && (
          <span className="flex items-center gap-1.5 px-2 py-1 rounded bg-sc-green/20 text-sc-green">
            {totalCompleted} completed
          </span>
        )}
        {totalFailed > 0 && (
          <span className="flex items-center gap-1.5 px-2 py-1 rounded bg-sc-red/20 text-sc-red">
            {totalFailed} failed
          </span>
        )}
      </div>
    </div>
  );
});

// =============================================================================
// Empty State
// =============================================================================

function AgentsEmptyState() {
  return (
    <div className="flex flex-col items-center justify-center py-16 px-4 text-center">
      <div className="w-16 h-16 rounded-full bg-sc-purple/20 flex items-center justify-center mb-4">
        <span className="text-2xl">◎</span>
      </div>
      <h3 className="text-lg font-medium text-sc-fg-primary mb-2">No agents yet</h3>
      <p className="text-sm text-sc-fg-muted max-w-md">
        Click <span className="text-sc-purple font-medium">Start Agent</span> to spawn an AI agent
        that will work autonomously on your project.
      </p>
    </div>
  );
}

// =============================================================================
// Main Page Content
// =============================================================================

type ViewMode = 'dashboard' | 'list';

function AgentsPageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [viewMode, setViewMode] = useState<ViewMode>('dashboard');

  const statusFilter = searchParams.get('status') as AgentStatus | null;
  const projectFilter = useProjectFilter(); // From global selector

  const {
    data: agentsData,
    isLoading,
    error,
  } = useAgents({
    project: projectFilter,
    status: statusFilter ?? undefined,
  });
  const { data: projectsData } = useProjects();

  const pauseAgent = usePauseAgent();
  const resumeAgent = useResumeAgent();
  const terminateAgent = useTerminateAgent();

  const agents = agentsData?.agents ?? [];
  const projects = projectsData?.entities ?? [];

  // Group agents by project
  const agentsByProject = useMemo(() => {
    const groups: Record<string, { name: string; agents: Agent[] }> = {};

    for (const agent of agents) {
      const projectId = agent.project_id || 'no-project';
      if (!groups[projectId]) {
        const project = projects.find(p => p.id === projectId);
        groups[projectId] = {
          name: project?.name || 'No Project',
          agents: [],
        };
      }
      groups[projectId].agents.push(agent);
    }

    // Sort by active agents first
    return Object.entries(groups).sort((a, b) => {
      const aActive = a[1].agents.filter(ag =>
        ['working', 'initializing'].includes(ag.status)
      ).length;
      const bActive = b[1].agents.filter(ag =>
        ['working', 'initializing'].includes(ag.status)
      ).length;
      return bActive - aActive;
    });
  }, [agents, projects]);

  // Filter handlers
  const handleStatusFilter = useCallback(
    (status: AgentStatus | null) => {
      const params = new URLSearchParams(searchParams);
      if (status) {
        params.set('status', status);
      } else {
        params.delete('status');
      }
      router.push(`/agents?${params.toString()}`);
    },
    [router, searchParams]
  );

  // Action handlers
  const handlePause = useCallback(
    (id: string) => {
      pauseAgent.mutate({ id });
    },
    [pauseAgent]
  );

  const handleResume = useCallback(
    (id: string) => {
      resumeAgent.mutate(id);
    },
    [resumeAgent]
  );

  const handleTerminate = useCallback(
    (id: string) => {
      terminateAgent.mutate({ id });
    },
    [terminateAgent]
  );

  return (
    <div className="space-y-4 animate-fade-in">
      <div className="flex items-center justify-between">
        <Breadcrumb />
        <div className="flex items-center gap-3">
          {/* View Toggle */}
          <div className="flex items-center gap-1 bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-lg p-1">
            <button
              type="button"
              onClick={() => setViewMode('dashboard')}
              className={`flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded transition-colors ${
                viewMode === 'dashboard'
                  ? 'bg-sc-purple text-white'
                  : 'text-sc-fg-muted hover:text-sc-fg-primary'
              }`}
            >
              <Dashboard width={14} height={14} />
              Dashboard
            </button>
            <button
              type="button"
              onClick={() => setViewMode('list')}
              className={`flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded transition-colors ${
                viewMode === 'list'
                  ? 'bg-sc-purple text-white'
                  : 'text-sc-fg-muted hover:text-sc-fg-primary'
              }`}
            >
              <List width={14} height={14} />
              List
            </button>
          </div>

          <SpawnAgentDialog
            trigger={
              <button
                type="button"
                className="flex items-center gap-2 px-4 py-2 text-sm font-medium bg-sc-purple hover:bg-sc-purple/80 text-white rounded-lg transition-colors"
              >
                <Plus width={16} height={16} />
                Start Agent
              </button>
            }
            onSpawned={id => {
              router.push(`/agents/${id}`);
            }}
          />
        </div>
      </div>

      {/* Dashboard View */}
      {viewMode === 'dashboard' && (
        <div className="space-y-6">
          {/* Summary Bar */}
          {agents.length > 0 && <SummaryBar agents={agents} />}

          {/* Dashboard Grid */}
          <div className="grid gap-6 lg:grid-cols-2">
            {/* Left Column: Health + Activity */}
            <div className="space-y-6">
              <HealthMonitor projectId={projectFilter} maxHeight="280px" />
              <ActivityFeed projectId={projectFilter} maxHeight="320px" />
            </div>

            {/* Right Column: Approvals */}
            <div>
              <ApprovalQueue projectId={projectFilter} maxHeight="640px" />
            </div>
          </div>

          {/* Quick Agent List */}
          {agents.length > 0 && (
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <h2 className="text-sm font-medium text-sc-fg-primary">Active Agents</h2>
                <button
                  type="button"
                  onClick={() => setViewMode('list')}
                  className="text-xs text-sc-purple hover:text-sc-purple/80"
                >
                  View all →
                </button>
              </div>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {agents
                  .filter(a =>
                    [
                      'initializing',
                      'working',
                      'resuming',
                      'waiting_approval',
                      'waiting_dependency',
                      'paused',
                    ].includes(a.status)
                  )
                  .slice(0, 6)
                  .map(agent => (
                    <AgentCard
                      key={agent.id}
                      agent={agent}
                      onPause={handlePause}
                      onResume={handleResume}
                      onTerminate={handleTerminate}
                    />
                  ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* List View */}
      {viewMode === 'list' && (
        <>
          {/* Summary Bar */}
          {agents.length > 0 && <SummaryBar agents={agents} />}

          {/* Status Filter */}
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs text-sc-fg-subtle font-medium">Status:</span>
            <FilterChip active={!statusFilter} onClick={() => handleStatusFilter(null)}>
              All
            </FilterChip>
            <FilterChip
              active={statusFilter === 'working'}
              onClick={() => handleStatusFilter('working')}
            >
              Active
            </FilterChip>
            <FilterChip
              active={statusFilter === 'paused'}
              onClick={() => handleStatusFilter('paused')}
            >
              Paused
            </FilterChip>
            <FilterChip
              active={statusFilter === 'waiting_approval'}
              onClick={() => handleStatusFilter('waiting_approval')}
            >
              Needs Approval
            </FilterChip>
            <FilterChip
              active={statusFilter === 'completed'}
              onClick={() => handleStatusFilter('completed')}
            >
              Completed
            </FilterChip>
          </div>

          {/* Content */}
          {isLoading ? (
            <LoadingState />
          ) : error ? (
            <ErrorState
              title="Failed to load agents"
              message={error instanceof Error ? error.message : 'Unknown error'}
            />
          ) : agents.length === 0 ? (
            <AgentsEmptyState />
          ) : (
            <div className="space-y-6">
              {agentsByProject.map(([projectId, { name, agents: projectAgents }]) => (
                <ProjectGroup
                  key={projectId}
                  projectName={name}
                  agents={projectAgents}
                  onPause={handlePause}
                  onResume={handleResume}
                  onTerminate={handleTerminate}
                />
              ))}
            </div>
          )}
        </>
      )}

      {/* Loading indicator for mutations */}
      {(pauseAgent.isPending || resumeAgent.isPending || terminateAgent.isPending) && (
        <div className="fixed bottom-4 right-4 bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-lg px-4 py-2 text-sm text-sc-fg-muted shadow-lg">
          Updating agent...
        </div>
      )}
    </div>
  );
}

// =============================================================================
// Page Export
// =============================================================================

export default function AgentsPage() {
  return (
    <Suspense fallback={<LoadingState />}>
      <AgentsPageContent />
    </Suspense>
  );
}
