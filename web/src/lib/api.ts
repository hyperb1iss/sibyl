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

export interface ManageResponse {
  success: boolean;
  action: string;
  entity_id: string | null;
  message: string;
  data: Record<string, unknown>;
}

// =============================================================================
// Source Types (Documentation Crawling)
// =============================================================================

export type SourceType = 'website' | 'github' | 'local' | 'api_docs';
export type CrawlStatus = 'pending' | 'in_progress' | 'completed' | 'failed' | 'partial';

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

// =============================================================================
// API Functions
// =============================================================================

async function fetchApi<T>(endpoint: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${endpoint}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...options?.headers,
    },
  });

  if (!response.ok) {
    const error = await response.text();
    throw new Error(error || `API error: ${response.status}`);
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
      limit?: number;
      include_content?: boolean;
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
  },

  // Admin
  admin: {
    health: () => fetchApi<HealthResponse>('/admin/health'),
    stats: () => fetchApi<StatsResponse>('/admin/stats'),
    ingest: (params?: { path?: string; force?: boolean }) =>
      fetchApi<{
        success: boolean;
        files_processed: number;
        entities_created: number;
        entities_updated: number;
        duration_seconds: number;
        errors: string[];
      }>('/admin/ingest', {
        method: 'POST',
        body: JSON.stringify(params || {}),
      }),
    ingestStatus: () =>
      fetchApi<{
        running: boolean;
        progress: number;
        files_processed: number;
        entities_created: number;
        entities_updated: number;
        errors: string[];
      }>('/admin/ingest/status'),
  },

  auth: {
    me: () => fetchApi<AuthMeResponse>('/auth/me'),
    logout: () =>
      fetchApi<void>('/auth/logout', {
        method: 'POST',
      }),
  },

  orgs: {
    list: () => fetchApi<OrgListResponse>('/orgs'),
    switch: (slug: string) =>
      fetchApi<OrgSwitchResponse>(`/orgs/${encodeURIComponent(slug)}/switch`, {
        method: 'POST',
      }),
  },

  // Tasks (via explore/manage endpoints)
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

    manage: (
      action:
        | 'start_task'
        | 'block_task'
        | 'unblock_task'
        | 'submit_review'
        | 'complete_task'
        | 'archive',
      entity_id: string,
      params?: {
        assignee?: string;
        blocker?: string;
        commit_shas?: string[];
        pr_url?: string;
        actual_hours?: number;
        learnings?: string;
      }
    ) =>
      fetchApi<ManageResponse>('/manage', {
        method: 'POST',
        body: JSON.stringify({
          action,
          entity_id,
          ...params,
        }),
      }),

    updateStatus: (id: string, status: TaskStatus) =>
      fetchApi<Entity>(`/entities/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ metadata: { status } }),
      }),
  },

  // Projects (via explore endpoint)
  projects: {
    list: () =>
      fetchApi<TaskListResponse>('/search/explore', {
        method: 'POST',
        body: JSON.stringify({
          mode: 'list',
          types: ['project'],
          limit: 100,
        }),
      }),

    get: (id: string) => fetchApi<Entity>(`/entities/${id}`),
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
};
