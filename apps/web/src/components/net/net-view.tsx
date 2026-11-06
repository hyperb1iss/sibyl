'use client';

import { useEffect, useMemo, useState } from 'react';
import { NetBodySkeleton } from '@/components/suspense-boundary';
import { ChevronDown } from '@/components/ui/icons';
import type {
  NetChannelsResponse,
  NetFeedResponse,
  NetMember,
  NetPresenceResponse,
  NetThread,
} from '@/lib/api/net';
import { useHandles } from '@/lib/hooks/net-identity';
import { ChannelList } from './channel-list';
import { plural, presenceFor } from './net-format';
import {
  ChannelStream,
  NetEmpty,
  type NetQueryState,
  PaneErrorState,
  ThreadView,
} from './net-panes';
import { ChannelName, CommandLine, HandlesContext } from './net-primitives';

export interface NetViewProps {
  /** Null for every channel. */
  channel: string | null;
  /** Only read with a channel. */
  thread: string | null;
  channels: NetQueryState<NetChannelsResponse>;
  feed: NetQueryState<NetFeedResponse>;
  /** The selected channel's threads; undefined while they load. */
  threads: NetThread[] | undefined;
  /** Null until presence loads. */
  presence: NetPresenceResponse | null;
  projectNames: Record<string, string>;
  /** The viewer's own inbox and what is new in it; null without handles. */
  inbox?: { channel: string; newCount: number } | null;
  /** Members by user id, for the handles beside their names. */
  directory?: Map<string, NetMember> | null;
}

function Stat({ value, label }: { value: number; label: string }) {
  return (
    <div className="flex flex-row-reverse items-baseline justify-end gap-1.5">
      <dt className="text-xs text-sc-fg-muted">{label}</dt>
      <dd className="text-sm font-semibold text-sc-fg-primary tabular-nums">
        {value.toLocaleString('en-US')}
      </dd>
    </div>
  );
}

/**
 * The Net page: what agents are saying, read only. Channels on the left, the
 * stream or one thread on the right; below `lg` the channel list folds into
 * a picker above the stream. Data comes in through props, so tests render it
 * without a server.
 */
export function NetView({
  channel,
  thread,
  channels,
  feed,
  threads,
  presence,
  projectNames,
  inbox = null,
  directory = null,
}: NetViewProps) {
  const [pickerOpen, setPickerOpen] = useState(false);
  const rows = channels.data?.channels ?? [];
  const listening = useMemo(
    () =>
      new Map(
        presence ? rows.map(row => [row.channel, presenceFor(presence, row.channel).listening]) : []
      ),
    [presence, rows]
  );
  const channelPresence = useMemo(
    () => (presence && channel ? presenceFor(presence, channel) : null),
    [presence, channel]
  );
  const threadInfo = thread ? threads?.find(row => row.thread === thread) : undefined;
  const totalMessages = rows.reduce((sum, row) => sum + row.message_count, 0);
  const listeningTotal = presence ? presenceFor(presence, null).listening.length : null;

  // A picked row navigates; fold the picker once the page follows.
  // biome-ignore lint/correctness/useExhaustiveDependencies: close on navigation only
  useEffect(() => {
    setPickerOpen(false);
  }, [channel, thread]);

  const list = (showHeading: boolean) => (
    <ChannelList
      showHeading={showHeading}
      channels={rows}
      selectedChannel={channel}
      selectedThread={thread}
      threads={threads}
      listening={listening}
      inbox={inbox}
    />
  );
  const handles = useHandles(directory);
  const inInbox = inbox !== null && channel === inbox.channel;

  // With an inbox of their own the viewer always has somewhere to look.
  const quiet =
    channels.data !== undefined && rows.length === 0 && channel === null && inbox === null;

  return (
    <HandlesContext.Provider value={handles}>
      <div className="space-y-4 animate-fade-in sm:space-y-5">
        <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
          <div className="min-w-0">
            <h1 className="text-xl font-bold text-sc-fg-primary sm:text-2xl">Net</h1>
            <p className="mt-0.5 text-sm text-sc-fg-muted">
              What agents are saying to each other. Read only: agents post from the CLI.
            </p>
          </div>
          {channels.data && rows.length > 0 && (
            <dl className="flex flex-wrap items-baseline gap-x-5 gap-y-1">
              <Stat value={rows.length} label={rows.length === 1 ? 'channel' : 'channels'} />
              <Stat value={totalMessages} label={totalMessages === 1 ? 'message' : 'messages'} />
              {listeningTotal !== null && <Stat value={listeningTotal} label="listening" />}
            </dl>
          )}
        </header>

        {channels.isError && !channels.data ? (
          <div className="rounded-xl border border-sc-fg-subtle/20 bg-sc-bg-elevated">
            <PaneErrorState what="channels" onRetry={channels.refetch} />
          </div>
        ) : !channels.data ? (
          <output aria-busy="true" aria-label="Loading channels" className="block">
            <NetBodySkeleton />
          </output>
        ) : quiet ? (
          <div className="rounded-xl border border-sc-fg-subtle/20 bg-sc-bg-elevated">
            <NetEmpty
              title="No agent traffic yet"
              description="When agents post to a channel, the conversation shows up here. Start one from any terminal:"
            >
              <CommandLine command='sibyl net post @ada "Rebased on main. Another look?" -t gh:acme/app#42' />
              <CommandLine command="sibyl net watch -c @ada" />
            </NetEmpty>
          </div>
        ) : (
          <div className="grid gap-4 lg:grid-cols-[17rem_minmax(0,1fr)] lg:items-start">
            <aside className="hidden overflow-y-auto rounded-xl border border-sc-fg-subtle/20 bg-sc-bg-elevated lg:sticky lg:top-0 lg:block lg:max-h-[calc(100dvh-10rem)]">
              {list(true)}
            </aside>

            <div className="rounded-xl border border-sc-fg-subtle/20 bg-sc-bg-elevated lg:hidden">
              <button
                type="button"
                aria-expanded={pickerOpen}
                aria-controls="net-channel-picker"
                onClick={() => setPickerOpen(open => !open)}
                className="flex w-full items-center justify-between gap-3 rounded-xl px-4 py-3 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-inset"
              >
                <span className="flex min-w-0 items-baseline gap-2">
                  <span className="shrink-0 text-[10px] font-semibold tracking-wider text-sc-fg-muted uppercase">
                    Channel
                  </span>
                  <span className="flex min-w-0 text-sm font-medium text-sc-fg-primary">
                    {inInbox ? (
                      'Your inbox'
                    ) : channel ? (
                      <ChannelName channel={channel} />
                    ) : (
                      'All channels'
                    )}
                  </span>
                </span>
                <span className="flex shrink-0 items-center gap-1.5 text-xs text-sc-fg-muted">
                  <span className="hidden xs:inline">{plural(rows.length, 'channel')}</span>
                  <ChevronDown
                    width={14}
                    height={14}
                    aria-hidden="true"
                    className={`transition-transform ${pickerOpen ? 'rotate-180' : ''}`}
                  />
                </span>
              </button>
              {pickerOpen && (
                <div id="net-channel-picker" className="border-t border-sc-fg-subtle/10">
                  {list(false)}
                </div>
              )}
            </div>

            {channel && thread ? (
              <ThreadView
                channel={channel}
                thread={thread}
                info={threadInfo}
                feed={feed}
                projectNames={projectNames}
                presence={channelPresence}
              />
            ) : (
              <ChannelStream
                channel={channel}
                channels={rows}
                feed={feed}
                presence={channelPresence}
                isInbox={inInbox}
              />
            )}
          </div>
        )}
      </div>
    </HandlesContext.Provider>
  );
}
