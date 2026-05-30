'use client';

import { useRouter } from 'next/navigation';
import { use, useMemo } from 'react';
import { RelatedEntitiesSection } from '@/components/entities/related-entities-section';
import { EntityBreadcrumb } from '@/components/layout/breadcrumb';
import { EpicDetailSkeleton } from '@/components/suspense-boundary';
import { EnhancedEmptyState } from '@/components/ui/empty-state';
import { CheckCircle, Clock, Layers, Pause, Target, Zap } from '@/components/ui/icons';
import { Skeleton } from '@/components/ui/spinner';
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

  // Task progress - calculate in single pass
  const { totalTasks, doneTasks, doingTasks, blockedTasks, progressPercent } = useMemo(() => {
    let done = 0;
    let doing = 0;
    let blocked = 0;
    for (const task of tasks) {
      const status = task.metadata?.status as string;
      if (status === 'done' || status === 'archived') done++;
      else if (status === 'doing') doing++;
      else if (status === 'blocked') blocked++;
    }
    const total = tasks.length;
    return {
      totalTasks: total,
      doneTasks: done,
      doingTasks: doing,
      blockedTasks: blocked,
      progressPercent: total > 0 ? Math.round((done / total) * 100) : 0,
    };
  }, [tasks]);

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
    return <EpicDetailSkeleton />;
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
          ${!isBlocked && !isInProgress && !isCompleted ? 'border-sc-orange/30 bg-sc-bg-base' : ''}
        `}
      >
        {/* Priority accent bar */}
        <div
          className={`absolute left-0 top-0 bottom-0 w-1 ${priorityConfig?.bgClass ?? 'bg-sc-orange'}`}
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
              className={`inline-flex items-center gap-1 text-xs font-bold px-2 py-0.5 rounded border ${priorityConfig?.bgClass ?? 'bg-sc-orange/20'} ${priorityConfig?.textClass ?? 'text-sc-orange'} ${priorityConfig?.borderClass ?? 'border-sc-orange/30'}`}
            >
              {priority === 'critical' && <Zap width={12} height={12} className="animate-pulse" />}
              {priorityConfig?.label ?? priority}
            </span>

            {/* Epic badge */}
            <span className="text-xs px-2 py-0.5 rounded bg-sc-orange/20 text-sc-orange border border-sc-orange/30 font-medium">
              Epic
            </span>

            {/* Project */}
            {parentProject && (
              <button
                type="button"
                onClick={() => router.push(`/projects/${parentProject.id}`)}
                aria-label={`View project ${parentProject.name}`}
                className="text-xs px-2 py-0.5 rounded bg-sc-bg-elevated text-sc-fg-muted hover:text-sc-cyan hover:bg-sc-cyan/10 transition-colors duration-200 flex items-center gap-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base"
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
                    progressPercent === 100 ? 'text-sc-green font-medium' : 'text-sc-fg-muted'
                  }
                >
                  {progressPercent}%
                </span>
              </div>
              <div className="h-2 bg-sc-bg-elevated rounded-full overflow-hidden">
                <div
                  className={`h-full transition-all duration-300 ${progressPercent === 100 ? 'bg-sc-green' : 'bg-sc-orange'}`}
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
          <Layers width={18} height={18} className="text-sc-orange" />
          Tasks
          <span className="text-sm text-sc-fg-muted font-normal">({totalTasks})</span>
        </h2>

        {tasksLoading ? (
          <div className="space-y-2">
            {Array.from({ length: 4 }).map((_, i) => (
              <div
                key={i}
                className="flex items-center gap-3 rounded-lg border border-sc-fg-subtle/10 bg-sc-bg-base p-3"
              >
                <Skeleton className="h-4 w-4 rounded-full" />
                <Skeleton className="h-4 flex-1 max-w-md" />
                <Skeleton className="h-4 w-16 rounded-full" />
              </div>
            ))}
          </div>
        ) : tasks.length === 0 ? (
          <div className="bg-sc-bg-base border border-sc-fg-subtle/10 rounded-xl">
            <EnhancedEmptyState
              icon={<Layers width={40} height={40} className="text-sc-orange" />}
              title="No tasks in this epic yet"
              description="Tasks grouped under this epic will show up here. Add tasks to start tracking progress toward the initiative."
              actions={[{ label: 'View Tasks', href: '/tasks', variant: 'secondary' }]}
            />
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
                  aria-label={`Open task ${task.name}`}
                  className="w-full flex items-center gap-3 p-3 bg-sc-bg-base border border-sc-fg-subtle/10 rounded-lg hover:border-sc-fg-subtle/30 hover:bg-sc-bg-elevated transition-colors duration-200 text-left group focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base"
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
                  <span className="flex-1 text-sm text-sc-fg-primary group-hover:text-sc-cyan transition-colors duration-200 truncate">
                    {task.name}
                  </span>

                  {/* Priority badge */}
                  <span className="shrink-0 text-[10px] px-1.5 py-0.5 rounded bg-sc-bg-elevated text-sc-fg-muted uppercase">
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

      {/* Related Entities Section */}
      {epic.related && epic.related.length > 0 && (
        <div className="bg-sc-bg-base border border-sc-fg-subtle/10 rounded-xl p-4">
          <RelatedEntitiesSection
            entityId={epic.id}
            entityName={epic.name}
            entityType="epic"
            related={epic.related}
            title="Related Entities"
          />
        </div>
      )}
    </div>
  );
}
