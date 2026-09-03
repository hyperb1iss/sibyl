/**
 * Resolve where a unified search result should link.
 *
 * Documents open in the source document viewer, raw memory opens the capture
 * review, work items open their dedicated pages, and everything else lands on
 * the generic entity detail view.
 */
export interface LinkableSearchResult {
  id: string;
  type: string;
  result_origin?: 'graph' | 'document' | 'raw_memory';
  metadata?: Record<string, unknown> | null;
}

export function getSearchResultHref(result: LinkableSearchResult): string {
  const documentId = result.metadata?.document_id as string | undefined;
  const sourceId = result.metadata?.source_id as string | undefined;

  if (result.result_origin === 'document' && sourceId && documentId) {
    return `/sources/${sourceId}/documents/${documentId}`;
  }

  if (result.result_origin === 'raw_memory') {
    const rawMemoryId = result.id.replace(/^raw_memory:/, '');
    return `/memory/captures?id=${encodeURIComponent(rawMemoryId)}`;
  }

  if (result.type === 'task') {
    return `/tasks/${result.id}`;
  }

  if (result.type === 'epic') {
    return `/epics/${result.id}`;
  }

  return `/entities/${result.id}`;
}
