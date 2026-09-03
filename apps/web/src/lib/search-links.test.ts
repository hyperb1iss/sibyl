import { describe, expect, it } from 'vitest';

import { getSearchResultHref } from './search-links';

describe('getSearchResultHref', () => {
  it('routes documents to the source document viewer', () => {
    expect(
      getSearchResultHref({
        id: 'doc_1',
        type: 'document',
        result_origin: 'document',
        metadata: { source_id: 'src_9', document_id: 'doc_1' },
      })
    ).toBe('/sources/src_9/documents/doc_1');
  });

  it('routes raw memory to the capture review and strips the table prefix', () => {
    expect(
      getSearchResultHref({ id: 'raw_memory:abc def', type: 'note', result_origin: 'raw_memory' })
    ).toBe('/memory/captures?id=abc%20def');
  });

  it('routes work items to their dedicated pages', () => {
    expect(getSearchResultHref({ id: 'task_1', type: 'task', result_origin: 'graph' })).toBe(
      '/tasks/task_1'
    );
    expect(getSearchResultHref({ id: 'epic_1', type: 'epic', result_origin: 'graph' })).toBe(
      '/epics/epic_1'
    );
  });

  it('falls back to entity detail for graph knowledge', () => {
    expect(getSearchResultHref({ id: 'pattern_1', type: 'pattern', result_origin: 'graph' })).toBe(
      '/entities/pattern_1'
    );
    expect(
      getSearchResultHref({
        id: 'doc_2',
        type: 'document',
        result_origin: 'document',
        metadata: {},
      })
    ).toBe('/entities/doc_2');
  });
});
