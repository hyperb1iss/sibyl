'use client';

import { use, useMemo } from 'react';
import { EntityBreadcrumb } from '@/components/layout/breadcrumb';
import { TaskDetailPanel, TaskDetailSkeleton } from '@/components/tasks';
import { ErrorState } from '@/components/ui/tooltip';
import { useProjects, useTask } from '@/lib/hooks';

interface TaskDetailPageProps {
  params: Promise<{ id: string }>;
}

export default function TaskDetailPage({ params }: TaskDetailPageProps) {
  const { id } = use(params);
  const { data: task, isLoading, error } = useTask(id);
  const { data: projectsData } = useProjects();

  // Find parent project for breadcrumb
  const parentProject = useMemo(() => {
    const projectId = task?.metadata?.project_id as string | undefined;
    if (!projectId || !projectsData?.entities) return undefined;
    const project = projectsData.entities.find(p => p.id === projectId);
    return project ? { id: project.id, name: project.name } : undefined;
  }, [task, projectsData]);

  if (error) {
    return (
      <div className="space-y-4 animate-fade-in">
        <EntityBreadcrumb entityType="task" entityName="Error" />
        <ErrorState
          title="Failed to load task"
          message={error instanceof Error ? error.message : 'Unknown error'}
        />
      </div>
    );
  }

  return (
    <div className="space-y-6 animate-fade-in">
      {/* Fluid breadcrumb navigation */}
      <EntityBreadcrumb
        entityType="task"
        entityName={task?.name || 'Loading...'}
        parentProject={parentProject}
      />

      {isLoading || !task ? <TaskDetailSkeleton /> : <TaskDetailPanel task={task} />}
    </div>
  );
}
