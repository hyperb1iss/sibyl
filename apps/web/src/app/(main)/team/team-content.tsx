'use client';

import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { useCallback, useMemo, useState } from 'react';
import { TeamActivityView } from '@/components/team';
import type { TeamActivityWindow } from '@/lib/api/activity';
import { DEFAULT_TEAM_ACTIVITY_WINDOW, isTeamActivityWindow } from '@/lib/constants/activity';
import { useMe, useProjects, useTeamActivity } from '@/lib/hooks';
import { useProjectContext, useProjectFilters } from '@/lib/project-context';

/**
 * Wires the Team page to the URL and the app-wide project selection. The
 * window lives in `?window=`, the scope in the shared `?projects=` parameter
 * the header selector owns, so a copied link reopens the same view.
 */
export function TeamContent() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const windowParam = searchParams.get('window');
  const activityWindow = isTeamActivityWindow(windowParam)
    ? windowParam
    : DEFAULT_TEAM_ACTIVITY_WINDOW;

  const { selectProject, clearProjects, scopeReady } = useProjectContext();
  const projectFilters = useProjectFilters();
  // A selected person narrows the feed on the server, so it reaches past the
  // team-wide latest 100.
  const [personId, setPersonId] = useState<string | null>(null);
  // An unresolved scope would read every project; wait for the selection.
  const activity = useTeamActivity(
    { window: activityWindow, projectIds: projectFilters, actorId: personId ?? undefined },
    { enabled: scopeReady }
  );

  const { data: projectsData } = useProjects({ includeArchived: true });
  const { data: me } = useMe();

  const { projects, projectNames } = useMemo(() => {
    const all = projectsData?.entities ?? [];
    const names: Record<string, string> = {};
    for (const project of all) names[project.id] = project.name;
    const active = all
      .filter(project => project.metadata?.status !== 'archived')
      .map(project => ({ id: project.id, name: project.name }))
      .sort((a, b) => a.name.localeCompare(b.name));
    return { projects: active, projectNames: names };
  }, [projectsData]);

  const setWindow = useCallback(
    (next: TeamActivityWindow) => {
      const params = new URLSearchParams(searchParams);
      params.set('window', next);
      router.replace(`${pathname}?${params.toString()}`, { scroll: false });
    },
    [pathname, router, searchParams]
  );

  return (
    <TeamActivityView
      activityWindow={activityWindow}
      onWindowChange={setWindow}
      projects={projects}
      projectNames={projectNames}
      selectedProjectIds={projectFilters ?? []}
      onSelectAllProjects={clearProjects}
      onSelectProject={selectProject}
      activity={activity}
      personId={personId}
      onPersonChange={setPersonId}
      currentUserId={me?.user.id}
    />
  );
}
