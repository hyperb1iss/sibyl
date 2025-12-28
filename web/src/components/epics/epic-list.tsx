'use client';

import { useRouter } from 'next/navigation';
import type { EpicSummary } from '@/lib/api';
import { EpicCard, EpicCardSkeleton } from './epic-card';

interface EpicListProps {
  epics: EpicSummary[];
  projectNames?: Record<string, string>;
  showProject?: boolean;
  isLoading?: boolean;
  emptyMessage?: string;
}

export function EpicList({
  epics,
  projectNames = {},
  showProject = true,
  isLoading = false,
  emptyMessage = 'No epics found',
}: EpicListProps) {
  const router = useRouter();

  if (isLoading) {
    return (
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {Array.from({ length: 6 }).map((_, i) => (
          <EpicCardSkeleton key={i} />
        ))}
      </div>
    );
  }

  if (epics.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-16 text-center">
        <span className="text-4xl text-[#ffb86c] mb-4">◈</span>
        <h3 className="text-lg font-medium text-sc-fg-primary mb-2">No epics yet</h3>
        <p className="text-sm text-sc-fg-muted">{emptyMessage}</p>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
      {epics.map(epic => {
        const projectId = epic.metadata?.project_id as string | undefined;
        return (
          <EpicCard
            key={epic.id}
            epic={epic}
            projectName={projectId ? projectNames[projectId] : undefined}
            showProject={showProject}
            onClick={epicId => router.push(`/epics/${epicId}`)}
            onProjectClick={pid => router.push(`/projects/${pid}`)}
          />
        );
      })}
    </div>
  );
}
