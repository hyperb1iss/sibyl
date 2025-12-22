'use client';

/**
 * React Query hooks for Sibyl API.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';

import type {
  CodeExampleParams,
  CodeExampleResponse,
  EntityCreate,
  EntityUpdate,
  RAGSearchParams,
  RAGSearchResponse,
  TaskStatus,
} from './api';
import { api } from './api';
import { TIMING } from './constants';
import { type ConnectionStatus, wsClient } from './websocket';

// =============================================================================
// Query Keys
// =============================================================================

export const queryKeys = {
  auth: {
    me: ['auth', 'me'] as const,
  },
  orgs: {
    list: ['orgs', 'list'] as const,
  },
  entities: {
    all: ['entities'] as const,
    list: (params?: Parameters<typeof api.entities.list>[0]) =>
      ['entities', 'list', params] as const,
    detail: (id: string) => ['entities', 'detail', id] as const,
  },
  search: {
    all: ['search'] as const,
    query: (params: Parameters<typeof api.search.query>[0]) => ['search', 'query', params] as const,
  },
  rag: {
    all: ['rag'] as const,
    search: (params: RAGSearchParams) => ['rag', 'search', params] as const,
    hybrid: (params: RAGSearchParams) => ['rag', 'hybrid', params] as const,
    code: (params: CodeExampleParams) => ['rag', 'code', params] as const,
    page: (documentId: string) => ['rag', 'page', documentId] as const,
    pageEntities: (documentId: string) => ['rag', 'page', documentId, 'entities'] as const,
    pages: (sourceId: string, params?: Record<string, unknown>) =>
      ['rag', 'pages', sourceId, params] as const,
  },
  graph: {
    all: ['graph'] as const,
    full: (params?: Parameters<typeof api.graph.full>[0]) => ['graph', 'full', params] as const,
    subgraph: (params: Parameters<typeof api.graph.subgraph>[0]) =>
      ['graph', 'subgraph', params] as const,
  },
  admin: {
    health: ['admin', 'health'] as const,
    stats: ['admin', 'stats'] as const,
    ingestStatus: ['admin', 'ingest-status'] as const,
  },
  tasks: {
    all: ['tasks'] as const,
    list: (params?: { project?: string; status?: TaskStatus }) => {
      const normalized =
        params && (params.project || params.status)
          ? { ...(params.project ? { project: params.project } : {}), ...(params.status ? { status: params.status } : {}) }
          : undefined;
      return ['tasks', 'list', normalized] as const;
    },
    detail: (id: string) => ['tasks', 'detail', id] as const,
  },
  projects: {
    all: ['projects'] as const,
    list: ['projects', 'list'] as const,
    detail: (id: string) => ['projects', 'detail', id] as const,
  },
  explore: {
    related: (entityId: string) => ['explore', 'related', entityId] as const,
  },
  sources: {
    all: ['sources'] as const,
    list: ['sources', 'list'] as const,
    detail: (id: string) => ['sources', 'detail', id] as const,
  },
};

// =============================================================================
// Auth + Orgs Hooks
// =============================================================================

export function useMe(options?: {
  enabled?: boolean;
  initialData?: import('./api').AuthMeResponse;
}) {
  return useQuery({
    queryKey: queryKeys.auth.me,
    queryFn: () => api.auth.me(),
    enabled: options?.enabled ?? true,
    retry: false,
    initialData: options?.initialData,
  });
}

export function useOrgs(options?: {
  enabled?: boolean;
  initialData?: import('./api').OrgListResponse;
}) {
  return useQuery({
    queryKey: queryKeys.orgs.list,
    queryFn: () => api.orgs.list(),
    enabled: options?.enabled ?? true,
    retry: false,
    initialData: options?.initialData,
  });
}

export function useSwitchOrg() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (slug: string) => api.orgs.switch(slug),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.auth.me });
      queryClient.invalidateQueries({ queryKey: queryKeys.orgs.list });
      queryClient.invalidateQueries({ queryKey: queryKeys.entities.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.tasks.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.projects.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.graph.all });
    },
  });
}

// =============================================================================
// Entity Hooks
// =============================================================================

export function useEntities(
  params?: Parameters<typeof api.entities.list>[0],
  initialData?: import('./api').EntityListResponse
) {
  return useQuery({
    queryKey: queryKeys.entities.list(params),
    queryFn: () => api.entities.list(params),
    initialData,
  });
}

export function useEntity(id: string, initialData?: import('./api').Entity) {
  return useQuery({
    queryKey: queryKeys.entities.detail(id),
    queryFn: () => api.entities.get(id),
    enabled: !!id,
    initialData,
  });
}

export function useCreateEntity() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (entity: EntityCreate) => api.entities.create(entity),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.entities.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.admin.stats });
      queryClient.invalidateQueries({ queryKey: queryKeys.graph.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.tasks.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.projects.all });
    },
  });
}

export function useUpdateEntity() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ id, updates }: { id: string; updates: EntityUpdate }) =>
      api.entities.update(id, updates),
    onSuccess: (_data, { id }) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.entities.detail(id) });
      queryClient.invalidateQueries({ queryKey: queryKeys.entities.list() });
      queryClient.invalidateQueries({ queryKey: queryKeys.graph.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.tasks.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.projects.all });
    },
  });
}

export function useDeleteEntity() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (id: string) => api.entities.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.entities.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.admin.stats });
      queryClient.invalidateQueries({ queryKey: queryKeys.graph.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.tasks.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.projects.all });
    },
  });
}

// =============================================================================
// Search Hooks
// =============================================================================

export function useSearch(
  params: Parameters<typeof api.search.query>[0],
  options?: { enabled?: boolean; initialData?: import('./api').SearchResponse }
) {
  return useQuery({
    queryKey: queryKeys.search.query(params),
    queryFn: () => api.search.query(params),
    enabled: (options?.enabled ?? true) && !!params.query,
    initialData: options?.initialData,
  });
}

// =============================================================================
// RAG Search Hooks (Documentation Search)
// =============================================================================

/**
 * Search documentation chunks using vector similarity.
 */
export function useRAGSearch(
  params: RAGSearchParams,
  options?: { enabled?: boolean; initialData?: RAGSearchResponse }
) {
  return useQuery({
    queryKey: queryKeys.rag.search(params),
    queryFn: () => api.rag.search(params),
    enabled: (options?.enabled ?? true) && !!params.query,
    initialData: options?.initialData,
  });
}

/**
 * Hybrid search combining vector similarity and full-text search.
 */
export function useRAGHybridSearch(
  params: RAGSearchParams,
  options?: { enabled?: boolean; initialData?: RAGSearchResponse }
) {
  return useQuery({
    queryKey: queryKeys.rag.hybrid(params),
    queryFn: () => api.rag.hybridSearch(params),
    enabled: (options?.enabled ?? true) && !!params.query,
    initialData: options?.initialData,
  });
}

/**
 * Search for code examples in documentation.
 */
export function useCodeExamples(
  params: CodeExampleParams,
  options?: { enabled?: boolean; initialData?: CodeExampleResponse }
) {
  return useQuery({
    queryKey: queryKeys.rag.code(params),
    queryFn: () => api.rag.codeExamples(params),
    enabled: (options?.enabled ?? true) && !!params.query,
    initialData: options?.initialData,
  });
}

/**
 * Get full page content by document ID.
 */
export function useFullPage(documentId: string, options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: queryKeys.rag.page(documentId),
    queryFn: () => api.rag.getPage(documentId),
    enabled: (options?.enabled ?? true) && !!documentId,
  });
}

/**
 * List pages for a documentation source.
 */
export function useSourcePages(
  sourceId: string,
  params?: { limit?: number; offset?: number; has_code?: boolean; is_index?: boolean }
) {
  return useQuery({
    queryKey: queryKeys.rag.pages(sourceId, params),
    queryFn: () => api.rag.listPages(sourceId, params),
    enabled: !!sourceId,
  });
}

/**
 * Update a document's title and/or content.
 */
export function useUpdateDocument() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      documentId,
      updates,
    }: {
      documentId: string;
      updates: { title?: string; content?: string };
    }) => api.rag.updateDocument(documentId, updates),
    onSuccess: (data, { documentId }) => {
      // Update the cache with the new data
      queryClient.setQueryData(queryKeys.rag.page(documentId), data);
      // Invalidate the pages list for the source
      queryClient.invalidateQueries({ queryKey: queryKeys.rag.pages(data.source_id) });
      // Invalidate the source detail to refresh document counts
      queryClient.invalidateQueries({ queryKey: queryKeys.sources.detail(data.source_id) });
    },
  });
}

/**
 * Get related entities for a document.
 */
export function useDocumentEntities(documentId: string, options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: queryKeys.rag.pageEntities(documentId),
    queryFn: () => api.rag.getDocumentEntities(documentId),
    enabled: (options?.enabled ?? true) && !!documentId,
  });
}

// =============================================================================
// Graph Hooks
// =============================================================================

export function useGraphData(params?: Parameters<typeof api.graph.full>[0]) {
  return useQuery({
    queryKey: queryKeys.graph.full(params),
    queryFn: () => api.graph.full(params),
  });
}

export function useSubgraph(params: Parameters<typeof api.graph.subgraph>[0]) {
  return useQuery({
    queryKey: queryKeys.graph.subgraph(params),
    queryFn: () => api.graph.subgraph(params),
    enabled: !!params.entity_id,
  });
}

// =============================================================================
// Admin Hooks
// =============================================================================

export function useHealth() {
  return useQuery({
    queryKey: queryKeys.admin.health,
    queryFn: api.admin.health,
    refetchInterval: TIMING.HEALTH_CHECK_INTERVAL,
  });
}

export function useStats(initialData?: import('./api').StatsResponse) {
  return useQuery({
    queryKey: queryKeys.admin.stats,
    queryFn: api.admin.stats,
    initialData,
  });
}

export function useIngest() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (params?: { path?: string; force?: boolean }) => api.admin.ingest(params),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.admin.ingestStatus });
    },
  });
}

export function useIngestStatus() {
  return useQuery({
    queryKey: queryKeys.admin.ingestStatus,
    queryFn: api.admin.ingestStatus,
    refetchInterval: query => (query.state.data?.running ? 1000 : false), // Poll while running
  });
}

// =============================================================================
// WebSocket Hook
// =============================================================================

export function useRealtimeUpdates() {
  const queryClient = useQueryClient();

  useEffect(() => {
    // Connect on mount
    wsClient.connect();

    // Helper to invalidate all entity-related queries
    const invalidateAllEntities = () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.entities.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.tasks.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.projects.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.sources.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.graph.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.admin.stats });
    };

    // Entity created - invalidate lists and stats
    const unsubCreate = wsClient.on('entity_created', data => {
      invalidateAllEntities();
      console.log('[WS] Entity created:', data.id);
    });

    // Entity updated - targeted + list invalidation
    const unsubUpdate = wsClient.on('entity_updated', data => {
      const entityId = data.id as string;
      if (entityId) {
        // Targeted invalidation for the specific entity
        queryClient.invalidateQueries({ queryKey: queryKeys.entities.detail(entityId) });
        queryClient.invalidateQueries({ queryKey: queryKeys.tasks.detail(entityId) });
        queryClient.invalidateQueries({ queryKey: queryKeys.projects.detail(entityId) });
        queryClient.invalidateQueries({ queryKey: queryKeys.sources.detail(entityId) });
        queryClient.invalidateQueries({ queryKey: queryKeys.explore.related(entityId) });
      }
      // Also invalidate lists as the entity might affect sorting/filtering
      queryClient.invalidateQueries({ queryKey: queryKeys.entities.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.tasks.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.projects.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.graph.all });
      console.log('[WS] Entity updated:', entityId);
    });

    // Entity deleted - remove from cache + invalidate lists
    const unsubDelete = wsClient.on('entity_deleted', data => {
      const entityId = data.id as string;
      if (entityId) {
        // Remove deleted entity from cache entirely
        queryClient.removeQueries({ queryKey: queryKeys.entities.detail(entityId) });
        queryClient.removeQueries({ queryKey: queryKeys.tasks.detail(entityId) });
        queryClient.removeQueries({ queryKey: queryKeys.projects.detail(entityId) });
        queryClient.removeQueries({ queryKey: queryKeys.sources.detail(entityId) });
      }
      invalidateAllEntities();
      console.log('[WS] Entity deleted:', entityId);
    });

    // Ingest progress - update status in real-time
    const unsubIngestProgress = wsClient.on('ingest_progress', data => {
      queryClient.setQueryData(queryKeys.admin.ingestStatus, data);
    });

    // Ingest complete - refresh everything
    const unsubIngestComplete = wsClient.on('ingest_complete', () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.admin.ingestStatus });
      invalidateAllEntities();
      console.log('[WS] Ingest complete');
    });

    // Health update
    const unsubHealth = wsClient.on('health_update', () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.admin.health });
    });

    // Search complete (if backend sends it)
    const unsubSearch = wsClient.on('search_complete', () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.search.all });
    });

    // Crawl progress - update in real-time
    const unsubCrawlProgress = wsClient.on('crawl_progress', data => {
      const sourceId = data.source_id as string;
      queryClient.setQueryData(['crawl_progress', sourceId], data);
      console.log('[WS] Crawl progress:', data);
    });

    // Cleanup on unmount
    return () => {
      unsubCreate();
      unsubUpdate();
      unsubDelete();
      unsubIngestProgress();
      unsubIngestComplete();
      unsubHealth();
      unsubSearch();
      unsubCrawlProgress();
      wsClient.disconnect();
    };
  }, [queryClient]);
}

/**
 * Hook to track WebSocket connection status.
 */
export function useConnectionStatus(): ConnectionStatus {
  const [status, setStatus] = useState<ConnectionStatus>(wsClient.status);

  useEffect(() => {
    const unsubscribe = wsClient.on('connection_status', data => {
      setStatus(data.status as ConnectionStatus);
    });

    // Sync initial status
    setStatus(wsClient.status);

    return unsubscribe;
  }, []);

  return status;
}

// =============================================================================
// Task Hooks
// =============================================================================

export function useTasks(params?: { project?: string; status?: TaskStatus }) {
  const normalized =
    params && (params.project || params.status)
      ? { ...(params.project ? { project: params.project } : {}), ...(params.status ? { status: params.status } : {}) }
      : undefined;

  return useQuery({
    queryKey: queryKeys.tasks.list(normalized),
    queryFn: () => api.tasks.list(normalized),
  });
}

export function useTask(id: string) {
  return useQuery({
    queryKey: queryKeys.tasks.detail(id),
    queryFn: () => api.tasks.get(id),
    enabled: !!id,
  });
}

export function useTaskManage() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      action,
      entity_id,
      params,
    }: {
      action: Parameters<typeof api.tasks.manage>[0];
      entity_id: string;
      params?: Parameters<typeof api.tasks.manage>[2];
    }) => api.tasks.manage(action, entity_id, params),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.tasks.all });
    },
  });
}

export function useTaskUpdateStatus() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ id, status }: { id: string; status: TaskStatus }) =>
      api.tasks.updateStatus(id, status),
    onSuccess: (_data, { id }) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.tasks.detail(id) });
      queryClient.invalidateQueries({ queryKey: queryKeys.tasks.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.entities.detail(id) });
    },
  });
}

// =============================================================================
// Project Hooks
// =============================================================================

export function useProjects(initialData?: import('./api').TaskListResponse) {
  return useQuery({
    queryKey: queryKeys.projects.list,
    queryFn: () => api.projects.list(),
    initialData,
  });
}

export function useProject(id: string) {
  return useQuery({
    queryKey: queryKeys.projects.detail(id),
    queryFn: () => api.projects.get(id),
    enabled: !!id,
  });
}

// =============================================================================
// Explore Hooks
// =============================================================================

export function useRelatedEntities(entityId: string) {
  return useQuery({
    queryKey: queryKeys.explore.related(entityId),
    queryFn: () =>
      api.search.explore({
        mode: 'related',
        entity_id: entityId,
        depth: 1,
        limit: 20,
      }),
    enabled: !!entityId,
  });
}

// =============================================================================
// Source Hooks
// =============================================================================

export function useSources() {
  return useQuery({
    queryKey: queryKeys.sources.list,
    queryFn: () => api.sources.list(),
  });
}

export function useSource(id: string) {
  return useQuery({
    queryKey: queryKeys.sources.detail(id),
    queryFn: () => api.sources.get(id),
    enabled: !!id,
  });
}

export function useCreateSource() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (source: Parameters<typeof api.sources.create>[0]) => api.sources.create(source),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.sources.all });
    },
  });
}

export function useDeleteSource() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (id: string) => api.sources.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.sources.all });
    },
  });
}

export function useCrawlSource() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (id: string) => api.sources.crawl(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.sources.all });
    },
  });
}

export function useSyncSource() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async (id: string) => {
      const res = await fetch(`/api/sources/${id}/sync`, { method: 'POST' });
      if (!res.ok) throw new Error('Failed to sync source');
      return res.json();
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.sources.all });
    },
  });
}

export function useCancelCrawl() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async (id: string) => {
      const res = await fetch(`/api/sources/${id}/cancel`, { method: 'POST' });
      if (!res.ok) throw new Error('Failed to cancel crawl');
      return res.json();
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.sources.all });
    },
  });
}

export interface CrawlProgressData {
  source_id: string;
  pages_crawled: number;
  max_pages: number;
  current_url: string;
  percentage: number;
}

export function useCrawlProgress(sourceId: string): CrawlProgressData | undefined {
  const queryClient = useQueryClient();
  const [progress, setProgress] = useState<CrawlProgressData | undefined>(
    queryClient.getQueryData(['crawl_progress', sourceId])
  );

  useEffect(() => {
    // Subscribe to query cache changes
    const unsubscribe = queryClient.getQueryCache().subscribe(event => {
      if (
        event.type === 'updated' &&
        event.query.queryKey[0] === 'crawl_progress' &&
        event.query.queryKey[1] === sourceId
      ) {
        setProgress(event.query.state.data as CrawlProgressData | undefined);
      }
    });

    return unsubscribe;
  }, [queryClient, sourceId]);

  return progress;
}

/**
 * Track crawl progress for all sources.
 * Returns a map of source_id -> progress data.
 */
export function useAllCrawlProgress(): Map<string, CrawlProgressData> {
  const queryClient = useQueryClient();
  const [progressMap, setProgressMap] = useState<Map<string, CrawlProgressData>>(new Map());

  useEffect(() => {
    // Subscribe to all crawl_progress updates
    const unsubscribe = queryClient.getQueryCache().subscribe(event => {
      if (event.type === 'updated' && event.query.queryKey[0] === 'crawl_progress') {
        const sourceId = event.query.queryKey[1] as string;
        const data = event.query.state.data as CrawlProgressData | undefined;

        setProgressMap(prev => {
          const next = new Map(prev);
          if (data) {
            next.set(sourceId, data);
          } else {
            next.delete(sourceId);
          }
          return next;
        });
      }
    });

    return unsubscribe;
  }, [queryClient]);

  return progressMap;
}
