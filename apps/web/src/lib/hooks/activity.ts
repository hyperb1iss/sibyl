'use client';

import { useQueries } from '@tanstack/react-query';
import { useCallback, useEffect, useState } from 'react';
import type {
  TeamActivityItem,
  TeamActivityParams,
  TeamActivityPerson,
  TeamActivityResponse,
  TeamActivityWindow,
} from '../api/activity';
import { activityApi } from '../api/activity';
import {
  TEAM_ACTIVITY_COUNT_KEYS,
  TEAM_ACTIVITY_RECENT_LIMIT,
  teamActivityTotal,
} from '../constants/activity';
import { TIMING } from '../constants/app';
import { queryKeys } from './query-keys';

export interface TeamActivityScope {
  window: TeamActivityWindow;
  /** One project. Omit, with no `projectIds`, for every project. */
  projectId?: string;
  /**
   * Several projects. The endpoint filters one project per request, so each
   * project is fetched on its own and the answers are merged.
   */
  projectIds?: string[];
}

export interface TeamActivityResult {
  data: TeamActivityResponse | undefined;
  /** No answer to show yet. */
  isLoading: boolean;
  /** A request is in flight, including a background refresh. */
  isFetching: boolean;
  /** The data on screen belongs to the previous window or scope. */
  isPlaceholderData: boolean;
  isError: boolean;
  error: Error | null;
  refetch: () => void;
}

function timeOf(iso: string | null): number {
  if (!iso) return 0;
  const time = Date.parse(iso);
  return Number.isNaN(time) ? 0 : time;
}

/**
 * Fold per-project answers into one. People are the same members in every
 * answer, so their counts add up and their latest activity wins; the feed is
 * re-sorted newest first and capped the way the server caps it.
 */
export function mergeTeamActivity(
  responses: TeamActivityResponse[]
): TeamActivityResponse | undefined {
  const [first] = responses;
  if (!first) return undefined;
  if (responses.length === 1) return first;

  const people = new Map<string, TeamActivityPerson>();
  for (const response of responses) {
    for (const person of response.people) {
      const seen = people.get(person.user_id);
      if (!seen) {
        people.set(person.user_id, { ...person, counts: { ...person.counts } });
        continue;
      }
      for (const key of TEAM_ACTIVITY_COUNT_KEYS) {
        seen.counts[key] += person.counts[key] ?? 0;
      }
      if (timeOf(person.last_active_at) > timeOf(seen.last_active_at)) {
        seen.last_active_at = person.last_active_at;
      }
    }
  }
  const ranked = [...people.values()].sort(
    (a, b) =>
      teamActivityTotal(b.counts) - teamActivityTotal(a.counts) ||
      timeOf(b.last_active_at) - timeOf(a.last_active_at)
  );

  const seenItems = new Set<string>();
  const recent: TeamActivityItem[] = [];
  for (const response of responses) {
    for (const item of response.recent) {
      const key = `${item.kind}:${item.id}`;
      if (seenItems.has(key)) continue;
      seenItems.add(key);
      recent.push(item);
    }
  }
  recent.sort((a, b) => timeOf(b.at) - timeOf(a.at));

  return {
    window: first.window,
    project_id: null,
    people: ranked,
    recent: recent.slice(0, TEAM_ACTIVITY_RECENT_LIMIT),
    truncated:
      responses.some(response => response.truncated) || recent.length > TEAM_ACTIVITY_RECENT_LIMIT,
  };
}

function scopeParams({ window, projectId, projectIds }: TeamActivityScope): TeamActivityParams[] {
  const ids = projectIds?.length ? [...new Set(projectIds)] : projectId ? [projectId] : [];
  if (ids.length === 0) return [{ window }];
  return ids.map(id => ({ window, project_id: id }));
}

/**
 * Who on the team did what inside a time window, for every project, one
 * project, or several. Mirrors a single query's state so callers do not care
 * how many requests the scope took.
 *
 * While a new window or scope loads, the previous answer stays on screen
 * flagged `isPlaceholderData`. The app's `keepPreviousData` default does not
 * reach `useQueries`: every new key gets a fresh observer with no previous
 * data, so the hook carries the last answer itself.
 */
export function useTeamActivity(
  scope: TeamActivityScope,
  options?: { enabled?: boolean }
): TeamActivityResult {
  const enabled = options?.enabled ?? true;
  const params = scopeParams(scope);

  const combine = useCallback(
    (
      results: Array<{
        data: TeamActivityResponse | undefined;
        isFetching: boolean;
        isPlaceholderData: boolean;
        isError: boolean;
        error: Error | null;
        refetch: () => Promise<unknown>;
      }>
    ): TeamActivityResult => {
      const answers = results.map(result => result.data);
      const complete = answers.every(answer => answer !== undefined);
      const failed = results.find(result => result.isError);
      return {
        data: complete ? mergeTeamActivity(answers as TeamActivityResponse[]) : undefined,
        isLoading: enabled && !complete && !failed,
        isFetching: results.some(result => result.isFetching),
        isPlaceholderData: results.some(result => result.isPlaceholderData),
        isError: failed !== undefined,
        error: failed?.error ?? null,
        refetch: () => {
          for (const result of results) {
            if (result.isError || !result.isFetching) void result.refetch();
          }
        },
      };
    },
    [enabled]
  );

  const current = useQueries({
    queries: params.map(query => ({
      queryKey: queryKeys.activity.team(query),
      queryFn: ({ signal }: { signal: AbortSignal }) => activityApi.team(query, { signal }),
      staleTime: TIMING.STALE_TIME,
      enabled,
    })),
    combine,
  });

  // The combined answer is structurally shared, so this only fires when the
  // data on screen actually changes.
  const [previous, setPrevious] = useState<TeamActivityResponse | undefined>(undefined);
  useEffect(() => {
    if (current.data) setPrevious(current.data);
  }, [current.data]);

  if (current.data || current.isError || !current.isLoading || !previous) return current;
  return { ...current, data: previous, isLoading: false, isPlaceholderData: true };
}
