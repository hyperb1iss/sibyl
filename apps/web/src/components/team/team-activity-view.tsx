'use client';

import { useMemo, useState } from 'react';
import { TeamFeedSkeleton, TeamPeopleSkeleton } from '@/components/suspense-boundary';
import { Button } from '@/components/ui/button';
import { ErrorState } from '@/components/ui/empty-state';
import { AlertTriangle, Users } from '@/components/ui/icons';
import { Spinner } from '@/components/ui/spinner';
import type {
  TeamActivityCounts,
  TeamActivityPerson,
  TeamActivityWindow,
} from '@/lib/api/activity';
import {
  isTeamActivityWindow,
  TEAM_ACTIVITY_COUNT_CONFIG,
  TEAM_ACTIVITY_COUNT_KEYS,
  TEAM_ACTIVITY_WINDOW_CONFIG,
  teamActivityTotal,
} from '@/lib/constants/activity';
import type { TeamActivityResult } from '@/lib/hooks/activity';
import { ActivityFeed } from './activity-feed';
import { PersonCard } from './person-card';
import { type ProjectOption, ProjectScopeSelect } from './project-scope-select';
import { WindowSwitcher } from './window-switcher';

export interface TeamActivityViewProps {
  activityWindow: TeamActivityWindow;
  onWindowChange: (window: TeamActivityWindow) => void;
  /** Projects offered by the scope picker. */
  projects: ProjectOption[];
  /** Names for project badges, archived projects included. */
  projectNames: Record<string, string>;
  /** The app-wide project selection; empty means every project. */
  selectedProjectIds: string[];
  onSelectAllProjects: () => void;
  onSelectProject: (projectId: string) => void;
  activity: Pick<
    TeamActivityResult,
    'data' | 'isLoading' | 'isFetching' | 'isPlaceholderData' | 'isError' | 'refetch'
  >;
  currentUserId?: string;
  /** Pins relative times, for stories and tests. */
  now?: number;
}

function sumCounts(people: TeamActivityPerson[]): TeamActivityCounts {
  const totals: TeamActivityCounts = {
    captures: 0,
    tasks_created: 0,
    tasks_completed: 0,
    decisions: 0,
    notes: 0,
    procedures: 0,
    other: 0,
  };
  for (const person of people) {
    for (const key of TEAM_ACTIVITY_COUNT_KEYS) totals[key] += person.counts[key] ?? 0;
  }
  return totals;
}

/**
 * The Team page body: who is active, what they did, newest first. Data comes
 * in through props so the page, stories, and tests share one renderer.
 */
export function TeamActivityView({
  activityWindow,
  onWindowChange,
  projects,
  projectNames,
  selectedProjectIds,
  onSelectAllProjects,
  onSelectProject,
  activity,
  currentUserId,
  now,
}: TeamActivityViewProps) {
  const { data, isLoading, isFetching, isPlaceholderData, isError, refetch } = activity;
  const [personId, setPersonId] = useState<string | null>(null);

  // Describe the window the data on screen covers, which trails the switcher
  // while a new window loads.
  const shownWindow =
    data && isTeamActivityWindow(data.window.label) ? data.window.label : activityWindow;
  const windowPhrase = TEAM_ACTIVITY_WINDOW_CONFIG[shownWindow].phrase;

  const people = data?.people ?? [];
  const peopleById = useMemo(
    () => new Map(people.map(person => [person.user_id, person])),
    [people]
  );
  const totals = useMemo(() => sumCounts(people), [people]);
  const totalUpdates = teamActivityTotal(totals);
  const activeCount = people.filter(person => teamActivityTotal(person.counts) > 0).length;
  const maxTotal = people.reduce(
    (max, person) => Math.max(max, teamActivityTotal(person.counts)),
    0
  );

  const person = personId ? peopleById.get(personId) : undefined;
  const recent = data?.recent ?? [];
  const feedItems = useMemo(
    () => (person ? recent.filter(item => item.actor_id === person.user_id) : recent),
    [person, recent]
  );

  const togglePerson = (userId: string) =>
    setPersonId(current => (current === userId ? null : userId));

  const summary = data
    ? `${activeCount} of ${people.length} ${people.length === 1 ? 'person' : 'people'} active in ${windowPhrase}`
    : isError
      ? 'Team activity is unavailable right now'
      : `Gathering ${windowPhrase}`;

  return (
    <div className="space-y-4 sm:space-y-6 animate-fade-in">
      <section className="rounded-xl border border-sc-fg-subtle/20 bg-gradient-to-br from-sc-bg-base via-sc-bg-elevated to-sc-purple/5 p-4 shadow-card sm:p-6">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
          <div className="flex min-w-0 items-center gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-sc-purple via-sc-magenta to-sc-coral shadow-glow-purple sm:h-12 sm:w-12">
              <Users width={22} height={22} className="text-sc-on-accent" aria-hidden="true" />
            </div>
            <div className="min-w-0">
              <h1 className="text-xl font-bold text-sc-fg-primary sm:text-2xl">Team activity</h1>
              <p className="flex items-center gap-2 text-sm text-sc-fg-muted">
                <span className="truncate">{summary}</span>
                {data && isFetching && (
                  <span className="inline-flex shrink-0 items-center gap-1.5 text-xs text-sc-cyan">
                    <Spinner size="xs" color="cyan" />
                    Updating
                  </span>
                )}
              </p>
            </div>
          </div>
          <div className="flex flex-col gap-2 xs:flex-row xs:flex-wrap xs:items-center">
            <WindowSwitcher value={activityWindow} onChange={onWindowChange} />
            <ProjectScopeSelect
              projects={projects}
              projectNames={projectNames}
              selectedIds={selectedProjectIds}
              onSelectAll={onSelectAllProjects}
              onSelectProject={onSelectProject}
            />
          </div>
        </div>

        {data && totalUpdates > 0 && (
          <ul
            className="mt-4 flex flex-wrap gap-2 border-t border-sc-fg-subtle/10 pt-4"
            aria-label={`Team totals for ${windowPhrase}`}
          >
            <li className="inline-flex items-center gap-1.5 rounded-full border border-sc-fg-subtle/20 bg-sc-bg-highlight px-2.5 py-1 text-xs">
              <span className="font-semibold tabular-nums text-sc-fg-primary">{totalUpdates}</span>
              <span className="text-sc-fg-muted">{totalUpdates === 1 ? 'update' : 'updates'}</span>
            </li>
            {TEAM_ACTIVITY_COUNT_KEYS.filter(key => totals[key] > 0).map(key => {
              const config = TEAM_ACTIVITY_COUNT_CONFIG[key];
              const Icon = config.icon;
              return (
                <li
                  key={key}
                  className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs ${config.chip}`}
                >
                  <Icon width={12} height={12} className={config.text} aria-hidden="true" />
                  <span className="font-semibold tabular-nums text-sc-fg-primary">
                    {totals[key]}
                  </span>
                  <span className="text-sc-fg-muted">
                    {totals[key] === 1 ? config.singular : config.label}
                  </span>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      {isError && data && (
        <div
          role="alert"
          className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-sc-yellow/40 bg-sc-yellow/5 px-4 py-2.5 text-sm text-sc-fg-secondary"
        >
          <span className="inline-flex items-center gap-2">
            <AlertTriangle width={16} height={16} className="text-sc-yellow" aria-hidden="true" />
            Couldn't refresh team activity. Showing the last answer.
          </span>
          <Button size="sm" variant="secondary" onClick={refetch}>
            Retry
          </Button>
        </div>
      )}

      {!data && isError ? (
        <div className="rounded-xl border border-sc-fg-subtle/20 bg-sc-bg-elevated shadow-card">
          <ErrorState
            title="Couldn't load team activity"
            message="The activity service did not answer. Check the API is reachable, then retry."
            action={<Button onClick={refetch}>Retry</Button>}
          />
        </div>
      ) : !data || isLoading ? (
        <output
          aria-busy="true"
          aria-label="Loading team activity"
          className="block space-y-4 sm:space-y-6"
        >
          <TeamPeopleSkeleton />
          <TeamFeedSkeleton />
        </output>
      ) : (
        <div
          aria-busy={isPlaceholderData || undefined}
          className={`space-y-4 transition-opacity duration-200 sm:space-y-6 ${
            isPlaceholderData ? 'opacity-60' : ''
          }`}
        >
          <section aria-labelledby="team-people-heading">
            <div className="mb-3 flex items-baseline justify-between gap-3">
              <h2
                id="team-people-heading"
                className="flex items-center gap-2 text-base font-semibold text-sc-fg-primary"
              >
                People
                <span className="rounded-full bg-sc-bg-highlight px-2 py-0.5 text-[11px] font-medium text-sc-fg-muted tabular-nums">
                  {people.length}
                </span>
              </h2>
              <p className="hidden text-xs text-sc-fg-muted sm:block">
                {person
                  ? 'Select them again to show everyone'
                  : 'Select a person to filter the feed'}
              </p>
            </div>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
              {people.map(member => (
                <PersonCard
                  key={member.user_id}
                  person={member}
                  maxTotal={maxTotal}
                  windowPhrase={windowPhrase}
                  selected={member.user_id === person?.user_id}
                  isYou={member.user_id === currentUserId}
                  onToggle={togglePerson}
                  now={now}
                />
              ))}
            </div>
          </section>

          <ActivityFeed
            items={feedItems}
            totalItems={recent.length}
            people={peopleById}
            projectNames={projectNames}
            scopedProjectId={selectedProjectIds.length === 1 ? selectedProjectIds[0] : undefined}
            truncated={data.truncated}
            windowPhrase={windowPhrase}
            person={person}
            onClearPerson={() => setPersonId(null)}
            now={now}
          />
        </div>
      )}
    </div>
  );
}
