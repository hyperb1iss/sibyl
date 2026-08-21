import type { MemoryScope } from './memory';
import type { BaseMetadata } from './shared';
import { fetchApi } from './transport';

export interface SearchResultMetadata extends BaseMetadata {
  document_id?: string;
  source_id?: string;
  chunk_index?: number;
  section_path?: string;
}

export type EntitySortField = 'name' | 'created_at' | 'updated_at' | 'entity_type';
export type SortOrder = 'asc' | 'desc';

export interface SearchResult {
  id: string;
  type: string;
  name: string;
  content: string;
  score: number;
  source: string | null;
  url: string | null;
  result_origin: 'graph' | 'document' | 'raw_memory';
  metadata: Record<string, unknown>;
}

export interface SearchResponse {
  results: SearchResult[];
  total: number;
  query: string;
  filters: Record<string, unknown>;
  graph_count?: number;
  document_count?: number;
  raw_memory_count?: number;
  limit?: number;
  offset?: number;
  has_more?: boolean;
  actual_total?: number;
}

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
  snippet: string | null;
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
  source_id: string;
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

export const searchApi = {
  query: (params: {
    query: string;
    types?: string[];
    language?: string;
    category?: string;
    status?: string;
    project?: string;
    source?: string;
    source_id?: string;
    source_name?: string;
    assignee?: string;
    since?: string;
    as_of?: string;
    limit?: number;
    include_content?: boolean;
    include_documents?: boolean;
    include_graph?: boolean;
    include_raw_memory?: boolean;
    memory_scope?: MemoryScope;
    scope_key?: string;
    participants?: string[];
    labels?: string[];
    thread_id?: string;
    occurred_after?: string;
    occurred_before?: string;
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
};

export const ragApi = {
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
};
