import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@/test/utils';
import { SearchContent } from './search-content';

const hooks = vi.hoisted(() => ({
  projectContext: { selectedProjects: ['a'], isAll: false, scopeReady: true },
  useSearch: vi.fn((_params: Record<string, unknown>, _options?: Record<string, unknown>) => ({
    data: undefined,
    isLoading: false,
    error: null,
  })),
}));

vi.mock('@/lib/project-context', async importOriginal => ({
  ...(await importOriginal<typeof import('@/lib/project-context')>()),
  useProjectContext: () => hooks.projectContext,
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
  beforeEach(() => {
    hooks.useSearch.mockClear();
    hooks.projectContext = { selectedProjects: ['a'], isAll: false, scopeReady: true };
  });

  it('scopes every memory search lane to all selected projects', () => {
    hooks.projectContext.selectedProjects = ['a', 'b'];
    render(<SearchContent initialQuery="telescope" />);
    for (const [params, options] of hooks.useSearch.mock.calls) {
      expect(params).toMatchObject({ project_ids: ['a', 'b'] });
      expect(options).toMatchObject({ keepPreviousResults: false });
    }
    expect(screen.getByText(/2 selected projects/)).toBeInTheDocument();
  });

  it('labels explicit All Projects and omits selection filters', () => {
    hooks.projectContext = { selectedProjects: [], isAll: true, scopeReady: true };
    render(<SearchContent initialQuery="telescope" />);
    for (const [params] of hooks.useSearch.mock.calls) {
      expect(params).toMatchObject({ project_ids: undefined });
    }
    expect(screen.getByText(/All Projects/)).toBeInTheDocument();
  });

  it('does not fetch until a project selection is ready', () => {
    hooks.projectContext = { selectedProjects: [], isAll: false, scopeReady: false };
    render(<SearchContent initialQuery="telescope" />);
    for (const [, options] of hooks.useSearch.mock.calls) {
      expect(options).toMatchObject({ enabled: false });
    }
  });

  it('keeps document search out of knowledge type filters', () => {
    render(<SearchContent initialQuery="" />);

    expect(screen.getByRole('tab', { name: /docs/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /document/i })).not.toBeInTheDocument();
  });

  it('uses unified search for all mode', () => {
    render(<SearchContent initialQuery="surreal" />);

    expect(hooks.useSearch).toHaveBeenCalledWith(
      expect.objectContaining({
        query: 'surreal',
        include_documents: true,
        include_graph: true,
        include_raw_memory: true,
        memory_scope: 'private',
      }),
      expect.objectContaining({ enabled: true })
    );
  });

  it('renders memory facets in all mode', () => {
    render(<SearchContent initialQuery="" />);

    expect(screen.getByLabelText(/source id/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/people/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/labels/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/occurred after/i)).toBeInTheDocument();
  });

  it('prepares raw-memory-only search for memory mode', () => {
    render(<SearchContent initialQuery="surreal" />);

    expect(hooks.useSearch).toHaveBeenCalledWith(
      expect.objectContaining({
        query: 'surreal',
        types: ['raw_memory'],
        include_documents: false,
        include_graph: false,
        include_raw_memory: true,
      }),
      expect.objectContaining({ enabled: false })
    );
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
