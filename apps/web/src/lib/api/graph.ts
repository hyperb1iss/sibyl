import type { BaseMetadata } from './shared';
import { fetchApi } from './transport';

export interface GraphNodeMetadata extends BaseMetadata {
  entity_type?: string;
  [key: string]: unknown; // Allow additional fields
}

export interface RelatedEntitySummary {
  id: string;
  name: string;
  entity_type: string;
  relationship: string;
  direction: 'outgoing' | 'incoming';
}

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
  related?: RelatedEntitySummary[] | null;
}

export interface EntityGetParams {
  include_summary?: boolean;
  related_limit?: number;
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

export type GraphResolution = 'overview' | 'detail';

export interface HierarchicalNode {
  id: string;
  name: string;
  type: string;
  label: string;
  color: string;
  summary: string;
  cluster_id: string;
  aggregate?: boolean;
  member_count?: number;
}

export interface HierarchicalEdge {
  source: string;
  target: string;
  type: string;
}

export interface HierarchicalCluster {
  id: string;
  label?: string;
  member_count: number;
  displayed_member_count?: number;
  level: number;
  type_distribution: Record<string, number>;
  displayed_type_distribution?: Record<string, number>;
  dominant_type: string;
  displayed_dominant_type?: string;
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
  resolution?: GraphResolution;
  recommended_resolution?: GraphResolution;
  /**
   * The domain map built from the same community run, present on a
   * whole-graph detail response. Semantic zoom composes both levels from
   * this one payload so their cluster ids always agree.
   */
  overview?: {
    nodes: HierarchicalNode[];
    edges: HierarchicalEdge[];
    clusters: HierarchicalCluster[];
  } | null;
}

export const entitiesApi = {
  list: (params?: {
    entity_type?: string;
    language?: string;
    category?: string;
    search?: string;
    project_ids?: string[];
    page?: number;
    page_size?: number;
    sort_by?: 'name' | 'created_at' | 'updated_at' | 'entity_type';
    sort_order?: 'asc' | 'desc';
  }) => {
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
    return fetchApi<EntityListResponse>(`/entities${query ? `?${query}` : ''}`);
  },

  get: (id: string, params?: EntityGetParams) => {
    const searchParams = new URLSearchParams();
    if (params?.include_summary === false) searchParams.set('include_summary', 'false');
    if (params?.related_limit !== undefined) {
      searchParams.set('related_limit', params.related_limit.toString());
    }
    const query = searchParams.toString();
    return fetchApi<Entity>(`/entities/${id}${query ? `?${query}` : ''}`);
  },

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
};

export const graphApi = {
  // Hierarchical graph with cluster assignments for rich visualization
  hierarchical: (params?: {
    max_nodes?: number;
    max_edges?: number;
    projects?: string[];
    types?: string[];
    refresh?: boolean;
    resolution?: GraphResolution;
    cluster_id?: string;
  }) => {
    const searchParams = new URLSearchParams();
    if (params?.max_nodes) searchParams.set('max_nodes', params.max_nodes.toString());
    if (params?.max_edges) searchParams.set('max_edges', params.max_edges.toString());
    if (params?.projects?.length) {
      for (const p of params.projects) searchParams.append('projects', p);
    }
    if (params?.types?.length) {
      for (const t of params.types) searchParams.append('types', t);
    }
    if (params?.refresh) searchParams.set('refresh', 'true');
    if (params?.resolution) searchParams.set('resolution', params.resolution);
    if (params?.cluster_id) searchParams.set('cluster_id', params.cluster_id);
    const query = searchParams.toString();
    return fetchApi<HierarchicalGraphResponse>(`/graph/hierarchical${query ? `?${query}` : ''}`);
  },
};
