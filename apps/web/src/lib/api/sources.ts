import type { BaseMetadata } from './shared';
import { fetchApi } from './transport';

export interface SourceMetadata extends BaseMetadata {
  crawl_status?: CrawlStatus;
  source_type?: SourceType;
  document_count?: number;
  total_tokens?: number;
  last_crawled?: string;
  url?: string;
  tags?: string[];
  crawl_error?: string;
  max_pages?: number;
  max_depth?: number;
}

export type CrawlStatus = 'pending' | 'in_progress' | 'completed' | 'failed' | 'partial';

/** Type for source types */
export type SourceType = 'website' | 'github' | 'local' | 'api_docs';

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

export const sourcesApi = {
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

  // Re-crawl an existing source to pick up changes
  sync: (id: string) =>
    fetchApi<{ source_id: string; status: string; message: string }>(`/sources/${id}/sync`, {
      method: 'POST',
    }),

  // Cancel an in-flight crawl
  cancelCrawl: (id: string) =>
    fetchApi<{ source_id: string; status: string; message: string }>(`/sources/${id}/cancel`, {
      method: 'POST',
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
};
