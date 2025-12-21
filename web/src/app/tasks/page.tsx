'use client';

import { FolderKanban, LayoutDashboard, ListTodo } from 'lucide-react';
import { useRouter, useSearchParams } from 'next/navigation';
import { Suspense, useCallback, useState } from 'react';
import { Breadcrumb } from '@/components/layout/breadcrumb';
import { PageHeader } from '@/components/layout/page-header';
import { KanbanBoard } from '@/components/tasks/kanban-board';
import { type QuickTaskData, QuickTaskModal } from '@/components/tasks/quick-task-modal';
import { CommandPalette, useKeyboardShortcuts } from '@/components/ui/command-palette';
import { TasksEmptyState } from '@/components/ui/empty-state';
import { LoadingState } from '@/components/ui/spinner';
import { FilterChip } from '@/components/ui/toggle';
import { ErrorState } from '@/components/ui/tooltip';
import type { TaskStatus } from '@/lib/api';
import { useCreateEntity, useProjects, useTasks, useTaskUpdateStatus } from '@/lib/hooks';

function TasksPageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const projectFilter = searchParams.get('project') || undefined;

  // State for modals
  const [isCommandPaletteOpen, setIsCommandPaletteOpen] = useState(false);
  const [isQuickTaskOpen, setIsQuickTaskOpen] = useState(false);

  const { data: tasksData, isLoading, error } = useTasks({ project: projectFilter });
  const { data: projectsData } = useProjects();
  const updateStatus = useTaskUpdateStatus();
  const createEntity = useCreateEntity();

  const projects = projectsData?.entities ?? [];
  const tasks = tasksData?.entities ?? [];

  // Find current project name for filtered view
  const currentProjectName = projectFilter
    ? projects.find(p => p.id === projectFilter)?.name
    : undefined;

  // Keyboard shortcuts
  useKeyboardShortcuts({
    onCommandPalette: () => setIsCommandPaletteOpen(true),
    onCreateTask: () => setIsQuickTaskOpen(true),
  });

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

  const handleCreateTask = useCallback(
    async (task: QuickTaskData) => {
      try {
        await createEntity.mutateAsync({
          name: task.title,
          description: task.description,
          entity_type: 'task',
          metadata: {
            status: 'todo',
            priority: task.priority,
            project_id: task.projectId,
            feature: task.feature,
            assignees: task.assignees,
            due_date: task.dueDate,
            estimated_hours: task.estimatedHours,
          },
        });
        setIsQuickTaskOpen(false);
      } catch (err) {
        console.error('Failed to create task:', err);
      }
    },
    [createEntity]
  );

  // Count tasks by status for the header
  const taskCounts = {
    total: tasks.length,
    doing: tasks.filter(t => t.metadata.status === 'doing').length,
    review: tasks.filter(t => t.metadata.status === 'review').length,
    done: tasks.filter(t => t.metadata.status === 'done').length,
  };

  // Build breadcrumb items based on filter
  const breadcrumbItems = [
    { label: 'Dashboard', href: '/', icon: LayoutDashboard },
    { label: 'Tasks', href: '/tasks', icon: ListTodo },
    ...(currentProjectName ? [{ label: currentProjectName, icon: FolderKanban }] : []),
  ];

  return (
    <div className="space-y-4 animate-fade-in">
      <Breadcrumb items={breadcrumbItems} custom />

      <PageHeader
        title="Task Board"
        description={
          currentProjectName
            ? `Tasks for ${currentProjectName}`
            : 'Manage tasks with a Kanban-style workflow'
        }
        meta={`${taskCounts.total} tasks | ${taskCounts.doing} in progress | ${taskCounts.review} in review`}
        action={
          <button
            type="button"
            onClick={() => setIsQuickTaskOpen(true)}
            className="px-4 py-2 bg-sc-purple hover:bg-sc-purple/80 text-white rounded-lg font-medium transition-colors flex items-center gap-2"
          >
            <span>+</span>
            <span>New Task</span>
            <kbd className="text-xs bg-white/20 px-1.5 py-0.5 rounded ml-1">C</kbd>
          </button>
        }
      />

      {/* Project Filter */}
      <div className="flex flex-wrap gap-2">
        <FilterChip active={!projectFilter} onClick={() => handleProjectFilter(null)}>
          All Projects
        </FilterChip>
        {projects.map(project => (
          <FilterChip
            key={project.id}
            active={projectFilter === project.id}
            onClick={() => handleProjectFilter(project.id)}
          >
            {project.name}
          </FilterChip>
        ))}
      </div>

      {/* Kanban Board or Empty State */}
      {error ? (
        <ErrorState
          title="Failed to load tasks"
          message={error instanceof Error ? error.message : 'Unknown error'}
        />
      ) : tasks.length === 0 && !isLoading ? (
        <TasksEmptyState
          projectName={currentProjectName}
          onCreateTask={() => setIsQuickTaskOpen(true)}
          onClearFilter={projectFilter ? () => handleProjectFilter(null) : undefined}
        />
      ) : (
        <KanbanBoard
          tasks={tasks}
          projects={projects.map(p => ({ id: p.id, name: p.name }))}
          isLoading={isLoading}
          onStatusChange={handleStatusChange}
          onTaskClick={handleTaskClick}
          onProjectFilter={handleProjectFilter}
        />
      )}

      {/* Update status indicator */}
      {updateStatus.isPending && (
        <div className="fixed bottom-4 right-4 bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-lg px-4 py-2 text-sm text-sc-fg-muted shadow-lg">
          Updating task...
        </div>
      )}

      {/* Quick Task Modal */}
      <QuickTaskModal
        isOpen={isQuickTaskOpen}
        onClose={() => setIsQuickTaskOpen(false)}
        onSubmit={handleCreateTask}
        projects={projects.map(p => ({ id: p.id, name: p.name }))}
        defaultProjectId={projectFilter}
        isSubmitting={createEntity.isPending}
      />

      {/* Command Palette */}
      <CommandPalette
        isOpen={isCommandPaletteOpen}
        onClose={() => setIsCommandPaletteOpen(false)}
        onCreateTask={() => setIsQuickTaskOpen(true)}
      />
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
