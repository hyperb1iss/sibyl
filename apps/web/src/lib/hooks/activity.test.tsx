import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fixtureTeamActivity } from '@/components/team/team-activity.fixtures';
import type { TeamActivityItem, TeamActivityResponse } from '../api/activity';
import { mergeTeamActivity, useTeamActivity } from './activity';

const NOW = Date.parse('2026-10-08T15:00:00Z');

function createWrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

function item(id: string, at: string, actor = 'user_ada'): TeamActivityItem {
  return {
    kind: 'capture',
    id,
    title: id,
    entity_type: null,
    project_id: null,
    actor_id: actor,
    at,
    href: `/archive/${id}`,
  };
}

function requestedUrls(fetchMock: ReturnType<typeof vi.fn>): URL[] {
  return fetchMock.mock.calls.map(([input]) => new URL(String(input), 'http://localhost'));
}

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
    expect(result.current.data?.people.map(person => person.name)[0]).toBe('Ada Lovelace');
  });

  it('scopes the request to one project', async () => {
    const { result } = renderHook(() => useTeamActivity({ window: '24h', projectId: 'p1' }), {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(result.current.data).toBeDefined());
    const [url] = requestedUrls(fetchMock);
    expect(url.searchParams.get('window')).toBe('24h');
    expect(url.searchParams.get('project_id')).toBe('p1');
  });

  it('fetches each of several projects and merges the answers', async () => {
    const { result } = renderHook(
      () => useTeamActivity({ window: '30d', projectIds: ['p1', 'p2'] }),
      { wrapper: createWrapper() }
    );

    await waitFor(() => expect(result.current.data).toBeDefined());
    expect(
      requestedUrls(fetchMock)
        .map(url => url.searchParams.get('project_id'))
        .sort()
    ).toEqual(['p1', 'p2']);
    const ada = result.current.data?.people.find(person => person.user_id === 'user_ada');
    // Same fixture twice: every count doubles.
    expect(ada?.counts.captures).toBe(24);
  });

  it('keeps the previous answer, flagged as a placeholder, while a new window loads', async () => {
    const { result, rerender } = renderHook(
      ({ window }: { window: '7d' | '24h' }) => useTeamActivity({ window }),
      { wrapper: createWrapper(), initialProps: { window: '7d' } }
    );
    await waitFor(() => expect(result.current.data).toBeDefined());
    const shown = result.current.data;

    fetchMock.mockImplementationOnce(() => new Promise<Response>(() => undefined));
    rerender({ window: '24h' });

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(result.current.data).toBe(shown);
    expect(result.current.isPlaceholderData).toBe(true);
    expect(result.current.isLoading).toBe(false);
  });

  it('drops the old answer once a window fails, so it cannot flash back', async () => {
    const { result, rerender } = renderHook(
      ({ window }: { window: '7d' | '24h' | '30d' }) => useTeamActivity({ window }),
      { wrapper: createWrapper(), initialProps: { window: '7d' } }
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

describe('mergeTeamActivity', () => {
  it('adds counts, keeps the latest activity, and re-ranks people', () => {
    const base = fixtureTeamActivity(NOW);
    const quietProject: TeamActivityResponse = {
      ...base,
      people: base.people.map(person =>
        person.user_id === 'user_dennis'
          ? {
              ...person,
              counts: { ...person.counts, captures: 40 },
              last_active_at: '2026-10-08T14:59:00Z',
            }
          : {
              ...person,
              counts: {
                captures: 0,
                tasks_created: 0,
                tasks_completed: 0,
                decisions: 0,
                notes: 0,
                procedures: 0,
                other: 0,
              },
            }
      ),
      recent: [],
    };

    const merged = mergeTeamActivity([base, quietProject]);

    expect(merged?.people[0].user_id).toBe('user_dennis');
    expect(merged?.people[0].last_active_at).toBe('2026-10-08T14:59:00Z');
    expect(merged?.people.find(person => person.user_id === 'user_ada')?.counts.captures).toBe(12);
    expect(merged?.project_id).toBeNull();
  });

  it('interleaves feeds newest first, drops repeats, and caps at the server limit', () => {
    const base = fixtureTeamActivity(NOW);
    const older = Array.from({ length: 60 }, (_, index) =>
      item(`a${index}`, new Date(NOW - (index * 2 + 1) * 60_000).toISOString())
    );
    const newer = Array.from({ length: 60 }, (_, index) =>
      item(`b${index}`, new Date(NOW - index * 2 * 60_000).toISOString())
    );

    const merged = mergeTeamActivity([
      { ...base, recent: older },
      { ...base, recent: [...newer, older[0]] },
    ]);

    expect(merged?.recent).toHaveLength(100);
    expect(merged?.recent.slice(0, 3).map(entry => entry.id)).toEqual(['b0', 'a0', 'b1']);
    expect(merged?.truncated).toBe(true);
  });

  it('passes a single answer through untouched', () => {
    const base = fixtureTeamActivity(NOW);
    expect(mergeTeamActivity([base])).toBe(base);
    expect(mergeTeamActivity([])).toBeUndefined();
  });
});
