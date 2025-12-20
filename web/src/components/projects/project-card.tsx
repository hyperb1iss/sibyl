'use client';

import type { TaskSummary } from '@/lib/api';

interface ProjectCardProps {
  project: TaskSummary;
  isSelected?: boolean;
  onClick?: () => void;
  taskCounts?: {
    total: number;
    done: number;
    doing: number;
  };
}

export function ProjectCard({ project, isSelected, onClick, taskCounts }: ProjectCardProps) {
  const progress = taskCounts && taskCounts.total > 0
    ? Math.round((taskCounts.done / taskCounts.total) * 100)
    : 0;

  return (
    <button
      type="button"
      onClick={onClick}
      className={`
        w-full text-left p-3 rounded-lg transition-all duration-150
        border
        ${isSelected
          ? 'bg-sc-purple/10 border-sc-purple/40 shadow-sm'
          : 'bg-sc-bg-base border-sc-fg-subtle/20 hover:border-sc-fg-subtle/40 hover:bg-sc-bg-highlight/50'
        }
      `}
    >
      <h3 className={`font-medium truncate ${isSelected ? 'text-sc-purple' : 'text-sc-fg-primary'}`}>
        {project.name}
      </h3>

      {project.description && (
        <p className="text-xs text-sc-fg-muted mt-1 line-clamp-2">
          {project.description}
        </p>
      )}

      {taskCounts && (
        <div className="mt-2">
          {/* Progress bar */}
          <div className="h-1 bg-sc-bg-elevated rounded-full overflow-hidden">
            <div
              className="h-full bg-sc-green transition-all duration-300"
              style={{ width: `${progress}%` }}
            />
          </div>
          <div className="flex justify-between mt-1 text-[10px] text-sc-fg-subtle">
            <span>{taskCounts.done}/{taskCounts.total} tasks</span>
            <span>{progress}%</span>
          </div>
        </div>
      )}
    </button>
  );
}

export function ProjectCardSkeleton() {
  return (
    <div className="p-3 rounded-lg bg-sc-bg-base border border-sc-fg-subtle/20 animate-pulse">
      <div className="h-4 w-3/4 bg-sc-bg-elevated rounded" />
      <div className="h-3 w-full bg-sc-bg-elevated rounded mt-2" />
      <div className="h-1 w-full bg-sc-bg-elevated rounded mt-3" />
    </div>
  );
}
