'use client';

import { use } from 'react';
import { AgentChatPanel } from '@/components/agents/agent-chat-panel';
import { EntityBreadcrumb } from '@/components/layout/breadcrumb';
import { LoadingState } from '@/components/ui/spinner';
import { ErrorState } from '@/components/ui/tooltip';
import { useAgent, useProjects } from '@/lib/hooks';

interface AgentDetailPageProps {
  params: Promise<{ id: string }>;
}

export default function AgentDetailPage({ params }: AgentDetailPageProps) {
  const { id } = use(params);
  const { data: agent, isLoading, error } = useAgent(id);
  const { data: projectsData } = useProjects();

  // Find parent project for breadcrumb
  const parentProject = agent?.project_id
    ? projectsData?.entities.find(p => p.id === agent.project_id)
    : undefined;

  if (error) {
    return (
      <div className="space-y-4 animate-fade-in">
        <EntityBreadcrumb entityType="task" entityName="Error" />
        <ErrorState
          title="Failed to load agent"
          message={error instanceof Error ? error.message : 'Unknown error'}
        />
      </div>
    );
  }

  if (isLoading || !agent) {
    return (
      <div className="space-y-4 animate-fade-in">
        <EntityBreadcrumb entityType="task" entityName="Loading..." />
        <LoadingState />
      </div>
    );
  }

  return (
    <div className="h-[calc(100vh-4rem)] flex flex-col animate-fade-in">
      {/* Breadcrumb */}
      <div className="shrink-0 pb-4">
        <EntityBreadcrumb
          entityType="task"
          entityName={agent.name}
          parentProject={
            parentProject ? { id: parentProject.id, name: parentProject.name } : undefined
          }
        />
      </div>

      {/* Split Panel - Chat + Workspace */}
      <AgentChatPanel agent={agent} />
    </div>
  );
}
