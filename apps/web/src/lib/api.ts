/**
 * API client for Sibyl backend.
 *
 * Uses fetch with React Query for data fetching and WebSocket for realtime updates.
 */

const API_BASE = '/api';

// =============================================================================
// Types (generated from OpenAPI will replace these)
// =============================================================================

export interface Entity {
  id: string;
  entity_type: string;
  name: string;
  description: string;
  content: string;
  category: string | null;
  languages: string[];
  tags: string[];
  metadata: Record<string, unknown>;
  source_file: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface EntityCreate {
  name: string;
  description?: string;
  content?: string;
  entity_type?: string;
  category?: string;
  languages?: string[];
  tags?: string[];
  metadata?: Record<string, unknown>;
}

export interface EntityUpdate {
  name?: string;
  description?: string;
  content?: string;
  category?: string;
  languages?: string[];
  tags?: string[];
  metadata?: Record<string, unknown>;
}

export interface EntityListResponse {
  entities: Entity[];
  total: number;
  page: number;
  page_size: number;
  has_more: boolean;
}

export interface SearchResult {
  id: string;
  type: string;
  name: string;
  content: string;
  score: number;
  source: string | null;
  metadata: Record<string, unknown>;
}

export interface SearchResponse {
  results: SearchResult[];
  total: number;
  query: string;
  filters: Record<string, unknown>;
}

export interface GraphNode {
  id: string;
  type: string;
  label: string;
  color: string;
  size: number;
  x?: number;
  y?: number;
  metadata: Record<string, unknown>;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  type: string;
  label: string;
  weight: number;
}

export interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
  node_count: number;
  edge_count: number;
}

// Cluster types for bubble visualization
export interface Cluster {
  id: string;
  count: number;
  dominant_type: string;
  type_distribution: Record<string, number>;
  level: number;
}

export interface ClustersResponse {
  clusters: Cluster[];
  total_nodes: number;
  total_clusters: number;
}

export interface ClusterDetailResponse {
  cluster_id: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
  node_count: number;
  edge_count: number;
}

export interface GraphStatsResponse {
  total_nodes: number;
  total_edges: number;
  by_type: Record<string, number>;
}

// Hierarchical graph with cluster assignments for rich visualization
export interface HierarchicalNode {
  id: string;
  name: string;
  type: string;
  label: string;
  color: string;
  summary: string;
  cluster_id: string;
}

export interface HierarchicalEdge {
  source: string;
  target: string;
  type: string;
}

export interface HierarchicalCluster {
  id: string;
  member_count: number;
  level: number;
  type_distribution: Record<string, number>;
  dominant_type: string;
}

export interface ClusterEdge {
  source: string;
  target: string;
  weight: number;
}

export interface HierarchicalGraphResponse {
  nodes: HierarchicalNode[];
  edges: HierarchicalEdge[];
  clusters: HierarchicalCluster[];
  cluster_edges: ClusterEdge[];
  total_nodes: number;
  total_edges: number;
  displayed_nodes?: number;
  displayed_edges?: number;
}

export interface HealthResponse {
  status: 'healthy' | 'unhealthy' | 'unknown';
  server_name: string;
  uptime_seconds: number;
  graph_connected: boolean;
  entity_counts: Record<string, number>;
  errors: string[];
}

export interface StatsResponse {
  entity_counts: Record<string, number>;
  total_entities: number;
}

// =============================================================================
// Metrics Types
// =============================================================================

export interface TaskStatusDistribution {
  backlog: number;
  todo: number;
  doing: number;
  blocked: number;
  review: number;
  done: number;
}

export interface TaskPriorityDistribution {
  critical: number;
  high: number;
  medium: number;
  low: number;
  someday: number;
}

export interface AssigneeStats {
  name: string;
  total: number;
  completed: number;
  in_progress: number;
}

export interface TimeSeriesPoint {
  date: string;
  value: number;
}

export interface ProjectMetrics {
  project_id: string;
  project_name: string;
  total_tasks: number;
  status_distribution: TaskStatusDistribution;
  priority_distribution: TaskPriorityDistribution;
  completion_rate: number;
  assignees: AssigneeStats[];
  tasks_created_last_7d: number;
  tasks_completed_last_7d: number;
  velocity_trend: TimeSeriesPoint[];
}

export interface ProjectMetricsResponse {
  metrics: ProjectMetrics;
}

export interface ProjectSummary {
  id: string;
  name: string;
  total: number;
  completed: number;
  completion_rate: number;
}

export interface OrgMetricsResponse {
  total_projects: number;
  total_tasks: number;
  status_distribution: TaskStatusDistribution;
  priority_distribution: TaskPriorityDistribution;
  completion_rate: number;
  top_assignees: AssigneeStats[];
  tasks_created_last_7d: number;
  tasks_completed_last_7d: number;
  velocity_trend: TimeSeriesPoint[];
  projects_summary: ProjectSummary[];
}

// =============================================================================
// Auth + Orgs
// =============================================================================

export interface AuthMeResponse {
  user: {
    id: string;
    github_id: number | null;
    email: string | null;
    name: string;
    avatar_url: string | null;
  };
  organization: { id: string; slug: string; name: string } | null;
  org_role: string | null;
}

export interface OrgSummary {
  id: string;
  slug: string;
  name: string;
  is_personal: boolean;
  role: string | null;
}

export interface OrgListResponse {
  orgs: OrgSummary[];
}

export interface OrgSwitchResponse {
  organization: { id: string; slug: string; name: string };
  access_token: string;
}

export interface OrgCreateRequest {
  name: string;
  slug?: string;
}

export interface OrgUpdateRequest {
  name?: string;
  slug?: string;
}

export interface OrgCreateResponse {
  organization: { id: string; slug: string; name: string };
  access_token: string;
}

export interface OrgGetResponse {
  organization: { id: string; slug: string; name: string };
  role: string;
}

export interface OrgMember {
  user: {
    id: string;
    github_id: number | null;
    email: string | null;
    name: string | null;
    avatar_url: string | null;
  };
  role: string;
  created_at: string;
}

export interface OrgMembersResponse {
  members: OrgMember[];
}

// =============================================================================
// Security Types (Sessions, API Keys, OAuth)
// =============================================================================

export interface Session {
  id: string;
  user_agent: string | null;
  ip_address: string | null;
  created_at: string;
  last_used_at: string | null;
  is_current: boolean;
}

export interface SessionsResponse {
  sessions: Session[];
}

export interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  scopes: string[];
  last_used_at: string | null;
  expires_at: string | null;
  created_at: string;
}

export interface ApiKeysResponse {
  api_keys: ApiKey[];
}

export interface ApiKeyCreateRequest {
  name: string;
  scopes?: string[];
  expires_in_days?: number;
}

export interface ApiKeyCreateResponse {
  api_key: ApiKey;
  key: string; // Full key, only shown once
}

export interface OAuthConnection {
  id: string;
  provider: string;
  provider_user_id: string;
  email: string | null;
  name: string | null;
  avatar_url: string | null;
  created_at: string;
}

export interface OAuthConnectionsResponse {
  connections: OAuthConnection[];
}

export interface PasswordChangeRequest {
  current_password: string;
  new_password: string;
}

// User Preferences (flexible dict stored on user)
export interface UserPreferences {
  theme?: 'light' | 'dark' | 'system';
  locale?: string;
  timezone?: string;
  graphShowLabels?: boolean;
  graphDefaultZoom?: number;
  dashboardDefaultView?: 'grid' | 'list';
  notifyOnTaskAssigned?: boolean;
  notifyOnMention?: boolean;
  is_onboarded?: boolean; // Has user completed onboarding wizard
  [key: string]: unknown; // Allow additional preferences
}

export interface PreferencesResponse {
  preferences: UserPreferences;
}

// =============================================================================
// Task Types
// =============================================================================

export type TaskStatus = 'backlog' | 'todo' | 'doing' | 'blocked' | 'review' | 'done' | 'archived';
export type TaskPriority = 'critical' | 'high' | 'medium' | 'low' | 'someday';

export interface Task {
  id: string;
  title: string;
  description: string;
  status: TaskStatus;
  priority: TaskPriority;
  task_order: number;
  project_id: string | null;
  feature: string | null;
  assignees: string[];
  due_date: string | null;
  technologies: string[];
  domain: string | null;
  branch_name: string | null;
  pr_url: string | null;
  created_at: string | null;
  updated_at: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface TaskListResponse {
  mode: string;
  entities: TaskSummary[];
  total: number;
  filters: Record<string, unknown>;
}

export interface TaskSummary {
  id: string;
  type: string;
  name: string;
  description: string;
  metadata: {
    status?: TaskStatus;
    priority?: TaskPriority;
    project_id?: string;
    assignees?: string[];
    [key: string]: unknown;
  };
}

export interface Project {
  id: string;
  title: string;
  description: string;
  status: 'planning' | 'active' | 'on_hold' | 'completed' | 'archived';
  repository_url: string | null;
  features: string[];
  tech_stack: string[];
  total_tasks: number;
  completed_tasks: number;
  in_progress_tasks: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface TaskActionResponse {
  success: boolean;
  action: string;
  task_id: string;
  message: string;
  data: Record<string, unknown>;
}

export interface EpicActionResponse {
  success: boolean;
  action: string;
  epic_id: string;
  message: string;
  data: Record<string, unknown>;
}

// =============================================================================
// Epic Types
// =============================================================================

export type EpicStatus = 'planning' | 'in_progress' | 'blocked' | 'completed' | 'archived';

export interface Epic {
  id: string;
  title: string;
  description: string;
  project_id: string;
  status: EpicStatus;
  priority: TaskPriority;
  assignees: string[];
  tags: string[];
  start_date: string | null;
  target_date: string | null;
  completed_date: string | null;
  total_tasks: number;
  completed_tasks: number;
  learnings: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface EpicListResponse {
  mode: string;
  entities: EpicSummary[];
  total: number;
  filters: Record<string, unknown>;
}

export interface EpicSummary {
  id: string;
  type: string;
  name: string;
  description: string;
  metadata: {
    status?: EpicStatus;
    priority?: TaskPriority;
    project_id?: string;
    assignees?: string[];
    total_tasks?: number;
    completed_tasks?: number;
    [key: string]: unknown;
  };
}

export interface EpicProgress {
  total_tasks: number;
  completed_tasks: number;
  doing_tasks: number;
  blocked_tasks: number;
  review_tasks: number;
  completion_pct: number;
}

// =============================================================================
// Source Types (Documentation Crawling)
// =============================================================================

export type SourceType = 'website' | 'github' | 'local' | 'api_docs';
export type CrawlStatus = 'pending' | 'in_progress' | 'completed' | 'failed' | 'partial';

export interface LocalSourceData {
  path: string;
  name: string;
  description: string;
  tags: string[];
}

export interface Source {
  id: string;
  name: string;
  description: string;
  url: string;
  source_type: SourceType;
  crawl_depth: number;
  crawl_patterns: string[];
  exclude_patterns: string[];
  crawl_status: CrawlStatus;
  last_crawled: string | null;
  document_count: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface SourceSummary {
  id: string;
  type: string;
  name: string;
  description: string;
  created_at?: string;
  updated_at?: string;
  metadata: {
    url?: string;
    source_type?: SourceType;
    crawl_status?: CrawlStatus;
    document_count?: number;
    total_tokens?: number;
    total_entities?: number;
    last_crawled?: string;
    crawl_error?: string;
    crawl_depth?: number;
    crawl_patterns?: string[];
    exclude_patterns?: string[];
    tags?: string[];
  };
}

export interface SourceListResponse {
  mode: string;
  entities: SourceSummary[];
  total: number;
  filters: Record<string, unknown>;
}

// Crawler API types (from /crawler endpoints)
export interface CrawlSource {
  id: string;
  name: string;
  url: string;
  source_type: SourceType;
  description: string | null;
  crawl_depth: number;
  crawl_status: CrawlStatus;
  document_count: number;
  chunk_count: number;
  last_crawled_at: string | null;
  last_error: string | null;
  created_at: string;
  include_patterns: string[];
  exclude_patterns: string[];
}

// =============================================================================
// RAG Search Types (Documentation Search)
// =============================================================================

export interface RAGSearchParams {
  query: string;
  source_id?: string;
  source_name?: string;
  match_count?: number;
  similarity_threshold?: number;
  return_mode?: 'chunks' | 'pages';
  include_context?: boolean;
}

export interface RAGChunkResult {
  chunk_id: string;
  document_id: string;
  source_id: string;
  source_name: string;
  url: string;
  title: string;
  content: string;
  context: string | null;
  similarity: number;
  chunk_type: 'text' | 'code' | 'heading' | 'list' | 'table';
  chunk_index: number;
  heading_path: string[];
  language: string | null;
}

export interface RAGPageResult {
  document_id: string;
  source_id: string;
  source_name: string;
  url: string;
  title: string;
  content: string;
  word_count: number;
  has_code: boolean;
  headings: string[];
  code_languages: string[];
  best_chunk_similarity: number;
}

export interface RAGSearchResponse {
  results: (RAGChunkResult | RAGPageResult)[];
  total: number;
  query: string;
  source_filter: string | null;
  return_mode: 'chunks' | 'pages';
}

export interface CodeExampleParams {
  query: string;
  language?: string;
  source_id?: string;
  match_count?: number;
}

export interface CodeExampleResult {
  chunk_id: string;
  document_id: string;
  source_name: string;
  url: string;
  title: string;
  code: string;
  context: string | null;
  language: string | null;
  similarity: number;
  heading_path: string[];
}

export interface CodeExampleResponse {
  examples: CodeExampleResult[];
  total: number;
  query: string;
  language_filter: string | null;
}

export interface FullPageResponse {
  document_id: string;
  source_id: string;
  source_name: string;
  url: string;
  title: string;
  content: string;
  raw_content: string | null;
  word_count: number;
  token_count: number;
  has_code: boolean;
  headings: string[];
  code_languages: string[];
  links: string[];
  crawled_at: string;
}

export interface DocumentUpdateRequest {
  title?: string;
  content?: string;
}

export interface DocumentRelatedEntity {
  id: string;
  name: string;
  entity_type: string;
  description: string;
  chunk_count: number;
}

export interface DocumentRelatedEntitiesResponse {
  document_id: string;
  entities: DocumentRelatedEntity[];
  total: number;
}

// Backup/Restore Types
export interface BackupData {
  version: string;
  created_at: string;
  organization_id: string;
  entity_count: number;
  relationship_count: number;
  entities: Record<string, unknown>[];
  relationships: Record<string, unknown>[];
}

export interface BackupResponse {
  success: boolean;
  entity_count: number;
  relationship_count: number;
  message: string;
  duration_seconds: number;
  backup_data: BackupData | null;
}

export interface RestoreResponse {
  success: boolean;
  entities_restored: number;
  relationships_restored: number;
  entities_skipped: number;
  relationships_skipped: number;
  errors: string[];
  duration_seconds: number;
}

// =============================================================================
// API Functions
// =============================================================================

// Track if we're currently refreshing to prevent multiple refresh attempts
let isRefreshing = false;
let refreshPromise: Promise<boolean> | null = null;

/**
 * Try to refresh the access token using the refresh token cookie.
 * Returns true if refresh succeeded, false if it failed.
 */
async function tryRefreshToken(): Promise<boolean> {
  // If already refreshing, wait for that to complete
  if (isRefreshing && refreshPromise) {
    return refreshPromise;
  }

  isRefreshing = true;
  refreshPromise = (async () => {
    try {
      const response = await fetch(`${API_BASE}/auth/refresh`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
      });
      return response.ok;
    } catch {
      return false;
    } finally {
      isRefreshing = false;
      refreshPromise = null;
    }
  })();

  return refreshPromise;
}

/**
 * Redirect to login page with return URL.
 */
function redirectToLogin(): never {
  const currentPath = window.location.pathname + window.location.search;
  window.location.href = `/login?next=${encodeURIComponent(currentPath)}`;
  // Return a promise that never resolves to prevent further execution
  return new Promise(() => {
    // Intentionally empty - blocks until page redirects
  }) as never;
}

async function fetchApi<T>(endpoint: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${endpoint}`, {
    ...options,
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...options?.headers,
    },
  });

  if (!response.ok) {
    // Handle 401 - try to refresh token before redirecting to login
    if (response.status === 401 && typeof window !== 'undefined') {
      // Don't try to refresh if we're on login page or if this IS the refresh endpoint
      if (window.location.pathname !== '/login' && endpoint !== '/auth/refresh') {
        // Try to refresh the token
        const refreshed = await tryRefreshToken();

        if (refreshed) {
          // Retry the original request with new token
          const retryResponse = await fetch(`${API_BASE}${endpoint}`, {
            ...options,
            credentials: 'include',
            headers: {
              'Content-Type': 'application/json',
              ...options?.headers,
            },
          });

          if (retryResponse.ok) {
            if (retryResponse.status === 204) {
              return undefined as T;
            }
            return retryResponse.json();
          }

          // Retry also failed - redirect to login
          if (retryResponse.status === 401) {
            return redirectToLogin();
          }

          const error = await retryResponse.text();
          throw new Error(error || `API error: ${retryResponse.status}`);
        }

        // Refresh failed - redirect to login
        return redirectToLogin();
      }
    }

    const error = await response.text();
    throw new Error(error || `API error: ${response.status}`);
  }

  // Handle 204 No Content (e.g., DELETE responses)
  if (response.status === 204) {
    return undefined as T;
  }

  return response.json();
}

// Entities
export const api = {
  // Entity CRUD
  entities: {
    list: (params?: {
      entity_type?: string;
      language?: string;
      category?: string;
      page?: number;
      page_size?: number;
    }) => {
      const searchParams = new URLSearchParams();
      if (params?.entity_type) searchParams.set('entity_type', params.entity_type);
      if (params?.language) searchParams.set('language', params.language);
      if (params?.category) searchParams.set('category', params.category);
      if (params?.page) searchParams.set('page', params.page.toString());
      if (params?.page_size) searchParams.set('page_size', params.page_size.toString());
      const query = searchParams.toString();
      return fetchApi<EntityListResponse>(`/entities${query ? `?${query}` : ''}`);
    },

    get: (id: string) => fetchApi<Entity>(`/entities/${id}`),

    create: (entity: EntityCreate) =>
      fetchApi<Entity>('/entities', {
        method: 'POST',
        body: JSON.stringify(entity),
      }),

    update: (id: string, updates: EntityUpdate) =>
      fetchApi<Entity>(`/entities/${id}`, {
        method: 'PATCH',
        body: JSON.stringify(updates),
      }),

    delete: (id: string) =>
      fetchApi<void>(`/entities/${id}`, {
        method: 'DELETE',
      }),
  },

  // Search
  search: {
    query: (params: {
      query: string;
      types?: string[];
      language?: string;
      category?: string;
      status?: string;
      project?: string;
      assignee?: string;
      since?: string;
      limit?: number;
      include_content?: boolean;
      include_documents?: boolean;
      include_graph?: boolean;
      use_enhanced?: boolean;
      boost_recent?: boolean;
    }) =>
      fetchApi<SearchResponse>('/search', {
        method: 'POST',
        body: JSON.stringify(params),
      }),

    explore: (params: {
      mode?: 'list' | 'related' | 'traverse';
      types?: string[];
      entity_id?: string;
      relationship_types?: string[];
      depth?: number;
      language?: string;
      category?: string;
      limit?: number;
    }) =>
      fetchApi<{
        mode: string;
        entities: unknown[];
        total: number;
        filters: Record<string, unknown>;
      }>('/search/explore', {
        method: 'POST',
        body: JSON.stringify(params),
      }),
  },

  // Graph
  graph: {
    nodes: (params?: { types?: string[]; limit?: number }) => {
      const searchParams = new URLSearchParams();
      if (params?.types) {
        for (const t of params.types) searchParams.append('types', t);
      }
      if (params?.limit) searchParams.set('limit', params.limit.toString());
      const query = searchParams.toString();
      return fetchApi<GraphNode[]>(`/graph/nodes${query ? `?${query}` : ''}`);
    },

    edges: (params?: { relationship_types?: string[]; limit?: number }) => {
      const searchParams = new URLSearchParams();
      if (params?.relationship_types) {
        for (const t of params.relationship_types) searchParams.append('relationship_types', t);
      }
      if (params?.limit) searchParams.set('limit', params.limit.toString());
      const query = searchParams.toString();
      return fetchApi<GraphEdge[]>(`/graph/edges${query ? `?${query}` : ''}`);
    },

    full: (params?: { types?: string[]; max_nodes?: number; max_edges?: number }) => {
      const searchParams = new URLSearchParams();
      if (params?.types) {
        for (const t of params.types) searchParams.append('types', t);
      }
      if (params?.max_nodes) searchParams.set('max_nodes', params.max_nodes.toString());
      if (params?.max_edges) searchParams.set('max_edges', params.max_edges.toString());
      const query = searchParams.toString();
      return fetchApi<GraphData>(`/graph/full${query ? `?${query}` : ''}`);
    },

    subgraph: (params: {
      entity_id: string;
      depth?: number;
      relationship_types?: string[];
      max_nodes?: number;
    }) =>
      fetchApi<GraphData>('/graph/subgraph', {
        method: 'POST',
        body: JSON.stringify(params),
      }),

    // Cluster endpoints for bubble visualization
    clusters: (params?: { refresh?: boolean }) => {
      const searchParams = new URLSearchParams();
      if (params?.refresh) searchParams.set('refresh', 'true');
      const query = searchParams.toString();
      return fetchApi<ClustersResponse>(`/graph/clusters${query ? `?${query}` : ''}`);
    },

    clusterDetail: (clusterId: string) =>
      fetchApi<ClusterDetailResponse>(`/graph/clusters/${encodeURIComponent(clusterId)}`),

    stats: () => fetchApi<GraphStatsResponse>('/graph/stats'),

    // Hierarchical graph with cluster assignments for rich visualization
    hierarchical: (params?: { max_nodes?: number; max_edges?: number; refresh?: boolean }) => {
      const searchParams = new URLSearchParams();
      if (params?.max_nodes) searchParams.set('max_nodes', params.max_nodes.toString());
      if (params?.max_edges) searchParams.set('max_edges', params.max_edges.toString());
      if (params?.refresh) searchParams.set('refresh', 'true');
      const query = searchParams.toString();
      return fetchApi<HierarchicalGraphResponse>(`/graph/hierarchical${query ? `?${query}` : ''}`);
    },
  },

  // Health check (public - no auth required)
  checkHealth: () => fetchApi<{ status: string }>('/health'),

  // Admin
  admin: {
    health: () => fetchApi<HealthResponse>('/admin/health'),
    stats: () => fetchApi<StatsResponse>('/admin/stats'),
    backup: () =>
      fetchApi<BackupResponse>('/admin/backup', {
        method: 'POST',
      }),
    restore: (backupData: BackupData, skipExisting = true) =>
      fetchApi<RestoreResponse>('/admin/restore', {
        method: 'POST',
        body: JSON.stringify({
          backup_data: backupData,
          skip_existing: skipExisting,
        }),
      }),
  },

  auth: {
    me: () => fetchApi<AuthMeResponse>('/auth/me'),
    logout: () =>
      fetchApi<void>('/auth/logout', {
        method: 'POST',
      }),
  },

  // Security (sessions, API keys, OAuth connections, password)
  security: {
    // Sessions
    sessions: {
      list: () => fetchApi<SessionsResponse>('/me/sessions'),
      revoke: (sessionId: string) =>
        fetchApi<{ success: boolean }>(`/me/sessions/${sessionId}`, {
          method: 'DELETE',
        }),
      revokeAll: () =>
        fetchApi<{ revoked: number }>('/me/sessions', {
          method: 'DELETE',
        }),
    },

    // API Keys
    apiKeys: {
      list: () => fetchApi<ApiKeysResponse>('/api-keys'),
      create: (data: ApiKeyCreateRequest) =>
        fetchApi<ApiKeyCreateResponse>('/api-keys', {
          method: 'POST',
          body: JSON.stringify(data),
        }),
      revoke: (keyId: string) =>
        fetchApi<{ success: boolean }>(`/api-keys/${keyId}/revoke`, {
          method: 'POST',
        }),
    },

    // OAuth Connections
    connections: {
      list: () => fetchApi<OAuthConnectionsResponse>('/me/connections'),
      remove: (connectionId: string) =>
        fetchApi<{ success: boolean }>(`/me/connections/${connectionId}`, {
          method: 'DELETE',
        }),
    },

    // Password
    changePassword: (data: PasswordChangeRequest) =>
      fetchApi<{ success: boolean }>('/me/password', {
        method: 'POST',
        body: JSON.stringify(data),
      }),
  },

  // User Preferences
  preferences: {
    get: () => fetchApi<PreferencesResponse>('/users/me/preferences'),
    update: (preferences: Partial<UserPreferences>) =>
      fetchApi<PreferencesResponse>('/users/me/preferences', {
        method: 'PATCH',
        body: JSON.stringify({ preferences }),
      }),
  },

  orgs: {
    list: () => fetchApi<OrgListResponse>('/orgs'),
    get: (slug: string) => fetchApi<OrgGetResponse>(`/orgs/${encodeURIComponent(slug)}`),
    create: (data: OrgCreateRequest) =>
      fetchApi<OrgCreateResponse>('/orgs', {
        method: 'POST',
        body: JSON.stringify(data),
      }),
    update: (slug: string, data: OrgUpdateRequest) =>
      fetchApi<{ organization: { id: string; slug: string; name: string } }>(
        `/orgs/${encodeURIComponent(slug)}`,
        {
          method: 'PATCH',
          body: JSON.stringify(data),
        }
      ),
    delete: (slug: string) =>
      fetchApi<void>(`/orgs/${encodeURIComponent(slug)}`, {
        method: 'DELETE',
      }),
    switch: (slug: string) =>
      fetchApi<OrgSwitchResponse>(`/orgs/${encodeURIComponent(slug)}/switch`, {
        method: 'POST',
      }),
    members: {
      list: (slug: string) =>
        fetchApi<OrgMembersResponse>(`/orgs/${encodeURIComponent(slug)}/members`),
      add: (slug: string, userId: string, role: string) =>
        fetchApi<{ user_id: string; role: string }>(`/orgs/${encodeURIComponent(slug)}/members`, {
          method: 'POST',
          body: JSON.stringify({ user_id: userId, role }),
        }),
      updateRole: (slug: string, userId: string, role: string) =>
        fetchApi<{ user_id: string; role: string }>(
          `/orgs/${encodeURIComponent(slug)}/members/${userId}`,
          {
            method: 'PATCH',
            body: JSON.stringify({ role }),
          }
        ),
      remove: (slug: string, userId: string) =>
        fetchApi<{ success: boolean }>(`/orgs/${encodeURIComponent(slug)}/members/${userId}`, {
          method: 'DELETE',
        }),
    },
  },

  // Tasks
  tasks: {
    list: (params?: { project?: string; status?: TaskStatus }) =>
      fetchApi<TaskListResponse>('/search/explore', {
        method: 'POST',
        body: JSON.stringify({
          mode: 'list',
          types: ['task'],
          project: params?.project,
          status: params?.status,
          limit: 200,
        }),
      }),

    get: (id: string) => fetchApi<Entity>(`/entities/${id}`),

    // RESTful task workflow endpoints
    start: (id: string, params?: { assignee?: string }) =>
      fetchApi<TaskActionResponse>(`/tasks/${id}/start`, {
        method: 'POST',
        body: params ? JSON.stringify(params) : undefined,
      }),

    block: (id: string, reason: string) =>
      fetchApi<TaskActionResponse>(`/tasks/${id}/block`, {
        method: 'POST',
        body: JSON.stringify({ reason }),
      }),

    unblock: (id: string) =>
      fetchApi<TaskActionResponse>(`/tasks/${id}/unblock`, {
        method: 'POST',
      }),

    review: (id: string, params?: { pr_url?: string; commit_shas?: string[] }) =>
      fetchApi<TaskActionResponse>(`/tasks/${id}/review`, {
        method: 'POST',
        body: params ? JSON.stringify(params) : undefined,
      }),

    complete: (id: string, params?: { actual_hours?: number; learnings?: string }) =>
      fetchApi<TaskActionResponse>(`/tasks/${id}/complete`, {
        method: 'POST',
        body: params ? JSON.stringify(params) : undefined,
      }),

    archive: (id: string, params?: { reason?: string }) =>
      fetchApi<TaskActionResponse>(`/tasks/${id}/archive`, {
        method: 'POST',
        body: params ? JSON.stringify(params) : undefined,
      }),

    updateStatus: (id: string, status: TaskStatus) =>
      fetchApi<Entity>(`/entities/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ metadata: { status } }),
      }),
  },

  // Projects (via explore endpoint)
  projects: {
    list: (options?: { includeArchived?: boolean }) =>
      fetchApi<TaskListResponse>('/search/explore', {
        method: 'POST',
        body: JSON.stringify({
          mode: 'list',
          types: ['project'],
          limit: 100,
          include_archived: options?.includeArchived ?? false,
        }),
      }),

    get: (id: string) => fetchApi<Entity>(`/entities/${id}`),
  },

  // Epics - feature grouping for tasks
  epics: {
    list: (params?: { project?: string; status?: EpicStatus }) =>
      fetchApi<EpicListResponse>('/search/explore', {
        method: 'POST',
        body: JSON.stringify({
          mode: 'list',
          types: ['epic'],
          project: params?.project,
          status: params?.status,
          limit: 200,
        }),
      }),

    get: (id: string) => fetchApi<Entity>(`/entities/${id}`),

    tasks: (id: string) =>
      fetchApi<TaskListResponse>('/search/explore', {
        method: 'POST',
        body: JSON.stringify({
          mode: 'list',
          types: ['task'],
          epic: id,
          limit: 200,
        }),
      }),

    // RESTful epic workflow endpoints
    start: (id: string) =>
      fetchApi<EpicActionResponse>(`/epics/${id}/start`, {
        method: 'POST',
      }),

    complete: (id: string, params?: { learnings?: string }) =>
      fetchApi<EpicActionResponse>(`/epics/${id}/complete`, {
        method: 'POST',
        body: params ? JSON.stringify(params) : undefined,
      }),

    archive: (id: string, params?: { reason?: string }) =>
      fetchApi<EpicActionResponse>(`/epics/${id}/archive`, {
        method: 'POST',
        body: params ? JSON.stringify(params) : undefined,
      }),

    update: (
      id: string,
      params: {
        status?: EpicStatus;
        priority?: TaskPriority;
        title?: string;
        description?: string;
        assignees?: string[];
        tags?: string[];
      }
    ) =>
      fetchApi<EpicActionResponse>(`/epics/${id}`, {
        method: 'PATCH',
        body: JSON.stringify(params),
      }),
  },

  // Sources (documentation crawling) - uses dedicated /sources endpoints
  sources: {
    list: () =>
      fetchApi<{ sources: CrawlSource[]; total: number }>('/sources').then(data => ({
        mode: 'list',
        entities: data.sources.map(s => ({
          id: s.id,
          type: 'source',
          name: s.name,
          description: s.description || '',
          created_at: s.created_at,
          updated_at: s.last_crawled_at || s.created_at,
          metadata: {
            url: s.url,
            source_type: s.source_type,
            crawl_status: s.crawl_status,
            document_count: s.document_count,
            last_crawled: s.last_crawled_at ?? undefined,
            crawl_depth: s.crawl_depth,
            crawl_patterns: s.include_patterns,
            exclude_patterns: s.exclude_patterns,
          },
        })),
        total: data.total,
        filters: {},
      })),

    get: (id: string) => fetchApi<CrawlSource>(`/sources/${id}`),

    create: (source: {
      name: string;
      url: string;
      description?: string;
      source_type?: SourceType;
      crawl_depth?: number;
      crawl_patterns?: string[];
      exclude_patterns?: string[];
    }) =>
      fetchApi<CrawlSource>('/sources', {
        method: 'POST',
        body: JSON.stringify({
          name: source.name,
          url: source.url,
          description: source.description || null,
          source_type: source.source_type || 'website',
          crawl_depth: source.crawl_depth || 2,
          include_patterns: source.crawl_patterns || [],
          exclude_patterns: source.exclude_patterns || [],
        }),
      }),

    delete: (id: string) =>
      fetchApi<void>(`/sources/${id}`, {
        method: 'DELETE',
      }),

    update: (
      id: string,
      updates: {
        name?: string;
        description?: string;
        crawl_depth?: number;
        include_patterns?: string[];
        exclude_patterns?: string[];
      }
    ) =>
      fetchApi<CrawlSource>(`/sources/${id}`, {
        method: 'PATCH',
        body: JSON.stringify(updates),
      }),

    // Trigger a crawl for a source
    crawl: (id: string, options?: { maxPages?: number; maxDepth?: number }) =>
      fetchApi<{ source_id: string; status: string; message: string }>(`/sources/${id}/ingest`, {
        method: 'POST',
        body: JSON.stringify({
          max_pages: options?.maxPages ?? 50,
          max_depth: options?.maxDepth ?? 3,
          generate_embeddings: true,
        }),
      }),

    // Get crawl status
    status: (id: string) =>
      fetchApi<{
        source_id: string;
        running: boolean;
        documents_crawled?: number;
        errors?: number;
      }>(`/sources/${id}/status`),

    // Preview URL metadata for better source naming
    preview: (url: string) =>
      fetchApi<{ url: string; title: string | null; suggested_name: string; domain: string }>(
        `/sources/preview?url=${encodeURIComponent(url)}`
      ),
  },

  // RAG (Documentation Search)
  rag: {
    // Vector similarity search on document chunks
    search: (params: RAGSearchParams) =>
      fetchApi<RAGSearchResponse>('/rag/search', {
        method: 'POST',
        body: JSON.stringify(params),
      }),

    // Hybrid search (vector + full-text)
    hybridSearch: (params: RAGSearchParams) =>
      fetchApi<RAGSearchResponse>('/rag/hybrid-search', {
        method: 'POST',
        body: JSON.stringify(params),
      }),

    // Code example search
    codeExamples: (params: CodeExampleParams) =>
      fetchApi<CodeExampleResponse>('/rag/code-examples', {
        method: 'POST',
        body: JSON.stringify(params),
      }),

    // Get full page content by ID
    getPage: (documentId: string) => fetchApi<FullPageResponse>(`/rag/pages/${documentId}`),

    // Update document title and/or content
    updateDocument: (documentId: string, updates: { title?: string; content?: string }) =>
      fetchApi<FullPageResponse>(`/rag/pages/${documentId}`, {
        method: 'PATCH',
        body: JSON.stringify(updates),
      }),

    // Get related entities for a document
    getDocumentEntities: (documentId: string) =>
      fetchApi<DocumentRelatedEntitiesResponse>(`/rag/pages/${documentId}/entities`),

    // Get full page content by URL
    getPageByUrl: (url: string) =>
      fetchApi<FullPageResponse>(`/rag/pages/by-url?url=${encodeURIComponent(url)}`),

    // List pages for a source
    listPages: (
      sourceId: string,
      params?: { limit?: number; offset?: number; has_code?: boolean; is_index?: boolean }
    ) => {
      const searchParams = new URLSearchParams();
      if (params?.limit) searchParams.set('limit', params.limit.toString());
      if (params?.offset) searchParams.set('offset', params.offset.toString());
      if (params?.has_code !== undefined) searchParams.set('has_code', params.has_code.toString());
      if (params?.is_index !== undefined) searchParams.set('is_index', params.is_index.toString());
      const query = searchParams.toString();
      return fetchApi<{
        source_id: string;
        source_name: string;
        pages: Array<{
          id: string;
          url: string;
          title: string;
          word_count: number;
          has_code: boolean;
          is_index: boolean;
        }>;
        total: number;
        has_more: boolean;
      }>(`/rag/sources/${sourceId}/pages${query ? `?${query}` : ''}`);
    },
  },

  // Metrics
  metrics: {
    // Get org-level metrics
    org: () => fetchApi<OrgMetricsResponse>('/metrics'),

    // Get project-level metrics
    project: (projectId: string) =>
      fetchApi<ProjectMetricsResponse>(`/metrics/projects/${projectId}`),
  },
};
