'use client';

import { use } from 'react';
import { EntityBreadcrumb } from '@/components/layout/breadcrumb';
import { PlanningSessionPanel } from '@/components/planning/planning-session-panel';
import { LoadingState } from '@/components/ui/spinner';
import { ErrorState } from '@/components/ui/tooltip';
import { usePlanningSession, useProjects } from '@/lib/hooks';

interface PlanningSessionDetailPageProps {
  params: Promise<{ id: string }>;
}

export default function PlanningSessionDetailPage({ params }: PlanningSessionDetailPageProps) {
  const { id } = use(params);

  // Fetch session - auto-polls during active phases
  const { data: session, isLoading, error } = usePlanningSession(id);
  const { data: projectsData } = useProjects();

  // Find parent project for breadcrumb
  const parentProject = session?.project_id
    ? projectsData?.entities.find(p => p.id === session.project_id)
    : undefined;

  if (error) {
    return (
      <div className="space-y-4 animate-fade-in">
        <EntityBreadcrumb entityType="planning" entityName="Error" />
        <ErrorState
          title="Failed to load planning session"
          message={error instanceof Error ? error.message : 'Unknown error'}
        />
      </div>
    );
  }

  if (isLoading || !session) {
    return (
      <div className="space-y-4 animate-fade-in">
        <EntityBreadcrumb entityType="planning" entityName="Loading..." />
        <LoadingState />
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col animate-fade-in overflow-hidden">
      {/* Breadcrumb - fixed at top */}
      <div className="shrink-0 pb-3">
        <EntityBreadcrumb
          entityType="planning"
          entityName={session.title || 'Planning Session'}
          parentProject={
            parentProject ? { id: parentProject.id, name: parentProject.name } : undefined
          }
        />
      </div>

      {/* Session Panel - fills remaining space */}
      <div className="flex-1 min-h-0 overflow-hidden">
        <PlanningSessionPanel session={session} />
      </div>
    </div>
  );
}
