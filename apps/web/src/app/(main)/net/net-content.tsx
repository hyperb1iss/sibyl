'use client';

import { useSearchParams } from 'next/navigation';
import { useMemo } from 'react';
import { NetView } from '@/components/net';
import {
  useNetChannels,
  useNetFeed,
  useNetPresence,
  useNetThreads,
  useProjects,
} from '@/lib/hooks';

/**
 * Wires the Net page to the URL: `?channel=` picks a channel and `?thread=`
 * one of its threads, so a link opens the same conversation for anyone.
 */
export function NetContent() {
  const searchParams = useSearchParams();
  // Channel names are lowercase; a hand-typed ?channel=@Ada means @ada.
  const channel = searchParams.get('channel')?.trim().toLowerCase() || null;
  const thread = channel ? searchParams.get('thread')?.trim() || null : null;

  const channels = useNetChannels();
  const feed = useNetFeed(channel, thread);
  const threads = useNetThreads(channel);
  const presence = useNetPresence();
  // Only a thread's header names a project.
  const { data: projectsData } = useProjects({ includeArchived: true, enabled: thread !== null });

  const projectNames = useMemo(() => {
    const names: Record<string, string> = {};
    for (const project of projectsData?.entities ?? []) names[project.id] = project.name;
    return names;
  }, [projectsData]);

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
    />
  );
}
