'use client';

import type { QueryClient, QueryKey } from '@tanstack/react-query';
import { useEffect, useState } from 'react';

import { queryKeys } from './query-keys';

/** Marks the queries under a key stale and refetches the mounted ones. */
export type Invalidator = (queryKey: QueryKey) => void;

export function queryClientInvalidator(queryClient: QueryClient): Invalidator {
  return queryKey => {
    queryClient.invalidateQueries({ queryKey });
  };
}

/** How long a burst of events is allowed to settle before one refetch runs. */
export const INVALIDATION_DEBOUNCE_MS = 250;

/**
 * Coalesce invalidations per query key with a trailing debounce.
 *
 * A write burst (an agent closing ten tasks, a crawl creating entities)
 * reaches the browser as a burst of websocket events, and invalidating on
 * each one cancels the in-flight refetch and starts another, so k events
 * cost k server executions of every mounted query under the key. One
 * trailing refetch per key per burst answers the same question once.
 */
export function createInvalidationScheduler(
  queryClient: QueryClient,
  delayMs: number = INVALIDATION_DEBOUNCE_MS
) {
  const timers = new Map<string, ReturnType<typeof setTimeout>>();
  const invalidate = queryClientInvalidator(queryClient);

  const schedule: Invalidator = queryKey => {
    const id = JSON.stringify(queryKey);
    const pending = timers.get(id);
    if (pending !== undefined) clearTimeout(pending);
    timers.set(
      id,
      setTimeout(() => {
        timers.delete(id);
        invalidate(queryKey);
      }, delayMs)
    );
  };

  const cancel = () => {
    for (const pending of timers.values()) clearTimeout(pending);
    timers.clear();
  };

  return { schedule, cancel };
}

/**
 * Invalidate queries based on entity type.
 * Avoids over-invalidation by only targeting relevant query keys.
 */
export function invalidateByEntityType(
  invalidate: Invalidator,
  entityType: string | undefined,
  entityId?: string,
  options?: { includeStats?: boolean }
) {
  if (options?.includeStats) {
    invalidate(queryKeys.admin.stats);
  }

  switch (entityType) {
    case 'task':
      invalidate(queryKeys.tasks.all);
      invalidate(['metrics']);
      if (entityId) {
        invalidate(queryKeys.tasks.detail(entityId));
      }
      break;

    case 'project':
      invalidate(queryKeys.projects.all);
      invalidate(['metrics']);
      if (entityId) {
        invalidate(queryKeys.projects.detail(entityId));
      }
      break;

    case 'source':
      invalidate(queryKeys.sources.all);
      if (entityId) {
        invalidate(queryKeys.sources.detail(entityId));
      }
      break;

    default:
      // For knowledge entities (pattern, episode, rule, etc.) - invalidate graph + entities
      invalidate(queryKeys.entities.all);
      invalidate(queryKeys.graph.all);
      if (entityId) {
        invalidate(queryKeys.entities.detail(entityId));
      }
      break;
  }
}

/**
 * Subscribe to a CSS media query and return whether it matches.
 * SSR-safe: returns false until hydrated.
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(false);

  useEffect(() => {
    const mql = window.matchMedia(query);
    setMatches(mql.matches);

    const handler = (e: MediaQueryListEvent) => setMatches(e.matches);
    mql.addEventListener('change', handler);
    return () => mql.removeEventListener('change', handler);
  }, [query]);

  return matches;
}

/**
 * Trail a fast-changing value by `delayMs` so downstream queries fire once
 * typing settles instead of on every keystroke.
 */
export function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);

  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(id);
  }, [value, delayMs]);

  return debounced;
}
