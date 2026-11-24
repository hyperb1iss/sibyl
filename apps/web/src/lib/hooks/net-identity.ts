'use client';

import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useMemo } from 'react';
import { type PreferencesResponse, preferencesApi } from '../api/auth';
import { type NetMember, type NetMessage, netApi } from '../api/net';
import { TIMING } from '../constants/app';
import { useMe, usePreferences } from './auth';
import { useNetFeed } from './net';
import { queryKeys } from './query-keys';

/*
 * Who the viewer is on sibylnet, and who everyone else is: the viewer's own
 * inbox from /auth/me and every member's handle from GET /net/directory.
 */

/**
 * The viewer's own inbox in the active organization, such as `@ada`; null
 * with no active organization.
 */
export function useMyInbox(): string | null {
  const { data } = useMe();
  return data?.inbox ?? null;
}

/** Every member by user id, or null until the directory has loaded. */
export function useNetDirectory(): Map<string, NetMember> | null {
  const query = useQuery({
    queryKey: queryKeys.net.directory,
    queryFn: ({ signal }) => netApi.directory({ signal }),
    staleTime: TIMING.STALE_TIME,
    refetchInterval: 60_000,
  });
  const members = query.data?.members;
  return useMemo(
    () => (members ? new Map(members.map(member => [member.user_id, member])) : null),
    [members]
  );
}

/**
 * Handles by user id, stable while nobody's handle changes. The directory
 * refreshes its listening flags often; rows that only show a handle should
 * not re-render for that.
 */
export function useHandles(directory: Map<string, NetMember> | null): Map<string, string> {
  const signature = directory
    ? [...directory.values()].map(member => `${member.user_id}=${member.handle}`).join('\n')
    : '';
  // biome-ignore lint/correctness/useExhaustiveDependencies: rebuilt only when a handle changes
  return useMemo(() => {
    const handles = new Map<string, string>();
    for (const member of directory?.values() ?? []) handles.set(member.user_id, member.handle);
    return handles;
  }, [signature]);
}

// ---------------------------------------------------------------------------
// New since the last visit
// ---------------------------------------------------------------------------

/** Preference holding, per organization and inbox, the newest message the viewer has seen. */
const SEEN_PREFERENCE = 'net_inbox_seen';

type SeenMap = Record<string, string>;

function seenKey(organizationId: string, inbox: string): string {
  return `${organizationId}:${inbox}`;
}

/**
 * Messages in the inbox newer than the last one the viewer saw, not counting
 * their own. Never having looked counts everything loaded.
 */
export function inboxNewCount(
  messages: NetMessage[],
  seenAt: string | null,
  viewerId: string | null
): number {
  return messages.filter(
    message =>
      message.author.user_id !== viewerId && (seenAt === null || message.created_at > seenAt)
  ).length;
}

export interface InboxStatus {
  /** `@<handle>`, or null with no active organization. */
  inbox: string | null;
  /** New messages since the viewer last opened the inbox. */
  newCount: number;
  /** Everything the count depends on has loaded. */
  ready: boolean;
  /** The inbox's newest messages, oldest first. */
  messages: NetMessage[];
  /** Record that the viewer has seen the inbox through `latest` (an ISO time). */
  markSeen: (latest: string) => void;
}

/**
 * The viewer's inbox with how much is new in it. "Seen" lives in the user's
 * server-side preferences, so it follows them across browsers.
 */
export function useInboxStatus(): InboxStatus {
  const queryClient = useQueryClient();
  const meQuery = useMe();
  const me = meQuery.data;
  const inbox = useMyInbox();
  const preferences = usePreferences();
  const feed = useNetFeed(inbox, null, { enabled: inbox !== null });

  const organizationId = me?.organization?.id ?? null;
  const key = organizationId && inbox ? seenKey(organizationId, inbox) : null;
  const seenMap = (preferences.data?.preferences[SEEN_PREFERENCE] ?? {}) as SeenMap;
  const seenAt = key ? (seenMap[key] ?? null) : null;
  const messages = feed.data?.messages ?? [];
  const newCount = inboxNewCount(messages, seenAt, me?.user.id ?? null);

  const markSeen = useCallback(
    (latest: string) => {
      if (!key) return;
      const cached = queryClient.getQueryData<PreferencesResponse>(queryKeys.preferences);
      const current = (cached?.preferences[SEEN_PREFERENCE] ?? {}) as SeenMap;
      if (current[key] && current[key] >= latest) return;
      // The server merges top-level keys, so send the whole map.
      const next = { ...current, [key]: latest };
      queryClient.setQueryData<PreferencesResponse>(queryKeys.preferences, previous => ({
        preferences: { ...(previous?.preferences ?? {}), [SEEN_PREFERENCE]: next },
      }));
      // A convenience: a failed write only means the count shows again.
      void preferencesApi
        .update({ [SEEN_PREFERENCE]: next })
        .then(saved => queryClient.setQueryData(queryKeys.preferences, saved))
        .catch(() => undefined);
    },
    [key, queryClient]
  );

  // Until /auth/me answers there is no telling whether the viewer has an inbox.
  const knowsInbox = me !== undefined || meQuery.isError;
  const ready =
    knowsInbox &&
    (inbox === null ||
      ((preferences.data !== undefined || preferences.isError) &&
        (feed.data !== undefined || feed.isError)));

  return { inbox, newCount, ready, messages, markSeen };
}
