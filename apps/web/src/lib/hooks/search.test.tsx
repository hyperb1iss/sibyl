import { keepPreviousData, QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useSearch } from './search';

const api = vi.hoisted(() => ({
  query: vi.fn(),
}));

vi.mock('../api/search', () => ({
  searchApi: { query: api.query },
  ragApi: {},
}));

function response(query: string) {
  return {
    results: [
      {
        id: `${query}_1`,
        type: 'task',
        name: query,
        content: '',
        score: 1,
        source: null,
        url: null,
        result_origin: 'graph',
        metadata: {},
      },
    ],
    total: 1,
    query,
    filters: {},
  };
}

// Mirrors the QueryClient defaults in components/providers.tsx, where
// placeholderData: keepPreviousData applies to every query.
function createWrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, placeholderData: keepPreviousData } },
  });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

describe('useSearch', () => {
  beforeEach(() => {
    api.query.mockReset();
    // "beta" never resolves so the in-flight state can be observed.
    api.query.mockImplementation(({ query }: { query: string }) =>
      query === 'beta' ? new Promise(() => undefined) : Promise.resolve(response(query))
    );
  });

  it('keeps the previous result set on screen while a new query loads by default', async () => {
    const { result, rerender } = renderHook(({ query }) => useSearch({ query }), {
      initialProps: { query: 'alpha' },
      wrapper: createWrapper(),
    });
    await waitFor(() => expect(result.current.data?.query).toBe('alpha'));

    rerender({ query: 'beta' });
    await waitFor(() => expect(result.current.isFetching).toBe(true));

    expect(result.current.data?.query).toBe('alpha');
    expect(result.current.isPlaceholderData).toBe(true);
  });

  it('clears results between queries when keepPreviousResults is false', async () => {
    const { result, rerender } = renderHook(
      ({ query }) => useSearch({ query }, { keepPreviousResults: false }),
      { initialProps: { query: 'alpha' }, wrapper: createWrapper() }
    );
    await waitFor(() => expect(result.current.data?.query).toBe('alpha'));

    rerender({ query: 'beta' });
    await waitFor(() => expect(result.current.isFetching).toBe(true));

    expect(result.current.data).toBeUndefined();
    expect(result.current.isPlaceholderData).toBe(false);
  });
});
