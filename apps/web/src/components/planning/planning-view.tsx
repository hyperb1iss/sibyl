'use client';

import Link from 'next/link';
import { useCallback, useMemo, useState } from 'react';
import { LightBulb, Plus, Search, Trash, X } from '@/components/ui/icons';
import { LoadingState } from '@/components/ui/spinner';
import { FilterChip } from '@/components/ui/toggle';
import { ErrorState } from '@/components/ui/tooltip';
import type { PlanningPhase, PlanningSession } from '@/lib/api';
import { formatDistanceToNow } from '@/lib/constants';
import {
  useCreatePlanningSession,
  useDiscardPlanningSession,
  usePlanningSessions,
  useProjects,
} from '@/lib/hooks';

// =============================================================================
// Phase Configuration
// =============================================================================

const PLANNING_PHASES: PlanningPhase[] = [
  'created',
  'brainstorming',
  'synthesizing',
  'drafting',
  'ready',
  'materialized',
  'discarded',
];

const PHASE_CONFIG: Record<
  PlanningPhase,
  { label: string; icon: string; color: string; bgClass: string; textClass: string }
> = {
  created: {
    label: 'Created',
    icon: '◇',
    color: '#80ffea',
    bgClass: 'bg-sc-cyan/15',
    textClass: 'text-sc-cyan',
  },
  brainstorming: {
    label: 'Brainstorming',
    icon: '◈',
    color: '#e135ff',
    bgClass: 'bg-sc-purple/15',
    textClass: 'text-sc-purple',
  },
  synthesizing: {
    label: 'Synthesizing',
    icon: '◆',
    color: '#ff6ac1',
    bgClass: 'bg-sc-coral/15',
    textClass: 'text-sc-coral',
  },
  drafting: {
    label: 'Drafting',
    icon: '◇',
    color: '#f1fa8c',
    bgClass: 'bg-sc-yellow/15',
    textClass: 'text-sc-yellow',
  },
  ready: {
    label: 'Ready',
    icon: '✓',
    color: '#50fa7b',
    bgClass: 'bg-sc-green/15',
    textClass: 'text-sc-green',
  },
  materialized: {
    label: 'Materialized',
    icon: '●',
    color: '#50fa7b',
    bgClass: 'bg-sc-green/15',
    textClass: 'text-sc-green',
  },
  discarded: {
    label: 'Discarded',
    icon: '○',
    color: '#6272a4',
    bgClass: 'bg-sc-fg-subtle/15',
    textClass: 'text-sc-fg-subtle',
  },
};

// Active phases (in-progress work)
const ACTIVE_PHASES: PlanningPhase[] = [
  'created',
  'brainstorming',
  'synthesizing',
  'drafting',
  'ready',
];

// =============================================================================
// Session Card Component
// =============================================================================

function SessionCard({
  session,
  projectName,
  showProject,
  onDiscard,
}: {
  session: PlanningSession;
  projectName?: string;
  showProject: boolean;
  onDiscard: (id: string) => void;
}) {
  const phaseConfig = PHASE_CONFIG[session.phase] ?? PHASE_CONFIG.created;
  const isActive = ACTIVE_PHASES.includes(session.phase);
  const isTerminal = ['materialized', 'discarded'].includes(session.phase);

  const threadCount = session.personas?.length ?? 0;
  const taskCount = session.task_drafts?.length ?? 0;

  return (
    <Link
      href={`/agents/planning/${session.id}`}
      className={`
        group block relative rounded-xl overflow-hidden shadow-card
        transition-all duration-200
        hover:shadow-card-hover hover:-translate-y-0.5
        border border-sc-fg-subtle/20 hover:border-sc-purple/40
        ${isActive ? 'bg-gradient-to-br from-sc-purple/10 via-sc-bg-base to-sc-bg-base' : 'bg-sc-bg-base'}
      `}
    >
      {/* Phase accent bar */}
      <div
        className="absolute left-0 top-0 bottom-0 w-1"
        style={{ backgroundColor: phaseConfig.color }}
      />

      <div className="pl-4 pr-3 py-3">
        {/* Top row: Phase badge + Project */}
        <div className="flex items-center justify-between gap-2 mb-2">
          <div className="flex items-center gap-1.5 min-w-0 flex-1">
            {/* Phase badge */}
            <span
              className={`shrink-0 text-[10px] px-1.5 py-0.5 rounded ${phaseConfig.bgClass} ${phaseConfig.textClass}`}
            >
              {phaseConfig.icon} {phaseConfig.label}
            </span>

            {/* Project name */}
            {showProject && projectName && (
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-sc-bg-elevated text-sc-fg-muted truncate">
                {projectName}
              </span>
            )}
          </div>

          {/* Actions */}
          {!isTerminal && (
            <button
              type="button"
              onClick={e => {
                e.preventDefault();
                e.stopPropagation();
                if (confirm('Discard this planning session?')) {
                  onDiscard(session.id);
                }
              }}
              className="p-1 text-sc-fg-subtle hover:text-sc-red hover:bg-sc-red/10 rounded-md transition-all duration-200 opacity-0 group-hover:opacity-100"
              title="Discard session"
            >
              <Trash width={14} height={14} />
            </button>
          )}
        </div>

        {/* Title */}
        <h3 className="text-sm font-medium text-sc-fg-primary line-clamp-2 leading-snug group-hover:text-white transition-colors">
          {session.title || session.prompt.slice(0, 80)}
          {!session.title && session.prompt.length > 80 && '...'}
        </h3>

        {/* Status indicators */}
        {isActive && (
          <div className="flex items-center gap-2 mt-2">
            <span className="w-2 h-2 rounded-full bg-sc-purple animate-pulse" />
            <span className="text-xs text-sc-fg-muted">
              {session.phase === 'brainstorming' &&
                `${threadCount} persona${threadCount !== 1 ? 's' : ''} thinking...`}
              {session.phase === 'synthesizing' && 'Synthesizing ideas...'}
              {session.phase === 'drafting' && 'Drafting spec & tasks...'}
              {session.phase === 'ready' && `${taskCount} task${taskCount !== 1 ? 's' : ''} ready`}
              {session.phase === 'created' && 'Ready to start'}
            </span>
          </div>
        )}

        {/* Materialized info */}
        {session.phase === 'materialized' && session.epic_id && (
          <div className="flex items-center gap-2 mt-2">
            <span className="text-xs text-sc-green">Epic created: {session.epic_id}</span>
          </div>
        )}

        {/* Footer: Metadata */}
        <div className="flex items-center justify-between mt-3 pt-2 border-t border-sc-fg-subtle/10">
          <div className="flex items-center gap-3 text-[10px] text-sc-fg-muted">
            {threadCount > 0 && (
              <span className="flex items-center gap-1" title="Personas">
                <span className="opacity-60">◈</span>
                {threadCount}
              </span>
            )}
            {taskCount > 0 && (
              <span className="flex items-center gap-1" title="Task drafts">
                <span className="opacity-60">◇</span>
                {taskCount}
              </span>
            )}
          </div>

          {/* Time indicator */}
          <span className="text-[10px] text-sc-fg-subtle px-1.5 py-0.5 rounded bg-sc-bg-elevated">
            {formatDistanceToNow(session.created_at)}
          </span>
        </div>
      </div>
    </Link>
  );
}

// =============================================================================
// Empty State
// =============================================================================

function PlanningEmptyState({ onCreateSession }: { onCreateSession: () => void }) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      <div className="w-16 h-16 rounded-full bg-sc-purple/10 flex items-center justify-center mb-4">
        <LightBulb width={32} height={32} className="text-sc-purple" />
      </div>
      <h3 className="text-lg font-medium text-sc-fg-primary mb-2">No Planning Sessions</h3>
      <p className="text-sm text-sc-fg-muted max-w-md mb-6">
        Planning Studio uses multiple AI personas to brainstorm, synthesize ideas, and generate
        structured implementation plans with tasks and specs.
      </p>
      <button
        type="button"
        onClick={onCreateSession}
        className="flex items-center gap-2 px-4 py-2 text-sm font-medium bg-sc-purple hover:bg-sc-purple/80 text-white rounded-lg transition-colors"
      >
        <Plus width={16} height={16} />
        Start Planning Session
      </button>
    </div>
  );
}

// =============================================================================
// New Session Form
// =============================================================================

function NewSessionForm({
  onSubmit,
  onCancel,
  isPending,
}: {
  onSubmit: (prompt: string, title?: string) => void;
  onCancel: () => void;
  isPending: boolean;
}) {
  const [prompt, setPrompt] = useState('');
  const [title, setTitle] = useState('');

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (prompt.trim()) {
      onSubmit(prompt.trim(), title.trim() || undefined);
    }
  };

  return (
    <form
      onSubmit={handleSubmit}
      className="p-4 bg-sc-bg-elevated border border-sc-purple/30 rounded-xl space-y-4"
    >
      <div>
        <label htmlFor="title" className="block text-sm font-medium text-sc-fg-primary mb-1">
          Title (optional)
        </label>
        <input
          id="title"
          type="text"
          value={title}
          onChange={e => setTitle(e.target.value)}
          placeholder="e.g., User Authentication System"
          className="w-full px-3 py-2 bg-sc-bg-base border border-sc-fg-subtle/20 rounded-lg text-sm text-sc-fg-primary placeholder:text-sc-fg-subtle focus:border-sc-purple focus:outline-none focus:ring-2 focus:ring-sc-purple/10"
        />
      </div>
      <div>
        <label htmlFor="prompt" className="block text-sm font-medium text-sc-fg-primary mb-1">
          What do you want to plan?
        </label>
        <textarea
          id="prompt"
          value={prompt}
          onChange={e => setPrompt(e.target.value)}
          placeholder="Describe the feature, system, or problem you want to plan..."
          rows={4}
          className="w-full px-3 py-2 bg-sc-bg-base border border-sc-fg-subtle/20 rounded-lg text-sm text-sc-fg-primary placeholder:text-sc-fg-subtle focus:border-sc-purple focus:outline-none focus:ring-2 focus:ring-sc-purple/10 resize-none"
          required
        />
      </div>
      <div className="flex items-center justify-end gap-3">
        <button
          type="button"
          onClick={onCancel}
          className="px-4 py-2 text-sm text-sc-fg-muted hover:text-sc-fg-primary transition-colors"
        >
          Cancel
        </button>
        <button
          type="submit"
          disabled={!prompt.trim() || isPending}
          className="flex items-center gap-2 px-4 py-2 text-sm font-medium bg-sc-purple hover:bg-sc-purple/80 disabled:bg-sc-purple/50 disabled:cursor-not-allowed text-white rounded-lg transition-colors"
        >
          {isPending ? (
            <>
              <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
              Creating...
            </>
          ) : (
            <>
              <Plus width={16} height={16} />
              Start Planning
            </>
          )}
        </button>
      </div>
    </form>
  );
}

// =============================================================================
// Planning View Component (for embedding in Agents page)
// =============================================================================

interface PlanningViewProps {
  projectFilter?: string;
}

export function PlanningView({ projectFilter }: PlanningViewProps) {
  const [searchQuery, setSearchQuery] = useState('');
  const [showNewForm, setShowNewForm] = useState(false);
  const [selectedPhases, setSelectedPhases] = useState<Set<PlanningPhase>>(new Set());

  // Fetch sessions
  const { data: sessionsData, isLoading, error } = usePlanningSessions({ project: projectFilter });
  const { data: projectsData } = useProjects();
  const createSession = useCreatePlanningSession();
  const discardSession = useDiscardPlanningSession();

  const sessions = sessionsData?.sessions ?? [];
  const projects = projectsData?.entities ?? [];

  // Build project name lookup
  const projectNames = useMemo(() => {
    const lookup: Record<string, string> = {};
    for (const project of projects) {
      lookup[project.id] = project.name;
    }
    return lookup;
  }, [projects]);

  // Filter sessions
  const filteredSessions = useMemo(() => {
    let filtered = sessions;

    // Filter by phases
    if (selectedPhases.size > 0) {
      filtered = filtered.filter(s => selectedPhases.has(s.phase));
    }

    // Filter by search query
    if (searchQuery.trim()) {
      const query = searchQuery.toLowerCase();
      filtered = filtered.filter(s => {
        const title = s.title?.toLowerCase() ?? '';
        const prompt = s.prompt?.toLowerCase() ?? '';
        return title.includes(query) || prompt.includes(query);
      });
    }

    // Sort by created_at descending
    return [...filtered].sort((a, b) => {
      const aTime = new Date(a.created_at).getTime();
      const bTime = new Date(b.created_at).getTime();
      return bTime - aTime;
    });
  }, [sessions, selectedPhases, searchQuery]);

  // Phase filter toggle
  const handlePhaseToggle = useCallback((phase: PlanningPhase) => {
    setSelectedPhases(prev => {
      const next = new Set(prev);
      if (next.has(phase)) {
        next.delete(phase);
      } else {
        next.add(phase);
      }
      return next;
    });
  }, []);

  // Clear phases filter
  const handleClearPhases = useCallback(() => {
    setSelectedPhases(new Set());
  }, []);

  // Create session handler
  const handleCreateSession = useCallback(
    (prompt: string, title?: string) => {
      createSession.mutate(
        {
          prompt,
          title,
          project_id: projectFilter || undefined,
        },
        {
          onSuccess: _session => {
            setShowNewForm(false);
          },
        }
      );
    },
    [createSession, projectFilter]
  );

  // Discard handler
  const handleDiscard = useCallback(
    (id: string) => {
      discardSession.mutate(id);
    },
    [discardSession]
  );

  return (
    <div className="space-y-4">
      {/* Header with New Session button */}
      {!showNewForm && sessions.length > 0 && (
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-sm font-medium text-sc-fg-primary flex items-center gap-2">
              <LightBulb width={16} height={16} className="text-sc-purple" />
              Planning Sessions
            </h2>
            <p className="text-xs text-sc-fg-muted mt-0.5">
              Multi-persona brainstorming and structured planning
            </p>
          </div>
          <button
            type="button"
            onClick={() => setShowNewForm(true)}
            className="flex items-center gap-2 px-3 py-1.5 text-sm font-medium bg-sc-purple hover:bg-sc-purple/80 text-white rounded-lg transition-colors"
          >
            <Plus width={14} height={14} />
            New Session
          </button>
        </div>
      )}

      {/* New Session Form */}
      {showNewForm && (
        <NewSessionForm
          onSubmit={handleCreateSession}
          onCancel={() => setShowNewForm(false)}
          isPending={createSession.isPending}
        />
      )}

      {/* Search + Filters */}
      {sessions.length > 0 && !showNewForm && (
        <div className="space-y-3">
          {/* Search Input */}
          <div className="relative">
            <Search
              width={16}
              height={16}
              className="absolute left-3 top-1/2 -translate-y-1/2 text-sc-fg-subtle"
            />
            <input
              type="text"
              value={searchQuery}
              onChange={e => setSearchQuery(e.target.value)}
              placeholder="Search sessions..."
              className="w-full pl-9 pr-3 py-2 bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-lg text-sm text-sc-fg-primary placeholder:text-sc-fg-subtle focus:border-sc-purple focus:outline-none focus:ring-2 focus:ring-sc-purple/10 transition-all"
            />
            {searchQuery && (
              <button
                type="button"
                onClick={() => setSearchQuery('')}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-sc-fg-subtle hover:text-sc-fg-primary"
              >
                <X width={14} height={14} />
              </button>
            )}
          </div>

          {/* Phase Filter */}
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs text-sc-fg-subtle font-medium">Phase:</span>
            {selectedPhases.size > 0 && (
              <button
                type="button"
                onClick={handleClearPhases}
                className="text-xs text-sc-fg-muted hover:text-sc-fg-primary flex items-center gap-1 px-2 py-0.5 rounded bg-sc-bg-elevated hover:bg-sc-bg-highlight transition-colors"
              >
                <X width={12} height={12} />
                Clear
              </button>
            )}
            {PLANNING_PHASES.filter(p => p !== 'discarded').map(phase => {
              const config = PHASE_CONFIG[phase];
              const isActive = selectedPhases.has(phase);
              return (
                <FilterChip key={phase} active={isActive} onClick={() => handlePhaseToggle(phase)}>
                  <span className="flex items-center gap-1">
                    <span>{config.icon}</span>
                    {config.label}
                  </span>
                </FilterChip>
              );
            })}
            {selectedPhases.size === 0 && (
              <span className="text-xs text-sc-fg-subtle italic ml-1">All phases</span>
            )}
          </div>
        </div>
      )}

      {/* Content */}
      {error ? (
        <ErrorState
          title="Failed to load sessions"
          message={error instanceof Error ? error.message : 'Unknown error'}
        />
      ) : isLoading ? (
        <LoadingState />
      ) : sessions.length === 0 && !showNewForm ? (
        <PlanningEmptyState onCreateSession={() => setShowNewForm(true)} />
      ) : filteredSessions.length === 0 && !showNewForm ? (
        <div className="py-12 text-center text-sc-fg-muted">
          No sessions match the selected filters
        </div>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {filteredSessions.map(session => (
            <SessionCard
              key={session.id}
              session={session}
              projectName={session.project_id ? projectNames[session.project_id] : undefined}
              showProject={!projectFilter}
              onDiscard={handleDiscard}
            />
          ))}
        </div>
      )}

      {/* Mutation loading indicator */}
      {discardSession.isPending && (
        <div className="fixed bottom-4 right-4 bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-lg px-4 py-2 text-sm text-sc-fg-muted shadow-lg">
          Updating session...
        </div>
      )}
    </div>
  );
}

// Re-export PHASE_CONFIG for use elsewhere
export { PHASE_CONFIG, ACTIVE_PHASES };
