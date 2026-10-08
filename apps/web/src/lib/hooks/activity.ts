'use client';

import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import type { TeamActivityParams, TeamActivityResponse, TeamActivityWindow } from '../api/activity';
import { activityApi } from '../api/activity';
import { TIMING } from '../constants/app';
import { queryKeys } from './query-keys';

export interface TeamActivityScope {
  window: TeamActivityWindow;
  /** One project. Omit, with no `projectIds`, for every project. */
  projectId?: string;
  /** Several projects; activity in any of them counts. */
  projectIds?: string[];
  /** Narrow the feed to one member; the people list stays whole. */
  actorId?: string;
}

export interface TeamActivityResult {
  data: TeamActivityResponse | undefined;
  /** No answer to show yet. */
  isLoading: boolean;
  /** A request is in flight, including a background refresh. */
  isFetching: boolean;
  /** The data on screen belongs to the previous window, scope, or member. */
  isPlaceholderData: boolean;
  isError: boolean;
  error: Error | null;
  refetch: () => void;
}

/**
 * The request for a scope, normalized so the same scope always maps to the
 * same query key: project ids deduplicated and sorted, empty fields left out.
 */
export function teamActivityParams({
  window,
  projectId,
  projectIds,
  actorId,
}: TeamActivityScope): TeamActivityParams {
  const ids = projectIds?.length ? projectIds : projectId ? [projectId] : [];
  const unique = [...new Set(ids)].sort();
  return {
    window,
    ...(unique.length > 0 ? { project_ids: unique } : {}),
    ...(actorId ? { actor_id: actorId } : {}),
  };
}

/**
 * Who on the team did what inside a time window, for every project, one, or
 * several, optionally with the feed narrowed to one member. One request per
 * scope; while a new scope loads, the previous answer stays on screen flagged
 * `isPlaceholderData`.
 */
export function useTeamActivity(
  scope: TeamActivityScope,
  options?: { enabled?: boolean }
): TeamActivityResult {
  const params = teamActivityParams(scope);
  // keepPreviousData reaches back to the last scope that had data, even past
  // a failed one, so a retry or the next switch would flash an answer from
  // two scopes ago. After a failure the hook shows a plain load instead,
  // until a real answer lands.
  const [afterFailure, setAfterFailure] = useState(false);
  const query = useQuery({
    queryKey: queryKeys.activity.team(params),
    queryFn: ({ signal }) => activityApi.team(params, { signal }),
    staleTime: TIMING.STALE_TIME,
    // Explicit rather than inherited, so the hook behaves the same under any
    // QueryClient.
    placeholderData: afterFailure ? undefined : keepPreviousData,
    enabled: options?.enabled ?? true,
  });
  const failedWithoutData = query.isError && query.data === undefined;
  const answered = query.data !== undefined && !query.isPlaceholderData;
  useEffect(() => {
    if (failedWithoutData) setAfterFailure(true);
    else if (answered) setAfterFailure(false);
  }, [failedWithoutData, answered]);

  return {
    data: query.data,
    isLoading: query.isLoading,
    isFetching: query.isFetching,
    isPlaceholderData: query.isPlaceholderData,
    isError: query.isError,
    error: query.error,
    refetch: () => {
      void query.refetch();
    },
  };
}
