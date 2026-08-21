'use client';

import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';

import { type ConnectionStatus, wsClient } from '../websocket';
import { queryKeys } from './query-keys';
import { invalidateByEntityType } from './shared';
import type { CrawlProgressData } from './sources';

export function useWebSocketStatus(enabled = true): ConnectionStatus {
  const [status, setStatus] = useState<ConnectionStatus>(wsClient.status);

  useEffect(() => {
    if (!enabled) {
      return;
    }

    setStatus(wsClient.status);
    return wsClient.on('connection_status', data => {
      setStatus(data.status);
    });
  }, [enabled]);

  return status;
}

export function useRealtimeUpdates(isAuthenticated?: boolean) {
  const queryClient = useQueryClient();

  useEffect(() => {
    if (isAuthenticated === undefined) {
      return;
    }

    // Only connect when authenticated
    if (!isAuthenticated) {
      wsClient.disconnect();
      return;
    }

    wsClient.connect();

    // Entity created - smart invalidation based on entity type
    const unsubCreate = wsClient.on('entity_created', data => {
      const entityType = data.entity_type || data.type;
      invalidateByEntityType(queryClient, entityType, data.id, { includeStats: true });
    });

    const unsubPending = wsClient.on('entity_pending', data => {
      const entityType = data.entity_type || data.type;
      invalidateByEntityType(queryClient, entityType, data.id, { includeStats: true });
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
      invalidateByEntityType(queryClient, entityType, data.id, { includeStats: true });
    });

    // Health update
    const unsubHealth = wsClient.on('health_update', () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.admin.health });
    });

    // Search complete (if backend sends it)
    const unsubSearch = wsClient.on('search_complete', () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.search.all });
    });

    const unsubGraphUpdated = wsClient.on('graph_updated', data => {
      queryClient.invalidateQueries({ queryKey: queryKeys.graph.all });
      if ((data.new_entities_created ?? 0) > 0) {
        queryClient.invalidateQueries({ queryKey: queryKeys.entities.all });
        queryClient.invalidateQueries({ queryKey: queryKeys.admin.stats });
      }
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

    const unsubCrawlSyncComplete = wsClient.on('crawl_sync_complete', data => {
      queryClient.invalidateQueries({ queryKey: queryKeys.sources.detail(data.source_id) });
      queryClient.invalidateQueries({ queryKey: queryKeys.sources.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.rag.pages(data.source_id) });
    });

    const refreshBackupQueries = (backupId: string, jobId?: string) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.backups.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.jobs.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.backups.detail(backupId) });
      if (jobId) {
        queryClient.invalidateQueries({ queryKey: queryKeys.backups.jobStatus(jobId) });
      }
    };

    const unsubBackupStarted = wsClient.on('backup_started', data => {
      refreshBackupQueries(data.backup_id, data.job_id);
    });

    const unsubBackupComplete = wsClient.on('backup_complete', data => {
      refreshBackupQueries(data.backup_id, data.job_id);
    });

    const unsubBackupFailed = wsClient.on('backup_failed', data => {
      refreshBackupQueries(data.backup_id, data.job_id);
    });

    const refreshTaskNotes = (taskId: string) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.tasks.notes(taskId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.tasks.detail(taskId) });
    };

    const unsubNotePending = wsClient.on('note_pending', data => {
      refreshTaskNotes(data.task_id);
    });

    const unsubNoteCreated = wsClient.on('note_created', data => {
      refreshTaskNotes(data.task_id);
    });

    const unsubSourceImportUpdated = wsClient.on('source_import_updated', data => {
      queryClient.setQueryData(queryKeys.memory.sourceImport(data.import_id), data);
      if (['paused', 'completed', 'failed', 'canceled'].includes(data.status)) {
        queryClient.invalidateQueries({ queryKey: queryKeys.memory.all });
        queryClient.invalidateQueries({ queryKey: queryKeys.rawCaptures.all });
      }
    });

    const unsubRawCaptureChanged = wsClient.on('raw_capture_changed', data => {
      queryClient.invalidateQueries({ queryKey: queryKeys.rawCaptures.all });
      for (const rawMemoryId of data.raw_memory_ids) {
        queryClient.invalidateQueries({ queryKey: queryKeys.rawCaptures.detail(rawMemoryId) });
      }
    });

    // Cleanup on unmount
    return () => {
      unsubCreate();
      unsubPending();
      unsubUpdate();
      unsubDelete();
      unsubHealth();
      unsubSearch();
      unsubGraphUpdated();
      unsubPermission();
      unsubCrawlStarted();
      unsubCrawlProgress();
      unsubCrawlComplete();
      unsubCrawlSyncComplete();
      unsubBackupStarted();
      unsubBackupComplete();
      unsubBackupFailed();
      unsubNotePending();
      unsubNoteCreated();
      unsubSourceImportUpdated();
      unsubRawCaptureChanged();
      wsClient.disconnect();
    };
  }, [queryClient, isAuthenticated]);
}
