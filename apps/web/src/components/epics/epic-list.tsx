'use client';

import { useRouter } from 'next/navigation';
import type { EpicSummary } from '@/lib/api';
import { EpicsEmptyState } from '@/components/ui/empty-state';
import { EpicCard, EpicCardSkeleton } from './epic-card';

interface EpicListProps {
  epics: EpicSummary[];
  projectNames?: Record<string, string>;
  showProject?: boolean;
  isLoading?: boolean;
  isFiltered?: boolean;
  onCreateEpic?: () => void;
}

export function EpicList({
  epics,
  projectNames = {},
  showProject = true,
  isLoading = false,
  isFiltered = false,
  onCreateEpic,
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
    return <EpicsEmptyState isFiltered={isFiltered} onCreateEpic={onCreateEpic} />;
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
