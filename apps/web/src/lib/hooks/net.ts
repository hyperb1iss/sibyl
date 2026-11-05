'use client';

import { type QueryClient, type QueryKey, useQuery } from '@tanstack/react-query';
import {
  NET_SETTLE_MS,
  NET_STREAM_LIMIT,
  NET_THREAD_LIMIT,
  type NetPresenceResponse,
  netApi,
} from '../api/net';
import { TIMING } from '../constants/app';
import { queryKeys } from './query-keys';
import { type Invalidator, queryClientInvalidator } from './shared';

/**
 * A post reaches the browser over the websocket at once, but the feed holds
 * it back for the settle window. Refetching on arrival would miss it, so
 * refetches wait out the window plus this margin.
 */
export const NET_REFETCH_DELAY_MS = NET_SETTLE_MS + 500;

/**
 * Websocket events are fire and forget. This bounds how stale an open page
 * gets if one is lost.
 */
const NET_SAFETY_REFETCH_MS = 30_000;

/** Watching expires two minutes after a reader's last request. */
const NET_PRESENCE_REFETCH_MS = 30_000;

/**
 * Refetch each key once its newest post has settled.
 *
 * Unlike the app-wide trailing debounce, a steady stream of posts cannot
 * starve this: two agents stuck replying to each other every second is the
 * thing a reader of this page most needs to see move. The first post of a
 * quiet spell refetches one delay later, and a post that lands while that
 * waits earns one more refetch, one delay after the last such post.
 */
export function createSettledInvalidationScheduler(
  queryClient: QueryClient,
  delayMs: number = NET_REFETCH_DELAY_MS
) {
  const invalidate = queryClientInvalidator(queryClient);
  const pending = new Map<string, { timer: ReturnType<typeof setTimeout>; dueAt: number }>();

  const fire = (id: string, queryKey: QueryKey) => {
    const entry = pending.get(id);
    pending.delete(id);
    invalidate(queryKey);
    const remaining = (entry?.dueAt ?? 0) - Date.now();
    if (entry && remaining > 0) {
      pending.set(id, {
        timer: setTimeout(() => fire(id, queryKey), remaining),
        dueAt: entry.dueAt,
      });
    }
  };

  const schedule: Invalidator = queryKey => {
    const id = JSON.stringify(queryKey);
    const dueAt = Date.now() + delayMs;
    const entry = pending.get(id);
    if (entry) {
      entry.dueAt = dueAt;
      return;
    }
    pending.set(id, { timer: setTimeout(() => fire(id, queryKey), delayMs), dueAt });
  };

  const cancel = () => {
    for (const entry of pending.values()) clearTimeout(entry.timer);
    pending.clear();
  };

  return { schedule, cancel };
}

/**
 * The queries a post to `channel` (and `thread`, empty when threadless)
 * makes stale: the channel list, the org-wide stream, the channel's stream,
 * its threads, the thread itself, and recent authors.
 */
export function netQueriesForPost(channel: string, thread: string): QueryKey[] {
  const keys: QueryKey[] = [
    queryKeys.net.channels,
    queryKeys.net.feed(null, null),
    queryKeys.net.feed(channel, null),
    queryKeys.net.threads(channel),
    queryKeys.net.presence(null),
  ];
  if (thread) keys.push(queryKeys.net.feed(channel, thread));
  return keys;
}

/** Every channel with any message, most recently active first. */
export function useNetChannels() {
  return useQuery({
    queryKey: queryKeys.net.channels,
    queryFn: ({ signal }) => netApi.channels({ signal }),
    staleTime: TIMING.STALE_TIME,
    refetchInterval: NET_SAFETY_REFETCH_MS,
  });
}

/**
 * The newest messages, oldest first: across the organization when `channel`
 * is null, else one channel, else one thread of it.
 */
export function useNetFeed(channel: string | null, thread: string | null) {
  const scopedThread = channel ? thread : null;
  return useQuery({
    queryKey: queryKeys.net.feed(channel, scopedThread),
    queryFn: ({ signal }) =>
      netApi.feed(
        {
          channel,
          thread: scopedThread,
          limit: scopedThread ? NET_THREAD_LIMIT : NET_STREAM_LIMIT,
        },
        { signal }
      ),
    staleTime: TIMING.STALE_TIME,
    refetchInterval: NET_SAFETY_REFETCH_MS,
  });
}

/** A channel's threads, most recently active first. */
export function useNetThreads(channel: string | null) {
  return useQuery({
    queryKey: queryKeys.net.threads(channel ?? ''),
    queryFn: ({ signal }) => netApi.threads(channel ?? '', { signal }),
    enabled: Boolean(channel),
    staleTime: TIMING.STALE_TIME,
    refetchInterval: NET_SAFETY_REFETCH_MS,
  });
}

/**
 * Who is watching and who posted lately, across the organization, or null
 * when the server has no presence to offer. Presence is a nicety: a server
 * without the endpoint, or a failed read, hides it rather than the page.
 */
export function useNetPresence(): NetPresenceResponse | null {
  const query = useQuery({
    queryKey: queryKeys.net.presence(null),
    queryFn: ({ signal }) => netApi.presence(null, { signal }),
    retry: false,
    staleTime: NET_PRESENCE_REFETCH_MS,
    refetchInterval: current => (current.state.data ? NET_PRESENCE_REFETCH_MS : false),
  });
  return query.isError ? null : (query.data ?? null);
}
