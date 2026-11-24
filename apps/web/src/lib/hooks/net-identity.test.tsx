import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { NetMessage } from '../api/net';
import { inboxNewCount, useMyInbox, useNetDirectory } from './net-identity';

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function message(user: string, at: string): NetMessage {
  return {
    id: `${user}-${at}`,
    channel: '@ada',
    thread: null,
    kind: 'message',
    body: 'hi',
    project_id: null,
    api_key_id: null,
    decision_memory_id: null,
    created_at: at,
    cursor: 'c',
    author: { user_id: user, name: user, email: null, agent: null, session: null },
  };
}

describe('inboxNewCount', () => {
  const messages = [
    message('u-grace', '2026-11-05T10:00:00Z'),
    message('u-ada', '2026-11-05T10:05:00Z'),
    message('u-grace', '2026-11-05T10:06:00Z'),
  ];

  it("counts messages after the last visit, never the viewer's own", () => {
    expect(inboxNewCount(messages, '2026-11-05T10:01:00Z', 'u-ada')).toBe(1);
    expect(inboxNewCount(messages, '2026-11-05T10:06:00Z', 'u-ada')).toBe(0);
  });

  it('counts everything from others when the inbox was never opened', () => {
    expect(inboxNewCount(messages, null, 'u-ada')).toBe(2);
  });
});

describe('handles', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('reads the inbox from /auth/me and members from the directory', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), 'http://localhost').pathname;
        if (path === '/api/auth/me')
          return json({
            user: { id: 'u-ada' },
            organization: null,
            org_role: null,
            handle: 'ada',
            inbox: '@ada',
          });
        return json({
          members: [
            {
              user_id: 'u-grace',
              name: 'Grace',
              handle: 'grace',
              inbox: '@grace',
              listening: 1,
              last_post_at: null,
            },
          ],
        });
      })
    );
    const inbox = renderHook(() => useMyInbox(), { wrapper });
    const directory = renderHook(() => useNetDirectory(), { wrapper });

    await waitFor(() => expect(inbox.result.current).toBe('@ada'));
    await waitFor(() => expect(directory.result.current?.get('u-grace')?.inbox).toBe('@grace'));
  });

  it('reads no inbox with no active organization, and no directory when its read fails', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(String(input), 'http://localhost').pathname;
      if (path === '/api/auth/me')
        return json({
          user: { id: 'u-ada' },
          organization: null,
          org_role: null,
          handle: null,
          inbox: null,
        });
      return json({ detail: 'directory unavailable' }, 503);
    });
    vi.stubGlobal('fetch', fetchMock);
    const inbox = renderHook(() => useMyInbox(), { wrapper });
    const directory = renderHook(() => useNetDirectory(), { wrapper });

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(directory.result.current).toBeNull());
    expect(inbox.result.current).toBeNull();
  });
});
