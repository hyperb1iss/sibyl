'use client';

import { useRouter } from 'next/navigation';
import { use, useMemo } from 'react';
import { EntityBreadcrumb } from '@/components/layout/breadcrumb';
import { CheckCircle, Clock, Layers, Pause, Target, Zap } from '@/components/ui/icons';
import { LoadingState } from '@/components/ui/spinner';
import { ErrorState } from '@/components/ui/tooltip';
import type { EpicStatus, TaskPriority } from '@/lib/api';
import { EPIC_STATUS_CONFIG, TASK_PRIORITY_CONFIG, TASK_STATUS_CONFIG } from '@/lib/constants';
import { useEpic, useEpicTasks, useProjects } from '@/lib/hooks';

interface EpicDetailPageProps {
  params: Promise<{ id: string }>;
}

export default function EpicDetailPage({ params }: EpicDetailPageProps) {
  const { id } = use(params);
  const router = useRouter();
  const { data: epic, isLoading, error } = useEpic(id);
  const { data: tasksData, isLoading: tasksLoading } = useEpicTasks(id);
  const { data: projectsData } = useProjects();

  // Find parent project for breadcrumb
  const parentProject = useMemo(() => {
    const projectId = epic?.metadata?.project_id as string | undefined;
    if (!projectId || !projectsData?.entities) return undefined;
    const project = projectsData.entities.find(p => p.id === projectId);
    return project ? { id: project.id, name: project.name } : undefined;
  }, [epic, projectsData]);

  const tasks = tasksData?.entities ?? [];
  const priority = (epic?.metadata?.priority ?? 'medium') as TaskPriority;
  const priorityConfig = TASK_PRIORITY_CONFIG[priority];

  // Task progress - this drives epic status
  const totalTasks = tasks.length;
  const doneTasks = tasks.filter(t =>
    ['done', 'archived'].includes(t.metadata?.status as string)
  ).length;
  const doingTasks = tasks.filter(t => t.metadata?.status === 'doing').length;
  const blockedTasks = tasks.filter(t => t.metadata?.status === 'blocked').length;
  const progressPercent = totalTasks > 0 ? Math.round((doneTasks / totalTasks) * 100) : 0;

  // Derive status from task states (epics auto-complete when all tasks done)
  const derivedStatus: EpicStatus = useMemo(() => {
    if (totalTasks === 0) return 'planning';
    if (progressPercent === 100) return 'completed';
    if (blockedTasks > 0) return 'blocked';
    if (doingTasks > 0) return 'in_progress';
    return 'planning';
  }, [totalTasks, progressPercent, blockedTasks, doingTasks]);

  const statusConfig = EPIC_STATUS_CONFIG[derivedStatus];

  if (error) {
    return (
      <div className="space-y-4 animate-fade-in">
        <EntityBreadcrumb entityType="epic" entityName="Error" />
        <ErrorState
          title="Failed to load epic"
          message={error instanceof Error ? error.message : 'Unknown error'}
        />
      </div>
    );
  }

  if (isLoading || !epic) {
    return (
      <div className="space-y-4 animate-fade-in">
        <EntityBreadcrumb entityType="epic" entityName="Loading..." />
        <LoadingState />
      </div>
    );
  }

  const isBlocked = derivedStatus === 'blocked';
  const isInProgress = derivedStatus === 'in_progress';
  const isCompleted = derivedStatus === 'completed';

  return (
    <div className="space-y-6 animate-fade-in">
      {/* Breadcrumb */}
      <EntityBreadcrumb entityType="epic" entityName={epic.name} parentProject={parentProject} />

      {/* Header Card */}
      <div
        className={`
          relative rounded-xl overflow-hidden border
          ${isBlocked ? 'border-sc-red/40 bg-gradient-to-br from-sc-red/10 via-sc-bg-base to-sc-bg-base' : ''}
          ${isInProgress ? 'border-sc-purple/40 bg-gradient-to-br from-sc-purple/10 via-sc-bg-base to-sc-bg-base' : ''}
          ${isCompleted ? 'border-sc-green/40 bg-gradient-to-br from-sc-green/10 via-sc-bg-base to-sc-bg-base' : ''}
          ${!isBlocked && !isInProgress && !isCompleted ? 'border-[#ffb86c]/30 bg-sc-bg-base' : ''}
        `}
      >
        {/* Priority accent bar */}
        <div
          className={`absolute left-0 top-0 bottom-0 w-1 ${priorityConfig?.bgClass ?? 'bg-[#ffb86c]'}`}
        />

        <div className="pl-5 pr-4 py-4">
          {/* Status Banner - prominent at top */}
          <div
            className={`flex items-center gap-2 px-3 py-2 rounded-lg mb-4 ${statusConfig?.bgClass}`}
          >
            {isBlocked && <Pause width={16} height={16} className="text-sc-red" />}
            {isInProgress && (
              <Clock width={16} height={16} className="text-sc-purple animate-pulse" />
            )}
            {isCompleted && <CheckCircle width={16} height={16} className="text-sc-green" />}
            {!isBlocked && !isInProgress && !isCompleted && (
              <Layers width={16} height={16} className="text-sc-cyan" />
            )}
            <span className={`text-sm font-medium ${statusConfig?.textClass}`}>
              {isCompleted
                ? 'All tasks complete'
                : isBlocked
                  ? `${blockedTasks} task${blockedTasks > 1 ? 's' : ''} blocked`
                  : isInProgress
                    ? `${doingTasks} task${doingTasks > 1 ? 's' : ''} in progress`
                    : totalTasks === 0
                      ? 'No tasks yet'
                      : `${totalTasks} task${totalTasks > 1 ? 's' : ''} planned`}
            </span>
            <span className={`ml-auto text-sm font-bold ${statusConfig?.textClass}`}>
              {progressPercent}%
            </span>
          </div>

          {/* Metadata row */}
          <div className="flex items-center gap-2 mb-3 flex-wrap">
            {/* Priority */}
            <span
              className={`inline-flex items-center gap-1 text-xs font-bold px-2 py-0.5 rounded border ${priorityConfig?.bgClass ?? 'bg-[#ffb86c]/20'} ${priorityConfig?.textClass ?? 'text-[#ffb86c]'} border-current/30`}
            >
              {priority === 'critical' && <Zap width={12} height={12} className="animate-pulse" />}
              {priorityConfig?.label ?? priority}
            </span>

            {/* Epic badge */}
            <span className="text-xs px-2 py-0.5 rounded bg-[#ffb86c]/20 text-[#ffb86c] border border-[#ffb86c]/30 font-medium">
              Epic
            </span>

            {/* Project */}
            {parentProject && (
              <button
                type="button"
                onClick={() => router.push(`/projects/${parentProject.id}`)}
                className="text-xs px-2 py-0.5 rounded bg-sc-bg-elevated text-sc-fg-muted hover:text-sc-cyan hover:bg-sc-cyan/10 transition-colors flex items-center gap-1"
              >
                <Target width={12} height={12} />
                {parentProject.name}
              </button>
            )}
          </div>

          {/* Title */}
          <h1 className="text-xl font-semibold text-sc-fg-primary mb-2">{epic.name}</h1>

          {/* Description */}
          {epic.description && (
            <p className="text-sm text-sc-fg-muted leading-relaxed mb-4">{epic.description}</p>
          )}

          {/* Progress bar */}
          {totalTasks > 0 && (
            <div className="mb-4">
              <div className="flex items-center justify-between text-xs mb-1.5">
                <span className="text-sc-fg-muted flex items-center gap-1.5">
                  <Layers width={12} height={12} />
                  {doneTasks}/{totalTasks} tasks completed
                </span>
                <span
                  className={
                    progressPercent === 100 ? 'text-sc-green font-medium' : 'text-sc-fg-subtle'
                  }
                >
                  {progressPercent}%
                </span>
              </div>
              <div className="h-2 bg-sc-bg-elevated rounded-full overflow-hidden">
                <div
                  className={`h-full transition-all duration-300 ${progressPercent === 100 ? 'bg-sc-green' : 'bg-[#ffb86c]'}`}
                  style={{ width: `${progressPercent}%` }}
                />
              </div>
              {(doingTasks > 0 || blockedTasks > 0) && (
                <div className="flex items-center gap-3 mt-1.5 text-xs">
                  {doingTasks > 0 && (
                    <span className="text-sc-purple">{doingTasks} in progress</span>
                  )}
                  {blockedTasks > 0 && <span className="text-sc-red">{blockedTasks} blocked</span>}
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Tasks Section */}
      <div className="space-y-3">
        <h2 className="text-lg font-medium text-sc-fg-primary flex items-center gap-2">
          <Layers width={18} height={18} className="text-[#ffb86c]" />
          Tasks
          <span className="text-sm text-sc-fg-muted font-normal">({totalTasks})</span>
        </h2>

        {tasksLoading ? (
          <div className="bg-sc-bg-base border border-sc-fg-subtle/10 rounded-lg p-8 text-center">
            <LoadingState />
          </div>
        ) : tasks.length === 0 ? (
          <div className="bg-sc-bg-base border border-sc-fg-subtle/10 rounded-lg p-8 text-center">
            <p className="text-sm text-sc-fg-muted">No tasks in this epic yet</p>
          </div>
        ) : (
          <div className="space-y-2">
            {tasks.map(task => {
              const taskStatus = (task.metadata?.status as string) ?? 'backlog';
              const taskPriority = (task.metadata?.priority as string) ?? 'medium';
              const taskStatusConfig =
                TASK_STATUS_CONFIG[taskStatus as keyof typeof TASK_STATUS_CONFIG];

              return (
                <button
                  key={task.id}
                  type="button"
                  onClick={() => router.push(`/tasks/${task.id}`)}
                  className="w-full flex items-center gap-3 p-3 bg-sc-bg-base border border-sc-fg-subtle/10 rounded-lg hover:border-sc-fg-subtle/30 hover:bg-sc-bg-elevated transition-colors text-left group"
                >
                  {/* Status indicator */}
                  <span
                    className={`shrink-0 w-2 h-2 rounded-full ${
                      taskStatus === 'done'
                        ? 'bg-sc-green'
                        : taskStatus === 'doing'
                          ? 'bg-sc-purple'
                          : taskStatus === 'blocked'
                            ? 'bg-sc-red'
                            : taskStatus === 'review'
                              ? 'bg-sc-cyan'
                              : 'bg-sc-fg-subtle'
                    }`}
                  />

                  {/* Task name */}
                  <span className="flex-1 text-sm text-sc-fg-primary group-hover:text-white truncate">
                    {task.name}
                  </span>

                  {/* Priority badge */}
                  <span className="shrink-0 text-[10px] px-1.5 py-0.5 rounded bg-sc-bg-elevated text-sc-fg-subtle uppercase">
                    {taskPriority}
                  </span>

                  {/* Status badge */}
                  <span
                    className={`shrink-0 text-[10px] px-1.5 py-0.5 rounded ${taskStatusConfig?.bgClass} ${taskStatusConfig?.textClass}`}
                  >
                    {taskStatusConfig?.label ?? taskStatus}
                  </span>
                </button>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
