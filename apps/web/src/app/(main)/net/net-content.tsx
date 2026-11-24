'use client';

import { useRouter, useSearchParams } from 'next/navigation';
import { useEffect, useMemo, useRef } from 'react';
import { NetView } from '@/components/net';
import { netHref } from '@/components/net/net-format';
import { NetSkeleton } from '@/components/suspense-boundary';
import {
  useNetChannels,
  useNetFeed,
  useNetPresence,
  useNetThreads,
  useProjects,
} from '@/lib/hooks';
import { useInboxStatus, useNetDirectory } from '@/lib/hooks/net-identity';

/** The address that always means "my own inbox", whoever reads the link. */
const MY_INBOX_ALIAS = '@me';

/** At most one "seen" write per this long while new messages keep arriving. */
const SEEN_WRITE_INTERVAL_MS = 15_000;

/**
 * Wires the Net page to the URL: `?channel=` picks a channel and `?thread=`
 * one of its threads, so a link opens the same conversation for anyone.
 * With no channel, the page opens on the viewer's inbox when it holds
 * something new, and on every channel otherwise.
 */
export function NetContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  // Channel names are lowercase; a hand-typed ?channel=@Ada means @ada.
  const requested = searchParams.get('channel')?.trim().toLowerCase() || null;
  const thread = requested ? searchParams.get('thread')?.trim() || null : null;

  const inboxStatus = useInboxStatus();
  const { inbox, newCount, ready, messages: inboxMessages, markSeen } = inboxStatus;
  const channel = requested === MY_INBOX_ALIAS && inbox ? inbox : requested;

  // Decide once per visit where a bare /net lands; after that, "All
  // channels" means all channels.
  const landed = useRef(requested !== null);
  const redirecting = useRef(false);
  if (requested !== null) redirecting.current = false;
  // Hold the page while that is unknown, and until the redirect lands, so
  // every channel never flashes up first.
  const deciding =
    requested === null &&
    (redirecting.current || (!landed.current && (!ready || (inbox !== null && newCount > 0))));
  useEffect(() => {
    if (landed.current || requested !== null || !ready) return;
    landed.current = true;
    if (inbox && newCount > 0) {
      redirecting.current = true;
      router.replace(netHref(inbox));
    }
  }, [inbox, newCount, ready, requested, router]);

  // `@me` in a shared link becomes the reader's own inbox in the address bar.
  useEffect(() => {
    if (requested === MY_INBOX_ALIAS && inbox) router.replace(netHref(inbox, thread));
  }, [inbox, requested, router, thread]);

  const channels = useNetChannels();
  const feed = useNetFeed(channel, thread);
  const threads = useNetThreads(channel);
  const presence = useNetPresence();
  const directory = useNetDirectory();
  // Only a thread's header names a project.
  const { data: projectsData } = useProjects({ includeArchived: true, enabled: thread !== null });

  // Reading the inbox marks it seen, through its newest message.
  const viewingInbox = inbox !== null && channel === inbox && thread === null;
  const newestInInbox = viewingInbox ? inboxMessages.at(-1)?.created_at : undefined;
  const lastSeenWrite = useRef(0);
  useEffect(() => {
    if (!newestInInbox) return;
    const wait = lastSeenWrite.current + SEEN_WRITE_INTERVAL_MS - Date.now();
    const write = () => {
      lastSeenWrite.current = Date.now();
      markSeen(newestInInbox);
    };
    if (wait <= 0) {
      write();
      return;
    }
    const timer = setTimeout(write, wait);
    return () => clearTimeout(timer);
  }, [newestInInbox, markSeen]);

  const projectNames = useMemo(() => {
    const names: Record<string, string> = {};
    for (const project of projectsData?.entities ?? []) names[project.id] = project.name;
    return names;
  }, [projectsData]);

  if (deciding) return <NetSkeleton />;

  return (
    <NetView
      channel={channel}
      thread={thread}
      channels={{
        data: channels.data,
        isLoading: channels.isLoading,
        isError: channels.isError,
        refetch: () => void channels.refetch(),
      }}
      feed={{
        data: feed.data,
        isLoading: feed.isLoading,
        isError: feed.isError,
        refetch: () => void feed.refetch(),
      }}
      threads={threads.data?.threads}
      presence={presence}
      projectNames={projectNames}
      inbox={inbox ? { channel: inbox, newCount: viewingInbox ? 0 : newCount } : null}
      directory={directory}
    />
  );
}
