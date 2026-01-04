'use client';

/**
 * React Query hooks for Sibyl API.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';

import type {
  AgentStatus,
  AgentType,
  ApprovalStatus,
  ApprovalType,
  CodeExampleParams,
  CodeExampleResponse,
  CreateNoteRequest,
  EntityCreate,
  EntityUpdate,
  EpicStatus,
  RAGSearchParams,
  RAGSearchResponse,
  RespondToApprovalRequest,
  SpawnAgentRequest,
  TaskPriority,
  TaskStatus,
  UpdateSettingsRequest,
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
    detail: (slug: string) => ['orgs', 'detail', slug] as const,
    members: (slug: string) => ['orgs', 'members', slug] as const,
  },
  security: {
    sessions: ['security', 'sessions'] as const,
    apiKeys: ['security', 'apiKeys'] as const,
    connections: ['security', 'connections'] as const,
  },
  preferences: ['preferences'] as const,
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
    clusters: (params?: { refresh?: boolean }) => ['graph', 'clusters', params] as const,
    clusterDetail: (clusterId: string) => ['graph', 'cluster', clusterId] as const,
    stats: ['graph', 'stats'] as const,
    hierarchical: (params?: { max_nodes?: number; max_edges?: number; refresh?: boolean }) =>
      ['graph', 'hierarchical', params] as const,
  },
  admin: {
    health: ['admin', 'health'] as const,
    stats: ['admin', 'stats'] as const,
  },
  setup: {
    status: ['setup', 'status'] as const,
    validation: ['setup', 'validation'] as const,
    mcpCommand: ['setup', 'mcp-command'] as const,
  },
  settings: {
    all: ['settings'] as const,
  },
  tasks: {
    all: ['tasks'] as const,
    list: (params?: { project?: string; status?: TaskStatus }) => {
      const normalized =
        params && (params.project || params.status)
          ? {
              ...(params.project ? { project: params.project } : {}),
              ...(params.status ? { status: params.status } : {}),
            }
          : undefined;
      return ['tasks', 'list', normalized] as const;
    },
    detail: (id: string) => ['tasks', 'detail', id] as const,
    notes: (id: string) => ['tasks', 'notes', id] as const,
  },
  projects: {
    all: ['projects'] as const,
    list: (includeArchived = false) => ['projects', 'list', { includeArchived }] as const,
    detail: (id: string) => ['projects', 'detail', id] as const,
    members: (id: string) => ['projects', 'members', id] as const,
  },
  epics: {
    all: ['epics'] as const,
    list: (params?: { project?: string; status?: EpicStatus }) => {
      const normalized =
        params && (params.project || params.status)
          ? {
              ...(params.project ? { project: params.project } : {}),
              ...(params.status ? { status: params.status } : {}),
            }
          : undefined;
      return ['epics', 'list', normalized] as const;
    },
    detail: (id: string) => ['epics', 'detail', id] as const,
    tasks: (id: string) => ['epics', 'tasks', id] as const,
    progress: (id: string) => ['epics', 'progress', id] as const,
  },
  explore: {
    related: (entityId: string) => ['explore', 'related', entityId] as const,
  },
  sources: {
    all: ['sources'] as const,
    list: ['sources', 'list'] as const,
    detail: (id: string) => ['sources', 'detail', id] as const,
  },
  metrics: {
    org: ['metrics', 'org'] as const,
    project: (id: string) => ['metrics', 'project', id] as const,
  },
  agents: {
    all: ['agents'] as const,
    list: (params?: { project?: string; status?: AgentStatus; agent_type?: AgentType }) => {
      const normalized =
        params && (params.project || params.status || params.agent_type)
          ? {
              ...(params.project ? { project: params.project } : {}),
              ...(params.status ? { status: params.status } : {}),
              ...(params.agent_type ? { agent_type: params.agent_type } : {}),
            }
          : undefined;
      return ['agents', 'list', normalized] as const;
    },
    detail: (id: string) => ['agents', 'detail', id] as const,
    messages: (id: string, limit?: number) => ['agents', 'messages', id, { limit }] as const,
    workspace: (id: string) => ['agents', 'workspace', id] as const,
    activityFeed: (project_id?: string) =>
      ['agents', 'activity', project_id ? { project_id } : undefined] as const,
    healthOverview: (project_id?: string) =>
      ['agents', 'health', project_id ? { project_id } : undefined] as const,
  },
  approvals: {
    all: ['approvals'] as const,
    list: (params?: {
      status?: ApprovalStatus;
      approval_type?: ApprovalType;
      agent_id?: string;
      project_id?: string;
    }) => {
      const normalized =
        params && (params.status || params.approval_type || params.agent_id || params.project_id)
          ? {
              ...(params.status ? { status: params.status } : {}),
              ...(params.approval_type ? { approval_type: params.approval_type } : {}),
              ...(params.agent_id ? { agent_id: params.agent_id } : {}),
              ...(params.project_id ? { project_id: params.project_id } : {}),
            }
          : undefined;
      return ['approvals', 'list', normalized] as const;
    },
    pending: (project_id?: string) =>
      ['approvals', 'pending', project_id ? { project_id } : undefined] as const,
    detail: (id: string) => ['approvals', 'detail', id] as const,
  },
};

// =============================================================================
// Smart Cache Invalidation Helpers
// =============================================================================

/**
 * Invalidate queries based on entity type.
 * Avoids over-invalidation by only targeting relevant query keys.
 */
function invalidateByEntityType(
  queryClient: ReturnType<typeof useQueryClient>,
  entityType: string | undefined,
  entityId?: string
) {
  // Always invalidate stats on create/delete
  queryClient.invalidateQueries({ queryKey: queryKeys.admin.stats });

  switch (entityType) {
    case 'task':
      queryClient.invalidateQueries({ queryKey: queryKeys.tasks.all });
      if (entityId) {
        queryClient.invalidateQueries({ queryKey: queryKeys.tasks.detail(entityId) });
      }
      break;

    case 'project':
      queryClient.invalidateQueries({ queryKey: queryKeys.projects.all });
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
    staleTime: TIMING.STALE_TIME,
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
    staleTime: TIMING.STALE_TIME,
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

export function useOrg(slug: string, options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: queryKeys.orgs.detail(slug),
    queryFn: () => api.orgs.get(slug),
    enabled: options?.enabled ?? !!slug,
    retry: false,
    staleTime: TIMING.STALE_TIME,
  });
}

export function useCreateOrg() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (data: import('./api').OrgCreateRequest) => api.orgs.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.orgs.list });
      queryClient.invalidateQueries({ queryKey: queryKeys.auth.me });
    },
  });
}

export function useUpdateOrg() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ slug, data }: { slug: string; data: import('./api').OrgUpdateRequest }) =>
      api.orgs.update(slug, data),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.orgs.list });
      queryClient.invalidateQueries({ queryKey: queryKeys.orgs.detail(variables.slug) });
    },
  });
}

export function useDeleteOrg() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (slug: string) => api.orgs.delete(slug),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.orgs.list });
      queryClient.invalidateQueries({ queryKey: queryKeys.auth.me });
    },
  });
}

export function useOrgMembers(slug: string, options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: queryKeys.orgs.members(slug),
    queryFn: () => api.orgs.members.list(slug),
    enabled: options?.enabled ?? !!slug,
    retry: false,
    staleTime: TIMING.STALE_TIME,
  });
}

export function useAddOrgMember() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ slug, userId, role }: { slug: string; userId: string; role: string }) =>
      api.orgs.members.add(slug, userId, role),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.orgs.members(variables.slug) });
    },
  });
}

export function useUpdateOrgMemberRole() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ slug, userId, role }: { slug: string; userId: string; role: string }) =>
      api.orgs.members.updateRole(slug, userId, role),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.orgs.members(variables.slug) });
    },
  });
}

export function useRemoveOrgMember() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ slug, userId }: { slug: string; userId: string }) =>
      api.orgs.members.remove(slug, userId),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.orgs.members(variables.slug) });
    },
  });
}

// =============================================================================
// Security Hooks (Sessions, API Keys, OAuth, Password)
// =============================================================================

export function useSessions() {
  return useQuery({
    queryKey: queryKeys.security.sessions,
    queryFn: () => api.security.sessions.list(),
  });
}

export function useRevokeSession() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (sessionId: string) => api.security.sessions.revoke(sessionId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.security.sessions });
    },
  });
}

export function useRevokeAllSessions() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: () => api.security.sessions.revokeAll(),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.security.sessions });
    },
  });
}

export function useApiKeys() {
  return useQuery({
    queryKey: queryKeys.security.apiKeys,
    queryFn: () => api.security.apiKeys.list(),
  });
}

export function useCreateApiKey() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (data: import('./api').ApiKeyCreateRequest) => api.security.apiKeys.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.security.apiKeys });
    },
  });
}

export function useRevokeApiKey() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (keyId: string) => api.security.apiKeys.revoke(keyId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.security.apiKeys });
    },
  });
}

export function useOAuthConnections() {
  return useQuery({
    queryKey: queryKeys.security.connections,
    queryFn: () => api.security.connections.list(),
  });
}

export function useRemoveOAuthConnection() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (connectionId: string) => api.security.connections.remove(connectionId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.security.connections });
    },
  });
}

export function useChangePassword() {
  return useMutation({
    mutationFn: (data: import('./api').PasswordChangeRequest) => api.security.changePassword(data),
  });
}

// =============================================================================
// Preferences Hooks
// =============================================================================

export function usePreferences() {
  return useQuery({
    queryKey: queryKeys.preferences,
    queryFn: () => api.preferences.get(),
  });
}

export function useUpdatePreferences() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (preferences: Partial<import('./api').UserPreferences>) =>
      api.preferences.update(preferences),
    onSuccess: data => {
      queryClient.setQueryData(queryKeys.preferences, data);
    },
  });
}

/**
 * Hook for tracking onboarding checklist progress.
 * Returns current state and methods to mark items complete.
 */
export function useOnboardingProgress() {
  const { data: prefsData, isLoading } = usePreferences();
  const updatePrefs = useUpdatePreferences();

  const checklist = prefsData?.preferences?.onboarding_checklist ?? {};

  const markComplete = (item: keyof import('./api').OnboardingChecklist) => {
    if (checklist[item]) return; // Already complete
    updatePrefs.mutate({
      onboarding_checklist: {
        ...checklist,
        [item]: true,
      },
    });
  };

  const isAllComplete =
    checklist.connected_claude && checklist.added_source && checklist.tried_search;

  return {
    checklist,
    isLoading,
    isAllComplete,
    markComplete,
    markConnectedClaude: () => markComplete('connected_claude'),
    markAddedSource: () => markComplete('added_source'),
    markTriedSearch: () => markComplete('tried_search'),
  };
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
    onSuccess: (data, variables) => {
      // Use entity type from response (most accurate) or input
      const entityType = data.entity_type || variables.entity_type;
      invalidateByEntityType(queryClient, entityType, data.id);
    },
  });
}

export function useUpdateEntity() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ id, updates }: { id: string; updates: EntityUpdate }) =>
      api.entities.update(id, updates),
    onSuccess: (data, { id }) => {
      // Use entity type from response
      invalidateByEntityType(queryClient, data.entity_type, id);
    },
  });
}

export function useDeleteEntity() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (id: string) => api.entities.delete(id),
    onSuccess: (_data, id) => {
      // Check cache for entity type before it's removed
      const cachedEntity = queryClient.getQueryData(queryKeys.entities.detail(id)) as
        | { entity_type?: string }
        | undefined;
      const entityType = cachedEntity?.entity_type;
      invalidateByEntityType(queryClient, entityType, id);
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

export function useClusters(params?: { refresh?: boolean }) {
  return useQuery({
    queryKey: queryKeys.graph.clusters(params),
    queryFn: () => api.graph.clusters(params),
  });
}

export function useClusterDetail(clusterId: string | null) {
  return useQuery({
    queryKey: queryKeys.graph.clusterDetail(clusterId || ''),
    queryFn: () => api.graph.clusterDetail(clusterId ?? ''),
    enabled: !!clusterId,
  });
}

export function useGraphStats() {
  return useQuery({
    queryKey: queryKeys.graph.stats,
    queryFn: () => api.graph.stats(),
  });
}

export function useHierarchicalGraph(params?: {
  max_nodes?: number;
  max_edges?: number;
  projects?: string[];
  types?: string[];
  refresh?: boolean;
}) {
  return useQuery({
    queryKey: queryKeys.graph.hierarchical(params),
    queryFn: () => api.graph.hierarchical(params),
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

// =============================================================================
// WebSocket Hook
// =============================================================================

export function useRealtimeUpdates(isAuthenticated = false) {
  const queryClient = useQueryClient();

  useEffect(() => {
    // Only connect when authenticated
    if (!isAuthenticated) {
      wsClient.disconnect();
      return;
    }

    wsClient.connect();

    // Entity created - smart invalidation based on entity type
    const unsubCreate = wsClient.on('entity_created', data => {
      const entityType = data.entity_type || data.type;
      invalidateByEntityType(queryClient, entityType, data.id);
    });

    // Entity updated - smart invalidation based on entity type
    const unsubUpdate = wsClient.on('entity_updated', data => {
      const entityType = data.entity_type || data.type;
      // Also invalidate related entities explorer
      queryClient.invalidateQueries({ queryKey: queryKeys.explore.related(data.id) });
      invalidateByEntityType(queryClient, entityType, data.id);
    });

    // Entity deleted - remove from cache + smart invalidation
    const unsubDelete = wsClient.on('entity_deleted', data => {
      const entityType = data.entity_type || data.type;
      // Remove from cache before invalidation
      queryClient.removeQueries({ queryKey: queryKeys.entities.detail(data.id) });
      queryClient.removeQueries({ queryKey: queryKeys.tasks.detail(data.id) });
      queryClient.removeQueries({ queryKey: queryKeys.projects.detail(data.id) });
      queryClient.removeQueries({ queryKey: queryKeys.sources.detail(data.id) });
      invalidateByEntityType(queryClient, entityType, data.id);
    });

    // Health update
    const unsubHealth = wsClient.on('health_update', () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.admin.health });
    });

    // Search complete (if backend sends it)
    const unsubSearch = wsClient.on('search_complete', () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.search.all });
    });

    // Permission changed - refresh auth data
    const unsubPermission = wsClient.on('permission_changed', () => {
      // Invalidate auth/me to refresh current user's permissions
      queryClient.invalidateQueries({ queryKey: queryKeys.auth.me });
      // Also invalidate org data in case role affects what's visible
      queryClient.invalidateQueries({ queryKey: queryKeys.orgs.list });
    });

    // Crawl started - refresh source to show crawling status
    const unsubCrawlStarted = wsClient.on('crawl_started', data => {
      queryClient.invalidateQueries({ queryKey: queryKeys.sources.detail(data.source_id) });
      queryClient.invalidateQueries({ queryKey: queryKeys.sources.all });
    });

    // Crawl progress - update in real-time with merged data
    const unsubCrawlProgress = wsClient.on('crawl_progress', data => {
      const { source_id, documents_stored } = data;

      // Merge new progress with existing (we get page-level and doc-level events)
      const existing = queryClient.getQueryData<CrawlProgressData>(['crawl_progress', source_id]);
      const merged: CrawlProgressData = {
        ...existing,
        source_id,
        source_name: data.source_name ?? existing?.source_name,
        pages_crawled: data.pages_crawled ?? existing?.pages_crawled ?? 0,
        max_pages: data.max_pages ?? existing?.max_pages ?? 0,
        current_url: data.current_url ?? existing?.current_url ?? '',
        percentage: data.percentage ?? existing?.percentage ?? 0,
        documents_crawled: data.documents_crawled ?? existing?.documents_crawled,
        documents_stored: documents_stored ?? existing?.documents_stored,
        chunks_created: data.chunks_created ?? existing?.chunks_created,
        chunks_added: data.chunks_added ?? existing?.chunks_added,
        errors: data.errors ?? existing?.errors,
      };
      queryClient.setQueryData(['crawl_progress', source_id], merged);

      // Also update source's document_count in cache for real-time display
      if (documents_stored !== undefined) {
        // Update source list cache
        queryClient.setQueryData(
          queryKeys.sources.list,
          (
            old: { entities: Array<{ id: string; metadata: Record<string, unknown> }> } | undefined
          ) => {
            if (!old?.entities) return old;
            return {
              ...old,
              entities: old.entities.map(s =>
                s.id === source_id
                  ? { ...s, metadata: { ...s.metadata, document_count: documents_stored } }
                  : s
              ),
            };
          }
        );

        // Also update source detail cache (for source detail page)
        queryClient.setQueryData(
          queryKeys.sources.detail(source_id),
          (old: { document_count?: number } | undefined) => {
            if (!old) return old;
            return { ...old, document_count: documents_stored };
          }
        );
      }
    });

    // Crawl complete - refresh source and documents
    const unsubCrawlComplete = wsClient.on('crawl_complete', data => {
      // Clear the progress data
      queryClient.removeQueries({ queryKey: ['crawl_progress', data.source_id] });
      // Refresh source detail and list
      queryClient.invalidateQueries({ queryKey: queryKeys.sources.detail(data.source_id) });
      queryClient.invalidateQueries({ queryKey: queryKeys.sources.all });
      // Refresh any documents/pages for this source
      queryClient.invalidateQueries({ queryKey: queryKeys.rag.pages(data.source_id) });
    });

    // Cleanup on unmount
    return () => {
      unsubCreate();
      unsubUpdate();
      unsubDelete();
      unsubHealth();
      unsubSearch();
      unsubPermission();
      unsubCrawlStarted();
      unsubCrawlProgress();
      unsubCrawlComplete();
      wsClient.disconnect();
    };
  }, [queryClient, isAuthenticated]);
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
      ? {
          ...(params.project ? { project: params.project } : {}),
          ...(params.status ? { status: params.status } : {}),
        }
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
      action:
        | 'start_task'
        | 'block_task'
        | 'unblock_task'
        | 'submit_review'
        | 'complete_task'
        | 'archive';
      entity_id: string;
      params?: {
        assignee?: string;
        blocker?: string;
        reason?: string;
        commit_shas?: string[];
        pr_url?: string;
        actual_hours?: number;
        learnings?: string;
      };
    }) => {
      // Route to RESTful endpoints based on action
      switch (action) {
        case 'start_task':
          return api.tasks.start(
            entity_id,
            params?.assignee ? { assignee: params.assignee } : undefined
          );
        case 'block_task':
          return api.tasks.block(entity_id, params?.blocker || params?.reason || 'Blocked');
        case 'unblock_task':
          return api.tasks.unblock(entity_id);
        case 'submit_review':
          return api.tasks.review(entity_id, {
            pr_url: params?.pr_url,
            commit_shas: params?.commit_shas,
          });
        case 'complete_task':
          return api.tasks.complete(entity_id, {
            actual_hours: params?.actual_hours,
            learnings: params?.learnings,
          });
        case 'archive':
          return api.tasks.archive(
            entity_id,
            params?.reason ? { reason: params.reason } : undefined
          );
        default:
          throw new Error(`Unknown action: ${action}`);
      }
    },
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
// Task Notes Hooks
// =============================================================================

export function useTaskNotes(taskId: string, options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: queryKeys.tasks.notes(taskId),
    queryFn: () => api.tasks.notes.list(taskId),
    enabled: (options?.enabled ?? true) && !!taskId,
  });
}

export function useAddTaskNote() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ taskId, data }: { taskId: string; data: CreateNoteRequest }) =>
      api.tasks.notes.create(taskId, data),
    onSuccess: (_data, { taskId }) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.tasks.notes(taskId) });
    },
  });
}

// =============================================================================
// Project Hooks
// =============================================================================

export function useProjects(
  options?: { includeArchived?: boolean },
  initialData?: import('./api').TaskListResponse
) {
  const includeArchived = options?.includeArchived ?? false;
  return useQuery({
    queryKey: queryKeys.projects.list(includeArchived),
    queryFn: () => api.projects.list({ includeArchived }),
    staleTime: TIMING.STALE_TIME,
    initialData,
  });
}

export function useProject(id: string) {
  return useQuery({
    queryKey: queryKeys.projects.detail(id),
    queryFn: () => api.projects.get(id),
    enabled: !!id,
    staleTime: TIMING.STALE_TIME,
  });
}

export function useProjectMembers(projectId: string, options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: queryKeys.projects.members(projectId),
    queryFn: () => api.projects.members.list(projectId),
    enabled: options?.enabled ?? !!projectId,
    retry: false,
    staleTime: TIMING.STALE_TIME,
  });
}

export function useAddProjectMember() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      projectId,
      userId,
      role,
    }: {
      projectId: string;
      userId: string;
      role: import('./api').ProjectRole;
    }) => api.projects.members.add(projectId, userId, role),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.projects.members(variables.projectId) });
    },
  });
}

export function useUpdateProjectMemberRole() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      projectId,
      userId,
      role,
    }: {
      projectId: string;
      userId: string;
      role: import('./api').ProjectRole;
    }) => api.projects.members.updateRole(projectId, userId, role),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.projects.members(variables.projectId) });
    },
  });
}

export function useRemoveProjectMember() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ projectId, userId }: { projectId: string; userId: string }) =>
      api.projects.members.remove(projectId, userId),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.projects.members(variables.projectId) });
    },
  });
}

// =============================================================================
// Epic Hooks
// =============================================================================

export function useEpics(params?: { project?: string; status?: EpicStatus }) {
  const normalized =
    params && (params.project || params.status)
      ? {
          ...(params.project ? { project: params.project } : {}),
          ...(params.status ? { status: params.status } : {}),
        }
      : undefined;

  return useQuery({
    queryKey: queryKeys.epics.list(normalized),
    queryFn: () => api.epics.list(normalized),
    staleTime: TIMING.STALE_TIME,
  });
}

export function useEpic(id: string) {
  return useQuery({
    queryKey: queryKeys.epics.detail(id),
    queryFn: () => api.epics.get(id),
    enabled: !!id,
    staleTime: TIMING.STALE_TIME,
  });
}

export function useEpicTasks(epicId: string) {
  return useQuery({
    queryKey: queryKeys.epics.tasks(epicId),
    queryFn: () => api.epics.tasks(epicId),
    enabled: !!epicId,
    staleTime: TIMING.STALE_TIME,
  });
}

export function useEpicManage() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      action,
      entity_id,
      params,
    }: {
      action: 'start_epic' | 'complete_epic' | 'archive_epic' | 'update_epic';
      entity_id: string;
      params?: {
        learnings?: string;
        reason?: string;
        status?: EpicStatus;
        priority?: TaskPriority;
        title?: string;
        description?: string;
        assignees?: string[];
        tags?: string[];
      };
    }) => {
      // Route to RESTful endpoints based on action
      switch (action) {
        case 'start_epic':
          return api.epics.start(entity_id);
        case 'complete_epic':
          return api.epics.complete(
            entity_id,
            params?.learnings ? { learnings: params.learnings } : undefined
          );
        case 'archive_epic':
          return api.epics.archive(
            entity_id,
            params?.reason ? { reason: params.reason } : undefined
          );
        case 'update_epic':
          return api.epics.update(entity_id, {
            status: params?.status,
            priority: params?.priority,
            title: params?.title,
            description: params?.description,
            assignees: params?.assignees,
            tags: params?.tags,
          });
        default:
          throw new Error(`Unknown action: ${action}`);
      }
    },
    onSuccess: () => {
      // Invalidate epics list and related queries
      queryClient.invalidateQueries({ queryKey: queryKeys.epics.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.tasks.all });
    },
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

export function useUpdateSource() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      id,
      updates,
    }: {
      id: string;
      updates: Parameters<typeof api.sources.update>[1];
    }) => api.sources.update(id, updates),
    onSuccess: (data, { id }) => {
      queryClient.setQueryData(queryKeys.sources.detail(id), data);
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
  source_name?: string;
  // Page-level progress (from CrawlerService)
  pages_crawled: number;
  max_pages: number;
  current_url: string;
  percentage: number;
  // Document-level stats (from Worker on_progress)
  documents_crawled?: number;
  documents_stored?: number;
  chunks_created?: number;
  chunks_added?: number;
  errors?: number;
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

// =============================================================================
// Metrics Hooks
// =============================================================================

/**
 * Fetch org-level metrics (aggregated across all projects).
 */
export function useOrgMetrics(initialData?: import('./api').OrgMetricsResponse) {
  return useQuery({
    queryKey: queryKeys.metrics.org,
    queryFn: api.metrics.org,
    initialData,
    staleTime: TIMING.STALE_TIME,
  });
}

/**
 * Fetch project-level metrics.
 */
export function useProjectMetrics(
  projectId: string,
  initialData?: import('./api').ProjectMetricsResponse
) {
  return useQuery({
    queryKey: queryKeys.metrics.project(projectId),
    queryFn: () => api.metrics.project(projectId),
    initialData,
    enabled: Boolean(projectId),
    staleTime: TIMING.STALE_TIME,
  });
}

// =============================================================================
// Agent Hooks
// =============================================================================

/**
 * Fetch agents with optional filtering.
 */
export function useAgents(params?: {
  project?: string;
  status?: AgentStatus;
  agent_type?: AgentType;
}) {
  const normalized =
    params && (params.project || params.status || params.agent_type)
      ? {
          ...(params.project ? { project: params.project } : {}),
          ...(params.status ? { status: params.status } : {}),
          ...(params.agent_type ? { agent_type: params.agent_type } : {}),
        }
      : undefined;

  const wsStatus = useConnectionStatus();
  const isWsConnected = wsStatus === 'connected';

  return useQuery({
    queryKey: queryKeys.agents.list(normalized),
    queryFn: () => api.agents.list(normalized),
    // Only poll when WebSocket is disconnected (WS handles real-time updates)
    refetchInterval: isWsConnected ? false : 30000,
  });
}

/**
 * Fetch a single agent by ID.
 * Includes polling fallback for when WebSocket is disconnected or agent status is stale.
 */
export function useAgent(id: string) {
  const wsStatus = useConnectionStatus();
  const isWsConnected = wsStatus === 'connected';

  return useQuery({
    queryKey: queryKeys.agents.detail(id),
    queryFn: () => api.agents.get(id),
    enabled: !!id,
    // Poll every 10s even when WS connected (catches dead agents after server restart)
    // Poll every 5s when WS disconnected for faster recovery
    refetchInterval: isWsConnected ? 10000 : 5000,
  });
}

/**
 * Spawn a new agent.
 */
export function useSpawnAgent() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (request: SpawnAgentRequest) => api.agents.spawn(request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.agents.all });
    },
  });
}

/**
 * Pause an agent.
 */
export function usePauseAgent() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ id, reason }: { id: string; reason?: string }) => api.agents.pause(id, reason),
    onSuccess: (_data, { id }) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.agents.detail(id) });
      queryClient.invalidateQueries({ queryKey: queryKeys.agents.all });
    },
  });
}

/**
 * Resume a paused agent.
 */
export function useResumeAgent() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (id: string) => api.agents.resume(id),
    onSuccess: (_data, id) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.agents.detail(id) });
      queryClient.invalidateQueries({ queryKey: queryKeys.agents.all });
    },
  });
}

/**
 * Terminate an agent.
 */
export function useTerminateAgent() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ id, reason }: { id: string; reason?: string }) =>
      api.agents.terminate(id, reason),
    onSuccess: (_data, { id }) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.agents.detail(id) });
      queryClient.invalidateQueries({ queryKey: queryKeys.agents.all });
    },
  });
}

/**
 * Rename an agent.
 */
export function useRenameAgent() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) => api.agents.rename(id, name),
    onSuccess: (_data, { id }) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.agents.detail(id) });
      queryClient.invalidateQueries({ queryKey: queryKeys.agents.all });
    },
  });
}

/**
 * Archive an agent (only terminal states).
 */
export function useArchiveAgent() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (id: string) => api.agents.archive(id),
    onSuccess: (_data, id) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.agents.detail(id) });
      queryClient.invalidateQueries({ queryKey: queryKeys.agents.all });
    },
  });
}

/**
 * Fetch messages for an agent.
 * Includes polling fallback when WebSocket is disconnected.
 */
export function useAgentMessages(id: string, limit?: number) {
  const wsStatus = useConnectionStatus();
  const isWsConnected = wsStatus === 'connected';

  return useQuery({
    queryKey: queryKeys.agents.messages(id, limit),
    queryFn: () => api.agents.getMessages(id, limit),
    enabled: !!id,
    // Poll when WS disconnected for faster recovery
    refetchInterval: isWsConnected ? false : 5000,
  });
}

/**
 * Send a message to an agent.
 */
export function useSendAgentMessage() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ id, content }: { id: string; content: string }) =>
      api.agents.sendMessage(id, content),
    onSuccess: (_data, { id }) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.agents.messages(id) });
    },
  });
}

/**
 * Fetch workspace state for an agent.
 * Note: No polling - use useAgentSubscription for real-time WebSocket updates.
 */
export function useAgentWorkspace(id: string) {
  return useQuery({
    queryKey: queryKeys.agents.workspace(id),
    queryFn: () => api.agents.getWorkspace(id),
    enabled: !!id,
  });
}

/**
 * Subscribe to real-time agent updates via WebSocket.
 * Automatically updates React Query cache when WebSocket events arrive.
 */
export function useAgentSubscription(agentId: string | undefined) {
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!agentId) return;

    let unsubStatus: (() => void) | undefined;
    let unsubMessage: (() => void) | undefined;
    let unsubWorkspace: (() => void) | undefined;

    // Subscribe to WebSocket agent events
    wsClient.connect();

    unsubStatus = wsClient.on('agent_status', data => {
      if (data.agent_id === agentId) {
        // Invalidate agent query to refetch latest status
        queryClient.invalidateQueries({ queryKey: queryKeys.agents.detail(agentId) });
        queryClient.invalidateQueries({ queryKey: queryKeys.agents.list() });
      }
    });

    unsubMessage = wsClient.on('agent_message', data => {
      if (data.agent_id === agentId) {
        // Invalidate all message queries for this agent (regardless of limit param)
        // This ensures any component viewing messages gets the update
        queryClient.invalidateQueries({
          queryKey: ['agents', 'messages', agentId],
        });
      }
    });

    unsubWorkspace = wsClient.on('agent_workspace', data => {
      if (data.agent_id === agentId) {
        // Invalidate workspace query to refetch
        queryClient.invalidateQueries({ queryKey: queryKeys.agents.workspace(agentId) });
      }
    });

    // Cleanup subscriptions
    return () => {
      unsubStatus?.();
      unsubMessage?.();
      unsubWorkspace?.();
    };
  }, [agentId, queryClient]);
}

/**
 * Subscribe to Tier 3 Haiku-generated status hints for an agent.
 * Returns a Map of tool_call_id → hint that updates in real-time.
 */
export function useStatusHints(agentId: string | undefined) {
  const [hints, setHints] = useState<Map<string, string>>(new Map());

  useEffect(() => {
    if (!agentId) return;

    wsClient.connect();

    const unsub = wsClient.on('status_hint', data => {
      if (data.agent_id === agentId && data.tool_call_id && data.hint) {
        setHints(prev => {
          const next = new Map(prev);
          next.set(data.tool_call_id, data.hint);
          return next;
        });
      }
    });

    return () => {
      unsub();
      setHints(new Map()); // Clear hints on unmount
    };
  }, [agentId]);

  return hints;
}

// =============================================================================
// Approval Hooks (Human-in-the-Loop)
// =============================================================================

/**
 * Fetch approvals with optional filtering.
 */
export function useApprovals(params?: {
  status?: ApprovalStatus;
  approval_type?: ApprovalType;
  agent_id?: string;
  project_id?: string;
}) {
  const normalized =
    params && (params.status || params.approval_type || params.agent_id || params.project_id)
      ? {
          ...(params.status ? { status: params.status } : {}),
          ...(params.approval_type ? { approval_type: params.approval_type } : {}),
          ...(params.agent_id ? { agent_id: params.agent_id } : {}),
          ...(params.project_id ? { project_id: params.project_id } : {}),
        }
      : undefined;

  const wsStatus = useConnectionStatus();
  const isWsConnected = wsStatus === 'connected';

  return useQuery({
    queryKey: queryKeys.approvals.list(normalized),
    queryFn: () => api.approvals.list(normalized),
    // Only poll when WebSocket is disconnected (WS handles real-time updates)
    refetchInterval: isWsConnected ? false : 10000,
  });
}

/**
 * Fetch pending approvals - convenience hook for the approval queue.
 */
export function usePendingApprovals(project_id?: string) {
  const wsStatus = useConnectionStatus();
  const isWsConnected = wsStatus === 'connected';

  return useQuery({
    queryKey: queryKeys.approvals.pending(project_id),
    queryFn: () => api.approvals.pending(project_id ? { project_id } : undefined),
    // Only poll when WebSocket is disconnected (WS handles real-time updates)
    refetchInterval: isWsConnected ? false : 5000,
  });
}

/**
 * Fetch a single approval by ID.
 */
export function useApproval(id: string) {
  return useQuery({
    queryKey: queryKeys.approvals.detail(id),
    queryFn: () => api.approvals.get(id),
    enabled: !!id,
  });
}

/**
 * Respond to an approval (approve, deny, or edit).
 */
export function useRespondToApproval() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ id, request }: { id: string; request: RespondToApprovalRequest }) =>
      api.approvals.respond(id, request),
    onSuccess: (_data, { id }) => {
      // Invalidate the specific approval and all lists
      queryClient.invalidateQueries({ queryKey: queryKeys.approvals.detail(id) });
      queryClient.invalidateQueries({ queryKey: queryKeys.approvals.all });
      // Also refresh agent data since approval response may unblock agent
      queryClient.invalidateQueries({ queryKey: queryKeys.agents.all });
    },
  });
}

/**
 * Dismiss/expire a stale approval (orphaned from dead agents).
 */
export function useDismissApproval() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (id: string) => api.approvals.dismiss(id),
    onSuccess: (_data, id) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.approvals.detail(id) });
      queryClient.invalidateQueries({ queryKey: queryKeys.approvals.all });
    },
  });
}

/**
 * Answer an agent's question (from AskUserQuestion tool).
 */
export function useAnswerQuestion() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ id, request }: { id: string; request: { answers: Record<string, string> } }) =>
      api.approvals.answerQuestion(id, request),
    onSuccess: () => {
      // Refresh agent data since answer may unblock agent
      queryClient.invalidateQueries({ queryKey: queryKeys.agents.all });
    },
  });
}

/**
 * Subscribe to real-time approval updates via WebSocket.
 * Automatically updates React Query cache when approval responses arrive.
 */
export function useApprovalSubscription() {
  const queryClient = useQueryClient();

  useEffect(() => {
    // Subscribe to WebSocket approval events
    wsClient.connect();

    const unsubResponse = wsClient.on('approval_response', data => {
      // Invalidate approval queries to refetch
      queryClient.invalidateQueries({ queryKey: queryKeys.approvals.detail(data.approval_id) });
      queryClient.invalidateQueries({ queryKey: queryKeys.approvals.all });
      // Agent may be unblocked, refresh agent data
      if (data.agent_id) {
        queryClient.invalidateQueries({ queryKey: queryKeys.agents.detail(data.agent_id) });
      }
    });

    // Cleanup subscription
    return () => {
      unsubResponse();
    };
  }, [queryClient]);
}

// =============================================================================
// Activity Feed Hooks
// =============================================================================

/**
 * Fetch cross-agent activity feed.
 */
export function useActivityFeed(project_id?: string) {
  const wsStatus = useConnectionStatus();
  const isWsConnected = wsStatus === 'connected';

  return useQuery({
    queryKey: queryKeys.agents.activityFeed(project_id),
    queryFn: () => api.agents.getActivityFeed(project_id ? { project_id } : undefined),
    // Only poll when WebSocket is disconnected (WS handles real-time updates)
    refetchInterval: isWsConnected ? false : 10000,
  });
}

/**
 * Fetch agent health overview with automatic polling.
 */
export function useHealthOverview(project_id?: string) {
  const wsStatus = useConnectionStatus();
  const isWsConnected = wsStatus === 'connected';

  return useQuery({
    queryKey: queryKeys.agents.healthOverview(project_id),
    queryFn: () => api.agents.getHealthOverview(project_id ? { project_id } : undefined),
    // Only poll when WebSocket is disconnected (WS handles real-time updates)
    refetchInterval: isWsConnected ? false : 15000,
  });
}

// =============================================================================
// Setup Wizard Hooks
// =============================================================================

/**
 * Check setup status - whether this is a fresh install needing configuration.
 * This is a public endpoint that works before any users exist.
 */
export function useSetupStatus(options?: { validateKeys?: boolean; enabled?: boolean }) {
  return useQuery({
    queryKey: queryKeys.setup.status,
    queryFn: () => api.setup.status(options?.validateKeys),
    enabled: options?.enabled ?? true,
    staleTime: 30000, // Cache for 30 seconds
    retry: false, // Don't retry on failure (server might be down)
  });
}

/**
 * Validate API keys are configured and working.
 */
export function useValidateApiKeys(options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: queryKeys.setup.validation,
    queryFn: () => api.setup.validateKeys(),
    enabled: options?.enabled ?? true,
    staleTime: 60000, // Cache for 1 minute
    retry: 1, // One retry on timeout
  });
}

/**
 * Get the Claude Code MCP command for this server.
 */
export function useMcpCommand(options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: queryKeys.setup.mcpCommand,
    queryFn: () => api.setup.mcpCommand(),
    enabled: options?.enabled ?? true,
    staleTime: Infinity, // Never stale (URL doesn't change)
  });
}

// =============================================================================
// Settings Hooks
// =============================================================================

/**
 * Get system settings (API key configuration status).
 */
export function useSettings(options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: queryKeys.settings.all,
    queryFn: () => api.settings.get(),
    enabled: options?.enabled ?? true,
    staleTime: 30000, // Cache for 30 seconds
  });
}

/**
 * Update system settings (save API keys to database).
 */
export function useUpdateSettings() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (request: UpdateSettingsRequest) => api.settings.update(request),
    onSuccess: () => {
      // Invalidate settings and setup status queries
      queryClient.invalidateQueries({ queryKey: queryKeys.settings.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.setup.status });
      queryClient.invalidateQueries({ queryKey: queryKeys.setup.validation });
    },
  });
}

/**
 * Delete a system setting from the database.
 */
export function useDeleteSetting() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (key: string) => api.settings.delete(key),
    onSuccess: () => {
      // Invalidate settings and setup status queries
      queryClient.invalidateQueries({ queryKey: queryKeys.settings.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.setup.status });
      queryClient.invalidateQueries({ queryKey: queryKeys.setup.validation });
    },
  });
}
