'use client';

import { useRouter, useSearchParams } from 'next/navigation';
import { Suspense, useCallback, useMemo, useState } from 'react';
import { toast } from 'sonner';
import { KanbanBoard } from '@/components/tasks/kanban-board';
import { type QuickTaskData, QuickTaskModal } from '@/components/tasks/quick-task-modal';
import { TaskListMobile } from '@/components/tasks/task-list-mobile';
import { CommandPalette, useKeyboardShortcuts } from '@/components/ui/command-palette';
import { TasksEmptyState } from '@/components/ui/empty-state';
import { Hash, Search, X } from '@/components/ui/icons';
import { LoadingState } from '@/components/ui/spinner';
import { TagChip } from '@/components/ui/toggle';
import { ErrorState } from '@/components/ui/tooltip';
import type { TaskStatus } from '@/lib/api';
import { useCreateEntity, useEpics, useProjects, useTasks, useTaskUpdateStatus } from '@/lib/hooks';
import { useProjectFilters } from '@/lib/project-context';

function TasksPageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();

  // Project filtering is handled by global context (header selector)
  const projectFilters = useProjectFilters();
  const tagFilter = searchParams.get('tag') || undefined;

  // State for modals and search
  const [isCommandPaletteOpen, setIsCommandPaletteOpen] = useState(false);
  const [isQuickTaskOpen, setIsQuickTaskOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');

  const { data: tasksData, isLoading, error } = useTasks({ project_ids: projectFilters });
  const { data: projectsData } = useProjects();
  const { data: epicsData } = useEpics();
  const updateStatus = useTaskUpdateStatus();
  const createEntity = useCreateEntity();

  const projects = projectsData?.entities ?? [];
  const allTasks = tasksData?.entities ?? [];
  const epics = (epicsData?.entities ?? []).map(e => ({
    id: e.id,
    name: e.name,
    projectId: e.metadata?.project_id as string | undefined,
  }));

  // Extract all unique tags from tasks
  const allTags = useMemo(() => {
    const tagSet = new Set<string>();
    for (const task of allTasks) {
      const tags = (task.metadata.tags as string[]) ?? [];
      for (const tag of tags) {
        tagSet.add(tag);
      }
    }
    return Array.from(tagSet).sort();
  }, [allTasks]);

  // Filter tasks by tag and search query
  const tasks = useMemo(() => {
    let filtered = allTasks;

    // Filter by tag
    if (tagFilter) {
      filtered = filtered.filter(task => {
        const tags = (task.metadata.tags as string[]) ?? [];
        return tags.includes(tagFilter);
      });
    }

    // Filter by search query (name, description, feature)
    if (searchQuery.trim()) {
      const query = searchQuery.toLowerCase();
      filtered = filtered.filter(task => {
        const name = task.name?.toLowerCase() ?? '';
        const description = task.description?.toLowerCase() ?? '';
        const feature = ((task.metadata.feature as string) ?? '').toLowerCase();
        return name.includes(query) || description.includes(query) || feature.includes(query);
      });
    }

    return filtered;
  }, [allTasks, tagFilter, searchQuery]);

  // Keyboard shortcuts
  useKeyboardShortcuts({
    onCommandPalette: () => setIsCommandPaletteOpen(true),
    onCreateTask: () => setIsQuickTaskOpen(true),
  });

  const handleTagFilter = useCallback(
    (tag: string | null) => {
      const params = new URLSearchParams(searchParams);
      if (tag) {
        params.set('tag', tag);
      } else {
        params.delete('tag');
      }
      router.push(`/tasks?${params.toString()}`);
    },
    [router, searchParams]
  );

  const handleStatusChange = useCallback(
    async (taskId: string, newStatus: TaskStatus) => {
      try {
        await updateStatus.mutateAsync({ id: taskId, status: newStatus });
      } catch (_err) {
        toast.error('Failed to update task status');
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
            epic_id: task.epicId,
            feature: task.feature,
            assignees: task.assignees,
            due_date: task.dueDate,
            estimated_hours: task.estimatedHours,
          },
        });
        setIsQuickTaskOpen(false);
        toast.success('Task created');
      } catch (_err) {
        toast.error('Failed to create task');
      }
    },
    [createEntity]
  );

  return (
    <div className="space-y-4 animate-fade-in">
      {/* Search + Filters */}
      <div className="space-y-2 sm:space-y-3">
        {/* Search Input - Full width on all sizes */}
        <div className="relative">
          <Search
            width={16}
            height={16}
            className="absolute left-3 top-1/2 -translate-y-1/2 text-sc-fg-subtle"
          />
          <input
            type="text"
            value={searchQuery}
            onChange={e => setSearchQuery(e.target.value)}
            placeholder="Search tasks by name, description, or feature..."
            className="w-full pl-9 pr-3 py-2 bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-lg text-sm text-sc-fg-primary placeholder:text-sc-fg-subtle focus:border-sc-purple focus:outline-none focus:ring-2 focus:ring-sc-purple/10 transition-all"
          />
          {searchQuery && (
            <button
              type="button"
              onClick={() => setSearchQuery('')}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-sc-fg-subtle hover:text-sc-fg-primary"
            >
              <X width={14} height={14} />
            </button>
          )}
        </div>

        {/* New Task Button */}
        <div className="flex items-center justify-end">
          <button
            type="button"
            onClick={() => setIsQuickTaskOpen(true)}
            className="shrink-0 px-4 py-2 bg-sc-purple hover:bg-sc-purple/80 text-white rounded-lg font-medium transition-colors flex items-center gap-1.5 text-sm"
          >
            <span>+</span>
            <span className="hidden sm:inline">New Task</span>
            <span className="sm:hidden">New</span>
            <kbd className="hidden sm:inline text-xs bg-white/20 px-1.5 py-0.5 rounded ml-1">C</kbd>
          </button>
        </div>

        {/* Tag Filter - Desktop only */}
        {allTags.length > 0 && (
          <div className="hidden sm:flex flex-wrap items-center gap-2">
            <span className="text-xs text-sc-fg-subtle font-medium flex items-center gap-1">
              <Hash width={12} height={12} />
              Tags:
            </span>
            {tagFilter && (
              <button
                type="button"
                onClick={() => handleTagFilter(null)}
                className="text-xs text-sc-fg-muted hover:text-sc-fg-primary flex items-center gap-1 px-2 py-0.5 rounded bg-sc-bg-elevated hover:bg-sc-bg-highlight transition-colors"
              >
                <X width={12} height={12} />
                Clear
              </button>
            )}
            {allTags.map(tag => (
              <TagChip
                key={tag}
                tag={tag}
                active={tagFilter === tag}
                onClick={() => handleTagFilter(tagFilter === tag ? null : tag)}
              />
            ))}
          </div>
        )}
      </div>

      {/* Task Board - Mobile List / Desktop Kanban */}
      {error ? (
        <ErrorState
          title="Failed to load tasks"
          message={error instanceof Error ? error.message : 'Unknown error'}
        />
      ) : tasks.length === 0 && !isLoading ? (
        <TasksEmptyState onCreateTask={() => setIsQuickTaskOpen(true)} />
      ) : (
        <>
          {/* Mobile: Filtered list with status tabs */}
          <div className="md:hidden">
            <TaskListMobile
              tasks={tasks}
              projects={projects.map(p => ({ id: p.id, name: p.name }))}
              onStatusChange={handleStatusChange}
              onTaskClick={handleTaskClick}
            />
          </div>

          {/* Desktop: Full Kanban board */}
          <div className="hidden md:block">
            <KanbanBoard
              tasks={tasks}
              projects={projects.map(p => ({ id: p.id, name: p.name }))}
              isLoading={isLoading}
              onStatusChange={handleStatusChange}
              onTaskClick={handleTaskClick}
            />
          </div>
        </>
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
        epics={epics}
        defaultProjectId={projectFilters?.length === 1 ? projectFilters[0] : undefined}
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
