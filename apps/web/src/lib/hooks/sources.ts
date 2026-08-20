'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';

import { sourcesApi } from '../api/sources';
import { queryKeys } from './query-keys';

export function useSources() {
  return useQuery({
    queryKey: queryKeys.sources.list,
    queryFn: () => sourcesApi.list(),
  });
}

export function useSource(id: string) {
  return useQuery({
    queryKey: queryKeys.sources.detail(id),
    queryFn: () => sourcesApi.get(id),
    enabled: !!id,
  });
}

export function useCreateSource() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (source: Parameters<typeof sourcesApi.create>[0]) => sourcesApi.create(source),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.sources.all });
    },
  });
}

export function useDeleteSource() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (id: string) => sourcesApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.sources.all });
    },
  });
}

export function useCrawlSource() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (id: string) => sourcesApi.crawl(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.sources.all });
    },
  });
}

export function useSyncSource() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (id: string) => sourcesApi.sync(id),
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
      updates: Parameters<typeof sourcesApi.update>[1];
    }) => sourcesApi.update(id, updates),
    onSuccess: (data, { id }) => {
      queryClient.setQueryData(queryKeys.sources.detail(id), data);
      queryClient.invalidateQueries({ queryKey: queryKeys.sources.all });
    },
  });
}

export function useCancelCrawl() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (id: string) => sourcesApi.cancelCrawl(id),
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
