import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@/test/utils';
import { SearchContent } from './search-content';

const hooks = vi.hoisted(() => ({
  useSearch: vi.fn(() => ({ data: undefined, isLoading: false, error: null })),
}));

vi.mock('@/lib/hooks', () => ({
  useCodeExamples: () => ({ data: undefined, isLoading: false, error: null }),
  useRAGHybridSearch: () => ({ data: undefined, isLoading: false, error: null }),
  useSearch: hooks.useSearch,
  useSources: () => ({ data: { entities: [] } }),
  useStats: () => ({
    data: {
      entity_counts: {
        pattern: 3,
        procedure: 2,
        rule: 1,
        template: 1,
        task: 4,
        episode: 5,
        topic: 1,
      },
    },
  }),
}));

describe('SearchContent', () => {
  it('keeps document search out of knowledge type filters', () => {
    render(<SearchContent initialQuery="" />);

    expect(screen.getByRole('tab', { name: /docs/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /document/i })).not.toBeInTheDocument();
  });

  it('uses graph-only search for knowledge mode', () => {
    render(<SearchContent initialQuery="surreal" />);

    expect(hooks.useSearch).toHaveBeenCalledWith(
      expect.objectContaining({
        query: 'surreal',
        include_documents: false,
        include_graph: true,
      }),
      expect.any(Object)
    );
  });
});
