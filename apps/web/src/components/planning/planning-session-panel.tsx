'use client';

import { AnimatePresence, motion } from 'motion/react';
import Link from 'next/link';
import { useState } from 'react';
import {
  ArrowRight,
  Check,
  CheckCircle,
  ChevronDown,
  ChevronRight,
  Combine,
  InfoCircle,
  Page,
  Sparks,
  WarningTriangle,
} from '@/components/ui/icons';
import { LoadingState } from '@/components/ui/spinner';
import type {
  PlanningMessage,
  PlanningSession,
  PlanningTaskDraft,
  PlanningThread,
} from '@/lib/api';
import { formatDistanceToNow } from '@/lib/constants';
import {
  useMaterializePlanningSession,
  usePlanningThreadMessages,
  usePlanningThreads,
  useRunSynthesis,
} from '@/lib/hooks';
import { PHASE_CONFIG } from './planning-view';

// =============================================================================
// Phase Actions
// =============================================================================

interface PhaseActionsProps {
  session: PlanningSession;
}

function PhaseActions({ session }: PhaseActionsProps) {
  const runSynthesis = useRunSynthesis();
  const materialize = useMaterializePlanningSession();

  const isLoading = runSynthesis.isPending || materialize.isPending;

  const handleSynthesize = () => {
    runSynthesis.mutate(session.id);
  };

  const handleMaterialize = () => {
    materialize.mutate({
      id: session.id,
      request: { project_id: session.project_id },
    });
  };

  return (
    <div className="flex items-center gap-2">
      {/* Created phase - show starting indicator or manual start */}
      {session.phase === 'created' && (
        <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-sc-purple/20 text-sc-purple">
          <span className="w-2 h-2 rounded-full bg-sc-purple animate-pulse" />
          Starting...
        </div>
      )}

      {session.phase === 'brainstorming' && (
        <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-sc-purple/20 text-sc-purple">
          <span className="w-2 h-2 rounded-full bg-sc-purple animate-pulse" />
          Personas thinking...
        </div>
      )}

      {session.phase === 'synthesizing' && (
        <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-sc-yellow/20 text-sc-yellow">
          <span className="w-2 h-2 rounded-full bg-sc-yellow animate-pulse" />
          Synthesizing ideas...
        </div>
      )}

      {session.phase === 'drafting' && (
        <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-sc-cyan/20 text-sc-cyan">
          <span className="w-2 h-2 rounded-full bg-sc-cyan animate-pulse" />
          Drafting spec & tasks...
        </div>
      )}

      {session.phase === 'ready' && (
        <button
          type="button"
          onClick={handleMaterialize}
          disabled={isLoading}
          className="flex items-center gap-2 px-4 py-2 rounded-lg bg-sc-green text-white font-medium hover:bg-sc-green/90 transition-colors disabled:opacity-50"
        >
          <CheckCircle width={16} height={16} />
          Create Epic & Tasks
        </button>
      )}

      {session.phase === 'materialized' && (
        <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-sc-green/20 text-sc-green">
          <Check width={16} height={16} />
          Materialized
        </div>
      )}

      {/* Manual advance buttons for completed phases */}
      {session.phase === 'brainstorming' && (
        <button
          type="button"
          onClick={handleSynthesize}
          disabled={isLoading}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-sc-fg-subtle/30 text-sc-fg-muted hover:text-sc-fg-primary hover:border-sc-fg-subtle/50 transition-colors text-sm disabled:opacity-50"
        >
          <ArrowRight width={14} height={14} />
          Skip to Synthesis
        </button>
      )}
    </div>
  );
}

// =============================================================================
// Session Header
// =============================================================================

interface SessionHeaderProps {
  session: PlanningSession;
}

function SessionHeader({ session }: SessionHeaderProps) {
  const phaseConfig = PHASE_CONFIG[session.phase] ?? PHASE_CONFIG.created;

  return (
    <div className="p-4 border-b border-sc-fg-subtle/20">
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1 min-w-0">
          {/* Phase badge */}
          <div className="flex items-center gap-2 mb-2">
            <span
              className={`text-xs px-2 py-0.5 rounded ${phaseConfig.bgClass} ${phaseConfig.textClass}`}
            >
              {phaseConfig.icon} {phaseConfig.label}
            </span>
            <span className="text-xs text-sc-fg-subtle">
              {formatDistanceToNow(session.created_at)}
            </span>
          </div>

          {/* Title */}
          <h1 className="text-lg font-semibold text-sc-fg-primary mb-1">
            {session.title || 'Untitled Session'}
          </h1>

          {/* Prompt */}
          <p className="text-sm text-sc-fg-muted line-clamp-2">{session.prompt}</p>
        </div>

        <PhaseActions session={session} />
      </div>
    </div>
  );
}

// =============================================================================
// Thread Message
// =============================================================================

interface ThreadMessageProps {
  message: PlanningMessage;
}

function ThreadMessage({ message }: ThreadMessageProps) {
  const isAssistant = message.role === 'assistant';

  return (
    <div className={`flex ${isAssistant ? 'justify-start' : 'justify-end'}`}>
      <div
        className={`
          max-w-[85%] rounded-lg px-3 py-2 text-sm
          ${
            isAssistant
              ? 'bg-sc-bg-elevated text-sc-fg-primary'
              : 'bg-sc-purple/20 text-sc-fg-primary'
          }
        `}
      >
        {/* Thinking block (collapsed by default) */}
        {message.thinking && (
          <details className="mb-2">
            <summary className="text-xs text-sc-fg-subtle cursor-pointer hover:text-sc-fg-muted">
              Show thinking...
            </summary>
            <pre className="mt-2 text-xs text-sc-fg-subtle whitespace-pre-wrap font-mono bg-sc-bg-base/50 rounded p-2 max-h-40 overflow-y-auto">
              {message.thinking}
            </pre>
          </details>
        )}

        {/* Content */}
        <div className="whitespace-pre-wrap">{message.content}</div>

        {/* Timestamp */}
        <div className="mt-1 text-[10px] text-sc-fg-subtle">
          {formatDistanceToNow(message.created_at)}
        </div>
      </div>
    </div>
  );
}

// =============================================================================
// Thread Panel (Expandable)
// =============================================================================

interface ThreadPanelProps {
  thread: PlanningThread;
  sessionId: string;
  defaultExpanded?: boolean;
}

function ThreadPanel({ thread, sessionId, defaultExpanded = false }: ThreadPanelProps) {
  const [isExpanded, setIsExpanded] = useState(defaultExpanded);
  const { data, isLoading } = usePlanningThreadMessages(sessionId, isExpanded ? thread.id : '');

  const messages = data?.messages ?? [];
  const isActive = thread.status === 'running';

  return (
    <div className="border border-sc-fg-subtle/20 rounded-lg overflow-hidden">
      {/* Thread Header */}
      <button
        type="button"
        onClick={() => setIsExpanded(!isExpanded)}
        className="w-full flex items-center gap-3 px-4 py-3 bg-sc-bg-elevated hover:bg-sc-bg-highlight transition-colors text-left"
      >
        {/* Expand/collapse icon */}
        <span className="text-sc-fg-subtle">
          {isExpanded ? (
            <ChevronDown width={16} height={16} />
          ) : (
            <ChevronRight width={16} height={16} />
          )}
        </span>

        {/* Persona info */}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <Sparks width={14} height={14} className="text-sc-purple shrink-0" />
            <span className="font-medium text-sc-fg-primary truncate">
              {thread.persona_name || thread.persona_role}
            </span>
            {thread.persona_focus && (
              <span className="text-xs text-sc-fg-subtle truncate hidden sm:inline">
                - {thread.persona_focus}
              </span>
            )}
          </div>
        </div>

        {/* Status */}
        <div className="flex items-center gap-2 shrink-0">
          {isActive && (
            <span className="flex items-center gap-1.5 text-xs text-sc-purple">
              <span className="w-1.5 h-1.5 rounded-full bg-sc-purple animate-pulse" />
              Thinking
            </span>
          )}
          {thread.status === 'completed' && (
            <span className="flex items-center gap-1 text-xs text-sc-green">
              <Check width={12} height={12} />
              Done
            </span>
          )}
          {thread.status === 'failed' && (
            <span className="flex items-center gap-1 text-xs text-sc-red">
              <WarningTriangle width={12} height={12} />
              Failed
            </span>
          )}
          <span className="text-xs text-sc-fg-subtle">
            {messages.length > 0 ? `${messages.length} msg` : ''}
          </span>
        </div>
      </button>

      {/* Messages */}
      <AnimatePresence>
        {isExpanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            <div className="p-4 bg-sc-bg-base border-t border-sc-fg-subtle/10 space-y-3 max-h-96 overflow-y-auto">
              {isLoading ? (
                <div className="flex items-center justify-center py-4">
                  <LoadingState />
                </div>
              ) : messages.length === 0 ? (
                <div className="text-center py-4 text-sm text-sc-fg-subtle">
                  <Page width={24} height={24} className="mx-auto mb-2 opacity-50" />
                  No messages yet
                </div>
              ) : (
                messages.map(msg => <ThreadMessage key={msg.id} message={msg} />)
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

// =============================================================================
// Threads Section
// =============================================================================

interface ThreadsSectionProps {
  session: PlanningSession;
}

function ThreadsSection({ session }: ThreadsSectionProps) {
  const { data, isLoading } = usePlanningThreads(session.id);
  const threads = data ?? [];

  // Show loading when fetching threads
  if (isLoading) {
    return (
      <div className="p-8 flex items-center justify-center">
        <LoadingState />
      </div>
    );
  }

  // No threads yet - show starting state for created/brainstorming phases
  if (threads.length === 0) {
    const isStarting = session.phase === 'created' || session.phase === 'brainstorming';
    return (
      <div className="p-8 text-center">
        {isStarting ? (
          <>
            <div className="w-12 h-12 mx-auto mb-3 rounded-full bg-sc-purple/20 flex items-center justify-center">
              <span className="w-3 h-3 rounded-full bg-sc-purple animate-pulse" />
            </div>
            <p className="text-sc-fg-primary font-medium">Starting brainstorming...</p>
            <p className="text-sm text-sc-fg-subtle mt-1">Personas are being initialized</p>
          </>
        ) : (
          <>
            <Sparks width={32} height={32} className="mx-auto mb-3 text-sc-fg-subtle opacity-50" />
            <p className="text-sc-fg-muted">No brainstorming threads</p>
          </>
        )}
      </div>
    );
  }

  return (
    <div className="p-4 space-y-3">
      <h2 className="text-sm font-medium text-sc-fg-muted mb-3 flex items-center gap-2">
        <Sparks width={14} height={14} />
        Brainstorming Threads ({threads.length})
      </h2>
      {threads.map((thread, idx) => (
        <ThreadPanel
          key={thread.id}
          thread={thread}
          sessionId={session.id}
          defaultExpanded={idx === 0}
        />
      ))}
    </div>
  );
}

// =============================================================================
// Synthesis Section
// =============================================================================

interface SynthesisSectionProps {
  session: PlanningSession;
}

function SynthesisSection({ session }: SynthesisSectionProps) {
  if (!session.synthesis) return null;

  return (
    <div className="p-4 border-t border-sc-fg-subtle/10">
      <h2 className="text-sm font-medium text-sc-fg-muted mb-3 flex items-center gap-2">
        <Combine width={14} height={14} />
        Synthesis
      </h2>
      <div className="bg-sc-bg-elevated rounded-lg p-4 text-sm text-sc-fg-primary whitespace-pre-wrap">
        {session.synthesis}
      </div>
    </div>
  );
}

// =============================================================================
// Spec Section
// =============================================================================

interface SpecSectionProps {
  session: PlanningSession;
}

function SpecSection({ session }: SpecSectionProps) {
  if (!session.spec_draft) return null;

  return (
    <div className="p-4 border-t border-sc-fg-subtle/10">
      <h2 className="text-sm font-medium text-sc-fg-muted mb-3 flex items-center gap-2">
        <InfoCircle width={14} height={14} />
        Specification Draft
      </h2>
      <div className="bg-sc-bg-elevated rounded-lg p-4 text-sm text-sc-fg-primary whitespace-pre-wrap font-mono">
        {session.spec_draft}
      </div>
    </div>
  );
}

// =============================================================================
// Task Drafts Section
// =============================================================================

interface TaskDraftCardProps {
  draft: PlanningTaskDraft;
  index: number;
}

function TaskDraftCard({ draft, index }: TaskDraftCardProps) {
  const priorityColors: Record<string, string> = {
    critical: 'bg-sc-red/20 text-sc-red',
    high: 'bg-sc-coral/20 text-sc-coral',
    medium: 'bg-sc-yellow/20 text-sc-yellow',
    low: 'bg-sc-cyan/20 text-sc-cyan',
  };

  return (
    <div className="bg-sc-bg-elevated rounded-lg p-3 border border-sc-fg-subtle/10">
      <div className="flex items-start gap-3">
        <span className="shrink-0 w-6 h-6 rounded-full bg-sc-purple/20 text-sc-purple text-xs font-bold flex items-center justify-center">
          {index + 1}
        </span>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="font-medium text-sc-fg-primary">{draft.title}</h3>
            {draft.priority && (
              <span
                className={`text-[10px] px-1.5 py-0.5 rounded ${priorityColors[draft.priority] ?? 'bg-sc-bg-highlight text-sc-fg-muted'}`}
              >
                {draft.priority}
              </span>
            )}
          </div>
          {draft.description && (
            <p className="mt-1 text-sm text-sc-fg-muted line-clamp-2">{draft.description}</p>
          )}
          {draft.tags && draft.tags.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1">
              {draft.tags.map(tag => (
                <span
                  key={tag}
                  className="text-[10px] px-1.5 py-0.5 rounded bg-sc-bg-highlight text-sc-fg-subtle"
                >
                  {tag}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

interface TaskDraftsSectionProps {
  session: PlanningSession;
}

function TaskDraftsSection({ session }: TaskDraftsSectionProps) {
  const drafts = session.task_drafts ?? [];

  if (drafts.length === 0) return null;

  return (
    <div className="p-4 border-t border-sc-fg-subtle/10">
      <h2 className="text-sm font-medium text-sc-fg-muted mb-3 flex items-center gap-2">
        <Check width={14} height={14} />
        Task Drafts ({drafts.length})
      </h2>
      <div className="space-y-2">
        {drafts.map((draft, idx) => (
          <TaskDraftCard key={`${draft.title}-${idx}`} draft={draft} index={idx} />
        ))}
      </div>
    </div>
  );
}

// =============================================================================
// Materialized Section
// =============================================================================

interface MaterializedSectionProps {
  session: PlanningSession;
}

function MaterializedSection({ session }: MaterializedSectionProps) {
  if (session.phase !== 'materialized') return null;

  return (
    <div className="p-4 border-t border-sc-fg-subtle/10">
      <div className="bg-sc-green/10 rounded-lg p-4 border border-sc-green/20">
        <h2 className="text-sm font-medium text-sc-green mb-3 flex items-center gap-2">
          <CheckCircle width={16} height={16} />
          Successfully Materialized
        </h2>

        <div className="space-y-2 text-sm">
          {session.epic_id && (
            <div className="flex items-center gap-2">
              <span className="text-sc-fg-muted">Epic:</span>
              <Link href={`/epics/${session.epic_id}`} className="text-sc-purple hover:underline">
                {session.epic_id}
              </Link>
            </div>
          )}

          {session.task_ids && session.task_ids.length > 0 && (
            <div className="flex items-center gap-2">
              <span className="text-sc-fg-muted">Tasks:</span>
              <span className="text-sc-fg-primary">{session.task_ids.length} created</span>
            </div>
          )}

          {session.materialized_at && (
            <div className="text-xs text-sc-fg-subtle mt-2">
              Materialized {formatDistanceToNow(session.materialized_at)}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// =============================================================================
// Main Panel
// =============================================================================

export interface PlanningSessionPanelProps {
  session: PlanningSession;
}

export function PlanningSessionPanel({ session }: PlanningSessionPanelProps) {
  return (
    <div className="h-full flex flex-col bg-sc-bg-base rounded-lg border border-sc-fg-subtle/20 overflow-hidden shadow-xl shadow-sc-purple/5">
      <SessionHeader session={session} />

      <div className="flex-1 overflow-y-auto">
        {/* Threads (always shown if exists) */}
        <ThreadsSection session={session} />

        {/* Synthesis */}
        <SynthesisSection session={session} />

        {/* Spec */}
        <SpecSection session={session} />

        {/* Task Drafts */}
        <TaskDraftsSection session={session} />

        {/* Materialized info */}
        <MaterializedSection session={session} />
      </div>
    </div>
  );
}
