'use client';

import { memo } from 'react';
import { CheckCircle, Clock, Layers, Pause, Zap } from '@/components/ui/icons';
import type { EpicStatus, EpicSummary, TaskPriority } from '@/lib/api';
import { EPIC_STATUS_CONFIG } from '@/lib/constants';

interface EpicCardProps {
  epic: EpicSummary;
  projectName?: string;
  showProject?: boolean;
  onClick?: (epicId: string) => void;
  onProjectClick?: (projectId: string) => void;
}

// Priority-based card styling
const PRIORITY_STYLES: Record<
  string,
  { bg: string; border: string; accent: string; badge: string }
> = {
  critical: {
    bg: 'bg-gradient-to-br from-sc-red/15 via-sc-bg-base to-sc-bg-base',
    border: 'border-sc-red/40 hover:border-sc-red/60',
    accent: 'bg-sc-red',
    badge: 'bg-sc-red/20 text-sc-red border-sc-red/30',
  },
  high: {
    bg: 'bg-gradient-to-br from-sc-coral/10 via-sc-bg-base to-sc-bg-base',
    border: 'border-sc-coral/30 hover:border-sc-coral/50',
    accent: 'bg-sc-coral',
    badge: 'bg-sc-coral/20 text-sc-coral border-sc-coral/30',
  },
  medium: {
    bg: 'bg-sc-bg-base',
    border: 'border-[#ffb86c]/20 hover:border-[#ffb86c]/40',
    accent: 'bg-[#ffb86c]',
    badge: 'bg-[#ffb86c]/20 text-[#ffb86c] border-[#ffb86c]/30',
  },
  low: {
    bg: 'bg-sc-bg-base',
    border: 'border-sc-cyan/20 hover:border-sc-cyan/40',
    accent: 'bg-sc-cyan',
    badge: 'bg-sc-cyan/20 text-sc-cyan border-sc-cyan/30',
  },
  someday: {
    bg: 'bg-sc-bg-base',
    border: 'border-sc-fg-subtle/20 hover:border-sc-fg-subtle/40',
    accent: 'bg-sc-fg-subtle',
    badge: 'bg-sc-fg-subtle/20 text-sc-fg-subtle border-sc-fg-subtle/30',
  },
};

const PRIORITY_LABELS: Record<string, string> = {
  critical: 'URGENT',
  high: 'High',
  medium: 'Med',
  low: 'Low',
  someday: 'Later',
};

export const EpicCard = memo(function EpicCard({
  epic,
  projectName,
  showProject = true,
  onClick,
  onProjectClick,
}: EpicCardProps) {
  const metadata = epic.metadata ?? {};
  const priority = (metadata.priority ?? 'medium') as TaskPriority;
  const projectId = metadata.project_id as string | undefined;
  const priorityStyle = PRIORITY_STYLES[priority] || PRIORITY_STYLES.medium;

  // Progress calculation from metadata
  const totalTasks = (metadata.total_tasks as number) ?? 0;
  const doneTasks = (metadata.completed_tasks as number) ?? 0;
  const doingTasks = (metadata.doing_tasks as number) ?? 0;
  const blockedTasks = (metadata.blocked_tasks as number) ?? 0;
  const progressPercent = totalTasks > 0 ? Math.round((doneTasks / totalTasks) * 100) : 0;
  const progress =
    totalTasks > 0
      ? { total: totalTasks, done: doneTasks, doing: doingTasks, blocked: blockedTasks }
      : null;

  // Derive status from task progress (epics auto-complete when all tasks done)
  const derivedStatus: EpicStatus = (() => {
    if (totalTasks === 0) return 'planning';
    if (progressPercent === 100) return 'completed';
    if (blockedTasks > 0) return 'blocked';
    if (doingTasks > 0) return 'in_progress';
    return 'planning';
  })();

  const statusConfig = EPIC_STATUS_CONFIG[derivedStatus as keyof typeof EPIC_STATUS_CONFIG];
  const isBlocked = derivedStatus === 'blocked';
  const isInProgress = derivedStatus === 'in_progress';
  const isCompleted = derivedStatus === 'completed';

  // Override styles for special statuses
  const cardBg = isBlocked
    ? 'bg-gradient-to-br from-sc-red/10 via-sc-bg-base to-sc-bg-base'
    : isInProgress
      ? 'bg-gradient-to-br from-sc-purple/10 via-sc-bg-base to-sc-bg-base'
      : isCompleted
        ? 'bg-gradient-to-br from-sc-green/10 via-sc-bg-base to-sc-bg-base'
        : priorityStyle.bg;

  const cardBorder = isBlocked
    ? 'border-sc-red/40 hover:border-sc-red/60'
    : isInProgress
      ? 'border-sc-purple/40 hover:border-sc-purple/60'
      : isCompleted
        ? 'border-sc-green/40 hover:border-sc-green/60'
        : priorityStyle.border;

  return (
    <div
      onClick={() => onClick?.(epic.id)}
      className={`
        relative rounded-xl overflow-hidden cursor-pointer
        transition-all duration-200
        hover:shadow-lg hover:shadow-black/20
        hover:-translate-y-0.5
        group select-none
        border ${cardBorder} ${cardBg}
      `}
      role="button"
      tabIndex={0}
      onKeyDown={e => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onClick?.(epic.id);
        }
      }}
    >
      {/* Priority accent bar */}
      <div className={`absolute left-0 top-0 bottom-0 w-1 ${priorityStyle.accent}`} />

      {/* Blocked overlay pattern */}
      {isBlocked && (
        <div
          className="absolute inset-0 opacity-5 pointer-events-none"
          style={{
            backgroundImage:
              'repeating-linear-gradient(45deg, transparent, transparent 10px, currentColor 10px, currentColor 11px)',
          }}
        />
      )}

      <div className="pl-4 pr-3 py-3">
        {/* Top row: Priority badge + Project + Status */}
        <div className="flex items-center justify-between gap-2 mb-2">
          <div className="flex items-center gap-1.5 min-w-0 flex-1">
            {/* Priority badge */}
            <span
              className={`shrink-0 inline-flex items-center gap-1 text-[10px] font-bold px-1.5 py-0.5 rounded border ${priorityStyle.badge}`}
            >
              {priority === 'critical' && <Zap width={10} height={10} className="animate-pulse" />}
              {PRIORITY_LABELS[priority]}
            </span>

            {/* Project badge */}
            {showProject && projectName && projectId && (
              <button
                type="button"
                onClick={e => {
                  e.stopPropagation();
                  onProjectClick?.(projectId);
                }}
                className="shrink-0 text-[10px] px-1.5 py-0.5 rounded bg-sc-bg-elevated text-sc-fg-muted hover:text-sc-cyan hover:bg-sc-cyan/10 transition-colors truncate max-w-[80px]"
                title={`View ${projectName}`}
              >
                {projectName}
              </button>
            )}

            {/* Epic indicator */}
            <span className="text-[10px] text-[#ffb86c] font-medium">Epic</span>
          </div>

          {/* Status indicator */}
          <div className={`shrink-0 flex items-center gap-1 text-xs ${statusConfig?.textClass}`}>
            {isBlocked && <Pause width={12} height={12} className="text-sc-red" />}
            {isInProgress && (
              <Clock width={12} height={12} className="text-sc-purple animate-pulse" />
            )}
            {isCompleted && <CheckCircle width={12} height={12} className="text-sc-green" />}
            {!isBlocked && !isInProgress && !isCompleted && (
              <span className="opacity-60">{statusConfig?.icon}</span>
            )}
          </div>
        </div>

        {/* Title */}
        <h4
          className={`text-sm font-medium line-clamp-2 leading-snug transition-colors ${
            isBlocked
              ? 'text-sc-red'
              : isCompleted
                ? 'text-sc-green'
                : 'text-sc-fg-primary group-hover:text-white'
          }`}
        >
          {epic.name}
        </h4>

        {/* Description preview */}
        {epic.description && (
          <p className="text-xs text-sc-fg-subtle line-clamp-1 mt-1.5">{epic.description}</p>
        )}

        {/* Progress bar */}
        {totalTasks > 0 && (
          <div className="mt-3">
            <div className="flex items-center justify-between text-[10px] mb-1">
              <span className="text-sc-fg-muted flex items-center gap-1">
                <Layers width={10} height={10} />
                {doneTasks}/{totalTasks} tasks
              </span>
              <span className={progressPercent === 100 ? 'text-sc-green' : 'text-sc-fg-subtle'}>
                {progressPercent}%
              </span>
            </div>
            <div className="h-1.5 bg-sc-bg-elevated rounded-full overflow-hidden">
              <div
                className={`h-full transition-all duration-300 ${
                  progressPercent === 100 ? 'bg-sc-green' : 'bg-[#ffb86c]'
                }`}
                style={{ width: `${progressPercent}%` }}
              />
            </div>
          </div>
        )}

        {/* Footer */}
        <div className="flex items-center justify-between mt-3 pt-2 border-t border-sc-fg-subtle/10">
          {/* Status label */}
          <span
            className={`inline-flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded ${statusConfig?.bgClass} ${statusConfig?.textClass}`}
          >
            {statusConfig?.label}
          </span>

          {/* Task breakdown if available */}
          {progress && (progress.doing > 0 || progress.blocked > 0) && (
            <div className="flex items-center gap-2 text-[10px]">
              {progress.doing > 0 && (
                <span className="text-sc-purple">{progress.doing} in progress</span>
              )}
              {progress.blocked > 0 && (
                <span className="text-sc-red">{progress.blocked} blocked</span>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
});

export function EpicCardSkeleton() {
  return (
    <div className="relative bg-sc-bg-base rounded-xl overflow-hidden border border-sc-fg-subtle/10 animate-pulse">
      <div className="absolute left-0 top-0 bottom-0 w-1 bg-sc-fg-subtle/20" />
      <div className="pl-4 pr-3 py-3">
        <div className="flex items-center gap-2 mb-2">
          <div className="h-4 w-12 bg-sc-bg-elevated rounded" />
          <div className="h-4 w-16 bg-sc-bg-elevated rounded" />
        </div>
        <div className="h-4 w-full bg-sc-bg-elevated rounded mb-1" />
        <div className="h-4 w-3/4 bg-sc-bg-elevated rounded" />
        <div className="mt-3">
          <div className="h-1.5 w-full bg-sc-bg-elevated rounded-full" />
        </div>
        <div className="flex items-center justify-between mt-3 pt-2 border-t border-sc-fg-subtle/10">
          <div className="h-4 w-16 bg-sc-bg-elevated rounded" />
          <div className="h-4 w-20 bg-sc-bg-elevated rounded" />
        </div>
      </div>
    </div>
  );
}
