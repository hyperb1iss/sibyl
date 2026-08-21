'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import type {
  CodeExampleParams,
  CodeExampleResponse,
  RAGSearchParams,
  RAGSearchResponse,
  SearchResponse,
} from '../api/search';
import { ragApi, searchApi } from '../api/search';
import { queryKeys } from './query-keys';

export function useSearch(
  params: Parameters<typeof searchApi.query>[0],
  options?: { enabled?: boolean; initialData?: SearchResponse }
) {
  return useQuery({
    queryKey: queryKeys.search.query(params),
    queryFn: () => searchApi.query(params),
    enabled: (options?.enabled ?? true) && !!params.query,
    initialData: options?.initialData,
  });
}

// =============================================================================
// RAG Search Hooks (Documentation Search)
// =============================================================================

/**
 * Hybrid search combining vector similarity and full-text search.
 */
export function useRAGHybridSearch(
  params: RAGSearchParams,
  options?: { enabled?: boolean; initialData?: RAGSearchResponse }
) {
  return useQuery({
    queryKey: queryKeys.rag.hybrid(params),
    queryFn: () => ragApi.hybridSearch(params),
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
    queryFn: () => ragApi.codeExamples(params),
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
    queryFn: () => ragApi.getPage(documentId),
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
    queryFn: () => ragApi.listPages(sourceId, params),
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
    }) => ragApi.updateDocument(documentId, updates),
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
    queryFn: () => ragApi.getDocumentEntities(documentId),
    enabled: (options?.enabled ?? true) && !!documentId,
  });
}
