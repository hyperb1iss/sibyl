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

  // Don't attempt server-side token refresh. The backend rotates refresh tokens,
  // and new cookies can't be propagated back to the browser from server components.
  // This would invalidate the browser's refresh token, causing logout loops.
  // Let the client-side code handle 401s and token refresh.
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
 *
 * IMPORTANT: User-specific data MUST use 'no-store' to prevent cross-user
 * data leakage. Next.js data cache keys don't automatically include auth
 * context, so cached responses could be served to the wrong user.
 *
 * - 'force-cache': Use cached data if available (static data only)
 * - 'no-store': Always fetch fresh (user-specific or real-time data)
 */
const CACHE_CONFIG = {
  /** Static data that rarely changes and is not user-specific */
  static: { cache: 'force-cache' as const },

  /**
   * User-specific data - MUST NOT BE CACHED.
   * Even with cookies passed, Next.js cache keys don't include cookie values,
   * so cached data could leak between users/orgs.
   */
  userScoped: { cache: 'no-store' as const },

  /** Real-time data (no caching) */
  realtime: { cache: 'no-store' as const },
} as const;

// =============================================================================
// Server API Functions
// =============================================================================

/**
 * Fetch stats (entity counts).
 * User-scoped: stats are filtered by org, so no caching.
 */
export async function fetchStats(): Promise<StatsResponse> {
  return serverFetch<StatsResponse>('/admin/stats', CACHE_CONFIG.userScoped);
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
 * User-scoped: entities are filtered by org/project access.
 */
export async function fetchEntities(params?: {
  entity_type?: string;
  language?: string;
  category?: string;
  search?: string;
  project_ids?: string[];
  page?: number;
  page_size?: number;
  sort_by?: 'name' | 'created_at' | 'updated_at' | 'entity_type';
  sort_order?: 'asc' | 'desc';
}): Promise<EntityListResponse> {
  const searchParams = new URLSearchParams();
  if (params?.entity_type) searchParams.set('entity_type', params.entity_type);
  if (params?.language) searchParams.set('language', params.language);
  if (params?.category) searchParams.set('category', params.category);
  if (params?.search) searchParams.set('search', params.search);
  if (params?.project_ids?.length) {
    // FastAPI expects repeated query params for list
    for (const id of params.project_ids) {
      searchParams.append('project_ids', id);
    }
  }
  if (params?.page) searchParams.set('page', params.page.toString());
  if (params?.page_size) searchParams.set('page_size', params.page_size.toString());
  if (params?.sort_by) searchParams.set('sort_by', params.sort_by);
  if (params?.sort_order) searchParams.set('sort_order', params.sort_order);

  const query = searchParams.toString();
  return serverFetch<EntityListResponse>(
    `/entities${query ? `?${query}` : ''}`,
    CACHE_CONFIG.userScoped
  );
}

/**
 * Fetch single entity by ID.
 * User-scoped: entity access is filtered by org/project permissions.
 */
export async function fetchEntity(id: string): Promise<Entity> {
  return serverFetch<Entity>(`/entities/${id}`, CACHE_CONFIG.userScoped);
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
 * User-scoped: tasks are filtered by org/project access.
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
    ...CACHE_CONFIG.userScoped,
  });
}

/**
 * Fetch projects list.
 * User-scoped: projects are filtered by org membership.
 */
export async function fetchProjects(): Promise<TaskListResponse> {
  return serverFetch<TaskListResponse>('/search/explore', {
    method: 'POST',
    body: JSON.stringify({
      mode: 'list',
      types: ['project'],
      limit: 100,
    }),
    ...CACHE_CONFIG.userScoped,
  });
}

/**
 * Fetch full graph data.
 * User-scoped: graph data is filtered by org.
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
  return serverFetch<GraphData>(`/graph/full${query ? `?${query}` : ''}`, CACHE_CONFIG.userScoped);
}

/**
 * Fetch related entities for a given entity.
 * User-scoped: related entities filtered by org/project access.
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
    ...CACHE_CONFIG.userScoped,
  });
}

// =============================================================================
// Metrics
// =============================================================================

/**
 * Fetch org-level metrics.
 * User-scoped: metrics are filtered by org membership.
 */
export async function fetchOrgMetrics(): Promise<import('./api').OrgMetricsResponse> {
  return serverFetch<import('./api').OrgMetricsResponse>('/metrics', CACHE_CONFIG.userScoped);
}

/**
 * Fetch project-level metrics.
 * User-scoped: requires project access.
 */
export async function fetchProjectMetrics(
  projectId: string
): Promise<import('./api').ProjectMetricsResponse> {
  return serverFetch<import('./api').ProjectMetricsResponse>(
    `/metrics/projects/${projectId}`,
    CACHE_CONFIG.userScoped
  );
}

// =============================================================================
// Notes on Caching
// =============================================================================

/**
 * Server-side data fetching uses 'no-store' for all user-specific data
 * to prevent cross-user cache leakage. Next.js data cache keys don't
 * automatically include cookie/auth context, so time-based caching
 * (revalidate) could serve cached data from one user to another.
 *
 * Client-side caching (React Query) handles data freshness appropriately
 * since it's per-session and doesn't share data between users.
 *
 * If you need server-side caching for performance, you would need to:
 * 1. Include user/org ID in the cache key (custom cache)
 * 2. Use a user-scoped cache store (Redis with user key prefix)
 * 3. Implement cache-per-user at the CDN/edge level
 */
