'use client';

import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { memo, Suspense, useCallback, useEffect, useMemo, useState } from 'react';
import { ActivityFeed } from '@/components/agents/activity-feed';
import { ApprovalQueue } from '@/components/agents/approval-queue';
import { SpawnAgentDialog } from '@/components/agents/spawn-agent-dialog';
import { Breadcrumb } from '@/components/layout/breadcrumb';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { AgentsEmptyState } from '@/components/ui/empty-state';
import {
  Archive,
  ChevronDown,
  Dashboard,
  EditPencil,
  Flash,
  List,
  Plus,
  StopCircle,
} from '@/components/ui/icons';
import { LoadingState } from '@/components/ui/spinner';
import { FilterChip } from '@/components/ui/toggle';
import { ErrorState } from '@/components/ui/tooltip';
import type { Agent, AgentStatus, TaskOrchestrator } from '@/lib/api';
import {
  AGENT_TYPE_CONFIG,
  type AgentTypeValue,
  formatDistanceToNow,
  ORCHESTRATOR_PHASE_CONFIG,
  type OrchestratorPhaseType,
} from '@/lib/constants';
import {
  useAgents,
  useArchiveAgent,
  useOrchestrators,
  useProjects,
  useRenameAgent,
  useTerminateAgent,
} from '@/lib/hooks';
import { useProjectFilter } from '@/lib/project-context';
import { readStorage, writeStorage } from '@/lib/storage';

// =============================================================================
// Agent Card Styling
// =============================================================================

// Status-based card styling for visual distinction
const AGENT_STATUS_STYLES: Record<
  string,
  { bg: string; border: string; accent: string; glow?: string }
> = {
  working: {
    bg: 'bg-gradient-to-br from-sc-purple/15 via-sc-bg-base to-sc-bg-base',
    border: 'border-sc-purple/40 hover:border-sc-purple/60',
    accent: 'bg-sc-purple',
    glow: 'shadow-[0_0_20px_rgba(225,53,255,0.15)]',
  },
  initializing: {
    bg: 'bg-gradient-to-br from-sc-cyan/10 via-sc-bg-base to-sc-bg-base',
    border: 'border-sc-cyan/30 hover:border-sc-cyan/50',
    accent: 'bg-sc-cyan',
  },
  resuming: {
    bg: 'bg-gradient-to-br from-sc-purple/10 via-sc-bg-base to-sc-bg-base',
    border: 'border-sc-purple/30 hover:border-sc-purple/50',
    accent: 'bg-sc-purple',
  },
  waiting_approval: {
    bg: 'bg-gradient-to-br from-sc-coral/15 via-sc-bg-base to-sc-bg-base',
    border: 'border-sc-coral/40 hover:border-sc-coral/60',
    accent: 'bg-sc-coral',
    glow: 'shadow-[0_0_20px_rgba(255,106,193,0.15)]',
  },
  waiting_dependency: {
    bg: 'bg-gradient-to-br from-sc-yellow/10 via-sc-bg-base to-sc-bg-base',
    border: 'border-sc-yellow/30 hover:border-sc-yellow/50',
    accent: 'bg-sc-yellow',
  },
  paused: {
    bg: 'bg-gradient-to-br from-sc-yellow/10 via-sc-bg-base to-sc-bg-base',
    border: 'border-sc-yellow/30 hover:border-sc-yellow/50',
    accent: 'bg-sc-yellow',
  },
  completed: {
    bg: 'bg-sc-bg-base',
    border: 'border-sc-green/20 hover:border-sc-green/40',
    accent: 'bg-sc-green',
  },
  failed: {
    bg: 'bg-gradient-to-br from-sc-red/10 via-sc-bg-base to-sc-bg-base',
    border: 'border-sc-red/30 hover:border-sc-red/50',
    accent: 'bg-sc-red',
  },
  terminated: {
    bg: 'bg-sc-bg-base',
    border: 'border-sc-fg-subtle/20 hover:border-sc-fg-subtle/40',
    accent: 'bg-sc-fg-subtle',
  },
};

// Tag category colors for visual distinction (matches task-card.tsx)
const TAG_STYLES: Record<string, string> = {
  // Agent types
  general: 'bg-sc-fg-subtle/15 text-sc-fg-muted border-sc-fg-subtle/30',
  planner: 'bg-sc-cyan/15 text-sc-cyan border-sc-cyan/30',
  implementer: 'bg-sc-purple/15 text-sc-purple border-sc-purple/30',
  tester: 'bg-sc-green/15 text-sc-green border-sc-green/30',
  reviewer: 'bg-sc-coral/15 text-sc-coral border-sc-coral/30',
  integrator: 'bg-sc-yellow/15 text-sc-yellow border-sc-yellow/30',
  orchestrator: 'bg-sc-purple/15 text-sc-purple border-sc-purple/30',
  // Domain tags
  frontend: 'bg-sc-cyan/15 text-sc-cyan border-sc-cyan/30',
  ui: 'bg-sc-cyan/15 text-sc-cyan border-sc-cyan/30',
  backend: 'bg-sc-purple/15 text-sc-purple border-sc-purple/30',
  api: 'bg-sc-purple/15 text-sc-purple border-sc-purple/30',
  database: 'bg-sc-coral/15 text-sc-coral border-sc-coral/30',
  devops: 'bg-sc-yellow/15 text-sc-yellow border-sc-yellow/30',
  security: 'bg-sc-red/15 text-sc-red border-sc-red/30',
  auth: 'bg-sc-red/15 text-sc-red border-sc-red/30',
  perf: 'bg-sc-coral/15 text-sc-coral border-sc-coral/30',
  docs: 'bg-sc-fg-subtle/15 text-sc-fg-muted border-sc-fg-subtle/30',
  // Action tags
  feature: 'bg-sc-green/15 text-sc-green border-sc-green/30',
  fix: 'bg-sc-red/15 text-sc-red border-sc-red/30',
  refactor: 'bg-sc-purple/15 text-sc-purple border-sc-purple/30',
  test: 'bg-sc-green/15 text-sc-green border-sc-green/30',
  migration: 'bg-sc-yellow/15 text-sc-yellow border-sc-yellow/30',
};

const DEFAULT_TAG_STYLE = 'bg-sc-bg-elevated text-sc-fg-muted border-sc-fg-subtle/20';

function getTagStyle(tag: string): string {
  return TAG_STYLES[tag.toLowerCase()] || DEFAULT_TAG_STYLE;
}

// =============================================================================
// Agent Card Component
// =============================================================================

const AgentCard = memo(function AgentCard({
  agent,
  orchestrator,
  onTerminate,
  onRename,
  onArchive,
}: {
  agent: Agent;
  orchestrator?: TaskOrchestrator;
  onTerminate: (id: string) => void;
  onRename: (id: string, name: string) => void;
  onArchive: (id: string) => void;
}) {
  const [isRenaming, setIsRenaming] = useState(false);
  const [newName, setNewName] = useState(agent.name);

  const typeConfig =
    AGENT_TYPE_CONFIG[agent.agent_type as AgentTypeValue] ?? AGENT_TYPE_CONFIG.general;

  // Simple: is it actively running right now? (recent heartbeat within 2 min)
  const isActive = (() => {
    if (!agent.last_heartbeat) return false;
    const lastBeat = new Date(agent.last_heartbeat).getTime();
    const twoMinutesAgo = Date.now() - 2 * 60 * 1000;
    return lastBeat >= twoMinutesAgo;
  })();

  // Is this a NEW agent? (created in last 5 minutes)
  const isNew = (() => {
    if (!agent.created_at) return false;
    const createdTime = new Date(agent.created_at).getTime();
    const fiveMinutesAgo = Date.now() - 5 * 60 * 1000;
    return createdTime >= fiveMinutesAgo;
  })();

  const needsApproval = agent.status === 'waiting_approval';
  const statusStyle = isActive
    ? AGENT_STATUS_STYLES.working
    : needsApproval
      ? AGENT_STATUS_STYLES.waiting_approval
      : AGENT_STATUS_STYLES.completed; // Inactive = muted styling

  const handleRename = () => {
    setIsRenaming(true);
  };

  const handleRenameSubmit = (e: React.SyntheticEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (newName !== agent.name) {
      onRename(agent.id, newName);
    }
    setIsRenaming(false);
  };

  const handleArchive = () => {
    if (confirm(`Archive "${agent.name}"? This cannot be undone.`)) {
      onArchive(agent.id);
    }
  };

  return (
    <Link
      href={`/agents/${agent.id}`}
      className={`
        group block relative rounded-xl overflow-hidden shadow-card
        transition-all duration-200
        hover:shadow-card-hover hover:-translate-y-0.5
        border ${statusStyle.border} ${statusStyle.bg}
        ${statusStyle.glow ?? ''}
        ${isNew ? 'ring-2 ring-sc-cyan/50 ring-offset-2 ring-offset-sc-bg-base' : ''}
      `}
    >
      {/* Status accent bar */}
      <div className={`absolute left-0 top-0 bottom-0 w-1 ${statusStyle.accent}`} />

      {/* NEW badge - top right corner */}
      {isNew && (
        <div className="absolute -top-1 -right-1 z-10">
          <span className="inline-flex items-center gap-0.5 text-[9px] font-bold px-2 py-0.5 rounded-full bg-sc-cyan text-sc-bg-dark shadow-lg animate-pulse">
            <Flash width={10} height={10} />
            NEW
          </span>
        </div>
      )}

      <div className="pl-4 pr-3 py-3">
        {/* Top row: Type badge + Status indicator + Actions */}
        <div className="flex items-center justify-between gap-2 mb-2">
          <div className="flex items-center gap-1.5 min-w-0 flex-1">
            {/* Type badge */}
            <span
              className="shrink-0 inline-flex items-center gap-1 text-[10px] font-bold px-1.5 py-0.5 rounded border"
              style={{
                backgroundColor: `${typeConfig.color}20`,
                color: typeConfig.color,
                borderColor: `${typeConfig.color}40`,
              }}
            >
              {typeConfig.icon} {typeConfig.label}
            </span>

            {/* Simple status: Active or Needs Approval */}
            {isActive && (
              <span className="shrink-0 text-[10px] px-1.5 py-0.5 rounded bg-sc-purple/20 text-sc-purple font-medium">
                ● Active
              </span>
            )}
            {needsApproval && (
              <span className="shrink-0 text-[10px] px-1.5 py-0.5 rounded bg-sc-coral/20 text-sc-coral font-medium">
                ⏳ Needs Approval
              </span>
            )}

            {/* Orchestrator phase badge */}
            {orchestrator && (
              <span
                className={`shrink-0 text-[10px] px-1.5 py-0.5 rounded ${ORCHESTRATOR_PHASE_CONFIG[orchestrator.current_phase as OrchestratorPhaseType]?.bgClass ?? 'bg-sc-fg-subtle/20'} ${ORCHESTRATOR_PHASE_CONFIG[orchestrator.current_phase as OrchestratorPhaseType]?.textClass ?? 'text-sc-fg-muted'}`}
              >
                {
                  ORCHESTRATOR_PHASE_CONFIG[orchestrator.current_phase as OrchestratorPhaseType]
                    ?.icon
                }{' '}
                {
                  ORCHESTRATOR_PHASE_CONFIG[orchestrator.current_phase as OrchestratorPhaseType]
                    ?.label
                }
                {orchestrator.rework_count > 0 && ` #${orchestrator.rework_count}`}
              </span>
            )}
          </div>

          {/* Actions Dropdown */}
          <DropdownMenu>
            <DropdownMenuTrigger
              onClick={e => e.preventDefault()}
              className="p-1 text-sc-fg-subtle hover:text-sc-cyan hover:bg-sc-cyan/10 rounded-md transition-all duration-200 opacity-0 group-hover:opacity-100"
            >
              <ChevronDown width={14} height={14} />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" onClick={e => e.stopPropagation()} className="w-44">
              <DropdownMenuItem onClick={handleRename} className="gap-2 text-xs">
                <EditPencil width={12} height={12} className="text-sc-cyan" />
                <span>Rename</span>
              </DropdownMenuItem>
              {isActive && (
                <>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem
                    destructive
                    onClick={() => onTerminate(agent.id)}
                    className="gap-2 text-xs"
                  >
                    <StopCircle width={12} height={12} />
                    <span>Stop</span>
                  </DropdownMenuItem>
                </>
              )}
              <DropdownMenuSeparator />
              <DropdownMenuItem onClick={handleArchive} className="gap-2 text-xs">
                <Archive width={12} height={12} className="text-sc-coral" />
                <span>Archive</span>
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>

        {/* Title */}
        {isRenaming ? (
          <input
            type="text"
            value={newName}
            onChange={e => setNewName(e.target.value)}
            onBlur={handleRenameSubmit}
            onKeyDown={e => {
              if (e.key === 'Enter') handleRenameSubmit(e);
              if (e.key === 'Escape') {
                setNewName(agent.name);
                setIsRenaming(false);
              }
            }}
            onClick={e => e.stopPropagation()}
            className="w-full text-sm font-medium text-sc-fg-primary bg-sc-bg-highlight border border-sc-purple/30 rounded px-2 py-1 outline-none focus:border-sc-purple"
            // biome-ignore lint/a11y/noAutofocus: User explicitly clicked rename
            autoFocus
          />
        ) : (
          <h3 className="text-sm font-medium text-sc-fg-primary line-clamp-2 leading-snug group-hover:text-white transition-colors">
            {agent.name}
          </h3>
        )}

        {/* Tags */}
        {agent.tags && agent.tags.length > 0 && (
          <div className="flex flex-wrap gap-1 mt-2">
            {agent.tags.slice(0, 4).map(tag => (
              <span
                key={tag}
                className={`text-[9px] px-1.5 py-0.5 rounded-full border font-medium ${getTagStyle(tag)}`}
              >
                {tag}
              </span>
            ))}
            {agent.tags.length > 4 && (
              <span className="text-[9px] px-1.5 py-0.5 rounded-full bg-sc-bg-elevated text-sc-fg-subtle">
                +{agent.tags.length - 4}
              </span>
            )}
          </div>
        )}

        {/* Error message */}
        {agent.error_message && (
          <p className="text-xs text-sc-red/90 line-clamp-1 mt-1.5 bg-sc-red/10 px-2 py-1 rounded">
            {agent.error_message}
          </p>
        )}

        {/* Active indicator with current activity */}
        {isActive && (
          <div className="flex items-start gap-2 mt-2 p-2 rounded-lg bg-sc-purple/10 border border-sc-purple/20">
            <span className="w-2 h-2 mt-1 shrink-0 rounded-full bg-sc-purple animate-pulse" />
            <div className="min-w-0 flex-1">
              {agent.current_activity ? (
                <p className="text-xs text-sc-fg-primary line-clamp-2 leading-relaxed">
                  {agent.current_activity}
                </p>
              ) : (
                <span className="text-xs text-sc-fg-muted italic">Working...</span>
              )}
            </div>
          </div>
        )}

        {/* Footer: Metrics */}
        <div className="flex items-center justify-between mt-3 pt-2 border-t border-sc-fg-subtle/10">
          <div className="flex items-center gap-3 text-[10px] text-sc-fg-muted">
            {agent.tokens_used > 0 && (
              <span className="flex items-center gap-1" title="Tokens used">
                <span className="opacity-60">◇</span>
                {agent.tokens_used.toLocaleString()}
              </span>
            )}
          </div>

          {/* Time indicator */}
          {agent.last_heartbeat && (
            <span className="text-[10px] text-sc-fg-subtle px-1.5 py-0.5 rounded bg-sc-bg-elevated">
              {formatDistanceToNow(agent.last_heartbeat)}
            </span>
          )}
        </div>
      </div>
    </Link>
  );
});

// =============================================================================
// Project Group Component
// =============================================================================

const ProjectGroup = memo(function ProjectGroup({
  projectName,
  agents,
  orchestratorsByWorker,
  onTerminate,
  onRename,
  onArchive,
}: {
  projectName: string;
  agents: Agent[];
  orchestratorsByWorker: Map<string, TaskOrchestrator>;
  onTerminate: (id: string) => void;
  onRename: (id: string, name: string) => void;
  onArchive: (id: string) => void;
}) {
  const activeCount = useMemo(() => {
    const twoMinutesAgo = Date.now() - 2 * 60 * 1000;
    return agents.filter(a => {
      if (!a.last_heartbeat) return false;
      return new Date(a.last_heartbeat).getTime() >= twoMinutesAgo;
    }).length;
  }, [agents]);

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
            orchestrator={orchestratorsByWorker.get(agent.id)}
            onTerminate={onTerminate}
            onRename={onRename}
            onArchive={onArchive}
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
  const { activeCount, needsApproval, newCount } = useMemo(() => {
    const twoMinutesAgo = Date.now() - 2 * 60 * 1000;
    const fiveMinutesAgo = Date.now() - 5 * 60 * 1000;
    let active = 0;
    let approval = 0;
    let newAgents = 0;
    for (const agent of agents) {
      if (agent.last_heartbeat) {
        const lastBeat = new Date(agent.last_heartbeat).getTime();
        if (lastBeat >= twoMinutesAgo) active++;
      }
      if (agent.status === 'waiting_approval') approval++;
      if (agent.created_at) {
        const createdTime = new Date(agent.created_at).getTime();
        if (createdTime >= fiveMinutesAgo) newAgents++;
      }
    }
    return { activeCount: active, needsApproval: approval, newCount: newAgents };
  }, [agents]);

  return (
    <div className="flex flex-wrap items-center gap-4 p-4 bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-lg">
      <div className="flex items-center gap-2">
        <span className="text-2xl font-bold text-sc-fg-primary">{agents.length}</span>
        <span className="text-sm text-sc-fg-muted">Total Agents</span>
      </div>
      <div className="h-6 w-px bg-sc-fg-subtle/20" />
      <div className="flex flex-wrap items-center gap-3 text-sm">
        {newCount > 0 && (
          <span className="flex items-center gap-1.5 px-2 py-1 rounded bg-sc-cyan/20 text-sc-cyan font-medium">
            <Flash width={12} height={12} />
            {newCount} new
          </span>
        )}
        {activeCount > 0 && (
          <span className="flex items-center gap-1.5 px-2 py-1 rounded bg-sc-purple/20 text-sc-purple">
            <span className="w-2 h-2 rounded-full bg-sc-purple animate-pulse" />
            {activeCount} active
          </span>
        )}
        {needsApproval > 0 && (
          <span className="flex items-center gap-1.5 px-2 py-1 rounded bg-sc-coral/20 text-sc-coral">
            {needsApproval} needs approval
          </span>
        )}
        {agents.length - activeCount > 0 && (
          <span className="flex items-center gap-1.5 px-2 py-1 rounded bg-sc-fg-subtle/10 text-sc-fg-muted">
            {agents.length - activeCount} inactive
          </span>
        )}
      </div>
    </div>
  );
});

// =============================================================================
// Main Page Content
// =============================================================================

type ViewMode = 'dashboard' | 'list';

function AgentsPageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();

  // Initialize view mode - always start with 'dashboard' for SSR consistency
  const [viewMode, setViewMode] = useState<ViewMode>('dashboard');

  // Sync view mode from localStorage after hydration
  // biome-ignore lint/correctness/useExhaustiveDependencies: only run on mount to restore stored preference
  useEffect(() => {
    const stored = readStorage<ViewMode>('agents:viewMode');
    if (stored && stored !== viewMode) {
      setViewMode(stored);
    }
  }, []);

  // Persist view mode preference
  useEffect(() => {
    writeStorage('agents:viewMode', viewMode);
  }, [viewMode]);

  const statusFilter = searchParams.get('status') as AgentStatus | null;
  const tagFilter = searchParams.get('tag');
  const projectFilter = useProjectFilter(); // From global selector

  const {
    data: agentsData,
    isLoading,
    error,
  } = useAgents({
    project: projectFilter,
    status: statusFilter ?? undefined,
  });
  const { data: projectsData, isLoading: projectsLoading } = useProjects();
  const { data: orchestratorsData } = useOrchestrators(projectFilter);

  // Create maps for quick lookups
  const orchestratorsByWorker = useMemo(() => {
    const map = new Map<string, TaskOrchestrator>();
    if (orchestratorsData?.orchestrators) {
      for (const orch of orchestratorsData.orchestrators) {
        if (orch.worker_id) {
          map.set(orch.worker_id, orch);
        }
      }
    }
    return map;
  }, [orchestratorsData]);

  // Agent mutations
  const terminateAgent = useTerminateAgent();
  const renameAgent = useRenameAgent();
  const archiveAgent = useArchiveAgent();

  const agents = agentsData?.agents ?? [];
  const projects = projectsData?.entities ?? [];

  // Extract unique tags from all agents for filter chips
  const allTags = useMemo(() => {
    const tagSet = new Set<string>();
    for (const agent of agents) {
      for (const tag of agent.tags ?? []) {
        tagSet.add(tag);
      }
    }
    return Array.from(tagSet).sort();
  }, [agents]);

  // Filter agents by tag if selected
  const filteredAgents = useMemo(() => {
    if (!tagFilter) return agents;
    return agents.filter(a => a.tags?.includes(tagFilter));
  }, [agents, tagFilter]);

  // All agents (no separation - orchestrated agents are just regular agents)
  const standaloneAgents = useMemo(() => filteredAgents, [filteredAgents]);

  // Helper: check if agent is active (recent heartbeat)
  const isAgentActive = useCallback((agent: Agent) => {
    if (!agent.last_heartbeat) return false;
    const lastBeat = new Date(agent.last_heartbeat).getTime();
    const twoMinutesAgo = Date.now() - 2 * 60 * 1000;
    return lastBeat >= twoMinutesAgo;
  }, []);

  // Helper: check if agent is new (created in last 5 minutes)
  const isAgentNew = useCallback((agent: Agent) => {
    if (!agent.created_at) return false;
    const createdTime = new Date(agent.created_at).getTime();
    const fiveMinutesAgo = Date.now() - 5 * 60 * 1000;
    return createdTime >= fiveMinutesAgo;
  }, []);

  // Sort agents: NEW first, then active, then by most recent activity
  const sortedAgents = useMemo(() => {
    return [...standaloneAgents].sort((a, b) => {
      const aNew = isAgentNew(a);
      const bNew = isAgentNew(b);
      // NEW agents first
      if (aNew && !bNew) return -1;
      if (!aNew && bNew) return 1;

      const aActive = isAgentActive(a);
      const bActive = isAgentActive(b);
      // Then active agents
      if (aActive && !bActive) return -1;
      if (!aActive && bActive) return 1;

      // Then by most recent heartbeat
      const aTime = a.last_heartbeat ? new Date(a.last_heartbeat).getTime() : 0;
      const bTime = b.last_heartbeat ? new Date(b.last_heartbeat).getTime() : 0;
      return bTime - aTime;
    });
  }, [standaloneAgents, isAgentActive, isAgentNew]);

  // Group sorted agents by project
  const standaloneByProject = useMemo(() => {
    const groups: Record<string, { name: string; agents: Agent[] }> = {};

    for (const agent of sortedAgents) {
      const projectId = agent.project_id || 'no-project';
      if (!groups[projectId]) {
        const project = projects.find(p => p.id === projectId);
        // Determine the display name:
        // - If project found: use project name
        // - If no project_id: "No Project"
        // - If projects still loading and has project_id: "Loading..."
        // - If projects loaded but not found: "Unknown Project"
        let groupName = 'No Project';
        if (projectId !== 'no-project') {
          if (project) {
            groupName = project.name;
          } else if (projectsLoading) {
            groupName = 'Loading...';
          } else {
            groupName = 'Unknown Project';
          }
        }
        groups[projectId] = {
          name: groupName,
          agents: [],
        };
      }
      groups[projectId].agents.push(agent);
    }

    // Sort groups by having active agents
    return Object.entries(groups).sort((a, b) => {
      const aActive = a[1].agents.filter(isAgentActive).length;
      const bActive = b[1].agents.filter(isAgentActive).length;
      return bActive - aActive;
    });
  }, [sortedAgents, projects, projectsLoading, isAgentActive]);

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

  const handleTagFilter = useCallback(
    (tag: string | null) => {
      const params = new URLSearchParams(searchParams);
      if (tag) {
        params.set('tag', tag);
      } else {
        params.delete('tag');
      }
      router.push(`/agents?${params.toString()}`);
    },
    [router, searchParams]
  );

  // Action handlers
  const handleTerminate = useCallback(
    (id: string) => {
      terminateAgent.mutate({ id });
    },
    [terminateAgent]
  );

  const handleRename = useCallback(
    (id: string, name: string) => {
      renameAgent.mutate({ id, name });
    },
    [renameAgent]
  );

  const handleArchive = useCallback(
    (id: string) => {
      archiveAgent.mutate(id);
    },
    [archiveAgent]
  );

  return (
    <div className="space-y-4 animate-fade-in">
      <div className="flex items-center justify-between">
        <Breadcrumb />
        <div className="flex items-center gap-3">
          {/* View Toggle */}
          <div className="relative grid grid-cols-2 bg-sc-bg-elevated border border-sc-purple/20 rounded-lg p-1 shadow-[0_2px_8px_rgba(0,0,0,0.2),0_0_16px_rgba(225,53,255,0.08)]">
            <button
              type="button"
              onClick={() => setViewMode('dashboard')}
              className={`
                relative flex items-center justify-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-md
                transition-all duration-200 z-10
                ${
                  viewMode === 'dashboard'
                    ? 'text-white'
                    : 'text-sc-fg-muted hover:text-sc-fg-primary'
                }
              `}
            >
              <Dashboard width={14} height={14} />
              <span className="hidden sm:inline">Dashboard</span>
            </button>
            <button
              type="button"
              onClick={() => setViewMode('list')}
              className={`
                relative flex items-center justify-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-md
                transition-all duration-200 z-10
                ${viewMode === 'list' ? 'text-white' : 'text-sc-fg-muted hover:text-sc-fg-primary'}
              `}
            >
              <List width={14} height={14} />
              <span className="hidden sm:inline">Agents</span>
            </button>
            {/* Sliding background */}
            <div
              className={`
                absolute top-1 bottom-1 w-[calc(50%-0.25rem)] rounded-md z-0
                bg-gradient-to-br from-sc-purple to-sc-purple/80
                shadow-[0_2px_8px_rgba(225,53,255,0.3),0_0_16px_rgba(225,53,255,0.2)]
                transition-all duration-300 ease-out
                ${viewMode === 'dashboard' ? 'left-1' : 'left-[calc(50%+0.125rem)]'}
              `}
            />
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
            {/* Left Column: Activity */}
            <div className="space-y-6">
              <ActivityFeed projectId={projectFilter} maxHeight="600px" />
            </div>

            {/* Right Column: Approvals */}
            <div>
              <ApprovalQueue projectId={projectFilter} maxHeight="600px" />
            </div>
          </div>

          {/* Quick Agent List */}
          {agents.length > 0 && (
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <h2 className="text-sm font-medium text-sc-fg-primary">Recent Agents</h2>
                <button
                  type="button"
                  onClick={() => setViewMode('list')}
                  className="text-xs text-sc-purple hover:text-sc-purple/80"
                >
                  View all →
                </button>
              </div>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {[...agents]
                  .sort((a, b) => {
                    const fiveMinutesAgo = Date.now() - 5 * 60 * 1000;
                    const twoMinutesAgo = Date.now() - 2 * 60 * 1000;

                    // NEW agents first
                    const aNew = a.created_at && new Date(a.created_at).getTime() >= fiveMinutesAgo;
                    const bNew = b.created_at && new Date(b.created_at).getTime() >= fiveMinutesAgo;
                    if (aNew && !bNew) return -1;
                    if (!aNew && bNew) return 1;

                    // Then active agents
                    const aActive =
                      a.last_heartbeat && new Date(a.last_heartbeat).getTime() >= twoMinutesAgo;
                    const bActive =
                      b.last_heartbeat && new Date(b.last_heartbeat).getTime() >= twoMinutesAgo;
                    if (aActive && !bActive) return -1;
                    if (!aActive && bActive) return 1;

                    // Then by most recent activity
                    const aTime = a.last_heartbeat ? new Date(a.last_heartbeat).getTime() : 0;
                    const bTime = b.last_heartbeat ? new Date(b.last_heartbeat).getTime() : 0;
                    return bTime - aTime;
                  })
                  .slice(0, 6)
                  .map(agent => (
                    <AgentCard
                      key={agent.id}
                      agent={agent}
                      orchestrator={orchestratorsByWorker.get(agent.id)}
                      onTerminate={handleTerminate}
                      onRename={handleRename}
                      onArchive={handleArchive}
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

          {/* Tag Filter */}
          {allTags.length > 0 && (
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs text-sc-fg-subtle font-medium">Tags:</span>
              <FilterChip active={!tagFilter} onClick={() => handleTagFilter(null)}>
                All
              </FilterChip>
              {allTags.slice(0, 12).map(tag => (
                <FilterChip
                  key={tag}
                  active={tagFilter === tag}
                  onClick={() => handleTagFilter(tag)}
                >
                  {tag}
                </FilterChip>
              ))}
              {allTags.length > 12 && (
                <span className="text-xs text-sc-fg-muted">+{allTags.length - 12} more</span>
              )}
            </div>
          )}

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
          ) : filteredAgents.length === 0 ? (
            <div className="py-12 text-center text-sc-fg-muted">
              No agents match the selected filters
            </div>
          ) : (
            <div className="space-y-4">
              {/* All Agents by Project */}
              {standaloneByProject.map(([projectId, { name, agents: projectAgents }]) => (
                <ProjectGroup
                  key={projectId}
                  projectName={name}
                  agents={projectAgents}
                  orchestratorsByWorker={orchestratorsByWorker}
                  onTerminate={handleTerminate}
                  onRename={handleRename}
                  onArchive={handleArchive}
                />
              ))}
            </div>
          )}
        </>
      )}

      {/* Loading indicator for mutations */}
      {terminateAgent.isPending && (
        <div className="fixed bottom-4 right-4 bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-lg px-4 py-2 text-sm text-sc-fg-muted shadow-lg">
          Stopping agent...
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
