'use client';

import Link from 'next/link';
import type { ReactNode } from 'react';
import { useId, useState } from 'react';
import { Skeleton } from '@/components/ui/spinner';
import type { NetChannel, NetThread, NetWatcher } from '@/lib/api/net';
import { listenerLabel, netHref, plural, visibleText } from './net-format';
import { ChannelName, RelativeTime, SafeText } from './net-primitives';

/** Threads listed under the open channel before "Show more". */
export const THREADS_SHOWN = 8;

const ROW_FOCUS =
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-inset';

function RailLink({
  href,
  current,
  within,
  onNavigate,
  children,
}: {
  href: string;
  /** This row is the page being shown. */
  current: boolean;
  /** The page shows something inside this row, such as one of its threads. */
  within?: boolean;
  onNavigate?: () => void;
  children: ReactNode;
}) {
  return (
    <Link
      href={href}
      onClick={onNavigate}
      aria-current={current ? 'page' : undefined}
      className={`relative block rounded-md px-2.5 py-2 transition-colors ${ROW_FOCUS} ${
        current
          ? 'bg-sc-bg-highlight text-sc-fg-primary'
          : within
            ? 'text-sc-fg-primary hover:bg-sc-bg-highlight/60'
            : 'text-sc-fg-secondary hover:bg-sc-bg-highlight/60 hover:text-sc-fg-primary'
      }`}
    >
      {current && (
        <span
          aria-hidden="true"
          className="absolute inset-y-1.5 left-0 w-0.5 rounded-full bg-sc-purple"
        />
      )}
      {children}
    </Link>
  );
}

function ThreadTree({
  channel,
  threads,
  selectedThread,
  onNavigate,
}: {
  channel: string;
  threads: NetThread[] | undefined;
  selectedThread: string | null;
  onNavigate?: () => void;
}) {
  const [showAll, setShowAll] = useState(false);
  const listId = useId();
  if (threads === undefined) {
    return (
      <div className="mt-1 mb-1.5 ml-4 space-y-2 border-l border-sc-fg-subtle/20 py-1 pl-3">
        <Skeleton className="h-3 w-32" />
        <Skeleton className="h-3 w-24" />
      </div>
    );
  }
  // The threadless row counts messages that have no thread to open.
  const named = threads.filter(
    (thread): thread is NetThread & { thread: string } => thread.thread !== null
  );
  if (named.length === 0) return null;
  // The open thread stays listed even when it is past the cut.
  const visible = showAll
    ? named
    : named.filter((thread, index) => index < THREADS_SHOWN || thread.thread === selectedThread);
  const hidden = named.length - visible.length;

  return (
    <ul
      id={listId}
      aria-label={`Threads in ${channel}`}
      className="mt-0.5 mb-1.5 ml-4 space-y-px border-l border-sc-fg-subtle/20 pl-1.5"
    >
      {visible.map(thread => {
        const current = thread.thread === selectedThread;
        return (
          <li key={thread.thread}>
            <Link
              href={netHref(channel, thread.thread)}
              onClick={onNavigate}
              aria-current={current ? 'page' : undefined}
              title={visibleText(thread.thread)}
              className={`relative flex items-center justify-between gap-2 rounded-md px-2 py-1 transition-colors ${ROW_FOCUS} ${
                current
                  ? 'bg-sc-bg-highlight text-sc-fg-primary'
                  : 'text-sc-fg-muted hover:bg-sc-bg-highlight/60 hover:text-sc-fg-primary'
              }`}
            >
              {current && (
                <span
                  aria-hidden="true"
                  className="absolute inset-y-1 left-0 w-0.5 rounded-full bg-sc-purple"
                />
              )}
              <span className="min-w-0 truncate font-mono text-[11px]">
                <SafeText text={thread.thread} />
              </span>
              <span className="shrink-0 text-[11px] tabular-nums">
                {thread.message_count.toLocaleString('en-US')}
              </span>
            </Link>
          </li>
        );
      })}
      {(hidden > 0 || showAll) && named.length > THREADS_SHOWN && (
        <li>
          <button
            type="button"
            aria-expanded={showAll}
            aria-controls={listId}
            onClick={() => setShowAll(value => !value)}
            className={`w-full rounded-md px-2 py-1 text-left text-[11px] font-medium text-sc-cyan hover:bg-sc-bg-highlight/60 ${ROW_FOCUS}`}
          >
            {showAll ? 'Show fewer threads' : `Show ${plural(hidden, 'more thread')}`}
          </button>
        </li>
      )}
    </ul>
  );
}

export interface ChannelListProps {
  channels: NetChannel[];
  selectedChannel: string | null;
  selectedThread: string | null;
  /** Threads of the selected channel; undefined while they load. */
  threads: NetThread[] | undefined;
  /** Who is listening on each channel; empty until presence loads. */
  listening: Map<string, NetWatcher[]>;
  /** Called after a row is chosen, so a phone-width drawer can close. */
  onNavigate?: () => void;
  /** The phone-width picker already says "Channel" above the list. */
  showHeading?: boolean;
}

/** Every channel, most recently active first, with the open one's threads beneath it. */
export function ChannelList({
  channels,
  selectedChannel,
  selectedThread,
  threads,
  listening,
  onNavigate,
  showHeading = true,
}: ChannelListProps) {
  const total = channels.reduce((sum, channel) => sum + channel.message_count, 0);

  return (
    <nav aria-label="Channels" className="flex flex-col">
      {showHeading && (
        <div className="flex items-baseline justify-between px-4 pt-3.5 pb-2">
          <h2 className="text-[10px] font-semibold tracking-wider text-sc-fg-muted uppercase">
            Channels
          </h2>
          <span className="text-[11px] text-sc-fg-muted tabular-nums">{channels.length}</span>
        </div>
      )}
      <ul className={`space-y-0.5 px-2 pb-2 ${showHeading ? '' : 'pt-2'}`}>
        <li>
          <RailLink href={netHref()} current={selectedChannel === null} onNavigate={onNavigate}>
            <span className="flex items-baseline justify-between gap-2">
              <span className="text-sm font-medium">All channels</span>
              <span className="text-xs text-sc-fg-muted tabular-nums">
                {total.toLocaleString('en-US')}
              </span>
            </span>
          </RailLink>
        </li>
        {channels.map(channel => {
          const open = channel.channel === selectedChannel;
          const listeners = listening.get(channel.channel) ?? [];
          return (
            <li key={channel.channel}>
              <RailLink
                href={netHref(channel.channel)}
                current={open && selectedThread === null}
                within={open}
                onNavigate={onNavigate}
              >
                <span className="flex items-baseline justify-between gap-2">
                  <ChannelName channel={channel.channel} className="text-sm font-medium" />
                  <span className="shrink-0 text-xs text-sc-fg-muted tabular-nums">
                    {channel.message_count.toLocaleString('en-US')}
                  </span>
                </span>
                <span className="mt-0.5 flex flex-wrap items-center gap-x-1.5 text-[11px] text-sc-fg-muted">
                  {[
                    channel.last_message_at && (
                      <RelativeTime key="last" iso={channel.last_message_at} />
                    ),
                    channel.thread_count > 0 && (
                      <span key="threads">{plural(channel.thread_count, 'thread')}</span>
                    ),
                    listeners.length > 0 && (
                      <span
                        key="listening"
                        title={listeners
                          .map(listener => visibleText(listenerLabel(listener)))
                          .join('\n')}
                        className="text-sc-fg-secondary"
                      >
                        {listeners.length} listening
                      </span>
                    ),
                  ]
                    .filter(Boolean)
                    .flatMap((part, index) =>
                      index === 0
                        ? [part]
                        : [
                            <span key={`dot-${index}`} aria-hidden="true">
                              ·
                            </span>,
                            part,
                          ]
                    )}
                </span>
              </RailLink>
              {open && (
                <ThreadTree
                  channel={channel.channel}
                  threads={threads}
                  selectedThread={selectedThread}
                  onNavigate={onNavigate}
                />
              )}
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
