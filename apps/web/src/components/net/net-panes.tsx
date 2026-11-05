'use client';

import Link from 'next/link';
import { type ReactNode, useId, useMemo, useState } from 'react';
import { Button } from '@/components/ui/button';
import { ErrorState } from '@/components/ui/empty-state';
import {
  AntennaSignal,
  ArrowLeft,
  CheckCircle,
  ChevronDown,
  Eye,
  Folder,
} from '@/components/ui/icons';
import { Skeleton } from '@/components/ui/spinner';
import type { NetChannel, NetFeedResponse, NetMessage, NetThread } from '@/lib/api/net';
import { formatActivityTime } from '@/lib/constants/activity';
import { formatDateTime } from '@/lib/constants/formatting';
import {
  authorName,
  type ChannelPresence,
  isRemembered,
  netHref,
  plural,
  spanPhrase,
} from './net-format';
import { NetMessageList } from './net-message';
import { AgentLabel, AuthorMark, ChannelName, CommandLine } from './net-primitives';

export interface NetQueryState<T> {
  data: T | undefined;
  isLoading: boolean;
  isError: boolean;
  refetch: () => void;
}

const PANE = 'min-w-0 rounded-xl border border-sc-fg-subtle/20 bg-sc-bg-elevated';
const FOCUS =
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated';

/** `#reviews` starts a comment in a shell, so it is quoted. */
export function shellChannel(channel: string): string {
  return /^[@a-z0-9._-]+$/.test(channel) ? channel : `'${channel}'`;
}

function shellThread(thread: string): string {
  return /^[A-Za-z0-9._:/@-]+$/.test(thread) ? thread : `'${thread.replace(/'/g, `'\\''`)}'`;
}

/** Centered, quiet, and it teaches the command that would fill it. */
export function NetEmpty({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center px-4 py-14 text-center animate-fade-in">
      <span className="mb-4 flex h-12 w-12 items-center justify-center rounded-xl border border-sc-fg-subtle/20 bg-sc-bg-highlight text-sc-fg-muted">
        <AntennaSignal width={22} height={22} aria-hidden="true" />
      </span>
      <h3 className="text-base font-semibold text-sc-fg-primary">{title}</h3>
      <p className="mt-1.5 max-w-md text-sm text-sc-fg-muted">{description}</p>
      {children && <div className="mt-5 w-full max-w-xl space-y-2">{children}</div>}
    </div>
  );
}

function PaneSkeleton({ rows = 5 }: { rows?: number }) {
  return (
    <output aria-busy="true" aria-label="Loading messages" className="block">
      {Array.from({ length: rows }).map((_, index) => (
        <div key={index} className="flex gap-3 border-b border-sc-fg-subtle/10 px-4 py-3.5">
          <Skeleton className="h-7 w-7 rounded-full" />
          <div className="flex-1 space-y-2">
            <Skeleton className="h-3.5 w-40" />
            <Skeleton className="h-3 w-full max-w-lg" />
            <Skeleton className="h-3 w-2/3 max-w-md" />
          </div>
        </div>
      ))}
    </output>
  );
}

export function PaneErrorState({ what, onRetry }: { what: string; onRetry: () => void }) {
  return (
    <ErrorState
      title={`Couldn't load ${what}`}
      message="The API did not answer. Check it is reachable, then retry."
      action={<Button onClick={onRetry}>Retry</Button>}
    />
  );
}

function Meta({ children }: { children: ReactNode }) {
  return (
    <p className="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs text-sc-fg-muted">
      {children}
    </p>
  );
}

function Dot() {
  return <span aria-hidden="true">·</span>;
}

// ---------------------------------------------------------------------------
// Presence
// ---------------------------------------------------------------------------

function PresenceToggle({
  presence,
  open,
  onToggle,
  panelId,
}: {
  presence: ChannelPresence;
  open: boolean;
  onToggle: () => void;
  panelId: string;
}) {
  const count = presence.watching.length;
  return (
    <button
      type="button"
      aria-expanded={open}
      aria-controls={panelId}
      onClick={onToggle}
      className={`inline-flex shrink-0 items-center gap-1.5 rounded-md border px-2 py-1 text-xs transition-colors ${FOCUS} ${
        open
          ? 'border-sc-fg-subtle/40 bg-sc-bg-highlight text-sc-fg-primary'
          : 'border-sc-fg-subtle/25 text-sc-fg-secondary hover:bg-sc-bg-highlight'
      }`}
    >
      <Eye width={13} height={13} aria-hidden="true" className="text-sc-fg-muted" />
      {count === 0 ? 'No one watching' : `${count} watching`}
      <ChevronDown
        width={12}
        height={12}
        aria-hidden="true"
        className={`text-sc-fg-muted transition-transform ${open ? 'rotate-180' : ''}`}
      />
    </button>
  );
}

function PresencePanel({ presence, panelId }: { presence: ChannelPresence; panelId: string }) {
  const rows = (
    title: string,
    people: { key: string; name: string; seed: string; agent: string | null; at: string }[]
  ) =>
    people.length > 0 && (
      <div>
        <h3 className="mb-1.5 text-[10px] font-semibold tracking-wider text-sc-fg-muted uppercase">
          {title}
        </h3>
        <ul className="space-y-1.5">
          {people.map(person => (
            <li key={person.key} className="flex min-w-0 items-center gap-2 text-xs">
              <AuthorMark name={person.name} seed={person.seed} />
              <span className="min-w-0 truncate font-medium text-sc-fg-primary">{person.name}</span>
              {person.agent && <AgentLabel agent={person.agent} />}
              <time
                dateTime={person.at}
                title={formatDateTime(person.at)}
                className="ml-auto shrink-0 text-sc-fg-muted tabular-nums"
              >
                {formatActivityTime(person.at)}
              </time>
            </li>
          ))}
        </ul>
      </div>
    );

  const watching = presence.watching.map(watcher => ({
    key: `${watcher.channel}:${watcher.user_id}:${watcher.agent ?? ''}:${watcher.session ?? ''}`,
    name: authorName({ name: watcher.name, email: null, user_id: watcher.user_id }),
    seed: watcher.user_id,
    agent: watcher.agent,
    at: watcher.last_seen,
  }));
  const recent = presence.recent.map(author => ({
    key: `${author.channel}:${author.user_id}:${author.agent ?? ''}`,
    name: authorName({ name: author.name, email: null, user_id: author.user_id }),
    seed: author.user_id,
    agent: author.agent,
    at: author.last_post_at,
  }));

  return (
    <div
      id={panelId}
      className="grid gap-4 border-b border-sc-fg-subtle/10 bg-sc-bg-base/60 px-4 py-3 sm:grid-cols-2"
    >
      {watching.length === 0 && recent.length === 0 ? (
        <p className="text-xs text-sc-fg-muted sm:col-span-2">
          No agent has read or posted here in the last hour.
        </p>
      ) : (
        <>
          {rows('Watching now', watching)}
          {rows('Posted in the last hour', recent)}
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Channel stream
// ---------------------------------------------------------------------------

export interface ChannelStreamProps {
  /** Null for every channel in the organization. */
  channel: string | null;
  /** Every channel's summary; the stream reads its own row, or all of them. */
  channels: NetChannel[];
  feed: NetQueryState<NetFeedResponse>;
  /** Null hides presence: the server has none to offer. */
  presence: ChannelPresence | null;
}

export function ChannelStream({ channel, channels, feed, presence }: ChannelStreamProps) {
  const [presenceOpen, setPresenceOpen] = useState(false);
  const panelId = useId();
  const summary = channel ? channels.find(row => row.channel === channel) : undefined;
  const messageTotal = channel
    ? (summary?.message_count ?? 0)
    : channels.reduce((sum, row) => sum + row.message_count, 0);
  const lastAt = channel
    ? summary?.last_message_at
    : channels.reduce<string | null>(
        (latest, row) =>
          row.last_message_at && (!latest || row.last_message_at > latest)
            ? row.last_message_at
            : latest,
        null
      );
  const messages = feed.data?.messages ?? [];
  const truncated = feed.data !== undefined && messageTotal > messages.length;

  return (
    <section aria-labelledby="net-stream-heading" className={PANE}>
      <header className="flex flex-wrap items-start justify-between gap-3 border-b border-sc-fg-subtle/10 px-4 py-3.5">
        <div className="min-w-0">
          <h2
            id="net-stream-heading"
            className="flex min-w-0 text-lg font-semibold text-sc-fg-primary"
          >
            {channel ? <ChannelName channel={channel} /> : 'All channels'}
          </h2>
          <Meta>
            <span>{plural(messageTotal, 'message')}</span>
            {channel ? (
              <>
                <Dot />
                <span>{plural(summary?.thread_count ?? 0, 'thread')}</span>
              </>
            ) : (
              <>
                <Dot />
                <span>{plural(channels.length, 'channel')}</span>
              </>
            )}
            {lastAt && (
              <>
                <Dot />
                <span>
                  last post{' '}
                  <time dateTime={lastAt} title={formatDateTime(lastAt)}>
                    {formatActivityTime(lastAt)}
                  </time>
                </span>
              </>
            )}
          </Meta>
        </div>
        {presence && (
          <PresenceToggle
            presence={presence}
            open={presenceOpen}
            onToggle={() => setPresenceOpen(open => !open)}
            panelId={panelId}
          />
        )}
      </header>
      {presence && presenceOpen && <PresencePanel presence={presence} panelId={panelId} />}

      {feed.isError && !feed.data ? (
        <PaneErrorState what="messages" onRetry={feed.refetch} />
      ) : !feed.data ? (
        <PaneSkeleton />
      ) : messages.length === 0 ? (
        <NetEmpty
          title={channel ? `Nothing in ${channel} yet` : 'No messages yet'}
          description="Agents post here from the CLI, and this page shows what they say."
        >
          <CommandLine
            command={`sibyl net post ${shellChannel(channel ?? '@ada')} "Rebased on main. Another look when you can?"`}
          />
        </NetEmpty>
      ) : (
        <>
          <NetMessageList messages={messages} scope={channel ? 'channel' : 'all'} />
          {truncated && (
            <footer className="border-t border-sc-fg-subtle/10 px-4 py-3 text-xs text-sc-fg-muted">
              <p>
                Showing the latest {messages.length.toLocaleString('en-US')} of{' '}
                {plural(messageTotal, 'message')}. Read further back from a terminal:
              </p>
              <div className="mt-2">
                <CommandLine
                  command={`sibyl net read ${channel ? `${shellChannel(channel)} ` : ''}-n 500`}
                />
              </div>
            </footer>
          )}
        </>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Thread
// ---------------------------------------------------------------------------

function participantsPhrase(messages: NetMessage[]): string | null {
  const names: string[] = [];
  const seen = new Set<string>();
  for (const message of messages) {
    if (seen.has(message.author.user_id)) continue;
    seen.add(message.author.user_id);
    names.push(authorName(message.author));
  }
  if (names.length === 0) return null;
  if (names.length === 1) return `from ${names[0]}`;
  if (names.length === 2) return `between ${names[0]} and ${names[1]}`;
  return `among ${names.length} people`;
}

function ThreadDetails({ info, projectName }: { info: NetThread; projectName?: string }) {
  return (
    <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1.5 border-b border-sc-fg-subtle/10 bg-sc-bg-base/60 px-4 py-3 text-xs">
      <dt className="text-sc-fg-muted">Project</dt>
      <dd className="min-w-0 truncate text-sc-fg-secondary">
        {info.project_id
          ? (projectName ?? info.project_id)
          : "Not pinned, so each author's part is remembered privately"}
      </dd>
      <dt className="text-sc-fg-muted">Remembered through</dt>
      <dd className="text-sc-fg-secondary">
        {info.reflected_through ? formatDateTime(info.reflected_through) : 'Not distilled yet'}
      </dd>
      {info.reflection_error && (
        <>
          <dt className="text-sc-fg-muted">Last distillation</dt>
          <dd className="min-w-0 break-words text-sc-red">{info.reflection_error}</dd>
        </>
      )}
      {info.reflection_skipped.length > 0 && (
        <>
          <dt className="text-sc-fg-muted">Skipped</dt>
          <dd className="min-w-0 space-y-0.5 text-sc-fg-secondary">
            {info.reflection_skipped.map(entry => (
              <p key={entry} className="break-words">
                {entry}
              </p>
            ))}
          </dd>
        </>
      )}
    </dl>
  );
}

export interface ThreadViewProps {
  channel: string;
  thread: string;
  /** The thread's row from the channel's thread list, once loaded. */
  info: NetThread | undefined;
  feed: NetQueryState<NetFeedResponse>;
  projectNames: Record<string, string>;
}

export function ThreadView({ channel, thread, info, feed, projectNames }: ThreadViewProps) {
  const [detailsOpen, setDetailsOpen] = useState(false);
  const detailsId = useId();
  const messages = feed.data?.messages ?? [];
  const total = info?.message_count ?? messages.length;
  const span = spanPhrase(
    info?.first_message_at ?? messages[0]?.created_at ?? null,
    info?.last_message_at ?? messages[messages.length - 1]?.created_at ?? null
  );
  const participants = useMemo(() => participantsPhrase(messages), [messages]);
  const projectName = info?.project_id ? projectNames[info.project_id] : undefined;
  const remembered = info !== undefined && isRemembered(info);

  return (
    <section aria-labelledby="net-thread-heading" className={PANE}>
      <header className="border-b border-sc-fg-subtle/10 px-4 py-3.5">
        <Link
          href={netHref(channel)}
          className={`inline-flex max-w-full items-center gap-1 rounded text-xs font-medium text-sc-fg-muted hover:text-sc-fg-primary ${FOCUS}`}
        >
          <ArrowLeft width={12} height={12} aria-hidden="true" className="shrink-0" />
          <ChannelName channel={channel} />
        </Link>
        <div className="mt-1.5 flex flex-wrap items-start justify-between gap-x-3 gap-y-2">
          <h2
            id="net-thread-heading"
            className="min-w-0 font-mono text-base font-semibold break-all text-sc-fg-primary"
          >
            {thread}
          </h2>
          <div className="flex shrink-0 items-center gap-2">
            {remembered && (
              <span
                title="Every message here has been distilled into memory"
                className="inline-flex items-center gap-1 text-xs text-sc-fg-muted"
              >
                <CheckCircle width={13} height={13} aria-hidden="true" className="text-sc-green" />
                Remembered
              </span>
            )}
            {info && (
              <button
                type="button"
                aria-expanded={detailsOpen}
                aria-controls={detailsId}
                onClick={() => setDetailsOpen(open => !open)}
                className={`inline-flex items-center gap-1 rounded-md border border-sc-fg-subtle/25 px-2 py-1 text-xs text-sc-fg-secondary hover:bg-sc-bg-highlight ${FOCUS}`}
              >
                Details
                <ChevronDown
                  width={12}
                  height={12}
                  aria-hidden="true"
                  className={`text-sc-fg-muted transition-transform ${detailsOpen ? 'rotate-180' : ''}`}
                />
              </button>
            )}
          </div>
        </div>
        {feed.data && (
          <>
            {/* What happened reads as one sentence; when, and where it is remembered, below it. */}
            <p className="mt-1 text-xs text-sc-fg-secondary">
              {plural(total, 'message')}
              {participants ? ` ${participants}` : ''}
              {span ? ` ${span}` : ''}
            </p>
            {(info?.last_message_at || projectName) && (
              <Meta>
                {info?.last_message_at && (
                  <span>
                    last post{' '}
                    <time
                      dateTime={info.last_message_at}
                      title={formatDateTime(info.last_message_at)}
                    >
                      {formatActivityTime(info.last_message_at)}
                    </time>
                  </span>
                )}
                {projectName && (
                  <span className="inline-flex max-w-[14rem] items-center gap-1 rounded border border-sc-fg-subtle/25 bg-sc-bg-highlight px-1.5 py-px text-[10.5px] text-sc-fg-secondary">
                    <Folder width={10} height={10} aria-hidden="true" className="shrink-0" />
                    <span className="truncate">{projectName}</span>
                  </span>
                )}
              </Meta>
            )}
          </>
        )}
      </header>
      {info && detailsOpen && (
        <div id={detailsId}>
          <ThreadDetails info={info} projectName={projectName} />
        </div>
      )}

      {feed.isError && !feed.data ? (
        <PaneErrorState what="this thread" onRetry={feed.refetch} />
      ) : !feed.data ? (
        <PaneSkeleton rows={4} />
      ) : messages.length === 0 ? (
        <NetEmpty
          title="No messages in this thread"
          description="A thread starts with its first post. Agents key threads on a pull request, a task, or a slug."
        >
          <CommandLine
            command={`sibyl net post ${shellChannel(channel)} -t ${shellThread(thread)} "First message"`}
          />
        </NetEmpty>
      ) : (
        <>
          <NetMessageList messages={messages} scope="thread" />
          {total > messages.length && (
            <footer className="border-t border-sc-fg-subtle/10 px-4 py-3 text-xs text-sc-fg-muted">
              Showing the latest {messages.length.toLocaleString('en-US')} of{' '}
              {plural(total, 'message')}.
            </footer>
          )}
        </>
      )}
    </section>
  );
}
