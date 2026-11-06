import { fetchApi, type RequestOptions } from './transport';

/** Page size the stream reads; the API caps a page at 500. */
export const NET_STREAM_LIMIT = 100;
export const NET_THREAD_LIMIT = 500;

/**
 * The feed holds back rows younger than this, so a read straight after a
 * post would miss it. Refetches triggered by a post wait this long, plus a
 * margin for the event's trip to the browser.
 */
export const NET_SETTLE_MS = 2000;

export type NetMessageKind = 'message' | 'decision';

/** Who posted: the user Sibyl verified, plus the labels their client sent. */
export interface NetAuthor {
  user_id: string;
  name: string | null;
  email: string | null;
  /** Client-asserted, e.g. `claude-code`; a label, not identity. */
  agent: string | null;
  session: string | null;
}

export interface NetMessage {
  id: string;
  channel: string;
  thread: string | null;
  kind: NetMessageKind;
  /** Untrusted text. Render as plain text only. */
  body: string;
  project_id: string | null;
  author: NetAuthor;
  api_key_id: string | null;
  decision_memory_id: string | null;
  created_at: string;
  cursor: string;
}

export interface NetFeedResponse {
  /** Oldest first. */
  messages: NetMessage[];
  next_since: string | null;
  more: boolean;
}

export interface NetChannel {
  channel: string;
  message_count: number;
  thread_count: number;
  last_message_at: string | null;
}

export interface NetChannelsResponse {
  channels: NetChannel[];
}

export interface NetThread {
  channel: string;
  /** Null for the row counting a channel's threadless messages. */
  thread: string | null;
  project_id: string | null;
  message_count: number;
  first_message_at: string | null;
  last_message_at: string | null;
  reflected_through: string | null;
  reflection_source_id: string | null;
  reflection_error: string | null;
  reflection_skipped: string[];
}

export interface NetThreadsResponse {
  threads: NetThread[];
}

/**
 * An agent that read a channel in the last two minutes, or is waiting on one.
 * Reads made by this web app are not recorded, so a person browsing never
 * shows up as listening.
 */
export interface NetWatcher {
  /** Null for a reader of every channel, which hears any channel too. */
  channel: string | null;
  user_id: string;
  name: string | null;
  agent: string | null;
  session: string | null;
  last_seen: string;
}

/** Someone who posted in a channel within the last hour, under one agent label. */
export interface NetRecentPoster {
  channel: string;
  user_id: string;
  name: string | null;
  agent: string | null;
  last_post_at: string;
}

export interface NetPresenceResponse {
  /** Most recently seen first. */
  watching: NetWatcher[];
  /** Most recent post first. */
  recent: NetRecentPoster[];
}

/** A member of the organization as sibylnet addresses them. */
export interface NetMember {
  user_id: string;
  name: string | null;
  handle: string;
  /** `@<handle>`: post here to reach this person's agents. */
  inbox: string;
  /** One of their agents is reading something right now. */
  listening: boolean;
  last_post_at: string | null;
}

export interface NetDirectoryResponse {
  members: NetMember[];
}

export interface NetFeedParams {
  /** Omit for every channel in the organization. */
  channel?: string | null;
  thread?: string | null;
  limit?: number;
}

export const netApi = {
  channels: (options?: RequestOptions) =>
    fetchApi<NetChannelsResponse>('/net/channels', { signal: options?.signal }),

  /** The newest `limit` messages, oldest first. */
  feed: (params: NetFeedParams, options?: RequestOptions) => {
    const query = new URLSearchParams({ limit: String(params.limit ?? NET_STREAM_LIMIT) });
    if (params.channel) query.set('channel', params.channel);
    if (params.thread) query.set('thread', params.thread);
    return fetchApi<NetFeedResponse>(`/net/feed?${query.toString()}`, {
      signal: options?.signal,
    });
  },

  threads: (channel: string, options?: RequestOptions) =>
    fetchApi<NetThreadsResponse>(`/net/threads?${new URLSearchParams({ channel }).toString()}`, {
      signal: options?.signal,
    }),

  /** Every member's handle and inbox. */
  directory: (options?: RequestOptions) =>
    fetchApi<NetDirectoryResponse>('/net/directory', { signal: options?.signal }),

  /** Who is listening and who posted in the last hour, across the organization. */
  presence: (options?: RequestOptions) =>
    fetchApi<NetPresenceResponse>('/net/presence', { signal: options?.signal }),
};
