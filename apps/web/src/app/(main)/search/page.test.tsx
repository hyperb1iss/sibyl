import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@/test/utils';

const apiServer = vi.hoisted(() => ({
  fetchSearchResults: vi.fn(),
  fetchStats: vi.fn(),
}));

const searchContent = vi.hoisted(() => vi.fn(() => <div data-testid="search-content" />));

vi.mock('@/lib/api-server', () => apiServer);
vi.mock('./search-content', () => ({
  SearchContent: searchContent,
}));

import SearchPage from './page';

describe('SearchPage', () => {
  it('keeps search results when stats fail', async () => {
    const results = {
      results: [
        {
          id: 'pattern-1',
          name: 'Retry pattern',
          type: 'pattern',
          description: 'Use bounded retries',
          score: 0.9,
          metadata: {},
        },
      ],
      total: 1,
      query: 'retry',
    };

    apiServer.fetchSearchResults.mockResolvedValue(results);
    apiServer.fetchStats.mockRejectedValue(new Error('backend down'));

    render(
      await SearchPage({
        searchParams: Promise.resolve({ q: 'retry' }),
      })
    );

    expect(screen.getByTestId('search-content')).toBeInTheDocument();
    expect(apiServer.fetchSearchResults).toHaveBeenCalledWith({
      query: 'retry',
      limit: 50,
      include_documents: true,
      include_graph: true,
      include_raw_memory: true,
    });
    expect(searchContent).toHaveBeenCalledWith(
      expect.objectContaining({
        initialQuery: 'retry',
        initialResults: results,
        initialStats: undefined,
      }),
      undefined
    );
  });
});
