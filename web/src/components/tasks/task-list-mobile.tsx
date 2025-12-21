'use client';

import { AnimatePresence, motion } from 'motion/react';
import { memo, useMemo, useState } from 'react';
import { ArrowDownAZ, Calendar, Sparkles, Zap } from '@/components/ui/icons';
import type { TaskStatus, TaskSummary } from '@/lib/api';
import { TASK_STATUS_CONFIG, type TaskStatusType } from '@/lib/constants';
import { TaskCard } from './task-card';

// Mobile-friendly status tabs (fewer options for cleaner UX)
const MOBILE_STATUSES: TaskStatusType[] = ['todo', 'doing', 'review', 'done'];

type SortOption = 'priority' | 'due_date' | 'created' | 'name';

const SORT_OPTIONS: Array<{ value: SortOption; label: string; icon: React.ReactNode }> = [
  { value: 'priority', label: 'Priority', icon: <Zap width={14} height={14} /> },
  { value: 'due_date', label: 'Due Date', icon: <Calendar width={14} height={14} /> },
  { value: 'created', label: 'Newest', icon: <Sparkles width={14} height={14} /> },
  { value: 'name', label: 'A-Z', icon: <ArrowDownAZ width={14} height={14} /> },
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
        return PRIORITY_ORDER[a.metadata.priority as string] ?? 2 - (PRIORITY_ORDER[b.metadata.priority as string] ?? 2);
      });

    case 'created':
      return sorted.sort((a, b) => {
        const aCreated = (a.metadata.created_at as string) || a.id;
        const bCreated = (b.metadata.created_at as string) || b.id;
        return bCreated.localeCompare(aCreated);
      });

    case 'name':
      return sorted.sort((a, b) => a.name.localeCompare(b.name));

    default:
      return sorted;
  }
}

interface TaskListMobileProps {
  tasks: TaskSummary[];
  projects?: Array<{ id: string; name: string }>;
  currentProjectId?: string;
  onStatusChange?: (taskId: string, newStatus: TaskStatus) => void;
  onTaskClick?: (taskId: string) => void;
  onProjectFilter?: (projectId: string) => void;
}

export const TaskListMobile = memo(function TaskListMobile({
  tasks,
  projects,
  currentProjectId,
  onStatusChange,
  onTaskClick,
  onProjectFilter,
}: TaskListMobileProps) {
  const [activeStatus, setActiveStatus] = useState<TaskStatusType>('todo');
  const [sortBy, setSortBy] = useState<SortOption>('priority');
  const [showSortMenu, setShowSortMenu] = useState(false);

  const projectMap = useMemo(() => {
    const map = new Map<string, string>();
    for (const project of projects ?? []) {
      map.set(project.id, project.name);
    }
    return map;
  }, [projects]);

  const showProjectOnCards = !currentProjectId;

  // Count tasks per status for badges
  const statusCounts = useMemo(() => {
    const counts: Record<TaskStatusType, number> = {
      backlog: 0,
      todo: 0,
      doing: 0,
      blocked: 0,
      review: 0,
      done: 0,
    };
    for (const task of tasks) {
      const status = (task.metadata.status ?? 'todo') as TaskStatusType;
      if (counts[status] !== undefined) {
        counts[status]++;
      }
    }
    return counts;
  }, [tasks]);

  // Filter and sort tasks by active status
  const filteredTasks = useMemo(() => {
    const filtered = tasks.filter(t => (t.metadata.status ?? 'todo') === activeStatus);
    return sortTasks(filtered, sortBy);
  }, [tasks, activeStatus, sortBy]);

  return (
    <div className="space-y-4">
      {/* Status Tabs + Sort */}
      <div className="flex gap-1 p-1 bg-sc-bg-elevated rounded-xl">
        {MOBILE_STATUSES.map(status => {
          const config = TASK_STATUS_CONFIG[status];
          const count = statusCounts[status];
          const isActive = activeStatus === status;

          return (
            <button
              key={status}
              type="button"
              onClick={() => setActiveStatus(status)}
              className={`
                flex-1 flex items-center justify-center gap-1.5 py-2 px-2 rounded-lg
                text-xs font-medium transition-all duration-200
                ${
                  isActive
                    ? `${config.bgClass} ${config.textClass}`
                    : 'text-sc-fg-muted hover:text-sc-fg-primary hover:bg-sc-bg-highlight/50'
                }
              `}
            >
              <span>{config.icon}</span>
              <span className="hidden xs:inline">{config.label}</span>
              {count > 0 && (
                <span
                  className={`
                    text-[10px] px-1.5 py-0.5 rounded-full
                    ${isActive ? 'bg-white/20' : 'bg-sc-bg-highlight'}
                  `}
                >
                  {count}
                </span>
              )}
            </button>
          );
        })}

        {/* Sort dropdown */}
        <div className="relative flex items-center">
          <button
            type="button"
            onClick={() => setShowSortMenu(!showSortMenu)}
            className="flex items-center justify-center w-8 h-8 rounded-lg text-sc-fg-muted hover:text-sc-fg-primary hover:bg-sc-bg-highlight/50 transition-colors"
            title={`Sort by ${SORT_OPTIONS.find(o => o.value === sortBy)?.label}`}
          >
            {SORT_OPTIONS.find(o => o.value === sortBy)?.icon}
          </button>

          {showSortMenu && (
            <>
              <div
                className="fixed inset-0 z-10"
                onClick={() => setShowSortMenu(false)}
              />
              <div className="absolute right-0 top-full mt-1 z-20 bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-lg shadow-lg py-1 min-w-[120px]">
                {SORT_OPTIONS.map(option => (
                  <button
                    key={option.value}
                    type="button"
                    onClick={() => {
                      setSortBy(option.value);
                      setShowSortMenu(false);
                    }}
                    className={`
                      w-full flex items-center gap-2 px-3 py-2 text-xs transition-colors
                      ${sortBy === option.value
                        ? 'text-sc-purple bg-sc-purple/10'
                        : 'text-sc-fg-muted hover:text-sc-fg-primary hover:bg-sc-bg-highlight'
                      }
                    `}
                  >
                    {option.icon}
                    <span>{option.label}</span>
                  </button>
                ))}
              </div>
            </>
          )}
        </div>
      </div>

      {/* Task List */}
      <div className="space-y-2">
        <AnimatePresence mode="popLayout">
          {filteredTasks.length === 0 ? (
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="flex flex-col items-center justify-center py-12 text-center"
            >
              <span className="text-2xl mb-2">
                {TASK_STATUS_CONFIG[activeStatus].icon}
              </span>
              <span className="text-sc-fg-muted text-sm">
                No {TASK_STATUS_CONFIG[activeStatus].label.toLowerCase()} tasks
              </span>
            </motion.div>
          ) : (
            filteredTasks.map(task => {
              const projectId = task.metadata.project_id as string | undefined;
              const projectName = projectId ? projectMap.get(projectId) : undefined;

              return (
                <motion.div
                  key={task.id}
                  layout
                  initial={{ opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, scale: 0.95 }}
                  transition={{ duration: 0.2 }}
                >
                  <TaskCard
                    task={task}
                    projectName={projectName}
                    showProject={showProjectOnCards}
                    draggable={false}
                    onClick={onTaskClick}
                    onProjectClick={onProjectFilter}
                  />
                </motion.div>
              );
            })
          )}
        </AnimatePresence>
      </div>
    </div>
  );
});
