'use client';

import { memo } from 'react';
import type { TaskPriority, TaskStatus, TaskSummary } from '@/lib/api';
import { TASK_PRIORITY_CONFIG, TASK_STATUS_CONFIG } from '@/lib/constants';

interface TaskCardProps {
  task: TaskSummary;
  projectName?: string;
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

  if (dueDay.getTime() === today.getTime()) {
    return 'Today';
  }
  if (dueDay.getTime() === tomorrow.getTime()) {
    return 'Tomorrow';
  }

  const diffDays = Math.ceil((dueDay.getTime() - today.getTime()) / (1000 * 60 * 60 * 24));
  if (diffDays < 0) {
    return `${Math.abs(diffDays)}d overdue`;
  }
  if (diffDays <= 7) {
    return `${diffDays}d left`;
  }

  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

export const TaskCard = memo(function TaskCard({
  task,
  projectName,
  onDragStart,
  onClick,
  onProjectClick,
}: TaskCardProps) {
  const status = (task.metadata.status ?? 'todo') as TaskStatus;
  const priority = (task.metadata.priority ?? 'medium') as TaskPriority;
  const statusConfig = TASK_STATUS_CONFIG[status as keyof typeof TASK_STATUS_CONFIG];
  const priorityConfig = TASK_PRIORITY_CONFIG[priority as keyof typeof TASK_PRIORITY_CONFIG];
  const assignees = task.metadata.assignees ?? [];
  const projectId = task.metadata.project_id as string | undefined;
  const dueDate = task.metadata.due_date as string | undefined;

  // Check if overdue (not done and past due)
  const isOverdue = dueDate && status !== 'done' && new Date(dueDate) < new Date();

  return (
    <div
      draggable
      onDragStart={e => onDragStart?.(e, task.id)}
      onClick={() => onClick?.(task.id)}
      className={`
        bg-sc-bg-base border rounded-lg p-3
        cursor-grab active:cursor-grabbing
        transition-all duration-150
        hover:shadow-md
        group select-none
        ${isOverdue ? 'border-sc-red/50 hover:border-sc-red/70' : 'border-sc-fg-subtle/20 hover:border-sc-fg-subtle/40'}
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
      {/* Project badge + Priority indicator */}
      <div className="flex items-center gap-2 mb-2 flex-wrap">
        {projectName && projectId && (
          <button
            type="button"
            onClick={e => {
              e.stopPropagation();
              onProjectClick?.(projectId);
            }}
            className="text-xs px-1.5 py-0.5 rounded bg-sc-cyan/20 text-sc-cyan border border-sc-cyan/30 hover:bg-sc-cyan/30 transition-colors truncate max-w-[120px]"
            title={`View ${projectName} tasks`}
          >
            ◇ {projectName}
          </button>
        )}
        <span
          className={`text-xs px-1.5 py-0.5 rounded ${priorityConfig?.bgClass} ${priorityConfig?.textClass}`}
        >
          {priorityConfig?.label ?? priority}
        </span>
        {typeof task.metadata.feature === 'string' && task.metadata.feature && (
          <span className="text-xs text-sc-fg-subtle truncate">{task.metadata.feature}</span>
        )}
      </div>

      {/* Title */}
      <h4 className="text-sm font-medium text-sc-fg-primary line-clamp-2 mb-2 group-hover:text-sc-purple transition-colors">
        {task.name}
      </h4>

      {/* Description preview */}
      {task.description && (
        <p className="text-xs text-sc-fg-muted line-clamp-2 mb-3">{task.description}</p>
      )}

      {/* Footer */}
      <div className="flex items-center justify-between">
        {/* Left side: Assignees + Due date */}
        <div className="flex items-center gap-2">
          {/* Assignees */}
          <div className="flex items-center -space-x-1">
            {assignees.slice(0, 3).map(assignee => (
              <div
                key={assignee}
                className="w-5 h-5 rounded-full bg-sc-bg-elevated border border-sc-bg-base flex items-center justify-center text-[10px] text-sc-fg-muted"
                title={assignee}
              >
                {assignee.charAt(0).toUpperCase()}
              </div>
            ))}
            {assignees.length > 3 && (
              <div className="w-5 h-5 rounded-full bg-sc-bg-elevated border border-sc-bg-base flex items-center justify-center text-[10px] text-sc-fg-subtle">
                +{assignees.length - 3}
              </div>
            )}
          </div>

          {/* Due date badge */}
          {dueDate && (
            <span
              className={`text-[10px] px-1.5 py-0.5 rounded ${
                isOverdue
                  ? 'bg-sc-red/20 text-sc-red'
                  : status === 'done'
                    ? 'bg-sc-green/20 text-sc-green'
                    : 'bg-sc-fg-subtle/10 text-sc-fg-subtle'
              }`}
              title={new Date(dueDate).toLocaleDateString()}
            >
              {formatDueDate(dueDate)}
            </span>
          )}
        </div>

        {/* Status icon */}
        <span className={`text-sm ${statusConfig?.textClass}`} title={statusConfig?.label}>
          {statusConfig?.icon}
        </span>
      </div>
    </div>
  );
});

export function TaskCardSkeleton() {
  return (
    <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-lg p-3 animate-pulse">
      <div className="flex items-center gap-2 mb-2">
        <div className="h-4 w-12 bg-sc-bg-elevated rounded" />
      </div>
      <div className="h-4 w-full bg-sc-bg-elevated rounded mb-2" />
      <div className="h-3 w-3/4 bg-sc-bg-elevated rounded mb-3" />
      <div className="flex items-center justify-between">
        <div className="flex -space-x-1">
          <div className="w-5 h-5 rounded-full bg-sc-bg-elevated" />
        </div>
        <div className="w-4 h-4 bg-sc-bg-elevated rounded" />
      </div>
    </div>
  );
}
