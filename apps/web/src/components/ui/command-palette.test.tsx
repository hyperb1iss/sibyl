import { beforeEach, describe, expect, it, vi } from 'vitest';
import { CommandPaletteProvider } from '@/components/layout/command-palette-context';
import { GlobalCommandPalette } from '@/components/layout/global-command-palette';
import { render, screen, waitFor, within } from '@/test/utils';
import { CommandPalette } from './command-palette';

const router = vi.hoisted(() => ({
  push: vi.fn(),
  replace: vi.fn(),
  prefetch: vi.fn(),
  back: vi.fn(),
  forward: vi.fn(),
  refresh: vi.fn(),
}));

const search = vi.hoisted(() => ({
  useSearch: vi.fn(),
}));

vi.mock('next/navigation', () => ({
  useRouter: () => router,
  usePathname: () => '/',
  useSearchParams: () => new URLSearchParams('projects=proj-a'),
  useParams: () => ({}),
}));

vi.mock('@/lib/hooks/search', () => ({
  useSearch: search.useSearch,
}));

const liveResults = {
  results: [
    {
      id: 'task_1',
      type: 'task',
      name: 'Ship the omnibox',
      content: 'Replace the split <mark>search</mark> surfaces',
      score: 0.9,
      source: null,
      url: null,
      result_origin: 'graph',
      metadata: {},
    },
    {
      id: 'pattern_1',
      type: 'pattern',
      name: 'Surreal namespace per org',
      content: 'Every graph operation requires org context',
      score: 0.8,
      source: null,
      url: null,
      result_origin: 'graph',
      metadata: {},
    },
    {
      id: 'doc_1',
      type: 'document',
      name: 'SurrealQL reference',
      content: 'SELECT statements',
      score: 0.7,
      source: 'surrealdb',
      url: 'https://surrealdb.com/docs',
      result_origin: 'document',
      metadata: { source_id: 'src_1', document_id: 'doc_1' },
    },
  ],
  total: 3,
  query: 'surreal',
  filters: {},
};

function renderGlobalPalette() {
  return render(
    <CommandPaletteProvider>
      <button type="button">Opener</button>
      <GlobalCommandPalette />
    </CommandPaletteProvider>
  );
}

describe('CommandPalette', () => {
  beforeEach(() => {
    router.push.mockClear();
    search.useSearch.mockReset();
    search.useSearch.mockReturnValue({ data: undefined, isFetching: false });
  });

  it('opens on Cmd+K, Cmd+Shift+K, and closes on Escape', async () => {
    const { user } = renderGlobalPalette();

    expect(screen.queryByRole('dialog', { name: /command palette/i })).not.toBeInTheDocument();

    await user.keyboard('{Meta>}k{/Meta}');
    expect(screen.getByRole('dialog', { name: /command palette/i })).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: /search or run a command/i })).toHaveFocus();

    await user.keyboard('{Escape}');
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: /command palette/i })).not.toBeInTheDocument()
    );

    await user.keyboard('{Control>}{Shift>}k{/Shift}{/Control}');
    expect(screen.getByRole('dialog', { name: /command palette/i })).toBeInTheDocument();
  });

  it('returns focus to the element that opened it', async () => {
    const { user } = renderGlobalPalette();
    const opener = screen.getByRole('button', { name: 'Opener' });

    await user.click(opener);
    expect(opener).toHaveFocus();

    await user.keyboard('{Meta>}k{/Meta}');
    expect(screen.getByRole('combobox', { name: /search or run a command/i })).toHaveFocus();

    await user.keyboard('{Escape}');
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it('lists commands and navigation with an always-present search handoff', async () => {
    render(<CommandPalette isOpen onClose={vi.fn()} />);

    const dialog = screen.getByRole('dialog', { name: /command palette/i });
    expect(within(dialog).getByRole('option', { name: /new task/i })).toBeInTheDocument();
    expect(
      within(dialog).queryByRole('option', { name: /capture memory/i })
    ).not.toBeInTheDocument();
    expect(within(dialog).getByRole('option', { name: /toggle theme/i })).toBeInTheDocument();
    expect(within(dialog).getByRole('option', { name: /go to settings/i })).toBeInTheDocument();
    expect(within(dialog).getByRole('option', { name: /open search/i })).toBeInTheDocument();
  });

  it('filters navigation by query', async () => {
    const { user } = render(<CommandPalette isOpen onClose={vi.fn()} />);

    await user.keyboard('graph');

    const dialog = screen.getByRole('dialog', { name: /command palette/i });
    expect(within(dialog).getByRole('option', { name: /go to graph/i })).toBeInTheDocument();
    expect(within(dialog).queryByRole('option', { name: /go to tasks/i })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole('option', { name: /new task/i })).not.toBeInTheDocument();
    expect(
      within(dialog).getByRole('option', { name: /search everything for "graph"/i })
    ).toBeInTheDocument();
  });

  it('moves selection with arrow keys and navigates on Enter with project context', async () => {
    const onClose = vi.fn();
    const { user } = render(<CommandPalette isOpen onClose={onClose} />);

    await user.keyboard('graph');

    const input = screen.getByRole('combobox', { name: /search or run a command/i });
    const graphOption = screen.getByRole('option', { name: /go to graph/i });
    expect(graphOption).toHaveAttribute('aria-selected', 'true');
    expect(input).toHaveAttribute('aria-activedescendant', graphOption.id);

    await user.keyboard('{ArrowDown}');
    const searchOption = screen.getByRole('option', { name: /search everything/i });
    expect(searchOption).toHaveAttribute('aria-selected', 'true');
    expect(input).toHaveAttribute('aria-activedescendant', searchOption.id);

    await user.keyboard('{ArrowUp}{Enter}');
    expect(onClose).toHaveBeenCalled();
    expect(router.push).toHaveBeenCalledWith('/graph?projects=proj-a');
  });

  it('shows live results grouped by kind once the query settles', async () => {
    search.useSearch.mockImplementation((params: { query: string }) => ({
      data: params.query === 'surreal' ? liveResults : undefined,
      isFetching: false,
    }));
    const { user } = render(<CommandPalette isOpen onClose={vi.fn()} />);

    await user.keyboard('surreal');

    const dialog = screen.getByRole('dialog', { name: /command palette/i });
    const work = await within(dialog).findByRole('group', { name: 'Work' });
    expect(within(work).getByRole('option', { name: /ship the omnibox/i })).toBeInTheDocument();
    expect(within(work).getByText('task')).toBeInTheDocument();

    const memory = within(dialog).getByRole('group', { name: 'Memory' });
    expect(
      within(memory).getByRole('option', { name: /surreal namespace per org/i })
    ).toBeInTheDocument();

    const docs = within(dialog).getByRole('group', { name: 'Docs' });
    expect(within(docs).getByRole('option', { name: /surrealql reference/i })).toBeInTheDocument();

    // Highlight markup is stripped from snippets.
    expect(within(work).getByText('Replace the split search surfaces')).toBeInTheDocument();

    expect(search.useSearch).toHaveBeenLastCalledWith(
      expect.objectContaining({
        query: 'surreal',
        include_documents: true,
        include_graph: true,
        include_raw_memory: true,
      }),
      { enabled: true }
    );

    await user.click(within(docs).getByRole('option', { name: /surrealql reference/i }));
    expect(router.push).toHaveBeenCalledWith('/sources/src_1/documents/doc_1');
  });

  it('routes the new task command to the board with project context', async () => {
    const onClose = vi.fn();
    const { user } = render(<CommandPalette isOpen onClose={onClose} />);

    await user.click(screen.getByRole('option', { name: /new task/i }));
    expect(onClose).toHaveBeenCalled();
    expect(router.push).toHaveBeenCalledWith('/tasks?new=1&projects=proj-a');
  });
});
