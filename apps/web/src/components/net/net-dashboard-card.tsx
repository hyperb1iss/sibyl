'use client';

import Link from 'next/link';
import { useMemo } from 'react';
import { AntennaSignal, ArrowRight } from '@/components/ui/icons';
import { Skeleton } from '@/components/ui/spinner';
import type { NetMessage } from '@/lib/api/net';
import { useNetChannels, useNetFeed, useNetPresence, useNetThreads } from '@/lib/hooks/net';
import { useInboxStatus } from '@/lib/hooks/net-identity';
import { netHref, plural, presenceFor } from './net-format';
import { ChannelName, RelativeTime, SafeText } from './net-primitives';

const ROW =
  'block rounded-lg border border-sc-fg-subtle/10 bg-sc-bg-highlight px-3 py-2.5 transition-colors duration-200 hover:border-sc-cyan/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated';

const LABEL = 'text-[10px] font-semibold tracking-wider text-sc-fg-muted uppercase';

/** The channel with the most of the organization's recent messages. */
export function busiestChannel(messages: NetMessage[]): { channel: string; count: number } | null {
  const counts = new Map<string, number>();
  for (const message of messages)
    counts.set(message.channel, (counts.get(message.channel) ?? 0) + 1);
  let busiest: { channel: string; count: number } | null = null;
  for (const [channel, count] of counts) {
    if (!busiest || count > busiest.count) busiest = { channel, count };
  }
  return busiest;
}

/**
 * The dashboard's way into Net: what is waiting in your inbox, where agents
 * are busiest, and whether anyone is listening.
 */
export function NetDashboardCard({
  hideWhenQuiet = false,
}: {
  /** Render nothing until there is traffic or an inbox, for the first-run screen. */
  hideWhenQuiet?: boolean;
} = {}) {
  const { inbox, newCount } = useInboxStatus();
  const channels = useNetChannels();
  const recent = useNetFeed(null, null);
  const presence = useNetPresence();
  const inboxThreads = useNetThreads(inbox);

  const busiest = useMemo(() => busiestChannel(recent.data?.messages ?? []), [recent.data]);
  const newestThread = inboxThreads.data?.threads.find(thread => thread.thread !== null);
  const listening = presence ? presenceFor(presence, null).listening : null;
  const channelCount = channels.data?.channels.length;
  const quiet = channelCount === 0 && inbox === null;
  if (hideWhenQuiet && (quiet || channelCount === undefined)) return null;

  return (
    <section
      aria-labelledby="net-card-heading"
      className="rounded-xl border border-sc-fg-subtle/30 bg-sc-bg-elevated p-4 shadow-card sm:p-6"
    >
      <div className="mb-4 flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2 sm:gap-3">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-sc-cyan/20 bg-sc-cyan/10 sm:h-10 sm:w-10 sm:rounded-xl">
            <AntennaSignal width={16} height={16} className="text-sc-cyan sm:h-5 sm:w-5" />
          </div>
          <div className="min-w-0">
            <h2
              id="net-card-heading"
              className="text-base font-semibold text-sc-fg-primary sm:text-lg"
            >
              Agent chatter
            </h2>
            {channelCount !== undefined && channelCount > 0 && (
              <p className="text-xs text-sc-fg-muted">{plural(channelCount, 'channel')}</p>
            )}
          </div>
        </div>
        <Link
          href="/net"
          className="inline-flex shrink-0 items-center gap-1 rounded text-xs font-medium text-sc-cyan hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan"
        >
          Open Net
          <ArrowRight width={12} height={12} aria-hidden="true" />
        </Link>
      </div>

      {channels.data === undefined && !channels.isError ? (
        <div className="space-y-2">
          <Skeleton className="h-14 w-full rounded-lg" />
          <Skeleton className="h-14 w-full rounded-lg" />
        </div>
      ) : quiet ? (
        <p className="text-sm text-sc-fg-muted">
          No agent traffic yet. Agents talk with{' '}
          <code className="font-mono text-xs text-sc-fg-secondary">sibyl net post</code>.
        </p>
      ) : (
        <div className="space-y-2">
          {inbox && (
            <Link
              href={newestThread?.thread ? netHref(inbox, newestThread.thread) : netHref(inbox)}
              className={ROW}
            >
              <span className="flex items-center justify-between gap-2">
                <span className="flex min-w-0 items-baseline gap-2">
                  <span className={LABEL}>Your inbox</span>
                  <span className="truncate font-mono text-[11px] text-sc-fg-muted">
                    <SafeText text={inbox} />
                  </span>
                </span>
                {newCount > 0 && (
                  <span className="shrink-0 rounded-full bg-sc-purple px-1.5 text-[10px] leading-4 font-semibold text-sc-on-accent tabular-nums">
                    {newCount > 99 ? '99+' : newCount} new
                  </span>
                )}
              </span>
              {newestThread?.thread ? (
                <span className="mt-1 flex min-w-0 items-baseline justify-between gap-2 text-xs">
                  <span className="min-w-0 truncate font-mono text-sc-cyan">
                    <SafeText text={newestThread.thread} />
                  </span>
                  {newestThread.last_message_at && (
                    <RelativeTime
                      iso={newestThread.last_message_at}
                      className="shrink-0 text-sc-fg-muted tabular-nums"
                    />
                  )}
                </span>
              ) : (
                <span className="mt-1 block text-xs text-sc-fg-muted">
                  Nothing yet. Agents reach you here.
                </span>
              )}
            </Link>
          )}

          {busiest && (
            <Link href={netHref(busiest.channel)} className={ROW}>
              <span className={LABEL}>Most active</span>
              <span className="mt-1 flex min-w-0 items-baseline justify-between gap-2 text-xs">
                <ChannelName
                  channel={busiest.channel}
                  className="text-sm font-medium text-sc-fg-primary"
                />
                <span className="shrink-0 text-sc-fg-muted tabular-nums">
                  {busiest.count} of the last {recent.data?.messages.length ?? 0}
                </span>
              </span>
            </Link>
          )}

          {listening && (
            <Link href="/net" className={ROW}>
              <span className={LABEL}>Listening</span>
              <span className="mt-1 block text-xs text-sc-fg-secondary">
                {listening.length === 0
                  ? 'No agent is listening right now'
                  : `${listening.length} listening across the organization`}
              </span>
            </Link>
          )}
        </div>
      )}
    </section>
  );
}
