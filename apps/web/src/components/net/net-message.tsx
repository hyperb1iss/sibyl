'use client';

import Link from 'next/link';
import { useMemo } from 'react';
import { ArrowRight } from '@/components/ui/icons';
import type { NetMessage } from '@/lib/api/net';
import { activityDayKey, formatActivityDay, formatActivityTime } from '@/lib/constants/activity';
import { formatDateTime } from '@/lib/constants/formatting';
import { authorName, decisionMemoryHref, netHref } from './net-format';
import { AgentLabel, AuthorMark, ChannelName, MessageBody } from './net-primitives';

/**
 * Where a list sits decides what each row repeats: the org-wide stream names
 * channel and thread, a channel's stream names the thread, a thread neither.
 */
export type NetListScope = 'all' | 'channel' | 'thread';

function clockTime(iso: string): string {
  return new Date(iso).toLocaleTimeString('en-US', {
    hour: 'numeric',
    minute: '2-digit',
    second: '2-digit',
  });
}

function DecisionLabel() {
  return (
    <span className="inline-flex items-center gap-1.5 text-[10.5px] font-semibold tracking-wider text-sc-purple uppercase">
      <span aria-hidden="true" className="h-1.5 w-1.5 rotate-45 bg-sc-purple" />
      Decision
    </span>
  );
}

const CHIP_FOCUS =
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-1 focus-visible:ring-offset-sc-bg-elevated';

export function NetMessageRow({ message, scope }: { message: NetMessage; scope: NetListScope }) {
  const name = authorName(message.author);
  const decision = message.kind === 'decision';
  const showChannel = scope === 'all';
  const showThread = scope !== 'thread' && message.thread !== null;

  return (
    <article
      aria-label={decision ? `Decision from ${name}` : `Message from ${name}`}
      className={`relative grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 px-4 py-2.5 ${
        decision ? 'bg-sc-purple/[0.045]' : ''
      }`}
    >
      {decision && (
        <span aria-hidden="true" className="absolute inset-y-0 left-0 w-0.5 bg-sc-purple" />
      )}
      <AuthorMark name={name} seed={message.author.user_id} />
      <div className="min-w-0">
        <header className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5">
          <span className="min-w-0 truncate text-sm font-semibold text-sc-fg-primary">{name}</span>
          {message.author.agent && (
            <AgentLabel agent={message.author.agent} session={message.author.session} />
          )}
          {decision && <DecisionLabel />}
          <time
            dateTime={message.created_at}
            title={formatDateTime(message.created_at)}
            className="ml-auto shrink-0 pl-2 text-xs whitespace-nowrap text-sc-fg-muted tabular-nums"
          >
            {scope === 'thread'
              ? clockTime(message.created_at)
              : formatActivityTime(message.created_at)}
          </time>
        </header>

        {(showChannel || showThread) && (
          <div className="mt-0.5 flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-1 text-xs">
            {showChannel && (
              <Link
                href={netHref(message.channel)}
                className={`inline-flex min-w-0 max-w-[14rem] rounded font-medium text-sc-fg-secondary hover:text-sc-fg-primary ${CHIP_FOCUS}`}
              >
                <ChannelName channel={message.channel} />
              </Link>
            )}
            {showChannel && showThread && (
              <span aria-hidden="true" className="text-sc-fg-subtle">
                /
              </span>
            )}
            {showThread && message.thread && (
              <Link
                href={netHref(message.channel, message.thread)}
                title={`Open thread ${message.thread}`}
                className={`inline-flex min-w-0 max-w-[18rem] rounded font-mono text-[11px] text-sc-cyan hover:underline ${CHIP_FOCUS}`}
              >
                <span className="truncate">{message.thread}</span>
              </Link>
            )}
          </div>
        )}

        <div className="mt-1">
          <MessageBody body={message.body} />
        </div>

        {decision && message.decision_memory_id && (
          <Link
            href={decisionMemoryHref(message.decision_memory_id)}
            className={`mt-2 inline-flex items-center gap-1 rounded text-xs font-medium text-sc-purple hover:underline ${CHIP_FOCUS}`}
          >
            Saved to memory
            <ArrowRight width={12} height={12} aria-hidden="true" />
          </Link>
        )}
      </div>
    </article>
  );
}

interface DayGroup {
  key: string;
  label: string;
  messages: NetMessage[];
}

/**
 * Messages under one heading per day. The feed answers oldest first; a
 * stream reads newest first like the rest of the app, a thread reads as the
 * conversation it is.
 */
export function NetMessageList({
  messages,
  scope,
}: {
  messages: NetMessage[];
  scope: NetListScope;
}) {
  const groups = useMemo(() => {
    const ordered = scope === 'thread' ? messages : [...messages].reverse();
    const days: DayGroup[] = [];
    for (const message of ordered) {
      const key = activityDayKey(message.created_at);
      const last = days[days.length - 1];
      if (last && last.key === key) last.messages.push(message);
      else
        days.push({
          key,
          label: formatActivityDay(message.created_at),
          messages: [message],
        });
    }
    return days;
  }, [messages, scope]);

  return (
    <div className="divide-y divide-sc-fg-subtle/10">
      {groups.map(group => (
        <section key={group.key} aria-label={group.label}>
          <h3 className="flex items-center justify-between border-b border-sc-fg-subtle/10 bg-sc-bg-highlight/50 px-4 py-1.5 text-[11px] font-semibold tracking-wider text-sc-fg-muted uppercase">
            <span>{group.label}</span>
            <span className="font-medium tabular-nums">{group.messages.length}</span>
          </h3>
          <ol className="divide-y divide-sc-fg-subtle/10">
            {group.messages.map(message => (
              <li key={message.id}>
                <NetMessageRow message={message} scope={scope} />
              </li>
            ))}
          </ol>
        </section>
      ))}
    </div>
  );
}
