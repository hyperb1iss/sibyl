'use client';

import { use } from 'react';
import Link from 'next/link';
import { PageHeader } from '@/components/layout/page-header';
import { TaskDetailPanel, TaskDetailSkeleton } from '@/components/tasks';
import { ErrorState } from '@/components/ui/tooltip';
import { useTask, useRelatedEntities } from '@/lib/hooks';

interface RelatedEntity {
  id: string;
  type: string;
  name: string;
  relationship?: string;
}

interface TaskDetailPageProps {
  params: Promise<{ id: string }>;
}

export default function TaskDetailPage({ params }: TaskDetailPageProps) {
  const { id } = use(params);
  const { data: task, isLoading, error } = useTask(id);
  const { data: relatedData } = useRelatedEntities(id);

  // Transform related entities for display
  const entities = (relatedData?.entities || []) as RelatedEntity[];
  const relatedKnowledge = entities
    .filter((e) => ['pattern', 'rule', 'template', 'topic'].includes(e.type))
    .map((e) => ({
      id: e.id,
      type: e.type,
      name: e.name,
      relationship: e.relationship || 'Related',
    }));

  if (error) {
    return (
      <div className="space-y-6">
        <PageHeader title="Task Details" />
        <ErrorState
          title="Failed to load task"
          message={error instanceof Error ? error.message : 'Unknown error'}
        />
      </div>
    );
  }

  return (
    <div className="space-y-6 animate-fade-in">
      <div className="flex items-center gap-3">
        <Link
          href="/tasks"
          className="text-sc-fg-subtle hover:text-sc-fg-primary transition-colors text-sm flex items-center gap-1"
        >
          <span>←</span> Back to Tasks
        </Link>
      </div>

      {isLoading || !task ? (
        <TaskDetailSkeleton />
      ) : (
        <TaskDetailPanel task={task} relatedKnowledge={relatedKnowledge} />
      )}
    </div>
  );
}
