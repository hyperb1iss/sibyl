'use client';

import * as Popover from '@radix-ui/react-popover';
import { ArrowDownAZ, ArrowUpDown, Calendar, Check, Flame, Sparkles, Zap } from 'lucide-react';
import { AnimatePresence, motion } from 'motion/react';
import { memo, useMemo, useState } from 'react';
import type { TaskStatus, TaskSummary } from '@/lib/api';
import { TASK_STATUS_CONFIG, TASK_STATUSES } from '@/lib/constants';
import { TaskCard, TaskCardSkeleton } from './task-card';

type SortOption = 'priority' | 'due_date' | 'created' | 'name' | 'manual';

const SORT_OPTIONS: Array<{ value: SortOption; label: string; icon: React.ReactNode }> = [
  { value: 'priority', label: 'Priority', icon: <Zap size={14} /> },
  { value: 'due_date', label: 'Due Date', icon: <Calendar size={14} /> },
  { value: 'created', label: 'Newest First', icon: <Sparkles size={14} /> },
  { value: 'name', label: 'Alphabetical', icon: <ArrowDownAZ size={14} /> },
  { value: 'manual', label: 'Manual', icon: <ArrowUpDown size={14} /> },
];

const PRIORITY_ORDER: Record<string, number> = {
  critical: 0,
  high: 1,
  medium: 2,
  low: 3,
  someday: 4,
};

function sortTasks(tasks: TaskSummary[], sortBy: SortOption): TaskSummary[] {
  const sorted = [...tasks];

  switch (sortBy) {
    case 'priority':
      return sorted.sort((a, b) => {
        const aPriority = PRIORITY_ORDER[a.metadata.priority as string] ?? 2;
        const bPriority = PRIORITY_ORDER[b.metadata.priority as string] ?? 2;
        if (aPriority !== bPriority) return aPriority - bPriority;
        // Secondary sort by due date
        const aDue = a.metadata.due_date as string | undefined;
        const bDue = b.metadata.due_date as string | undefined;
        if (aDue && bDue) return new Date(aDue).getTime() - new Date(bDue).getTime();
        if (aDue) return -1;
        if (bDue) return 1;
        return 0;
      });

    case 'due_date':
      return sorted.sort((a, b) => {
        const aDue = a.metadata.due_date as string | undefined;
        const bDue = b.metadata.due_date as string | undefined;
        if (aDue && bDue) return new Date(aDue).getTime() - new Date(bDue).getTime();
        if (aDue) return -1;
        if (bDue) return 1;
        // Secondary sort by priority
        const aPriority = PRIORITY_ORDER[a.metadata.priority as string] ?? 2;
        const bPriority = PRIORITY_ORDER[b.metadata.priority as string] ?? 2;
        return aPriority - bPriority;
      });

    case 'created':
      return sorted.sort((a, b) => {
        // Use metadata.created_at or fall back to id comparison (UUIDs are time-ordered)
        const aCreated = (a.metadata.created_at as string) || a.id;
        const bCreated = (b.metadata.created_at as string) || b.id;
        return bCreated.localeCompare(aCreated); // Newest first
      });

    case 'name':
      return sorted.sort((a, b) => a.name.localeCompare(b.name));

    default: // 'manual' or unknown
      return sorted.sort((a, b) => {
        const aOrder = (a.metadata.task_order as number) ?? 0;
        const bOrder = (b.metadata.task_order as number) ?? 0;
        return bOrder - aOrder; // Higher order = more important = first
      });
  }
}

interface KanbanBoardProps {
  tasks: TaskSummary[];
  projects?: Array<{ id: string; name: string }>;
  isLoading?: boolean;
  currentProjectId?: string;
  onStatusChange?: (taskId: string, newStatus: TaskStatus) => void;
  onTaskClick?: (taskId: string) => void;
  onProjectFilter?: (projectId: string) => void;
}

interface KanbanColumnProps {
  status: TaskStatus;
  tasks: TaskSummary[];
  projectMap: Map<string, string>;
  showProjectOnCards: boolean;
  sortBy: SortOption;
  onSortChange: (sort: SortOption) => void;
  onDrop: (taskId: string, status: TaskStatus) => void;
  onTaskClick?: (taskId: string) => void;
  onProjectClick?: (projectId: string) => void;
  dragOverStatus: TaskStatus | null;
  onDragOver: (status: TaskStatus) => void;
  onDragLeave: () => void;
}

const SortDropdown = memo(function SortDropdown({
  value,
  onChange,
}: {
  value: SortOption;
  onChange: (sort: SortOption) => void;
}) {
  const [isOpen, setIsOpen] = useState(false);
  const current = SORT_OPTIONS.find(o => o.value === value);

  return (
    <Popover.Root open={isOpen} onOpenChange={setIsOpen}>
      <Popover.Trigger asChild>
        <button
          type="button"
          className="p-1 rounded-md text-sc-fg-subtle hover:text-sc-fg-muted hover:bg-sc-bg-highlight/50 transition-colors"
          title={`Sort by ${current?.label}`}
        >
          <ArrowUpDown size={12} />
        </button>
      </Popover.Trigger>

      <AnimatePresence>
        {isOpen && (
          <Popover.Portal forceMount>
            <Popover.Content align="end" sideOffset={4} asChild>
              <motion.div
                initial={{ opacity: 0, y: -4, scale: 0.96 }}
                animate={{ opacity: 1, y: 0, scale: 1 }}
                exit={{ opacity: 0, y: -4, scale: 0.96 }}
                transition={{ duration: 0.15 }}
                className="z-50 w-40 bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-xl shadow-xl shadow-black/30 py-1 overflow-hidden"
              >
                {SORT_OPTIONS.map(option => (
                  <button
                    key={option.value}
                    type="button"
                    onClick={() => {
                      onChange(option.value);
                      setIsOpen(false);
                    }}
                    className={`w-full flex items-center gap-2 px-3 py-1.5 text-xs transition-colors ${
                      value === option.value
                        ? 'text-sc-purple bg-sc-purple/10'
                        : 'text-sc-fg-muted hover:text-sc-fg-primary hover:bg-sc-bg-highlight/50'
                    }`}
                  >
                    <span className="opacity-70">{option.icon}</span>
                    <span className="flex-1 text-left">{option.label}</span>
                    {value === option.value && <Check size={12} className="text-sc-purple" />}
                  </button>
                ))}
              </motion.div>
            </Popover.Content>
          </Popover.Portal>
        )}
      </AnimatePresence>
    </Popover.Root>
  );
});

const KanbanColumn = memo(function KanbanColumn({
  status,
  tasks,
  projectMap,
  showProjectOnCards,
  sortBy,
  onSortChange,
  onDrop,
  onTaskClick,
  onProjectClick,
  dragOverStatus,
  onDragOver,
  onDragLeave,
}: KanbanColumnProps) {
  const config = TASK_STATUS_CONFIG[status as keyof typeof TASK_STATUS_CONFIG];
  const isDragOver = dragOverStatus === status;

  // Sort tasks
  const sortedTasks = useMemo(() => sortTasks(tasks, sortBy), [tasks, sortBy]);

  // Count high priority tasks
  const urgentCount = tasks.filter(
    t => t.metadata.priority === 'critical' || t.metadata.priority === 'high'
  ).length;

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    onDragOver(status);
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    const taskId = e.dataTransfer.getData('text/plain');
    if (taskId) {
      onDrop(taskId, status);
    }
    onDragLeave();
  };

  return (
    <div
      className="flex-1 min-w-[300px] max-w-[380px]"
      onDragOver={handleDragOver}
      onDragLeave={onDragLeave}
      onDrop={handleDrop}
    >
      {/* Column header */}
      <div className="flex items-center justify-between mb-3 px-1">
        <div className="flex items-center gap-2">
          <span className={`text-base ${config?.textClass}`}>{config?.icon}</span>
          <h3 className="text-sm font-semibold text-sc-fg-primary">{config?.label}</h3>
          <span className="text-xs text-sc-fg-muted bg-sc-bg-elevated px-1.5 py-0.5 rounded-full">
            {tasks.length}
          </span>
          {urgentCount > 0 && (
            <span className="flex items-center gap-0.5 text-[10px] text-sc-coral bg-sc-coral/10 px-1.5 py-0.5 rounded-full">
              <Flame size={10} />
              {urgentCount}
            </span>
          )}
        </div>
        <SortDropdown value={sortBy} onChange={onSortChange} />
      </div>

      {/* Column content */}
      <div
        className={`
          min-h-[200px] p-2 rounded-xl
          bg-sc-bg-highlight/20 border-2 border-dashed
          transition-all duration-200
          ${isDragOver ? 'border-sc-purple/50 bg-sc-purple/5 scale-[1.01]' : 'border-transparent'}
        `}
      >
        <div className="space-y-2">
          {sortedTasks.map(task => {
            const projectId = task.metadata.project_id as string | undefined;
            const projectName = projectId ? projectMap.get(projectId) : undefined;

            return (
              <TaskCard
                key={task.id}
                task={task}
                projectName={projectName}
                showProject={showProjectOnCards}
                onDragStart={(e, id) => {
                  e.dataTransfer.setData('text/plain', id);
                  e.dataTransfer.effectAllowed = 'move';
                }}
                onClick={onTaskClick}
                onProjectClick={onProjectClick}
              />
            );
          })}
        </div>

        {tasks.length === 0 && !isDragOver && (
          <div className="flex flex-col items-center justify-center h-24 text-center">
            <span className="text-sc-fg-subtle text-sm">No tasks</span>
            <span className="text-sc-fg-subtle/60 text-xs mt-1">Drag tasks here</span>
          </div>
        )}

        {isDragOver && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            className="flex items-center justify-center h-14 mt-2 border-2 border-dashed border-sc-purple/40 rounded-xl bg-sc-purple/5 text-sc-purple text-sm"
          >
            Drop here
          </motion.div>
        )}
      </div>
    </div>
  );
});

export function KanbanBoard({
  tasks,
  projects,
  isLoading,
  currentProjectId,
  onStatusChange,
  onTaskClick,
  onProjectFilter,
}: KanbanBoardProps) {
  const [dragOverStatus, setDragOverStatus] = useState<TaskStatus | null>(null);
  const [columnSorts, setColumnSorts] = useState<Record<TaskStatus, SortOption>>({
    backlog: 'priority',
    todo: 'priority',
    doing: 'priority',
    blocked: 'priority',
    review: 'created',
    done: 'created',
    archived: 'created',
  });

  // Build project lookup map
  const projectMap = useMemo(() => {
    const map = new Map<string, string>();
    for (const project of projects ?? []) {
      map.set(project.id, project.name);
    }
    return map;
  }, [projects]);

  // Group tasks by status
  const tasksByStatus = useMemo(() => {
    const grouped: Record<TaskStatus, TaskSummary[]> = {
      backlog: [],
      todo: [],
      doing: [],
      blocked: [],
      review: [],
      done: [],
      archived: [],
    };

    for (const task of tasks) {
      const status = (task.metadata.status ?? 'todo') as TaskStatus;
      if (grouped[status]) {
        grouped[status].push(task);
      }
    }

    return grouped;
  }, [tasks]);

  // Don't show project on cards if we're already filtering by project
  const showProjectOnCards = !currentProjectId;

  const handleDrop = (taskId: string, newStatus: TaskStatus) => {
    onStatusChange?.(taskId, newStatus);
  };

  const handleSortChange = (status: TaskStatus, sort: SortOption) => {
    setColumnSorts(prev => ({ ...prev, [status]: sort }));
  };

  if (isLoading) {
    return (
      <div className="flex gap-4 overflow-x-auto pb-4">
        {TASK_STATUSES.map(status => (
          <div key={status} className="flex-1 min-w-[300px] max-w-[380px]">
            <div className="flex items-center gap-2 mb-3 px-1">
              <div className="w-5 h-5 bg-sc-bg-elevated rounded animate-pulse" />
              <div className="w-16 h-4 bg-sc-bg-elevated rounded animate-pulse" />
            </div>
            <div className="p-2 rounded-xl bg-sc-bg-highlight/20 space-y-2">
              <TaskCardSkeleton />
              <TaskCardSkeleton />
            </div>
          </div>
        ))}
      </div>
    );
  }

  return (
    <div className="flex gap-4 overflow-x-auto pb-4">
      {TASK_STATUSES.map(status => (
        <KanbanColumn
          key={status}
          status={status}
          tasks={tasksByStatus[status] || []}
          projectMap={projectMap}
          showProjectOnCards={showProjectOnCards}
          sortBy={columnSorts[status]}
          onSortChange={sort => handleSortChange(status, sort)}
          onDrop={handleDrop}
          onTaskClick={onTaskClick}
          onProjectClick={onProjectFilter}
          dragOverStatus={dragOverStatus}
          onDragOver={setDragOverStatus}
          onDragLeave={() => setDragOverStatus(null)}
        />
      ))}
    </div>
  );
}
