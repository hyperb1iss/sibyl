'use client';

import { useRouter, useSearchParams } from 'next/navigation';
import { useCallback, useMemo } from 'react';

import { EmptyState, ErrorState } from '@/components/ui/tooltip';
import { PageHeader } from '@/components/layout/page-header';
import { ProjectCard, ProjectCardSkeleton } from '@/components/projects/project-card';
import { ProjectDetail, ProjectDetailSkeleton } from '@/components/projects/project-detail';
import { useProjects, useTasks } from '@/lib/hooks';
import type { TaskListResponse } from '@/lib/api';

interface ProjectsContentProps {
  initialProjects: TaskListResponse;
}

export function ProjectsContent({ initialProjects }: ProjectsContentProps) {
  const router = useRouter();
  const searchParams = useSearchParams();

  const selectedProjectId = searchParams.get('id');

  // Hydrate from server data
  const { data: projectsData, isLoading: projectsLoading, error: projectsError } = useProjects(initialProjects);
  const { data: tasksData, isLoading: tasksLoading } = useTasks(
    selectedProjectId ? { project: selectedProjectId } : undefined
  );

  const projects = projectsData?.entities ?? [];
  const tasks = tasksData?.entities ?? [];

  // Auto-select first project if none selected
  const effectiveSelectedId = selectedProjectId ?? projects[0]?.id ?? null;
  const selectedProject = projects.find((p) => p.id === effectiveSelectedId);

  // Calculate task counts for each project
  const projectTaskCounts = useMemo(() => {
    const counts: Record<string, { total: number; done: number; doing: number }> = {};
    // For now, we only have counts for the selected project
    if (selectedProjectId && tasks.length > 0) {
      counts[selectedProjectId] = {
        total: tasks.length,
        done: tasks.filter((t) => t.metadata.status === 'done').length,
        doing: tasks.filter((t) => t.metadata.status === 'doing').length,
      };
    }
    return counts;
  }, [selectedProjectId, tasks]);

  const handleSelectProject = useCallback(
    (projectId: string) => {
      const params = new URLSearchParams(searchParams);
      params.set('id', projectId);
      router.push(`/projects?${params.toString()}`);
    },
    [router, searchParams]
  );

  if (projectsError) {
    return (
      <div className="space-y-6">
        <PageHeader
          title="Projects"
          description="Manage your development projects"
        />
        <ErrorState
          title="Failed to load projects"
          message={projectsError instanceof Error ? projectsError.message : 'Unknown error'}
        />
      </div>
    );
  }

  return (
    <div className="space-y-6 animate-fade-in">
      <PageHeader
        title="Projects"
        description="Manage your development projects"
        meta={`${projects.length} projects`}
      />

      <div className="flex gap-6">
        {/* Sidebar - Project List */}
        <div className="w-72 shrink-0">
          <div className="sticky top-4 space-y-2">
            <h2 className="text-sm font-semibold text-sc-fg-muted px-1 mb-3">
              All Projects
            </h2>

            {projectsLoading ? (
              <div className="space-y-2">
                <ProjectCardSkeleton />
                <ProjectCardSkeleton />
                <ProjectCardSkeleton />
              </div>
            ) : projects.length === 0 ? (
              <p className="text-sm text-sc-fg-subtle px-1">No projects yet</p>
            ) : (
              <div className="space-y-2">
                {projects.map((project) => (
                  <ProjectCard
                    key={project.id}
                    project={project}
                    isSelected={project.id === effectiveSelectedId}
                    onClick={() => handleSelectProject(project.id)}
                    taskCounts={projectTaskCounts[project.id]}
                  />
                ))}
              </div>
            )}
          </div>
        </div>

        {/* Main Content - Project Detail */}
        <div className="flex-1 min-w-0">
          {projectsLoading || tasksLoading ? (
            <ProjectDetailSkeleton />
          ) : !selectedProject ? (
            <EmptyState
              icon="◇"
              title="No project selected"
              description="Select a project from the sidebar to view details"
            />
          ) : (
            <ProjectDetail project={selectedProject} tasks={tasks} />
          )}
        </div>
      </div>
    </div>
  );
}
