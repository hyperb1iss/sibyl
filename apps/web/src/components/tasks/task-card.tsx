'use client';

import { memo } from 'react';
import { AlertTriangle, Calendar, Clock, Pause, Zap } from '@/components/ui/icons';
import type { TaskPriority, TaskStatus, TaskSummary } from '@/lib/api';
import { TASK_STATUS_CONFIG } from '@/lib/constants';

interface TaskCardProps {
  task: TaskSummary;
  projectName?: string;
  showProject?: boolean;
  draggable?: boolean;
  onDragStart?: (e: React.DragEvent, taskId: string) => void;
  onClick?: (taskId: string) => void;
  onProjectClick?: (projectId: string) => void;
}

function formatDueDate(dateStr: string): string {
  const date = new Date(dateStr);
  const today = new Date();
  today.setHours(0, 0, 0, 0);

  const tomorrow = new Date(today);
  tomorrow.setDate(tomorrow.getDate() + 1);

  const dueDay = new Date(date);
  dueDay.setHours(0, 0, 0, 0);

  if (dueDay.getTime() === today.getTime()) return 'Today';
  if (dueDay.getTime() === tomorrow.getTime()) return 'Tomorrow';

  const diffDays = Math.ceil((dueDay.getTime() - today.getTime()) / (1000 * 60 * 60 * 24));
  if (diffDays < 0) return `${Math.abs(diffDays)}d overdue`;
  if (diffDays <= 7) return `${diffDays}d`;

  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

// Priority-based card styling - make each priority visually distinct
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
    border: 'border-sc-purple/20 hover:border-sc-purple/40',
    accent: 'bg-sc-purple',
    badge: 'bg-sc-purple/20 text-sc-purple border-sc-purple/30',
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

// Tag category colors for visual distinction
const TAG_STYLES: Record<string, string> = {
  // Domain tags
  frontend: 'bg-sc-cyan/15 text-sc-cyan border-sc-cyan/30',
  backend: 'bg-sc-purple/15 text-sc-purple border-sc-purple/30',
  database: 'bg-sc-coral/15 text-sc-coral border-sc-coral/30',
  devops: 'bg-sc-yellow/15 text-sc-yellow border-sc-yellow/30',
  testing: 'bg-sc-green/15 text-sc-green border-sc-green/30',
  security: 'bg-sc-red/15 text-sc-red border-sc-red/30',
  performance: 'bg-sc-coral/15 text-sc-coral border-sc-coral/30',
  docs: 'bg-sc-fg-subtle/15 text-sc-fg-muted border-sc-fg-subtle/30',
  // Type tags
  feature: 'bg-sc-green/15 text-sc-green border-sc-green/30',
  bug: 'bg-sc-red/15 text-sc-red border-sc-red/30',
  refactor: 'bg-sc-purple/15 text-sc-purple border-sc-purple/30',
  chore: 'bg-sc-fg-subtle/15 text-sc-fg-muted border-sc-fg-subtle/30',
  research: 'bg-sc-cyan/15 text-sc-cyan border-sc-cyan/30',
};

const DEFAULT_TAG_STYLE = 'bg-sc-bg-elevated text-sc-fg-muted border-sc-fg-subtle/20';

function getTagStyle(tag: string): string {
  return TAG_STYLES[tag.toLowerCase()] || DEFAULT_TAG_STYLE;
}

export const TaskCard = memo(function TaskCard({
  task,
  projectName,
  showProject = true,
  draggable: isDraggable = true,
  onDragStart,
  onClick,
  onProjectClick,
}: TaskCardProps) {
  const status = (task.metadata.status ?? 'todo') as TaskStatus;
  const priority = (task.metadata.priority ?? 'medium') as TaskPriority;
  const statusConfig = TASK_STATUS_CONFIG[status as keyof typeof TASK_STATUS_CONFIG];
  const assignees = task.metadata.assignees ?? [];
  const projectId = task.metadata.project_id as string | undefined;
  const dueDate = task.metadata.due_date as string | undefined;
  const feature = task.metadata.feature as string | undefined;
  const tags = (task.metadata.tags as string[]) ?? [];

  const isOverdue = dueDate && status !== 'done' && new Date(dueDate) < new Date();
  const isBlocked = status === 'blocked';
  const isDoing = status === 'doing';
  const priorityStyle = PRIORITY_STYLES[priority] || PRIORITY_STYLES.medium;

  // Override styles for special statuses
  const cardBg = isBlocked
    ? 'bg-gradient-to-br from-sc-yellow/10 via-sc-bg-base to-sc-bg-base'
    : isDoing
      ? 'bg-gradient-to-br from-sc-purple/10 via-sc-bg-base to-sc-bg-base'
      : priorityStyle.bg;

  const cardBorder = isBlocked
    ? 'border-sc-yellow/40 hover:border-sc-yellow/60'
    : isDoing
      ? 'border-sc-purple/40 hover:border-sc-purple/60'
      : isOverdue
        ? 'border-sc-red/50 hover:border-sc-red/70'
        : priorityStyle.border;

  return (
    <div
      draggable={isDraggable}
      onDragStart={isDraggable ? e => onDragStart?.(e, task.id) : undefined}
      onClick={() => onClick?.(task.id)}
      className={`
        relative rounded-xl overflow-hidden shadow-card
        ${isDraggable ? 'cursor-grab active:cursor-grabbing' : 'cursor-pointer'}
        transition-all duration-200
        hover:shadow-card-hover
        hover:-translate-y-0.5
        group select-none
        border ${cardBorder} ${cardBg}
      `}
      role="button"
      tabIndex={0}
      onKeyDown={e => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onClick?.(task.id);
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
        {/* Top row: Priority badge + Project/Feature + Status indicator */}
        <div className="flex items-center justify-between gap-2 mb-2">
          <div className="flex items-center gap-1.5 min-w-0 flex-1">
            {/* Priority badge - always visible */}
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
                title={`Filter by ${projectName}`}
              >
                {projectName}
              </button>
            )}

            {/* Feature tag */}
            {feature && <span className="text-[10px] text-sc-fg-subtle truncate">{feature}</span>}
          </div>

          {/* Status indicator */}
          <div className={`shrink-0 flex items-center gap-1 text-xs ${statusConfig?.textClass}`}>
            {isBlocked && <Pause width={12} height={12} className="text-sc-yellow" />}
            {isDoing && <Clock width={12} height={12} className="text-sc-purple animate-pulse" />}
            {!isBlocked && !isDoing && <span className="opacity-60">{statusConfig?.icon}</span>}
          </div>
        </div>

        {/* Title */}
        <h4
          className={`text-sm font-medium line-clamp-2 leading-snug transition-colors ${
            isBlocked
              ? 'text-sc-yellow'
              : isDoing
                ? 'text-sc-fg-primary'
                : 'text-sc-fg-primary group-hover:text-white'
          }`}
        >
          {task.name}
        </h4>

        {/* Description preview */}
        {task.description && (
          <p className="text-xs text-sc-fg-subtle line-clamp-1 mt-1.5">{task.description}</p>
        )}

        {/* Tags */}
        {tags.length > 0 && (
          <div className="flex flex-wrap gap-1 mt-2">
            {tags.slice(0, 4).map(tag => (
              <span
                key={tag}
                className={`text-[9px] px-1.5 py-0.5 rounded-full border font-medium ${getTagStyle(tag)}`}
              >
                {tag}
              </span>
            ))}
            {tags.length > 4 && (
              <span className="text-[9px] px-1.5 py-0.5 rounded-full bg-sc-bg-elevated text-sc-fg-subtle">
                +{tags.length - 4}
              </span>
            )}
          </div>
        )}

        {/* Footer */}
        <div className="flex items-center justify-between mt-3 pt-2 border-t border-sc-fg-subtle/10">
          {/* Left: Assignees */}
          <div className="flex items-center gap-2">
            {assignees.length > 0 && (
              <div className="flex items-center -space-x-1.5">
                {assignees.slice(0, 2).map(assignee => (
                  <div
                    key={assignee}
                    className="w-5 h-5 rounded-full bg-sc-bg-surface border-2 border-sc-bg-base flex items-center justify-center text-[10px] font-medium text-sc-fg-muted"
                    title={assignee}
                  >
                    {assignee.charAt(0).toUpperCase()}
                  </div>
                ))}
                {assignees.length > 2 && (
                  <div className="w-5 h-5 rounded-full bg-sc-bg-surface border-2 border-sc-bg-base flex items-center justify-center text-[9px] text-sc-fg-subtle">
                    +{assignees.length - 2}
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Right: Due date */}
          {dueDate && (
            <span
              className={`flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded ${
                isOverdue
                  ? 'bg-sc-red/20 text-sc-red'
                  : status === 'done'
                    ? 'bg-sc-green/20 text-sc-green'
                    : 'bg-sc-bg-elevated text-sc-fg-muted'
              }`}
              title={new Date(dueDate).toLocaleDateString()}
            >
              {isOverdue && <AlertTriangle width={10} height={10} />}
              <Calendar width={10} height={10} />
              {formatDueDate(dueDate)}
            </span>
          )}
        </div>
      </div>
    </div>
  );
});

export function TaskCardSkeleton() {
  return (
    <div className="relative bg-sc-bg-base rounded-xl overflow-hidden border border-sc-fg-subtle/30 shadow-card animate-pulse">
      <div className="absolute left-0 top-0 bottom-0 w-1 bg-sc-fg-subtle/20" />
      <div className="pl-4 pr-3 py-3">
        <div className="flex items-center gap-2 mb-2">
          <div className="h-4 w-12 bg-sc-bg-elevated rounded" />
          <div className="h-4 w-16 bg-sc-bg-elevated rounded" />
        </div>
        <div className="h-4 w-full bg-sc-bg-elevated rounded mb-1" />
        <div className="h-4 w-3/4 bg-sc-bg-elevated rounded" />
        <div className="flex items-center justify-between mt-3 pt-2 border-t border-sc-fg-subtle/10">
          <div className="flex -space-x-1">
            <div className="w-5 h-5 rounded-full bg-sc-bg-elevated" />
          </div>
          <div className="h-4 w-14 bg-sc-bg-elevated rounded" />
        </div>
      </div>
    </div>
  );
}
