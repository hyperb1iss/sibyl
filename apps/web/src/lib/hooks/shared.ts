'use client';

import type { useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';

import { queryKeys } from './query-keys';

/**
 * Invalidate queries based on entity type.
 * Avoids over-invalidation by only targeting relevant query keys.
 */
export function invalidateByEntityType(
  queryClient: ReturnType<typeof useQueryClient>,
  entityType: string | undefined,
  entityId?: string,
  options?: { includeStats?: boolean }
) {
  if (options?.includeStats) {
    queryClient.invalidateQueries({ queryKey: queryKeys.admin.stats });
  }

  switch (entityType) {
    case 'task':
      queryClient.invalidateQueries({ queryKey: queryKeys.tasks.all });
      queryClient.invalidateQueries({ queryKey: ['metrics'] });
      if (entityId) {
        queryClient.invalidateQueries({ queryKey: queryKeys.tasks.detail(entityId) });
      }
      break;

    case 'project':
      queryClient.invalidateQueries({ queryKey: queryKeys.projects.all });
      queryClient.invalidateQueries({ queryKey: ['metrics'] });
      if (entityId) {
        queryClient.invalidateQueries({ queryKey: queryKeys.projects.detail(entityId) });
      }
      break;

    case 'source':
      queryClient.invalidateQueries({ queryKey: queryKeys.sources.all });
      if (entityId) {
        queryClient.invalidateQueries({ queryKey: queryKeys.sources.detail(entityId) });
      }
      break;

    default:
      // For knowledge entities (pattern, episode, rule, etc.) - invalidate graph + entities
      queryClient.invalidateQueries({ queryKey: queryKeys.entities.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.graph.all });
      if (entityId) {
        queryClient.invalidateQueries({ queryKey: queryKeys.entities.detail(entityId) });
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
