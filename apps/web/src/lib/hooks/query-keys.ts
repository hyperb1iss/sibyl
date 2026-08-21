import type { AIModelKind, adminApi, jobsApi, telemetryApi } from '../api/admin';
import type { entitiesApi } from '../api/graph';
import type { memoryApi, rawCapturesApi, sessionApi } from '../api/memory';
import type { CodeExampleParams, RAGSearchParams, searchApi } from '../api/search';
import type { EpicStatus, TaskStatus } from '../api/work-items';

export const queryKeys = {
  auth: {
    me: ['auth', 'me'] as const,
    providers: ['auth', 'providers'] as const,
  },
  orgs: {
    list: ['orgs', 'list'] as const,
    detail: (slug: string) => ['orgs', 'detail', slug] as const,
    members: (slug: string) => ['orgs', 'members', slug] as const,
    invitations: (slug: string) => ['orgs', 'invitations', slug] as const,
  },
  security: {
    sessions: ['security', 'sessions'] as const,
    apiKeys: ['security', 'apiKeys'] as const,
  },
  preferences: ['preferences'] as const,
  entities: {
    all: ['entities'] as const,
    list: (params?: Parameters<typeof entitiesApi.list>[0]) =>
      ['entities', 'list', params] as const,
    detail: (id: string, params?: Parameters<typeof entitiesApi.get>[1]) =>
      ['entities', 'detail', id, params] as const,
  },
  rawCaptures: {
    all: ['raw-captures'] as const,
    list: (params?: Parameters<typeof rawCapturesApi.list>[0]) =>
      ['raw-captures', 'list', params] as const,
    detail: (id: string) => ['raw-captures', 'detail', id] as const,
  },
  memory: {
    all: ['memory'] as const,
    audit: (params?: Parameters<typeof memoryApi.audit.list>[0]) =>
      ['memory', 'audit', params] as const,
    spaces: ['memory', 'spaces'] as const,
    sourceAdapters: ['memory', 'source-adapters'] as const,
    sourceImport: (importId: string) => ['memory', 'source-import', importId] as const,
    sourceInspect: (sourceId: string) => ['memory', 'source-inspect', sourceId] as const,
  },
  session: {
    all: ['session'] as const,
    bundle: (params?: Parameters<typeof sessionApi.bundle>[0]) =>
      ['session', 'bundle', params] as const,
  },
  search: {
    all: ['search'] as const,
    query: (params: Parameters<typeof searchApi.query>[0]) => ['search', 'query', params] as const,
  },
  rag: {
    all: ['rag'] as const,
    hybrid: (params: RAGSearchParams) => ['rag', 'hybrid', params] as const,
    code: (params: CodeExampleParams) => ['rag', 'code', params] as const,
    page: (documentId: string) => ['rag', 'page', documentId] as const,
    pageEntities: (documentId: string) => ['rag', 'page', documentId, 'entities'] as const,
    pages: (sourceId: string, params?: Record<string, unknown>) =>
      ['rag', 'pages', sourceId, params] as const,
  },
  graph: {
    all: ['graph'] as const,
    hierarchical: (params?: { max_nodes?: number; max_edges?: number; refresh?: boolean }) =>
      ['graph', 'hierarchical', params] as const,
  },
  admin: {
    health: ['admin', 'health'] as const,
    stats: ['admin', 'stats'] as const,
    audit: (params?: Parameters<typeof adminApi.audit.list>[0]) =>
      ['admin', 'audit', params] as const,
  },
  telemetry: {
    summary: (params?: Parameters<typeof telemetryApi.summary>[0]) =>
      ['telemetry', 'summary', params] as const,
  },
  setup: {
    status: ['setup', 'status'] as const,
    validation: ['setup', 'validation'] as const,
    integration: ['setup', 'integration'] as const,
  },
  settings: {
    all: ['settings'] as const,
    llm: ['settings', 'ai', 'llm'] as const,
    registry: (kind?: AIModelKind) => ['settings', 'ai', 'registry', kind ?? 'all'] as const,
  },
  tasks: {
    all: ['tasks'] as const,
    list: (params?: { project?: string; project_ids?: string[]; status?: TaskStatus }) => {
      const normalized =
        params && (params.project || params.project_ids?.length || params.status)
          ? {
              ...(params.project ? { project: params.project } : {}),
              ...(params.project_ids?.length ? { project_ids: [...params.project_ids] } : {}),
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
    list: (params?: { project?: string; project_ids?: string[]; status?: EpicStatus }) => {
      const normalized =
        params && (params.project || params.project_ids?.length || params.status)
          ? {
              ...(params.project ? { project: params.project } : {}),
              ...(params.project_ids?.length ? { project_ids: [...params.project_ids] } : {}),
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
    projectsSummary: ['metrics', 'projects-summary'] as const,
    project: (id: string) => ['metrics', 'project', id] as const,
  },
  backups: {
    all: ['backups'] as const,
    settings: ['backups', 'settings'] as const,
    list: ['backups', 'list'] as const,
    detail: (id: string) => ['backups', 'detail', id] as const,
    jobStatus: (jobId: string) => ['backups', 'job', jobId] as const,
  },
  jobs: {
    all: ['jobs'] as const,
    list: (params?: Parameters<typeof jobsApi.list>[0]) => ['jobs', 'list', params] as const,
  },
};
