'use client';

import Link from 'next/link';
import { memo, type ReactNode, useId, useMemo, useState } from 'react';
import { ArrowRight, RefreshDouble } from '@/components/ui/icons';
import type { NetMessage } from '@/lib/api/net';
import { activityDayKey, formatActivityDay, formatActivityTime } from '@/lib/constants/activity';
import { formatDateTime } from '@/lib/constants/formatting';
import { useNow } from '@/lib/hooks/now';
import {
  authorName,
  decisionMemoryHref,
  foldRuns,
  type NetListItem,
  netHref,
  runSpanPhrase,
  visibleText,
} from './net-format';
import { AgentLabel, AuthorMark, ChannelName, MessageBody, SafeText } from './net-primitives';

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

/** A stream says how long ago; a thread gives the clock, so its cadence shows. */
function MessageTime({ iso, scope }: { iso: string; scope: NetListScope }) {
  const now = useNow();
  return (
    <time
      dateTime={iso}
      title={formatDateTime(iso)}
      className="ml-auto shrink-0 pl-2 text-xs whitespace-nowrap text-sc-fg-muted tabular-nums"
    >
      {scope === 'thread' ? clockTime(iso) : formatActivityTime(iso, now)}
    </time>
  );
}

/** Where a message was said, when the list does not already say it. */
function MessageContext({ message, scope }: { message: NetMessage; scope: NetListScope }) {
  const showChannel = scope === 'all';
  const showThread = scope !== 'thread' && message.thread !== null;
  if (!showChannel && !showThread) return null;
  return (
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
          title={`Open thread ${visibleText(message.thread)}`}
          className={`inline-flex min-w-0 max-w-[18rem] rounded font-mono text-[11px] text-sc-cyan hover:underline ${CHIP_FOCUS}`}
        >
          <span className="truncate">
            <SafeText text={message.thread} />
          </span>
        </Link>
      )}
    </div>
  );
}

/**
 * One message. Memoized: rows only re-render when their message changes,
 * and React Query keeps unchanged messages the same objects across refetches.
 */
export const NetMessageRow = memo(function NetMessageRow({
  message,
  scope,
}: {
  message: NetMessage;
  scope: NetListScope;
}) {
  const name = authorName(message.author);
  const decision = message.kind === 'decision';

  return (
    <article
      aria-label={`${decision ? 'Decision' : 'Message'} from ${visibleText(name)}`}
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
          <span className="min-w-0 truncate text-sm font-semibold text-sc-fg-primary">
            <SafeText text={name} />
          </span>
          {message.author.agent && (
            <AgentLabel agent={message.author.agent} session={message.author.session} />
          )}
          {decision && <DecisionLabel />}
          <MessageTime iso={message.created_at} scope={scope} />
        </header>
        <MessageContext message={message} scope={scope} />
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
});

/**
 * One author's agent saying the same thing again and again, folded into a
 * row with a count: the shape a loop takes. Yellow, because it asks for a
 * look, and quiet, because it is not yet a failure.
 */
function NetRunRowView({ messages, scope }: { messages: NetMessage[]; scope: NetListScope }) {
  const [expanded, setExpanded] = useState(false);
  const listId = useId();
  const latest = messages.reduce((newest, message) =>
    message.created_at > newest.created_at ? message : newest
  );
  const name = authorName(latest.author);
  const count = messages.length;
  const summary = `Same message ${count} times ${runSpanPhrase(messages)}`;

  return (
    <article
      aria-label={`${visibleText(name)} repeated a message ${count} times`}
      className="relative grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 px-4 py-2.5"
    >
      <span aria-hidden="true" className="absolute inset-y-0 left-0 w-0.5 bg-sc-yellow/70" />
      <AuthorMark name={name} seed={latest.author.user_id} />
      <div className="min-w-0">
        <header className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5">
          <span className="min-w-0 truncate text-sm font-semibold text-sc-fg-primary">
            <SafeText text={name} />
          </span>
          {latest.author.agent && (
            <AgentLabel agent={latest.author.agent} session={latest.author.session} />
          )}
          <span
            title={summary}
            className="rounded border border-sc-yellow/40 bg-sc-yellow/10 px-1.5 font-mono text-[11px] leading-[1.125rem] font-semibold text-sc-yellow tabular-nums"
          >
            ×{count}
          </span>
          <MessageTime iso={latest.created_at} scope={scope} />
        </header>
        <MessageContext message={latest} scope={scope} />
        {!expanded && (
          <div className="mt-1">
            <MessageBody body={latest.body} />
          </div>
        )}
        {expanded && (
          <ol
            id={listId}
            aria-label={`${count} messages from ${visibleText(name)}`}
            className="mt-1.5 space-y-1.5 border-l border-sc-yellow/30 pl-3"
          >
            {messages.map(message => (
              <li
                key={message.id}
                className="grid grid-cols-[auto_minmax(0,1fr)] items-baseline gap-x-3"
              >
                <time
                  dateTime={message.created_at}
                  title={formatDateTime(message.created_at)}
                  className="text-[11px] whitespace-nowrap text-sc-fg-muted tabular-nums"
                >
                  {clockTime(message.created_at)}
                </time>
                <MessageBody body={message.body} />
              </li>
            ))}
          </ol>
        )}
        <p className="mt-1.5 flex items-start gap-1.5 text-xs text-sc-fg-muted">
          <RefreshDouble
            width={12}
            height={12}
            aria-hidden="true"
            className="mt-px shrink-0 text-sc-yellow"
          />
          {/* One sentence that wraps as text; the dot stays with the summary. */}
          <span>
            {summary}
            <span aria-hidden="true">{'\u00a0· '}</span>
            <button
              type="button"
              aria-expanded={expanded}
              aria-controls={expanded ? listId : undefined}
              onClick={() => setExpanded(open => !open)}
              className={`rounded font-medium whitespace-nowrap text-sc-cyan hover:underline ${CHIP_FOCUS}`}
            >
              {expanded ? 'Fold' : 'Show each'}
            </button>
          </span>
        </p>
      </div>
    </article>
  );
}

/**
 * Memoized on the run's message objects, not the array holding them: the
 * fold builds a new array on every pass, but the messages inside are the
 * same objects until one of them changes.
 */
export const NetRunRow = memo(
  NetRunRowView,
  (previous, next) =>
    previous.scope === next.scope &&
    previous.messages.length === next.messages.length &&
    previous.messages.every((message, index) => message === next.messages[index])
);

interface DayGroup {
  key: string;
  /** A message from the day, to name it by. */
  sample: string;
  count: number;
  items: NetListItem[];
}

/**
 * One day's heading and rows. The heading reads the shared clock itself, so
 * "Today" turning into "Yesterday" re-renders the heading, not the rows.
 */
function DaySection({
  sample,
  count,
  children,
}: {
  sample: string;
  count: number;
  children: ReactNode;
}) {
  const label = formatActivityDay(sample, useNow());
  return (
    <section aria-label={label}>
      <h3 className="flex items-center justify-between border-b border-sc-fg-subtle/10 bg-sc-bg-highlight/50 px-4 py-1.5 text-[11px] font-semibold tracking-wider text-sc-fg-muted uppercase">
        <span>{label}</span>
        <span className="font-medium tabular-nums">{count}</span>
      </h3>
      {children}
    </section>
  );
}

// A run is keyed by its oldest message, which stays put while newer repeats
// join it, so an expanded run stays expanded as the loop goes on.
function itemKey(item: NetListItem): string {
  if (item.kind === 'message') return item.message.id;
  const oldest = item.messages.reduce((first, message) =>
    message.created_at < first.created_at ? message : first
  );
  return `run:${oldest.id}`;
}

/**
 * Messages under one heading per day, with runs of repeats folded. The feed
 * answers oldest first; a stream reads newest first like the rest of the
 * app, a thread reads as the conversation it is.
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
    const days: { key: string; messages: NetMessage[] }[] = [];
    for (const message of ordered) {
      const key = activityDayKey(message.created_at);
      const last = days[days.length - 1];
      if (last && last.key === key) last.messages.push(message);
      else days.push({ key, messages: [message] });
    }
    return days.map<DayGroup>(day => ({
      key: day.key,
      sample: day.messages[0].created_at,
      count: day.messages.length,
      items: foldRuns(day.messages),
    }));
  }, [messages, scope]);

  return (
    <div className="divide-y divide-sc-fg-subtle/10">
      {groups.map(group => (
        <DaySection key={group.key} sample={group.sample} count={group.count}>
          <ol className="divide-y divide-sc-fg-subtle/10">
            {group.items.map(item => (
              <li key={itemKey(item)}>
                {item.kind === 'message' ? (
                  <NetMessageRow message={item.message} scope={scope} />
                ) : (
                  <NetRunRow messages={item.messages} scope={scope} />
                )}
              </li>
            ))}
          </ol>
        </DaySection>
      ))}
    </div>
  );
}
