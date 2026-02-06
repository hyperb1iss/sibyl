'use client';

import { Suspense } from 'react';
import { AgentCommandCenter } from '@/components/agents/command-center';
import { LoadingState } from '@/components/ui/spinner';
import { useAgents, useProjects } from '@/lib/hooks';
import { useProjectFilter } from '@/lib/project-context';

// =============================================================================
// Page Content
// =============================================================================

function AgentsPageContent() {
  const projectFilter = useProjectFilter();

  const { data: agentsData, isLoading, error } = useAgents({ project: projectFilter });

  const { data: projectsData } = useProjects();

  const agents = agentsData?.agents ?? [];
  const projects = (projectsData?.entities ?? []).map(p => ({ id: p.id, name: p.name }));

  return (
    <div className="h-[calc(100vh-8rem)]">
      <AgentCommandCenter
        agents={agents}
        projects={projects}
        projectFilter={projectFilter}
        isLoading={isLoading}
        error={error}
      />
    </div>
  );
}

// =============================================================================
// Page Export
// =============================================================================

export default function AgentsPage() {
  return (
    <Suspense fallback={<LoadingState />}>
      <AgentsPageContent />
    </Suspense>
  );
}
