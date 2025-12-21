'use client';

import { useRouter, useSearchParams } from 'next/navigation';
import { Suspense, useCallback } from 'react';

import { FilterChip } from '@/components/ui/toggle';
import { LoadingState } from '@/components/ui/spinner';
import { EmptyState, ErrorState } from '@/components/ui/tooltip';
import { PageHeader } from '@/components/layout/page-header';
import { KanbanBoard } from '@/components/tasks/kanban-board';
import { useProjects, useTasks, useTaskUpdateStatus } from '@/lib/hooks';
import type { TaskStatus } from '@/lib/api';

function TasksPageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const projectFilter = searchParams.get('project') || undefined;

  const { data: tasksData, isLoading, error } = useTasks({ project: projectFilter });
  const { data: projectsData } = useProjects();
  const updateStatus = useTaskUpdateStatus();

  const projects = projectsData?.entities ?? [];
  const tasks = tasksData?.entities ?? [];

  const handleProjectFilter = useCallback(
    (projectId: string | null) => {
      const params = new URLSearchParams(searchParams);
      if (projectId) {
        params.set('project', projectId);
      } else {
        params.delete('project');
      }
      router.push(`/tasks?${params.toString()}`);
    },
    [router, searchParams]
  );

  const handleStatusChange = useCallback(
    async (taskId: string, newStatus: TaskStatus) => {
      try {
        await updateStatus.mutateAsync({ id: taskId, status: newStatus });
      } catch (err) {
        console.error('Failed to update task status:', err);
      }
    },
    [updateStatus]
  );

  const handleTaskClick = useCallback(
    (taskId: string) => {
      router.push(`/tasks/${taskId}`);
    },
    [router]
  );

  // Count tasks by status for the header
  const taskCounts = {
    total: tasks.length,
    doing: tasks.filter((t) => t.metadata.status === 'doing').length,
    review: tasks.filter((t) => t.metadata.status === 'review').length,
    done: tasks.filter((t) => t.metadata.status === 'done').length,
  };

  return (
    <div className="space-y-6 animate-fade-in">
      <PageHeader
        title="Task Board"
        description="Manage tasks with a Kanban-style workflow"
        meta={`${taskCounts.total} tasks | ${taskCounts.doing} in progress | ${taskCounts.review} in review`}
      />

      {/* Project Filter */}
      <div className="flex flex-wrap gap-2">
        <FilterChip active={!projectFilter} onClick={() => handleProjectFilter(null)}>
          All Projects
        </FilterChip>
        {projects.map((project) => (
          <FilterChip
            key={project.id}
            active={projectFilter === project.id}
            onClick={() => handleProjectFilter(project.id)}
          >
            {project.name}
          </FilterChip>
        ))}
      </div>

      {/* Kanban Board */}
      {error ? (
        <ErrorState
          title="Failed to load tasks"
          message={error instanceof Error ? error.message : 'Unknown error'}
        />
      ) : tasks.length === 0 && !isLoading ? (
        <EmptyState
          icon="☰"
          title="No tasks found"
          description={
            projectFilter
              ? 'This project has no tasks yet'
              : 'Create some tasks to get started'
          }
        />
      ) : (
        <KanbanBoard
          tasks={tasks}
          isLoading={isLoading}
          onStatusChange={handleStatusChange}
          onTaskClick={handleTaskClick}
        />
      )}

      {/* Update status indicator */}
      {updateStatus.isPending && (
        <div className="fixed bottom-4 right-4 bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-lg px-4 py-2 text-sm text-sc-fg-muted shadow-lg">
          Updating task...
        </div>
      )}
    </div>
  );
}

export default function TasksPage() {
  return (
    <Suspense fallback={<LoadingState />}>
      <TasksPageContent />
    </Suspense>
  );
}
