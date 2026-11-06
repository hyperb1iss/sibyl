import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query';
import { act, render, renderHook, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ switch: vi.fn() }));

vi.mock('../api/auth', async importOriginal => {
  const original = await importOriginal<typeof import('../api/auth')>();
  return { ...original, orgsApi: { ...original.orgsApi, switch: api.switch } };
});

import { useSwitchOrg } from './auth';
import { queryKeys } from './query-keys';

function NetChannels({ org }: { org: { current: string } }) {
  const { data } = useQuery({
    queryKey: queryKeys.net.channels,
    queryFn: async () => `${org.current} channels`,
    staleTime: Number.POSITIVE_INFINITY,
  });
  return <p>{data ?? 'loading'}</p>;
}

describe('useSwitchOrg', () => {
  let client: QueryClient;
  const org = { current: 'acme' };

  beforeEach(() => {
    org.current = 'acme';
    api.switch.mockReset();
    api.switch.mockImplementation(async (slug: string) => {
      org.current = slug;
      return { organization: { id: slug, slug, name: slug }, access_token: 'token' };
    });
    client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  });

  function wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  }

  it('drops every answer from the old organization and keeps who the user is', async () => {
    client.setQueryData(queryKeys.auth.me, { user: { id: 'u-ada' } });
    client.setQueryData(queryKeys.preferences, { theme: 'neon' });
    client.setQueryData(queryKeys.net.channels, 'acme channels');
    client.setQueryData(queryKeys.net.feed('@ada', null), { messages: ['acme'] });
    client.setQueryData(queryKeys.activity.team({ window: '7d' }), { people: ['acme'] });
    client.setQueryData(queryKeys.tasks.list(), { entities: ['acme'] });

    const { result } = renderHook(() => useSwitchOrg(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync('globex');
    });

    expect(client.getQueryData(queryKeys.net.channels)).toBeUndefined();
    expect(client.getQueryData(queryKeys.net.feed('@ada', null))).toBeUndefined();
    expect(client.getQueryData(queryKeys.activity.team({ window: '7d' }))).toBeUndefined();
    expect(client.getQueryData(queryKeys.tasks.list())).toBeUndefined();
    expect(client.getQueryData(queryKeys.auth.me)).toEqual({ user: { id: 'u-ada' } });
    expect(client.getQueryData(queryKeys.preferences)).toEqual({ theme: 'neon' });
  });

  it('moves a mounted screen to the new organization without a reload', async () => {
    render(<NetChannels org={org} />, { wrapper });
    expect(await screen.findByText('acme channels')).toBeInTheDocument();

    const { result } = renderHook(() => useSwitchOrg(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync('globex');
    });

    await waitFor(() => expect(screen.getByText('globex channels')).toBeInTheDocument());
    expect(screen.queryByText('acme channels')).toBeNull();
  });
});
