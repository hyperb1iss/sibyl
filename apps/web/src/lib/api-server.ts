import 'server-only';

import { cookies } from 'next/headers';
import { serverOnly } from 'next-dynenv';
import type {
  Entity,
  EntityListResponse,
  GraphData,
  HealthResponse,
  SearchResponse,
  StatsResponse,
  TaskListResponse,
} from './api';

// =============================================================================
// Server-Side API Configuration
// =============================================================================

/**
 * Base URL for API calls from the server.
 * In development, we need the full URL since rewrites don't apply server-side.
 * In production, this should be the internal service URL.
 */
const API_BASE = serverOnly('SIBYL_API_URL', 'http://localhost:3334/api');

/**
 * Default fetch options for server-side requests.
 */
const DEFAULT_OPTIONS: RequestInit = {
  headers: {
    'Content-Type': 'application/json',
  },
};

// =============================================================================
// Core Fetch Utility
// =============================================================================

async function serverFetch<T>(
  endpoint: string,
  options?: RequestInit & { cache?: RequestCache; next?: NextFetchRequestConfig }
): Promise<T> {
  const url = `${API_BASE}${endpoint}`;
  const cookieStore = await cookies();
  const cookieHeader = cookieStore.toString();

  const response = await fetch(url, {
    ...DEFAULT_OPTIONS,
    ...options,
    headers: {
      ...DEFAULT_OPTIONS.headers,
      ...options?.headers,
      ...(cookieHeader ? { cookie: cookieHeader } : {}),
    },
  });

  // Handle 401 - try to refresh token and retry once
  if (response.status === 401 && endpoint !== '/auth/refresh') {
    const refreshResponse = await fetch(`${API_BASE}/auth/refresh`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(cookieHeader ? { cookie: cookieHeader } : {}),
      },
    });

    if (refreshResponse.ok) {
      // Refresh succeeded - extract new tokens from set-cookie headers
      // and use them for the retry request
      const newCookies: string[] = [];
      const setCookieHeaders = refreshResponse.headers.getSetCookie?.() || [];

      for (const header of setCookieHeaders) {
        // Extract cookie name=value from set-cookie header (before the first ';')
        const cookiePart = header.split(';')[0];
        if (cookiePart) {
          newCookies.push(cookiePart);
        }
      }

      const retryCookieHeader = newCookies.length > 0 ? newCookies.join('; ') : cookieHeader;

      const retryResponse = await fetch(url, {
        ...DEFAULT_OPTIONS,
        ...options,
        headers: {
          ...DEFAULT_OPTIONS.headers,
          ...options?.headers,
          cookie: retryCookieHeader,
        },
      });

      if (retryResponse.ok) {
        return retryResponse.json();
      }

      // Retry also failed - throw the error
      const error = await retryResponse.text();
      throw new Error(error || `API error: ${retryResponse.status}`);
    }

    // Refresh failed - throw the original 401 error
    const error = await response.text();
    throw new Error(error || `API error: ${response.status}`);
  }

  if (!response.ok) {
    const error = await response.text();
    throw new Error(error || `API error: ${response.status}`);
  }

  return response.json();
}

// =============================================================================
// Cache Configuration Types
// =============================================================================

interface NextFetchRequestConfig {
  revalidate?: number | false;
  tags?: string[];
}

/**
 * Cache strategies for different data types.
 * - 'force-cache': Use cached data if available (static data)
 * - 'no-store': Always fetch fresh (real-time data)
 * - revalidate: Time-based revalidation in seconds
 */
const CACHE_CONFIG = {
  /** Static data that rarely changes */
  static: { cache: 'force-cache' as const },

  /** Data that changes occasionally (revalidate every 60s) */
  dynamic: { next: { revalidate: 60 } },

  /** Real-time data (no caching) */
  realtime: { cache: 'no-store' as const },
} as const;

// =============================================================================
// Server API Functions
// =============================================================================

/**
 * Fetch stats (entity counts).
 * Cached for 60 seconds since it doesn't change frequently.
 */
export async function fetchStats(): Promise<StatsResponse> {
  return serverFetch<StatsResponse>('/admin/stats', {
    ...CACHE_CONFIG.dynamic,
    next: { ...CACHE_CONFIG.dynamic.next, tags: ['stats'] },
  });
}

/**
 * Simple health check - no auth required.
 * Used for connectivity checks that shouldn't trigger logout.
 */
export async function checkServerHealth(): Promise<{ status: string }> {
  return serverFetch<{ status: string }>('/health', CACHE_CONFIG.realtime);
}

/**
 * Fetch detailed server health (requires auth).
 * Used for dashboard display.
 */
export async function fetchHealth(): Promise<HealthResponse> {
  return serverFetch<HealthResponse>('/admin/health', CACHE_CONFIG.realtime);
}

/**
 * Fetch paginated entity list.
 * Short cache for initial load, client will take over for interactions.
 */
export async function fetchEntities(params?: {
  entity_type?: string;
  language?: string;
  category?: string;
  page?: number;
  page_size?: number;
}): Promise<EntityListResponse> {
  const searchParams = new URLSearchParams();
  if (params?.entity_type) searchParams.set('entity_type', params.entity_type);
  if (params?.language) searchParams.set('language', params.language);
  if (params?.category) searchParams.set('category', params.category);
  if (params?.page) searchParams.set('page', params.page.toString());
  if (params?.page_size) searchParams.set('page_size', params.page_size.toString());

  const query = searchParams.toString();
  return serverFetch<EntityListResponse>(`/entities${query ? `?${query}` : ''}`, {
    ...CACHE_CONFIG.dynamic,
    next: { ...CACHE_CONFIG.dynamic.next, tags: ['entities'] },
  });
}

/**
 * Fetch single entity by ID.
 * Cached with entity-specific tag for targeted invalidation.
 */
export async function fetchEntity(id: string): Promise<Entity> {
  return serverFetch<Entity>(`/entities/${id}`, {
    ...CACHE_CONFIG.dynamic,
    next: { ...CACHE_CONFIG.dynamic.next, tags: ['entities', `entity-${id}`] },
  });
}

/**
 * Fetch search results.
 * No caching - search is user-initiated and should be fresh.
 */
export async function fetchSearchResults(params: {
  query: string;
  types?: string[];
  language?: string;
  category?: string;
  limit?: number;
  include_content?: boolean;
}): Promise<SearchResponse> {
  return serverFetch<SearchResponse>('/search', {
    method: 'POST',
    body: JSON.stringify(params),
    ...CACHE_CONFIG.realtime,
  });
}

/**
 * Fetch tasks list.
 * Short cache for initial load.
 */
export async function fetchTasks(params?: {
  project?: string;
  status?: string;
}): Promise<TaskListResponse> {
  return serverFetch<TaskListResponse>('/search/explore', {
    method: 'POST',
    body: JSON.stringify({
      mode: 'list',
      types: ['task'],
      project: params?.project,
      status: params?.status,
      limit: 200,
    }),
    ...CACHE_CONFIG.dynamic,
    next: { ...CACHE_CONFIG.dynamic.next, tags: ['tasks'] },
  });
}

/**
 * Fetch projects list.
 * Cached since projects don't change frequently.
 */
export async function fetchProjects(): Promise<TaskListResponse> {
  return serverFetch<TaskListResponse>('/search/explore', {
    method: 'POST',
    body: JSON.stringify({
      mode: 'list',
      types: ['project'],
      limit: 100,
    }),
    ...CACHE_CONFIG.dynamic,
    next: { ...CACHE_CONFIG.dynamic.next, tags: ['projects'] },
  });
}

/**
 * Fetch full graph data.
 * Cached with graph tag.
 */
export async function fetchGraphData(params?: {
  types?: string[];
  max_nodes?: number;
  max_edges?: number;
}): Promise<GraphData> {
  const searchParams = new URLSearchParams();
  if (params?.types) {
    for (const t of params.types) searchParams.append('types', t);
  }
  if (params?.max_nodes) searchParams.set('max_nodes', params.max_nodes.toString());
  if (params?.max_edges) searchParams.set('max_edges', params.max_edges.toString());

  const query = searchParams.toString();
  return serverFetch<GraphData>(`/graph/full${query ? `?${query}` : ''}`, {
    ...CACHE_CONFIG.dynamic,
    next: { ...CACHE_CONFIG.dynamic.next, tags: ['graph'] },
  });
}

/**
 * Fetch related entities for a given entity.
 */
export async function fetchRelatedEntities(
  entityId: string,
  depth = 1
): Promise<{ mode: string; entities: unknown[]; total: number }> {
  return serverFetch('/search/explore', {
    method: 'POST',
    body: JSON.stringify({
      mode: 'related',
      entity_id: entityId,
      depth,
      limit: 20,
    }),
    ...CACHE_CONFIG.dynamic,
    next: { ...CACHE_CONFIG.dynamic.next, tags: ['entities', `related-${entityId}`] },
  });
}

// =============================================================================
// Metrics
// =============================================================================

export async function fetchOrgMetrics(): Promise<import('./api').OrgMetricsResponse> {
  return serverFetch<import('./api').OrgMetricsResponse>('/metrics', {
    ...CACHE_CONFIG.dynamic,
    next: { ...CACHE_CONFIG.dynamic.next, revalidate: 60, tags: ['metrics'] },
  });
}

export async function fetchProjectMetrics(
  projectId: string
): Promise<import('./api').ProjectMetricsResponse> {
  return serverFetch<import('./api').ProjectMetricsResponse>(`/metrics/projects/${projectId}`, {
    ...CACHE_CONFIG.dynamic,
    next: {
      ...CACHE_CONFIG.dynamic.next,
      revalidate: 60,
      tags: ['metrics', `project-${projectId}`],
    },
  });
}

// =============================================================================
// Cache Revalidation Helpers
// =============================================================================

/**
 * Revalidate cache tags from Server Actions.
 * Import and call these from your Server Actions after mutations.
 *
 * @example
 * // In a Server Action
 * 'use server'
 * import { revalidateTag } from 'next/cache';
 * import { CACHE_TAGS } from '@/lib/api-server';
 *
 * async function createEntity(data) {
 *   await api.create(data);
 *   revalidateTag(CACHE_TAGS.entities);
 *   revalidateTag(CACHE_TAGS.stats);
 * }
 */
export const CACHE_TAGS = {
  entities: 'entities',
  stats: 'stats',
  tasks: 'tasks',
  projects: 'projects',
  graph: 'graph',
} as const;
