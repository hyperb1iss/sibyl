import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fixtureTeamActivity } from '@/components/team/team-activity.fixtures';
import type { TeamActivityWindow } from '../api/activity';
import { teamActivityParams, useTeamActivity } from './activity';

const NOW = Date.parse('2026-10-08T15:00:00Z');

// No placeholder default here, unlike the app's client: the hook must keep
// the previous answer on its own.
function createWrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

function requestedUrls(fetchMock: ReturnType<typeof vi.fn>): URL[] {
  return fetchMock.mock.calls.map(([input]) => new URL(String(input), 'http://localhost'));
}

describe('teamActivityParams', () => {
  it('sorts and dedupes projects and leaves empty fields out', () => {
    expect(teamActivityParams({ window: '7d' })).toEqual({ window: '7d' });
    expect(teamActivityParams({ window: '7d', projectIds: [] })).toEqual({ window: '7d' });
    expect(teamActivityParams({ window: '7d', projectId: 'p1' })).toEqual({
      window: '7d',
      project_ids: ['p1'],
    });
    expect(
      teamActivityParams({ window: '24h', projectIds: ['p2', 'p1', 'p2'], actorId: 'user_ada' })
    ).toEqual({ window: '24h', project_ids: ['p1', 'p2'], actor_id: 'user_ada' });
  });
});

describe('useTeamActivity', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn(
      async () =>
        new Response(JSON.stringify(fixtureTeamActivity(NOW)), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
    );
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('asks for every project when no project is chosen', async () => {
    const { result } = renderHook(() => useTeamActivity({ window: '7d' }), {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(result.current.data).toBeDefined());
    const [url] = requestedUrls(fetchMock);
    expect(url.pathname).toBe('/api/activity/team');
    expect(url.searchParams.get('window')).toBe('7d');
    expect(url.searchParams.has('project_id')).toBe(false);
    expect(url.searchParams.has('actor_id')).toBe(false);
  });

  it('sends several projects as one request with a repeated project_id', async () => {
    const { result } = renderHook(
      () => useTeamActivity({ window: '30d', projectIds: ['p2', 'p1'] }),
      { wrapper: createWrapper() }
    );

    await waitFor(() => expect(result.current.data).toBeDefined());
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(requestedUrls(fetchMock)[0].searchParams.getAll('project_id')).toEqual(['p1', 'p2']);
  });

  it('keeps one cache entry whatever order the projects arrive in', async () => {
    const { result, rerender } = renderHook(
      ({ ids }: { ids: string[] }) => useTeamActivity({ window: '7d', projectIds: ids }),
      { wrapper: createWrapper(), initialProps: { ids: ['p2', 'p1'] } }
    );
    await waitFor(() => expect(result.current.data).toBeDefined());

    rerender({ ids: ['p1', 'p2', 'p1'] });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(result.current.isPlaceholderData).toBe(false);
  });

  it('narrows the feed to one member with actor_id', async () => {
    const { result } = renderHook(
      () => useTeamActivity({ window: '7d', projectId: 'p1', actorId: 'user_ada' }),
      { wrapper: createWrapper() }
    );

    await waitFor(() => expect(result.current.data).toBeDefined());
    const [url] = requestedUrls(fetchMock);
    expect(url.searchParams.getAll('project_id')).toEqual(['p1']);
    expect(url.searchParams.get('actor_id')).toBe('user_ada');
  });

  it('keeps the previous answer, flagged as a placeholder, while a new scope loads', async () => {
    const { result, rerender } = renderHook(
      ({ actorId }: { actorId?: string }) => useTeamActivity({ window: '7d', actorId }),
      { wrapper: createWrapper(), initialProps: {} as { actorId?: string } }
    );
    await waitFor(() => expect(result.current.data).toBeDefined());
    const shown = result.current.data;

    fetchMock.mockImplementationOnce(() => new Promise<Response>(() => undefined));
    rerender({ actorId: 'user_ada' });

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(result.current.data).toBe(shown);
    expect(result.current.isPlaceholderData).toBe(true);
    expect(result.current.isLoading).toBe(false);
  });

  it('drops the old answer once a window fails, so it cannot flash back', async () => {
    const { result, rerender } = renderHook(
      ({ window }: { window: TeamActivityWindow }) => useTeamActivity({ window }),
      { wrapper: createWrapper(), initialProps: { window: '7d' as TeamActivityWindow } }
    );
    await waitFor(() => expect(result.current.data).toBeDefined());

    fetchMock.mockImplementationOnce(async () => new Response('boom', { status: 500 }));
    rerender({ window: '24h' });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.data).toBeUndefined();

    fetchMock.mockImplementationOnce(() => new Promise<Response>(() => undefined));
    rerender({ window: '30d' });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(result.current.data).toBeUndefined();
    expect(result.current.isLoading).toBe(true);
  });

  it('waits while disabled', () => {
    const { result } = renderHook(() => useTeamActivity({ window: '7d' }, { enabled: false }), {
      wrapper: createWrapper(),
    });

    expect(fetchMock).not.toHaveBeenCalled();
    expect(result.current.isLoading).toBe(false);
  });
});
