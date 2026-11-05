import type {
  NetAuthor,
  NetMessage,
  NetPresenceResponse,
  NetRecentAuthor,
  NetThread,
  NetWatcher,
} from '@/lib/api/net';

/** `@ada` is someone's agent inbox, `#reviews` a shared room, anything else a bare name. */
export function channelParts(channel: string): { sigil: '@' | '#' | ''; name: string } {
  const first = channel.charAt(0);
  if (first === '@' || first === '#') return { sigil: first, name: channel.slice(1) };
  return { sigil: '', name: channel };
}

/** The Net page at a channel, or a thread in it; no channel means every channel. */
export function netHref(channel?: string | null, thread?: string | null): string {
  const params = new URLSearchParams();
  if (channel) params.set('channel', channel);
  if (channel && thread) params.set('thread', thread);
  const query = params.toString();
  return query ? `/net?${query}` : '/net';
}

/** A message that decided something links to the memory it was saved as. */
export function decisionMemoryHref(memoryId: string): string {
  return `/memory/captures?id=${encodeURIComponent(memoryId)}`;
}

export function authorName(author: Pick<NetAuthor, 'name' | 'email' | 'user_id'>): string {
  return author.name?.trim() || author.email?.trim() || `user ${author.user_id.slice(0, 8)}`;
}

// ---------------------------------------------------------------------------
// Bodies
// ---------------------------------------------------------------------------

/**
 * Characters that change how text around them displays without being seen:
 * C0 and C1 controls other than tab and newline, DEL, and the bidi
 * embeddings, overrides and isolates that can make a line read differently
 * than it is written.
 */
// biome-ignore lint/suspicious/noControlCharactersInRegex: finding control characters is the point
const HIDDEN_CONTROL = /[\u0000-\u0008\u000B-\u001F\u007F-\u009F‪-‮⁦-⁩]/g;

export type BodySegment = { kind: 'text'; text: string } | { kind: 'control'; code: number };

const C0_NAMES = [
  'NUL',
  'SOH',
  'STX',
  'ETX',
  'EOT',
  'ENQ',
  'ACK',
  'BEL',
  'BS',
  'HT',
  'LF',
  'VT',
  'FF',
  'CR',
  'SO',
  'SI',
  'DLE',
  'DC1',
  'DC2',
  'DC3',
  'DC4',
  'NAK',
  'SYN',
  'ETB',
  'CAN',
  'EM',
  'SUB',
  'ESC',
  'FS',
  'GS',
  'RS',
  'US',
];

const BIDI_NAMES: Record<number, string> = {
  8234: 'LRE',
  8235: 'RLE',
  8236: 'PDF',
  8237: 'LRO',
  8238: 'RLO',
  8294: 'LRI',
  8295: 'RLI',
  8296: 'FSI',
  8297: 'PDI',
};

/** The short name a control character goes by: ESC, BEL, RLO, or its code point. */
export function controlName(code: number): string {
  if (code < 0x20) return C0_NAMES[code];
  if (code === 0x7f) return 'DEL';
  return BIDI_NAMES[code] ?? controlLabel(code);
}

export function controlLabel(code: number): string {
  return `U+${code.toString(16).toUpperCase().padStart(4, '0')}`;
}

/**
 * Split a body into plain text and the hidden control characters inside it,
 * so they render as visible marks instead of acting on the text. Windows line
 * endings become plain newlines first.
 */
export function bodySegments(body: string): BodySegment[] {
  const text = body.replace(/\r\n/g, '\n');
  const segments: BodySegment[] = [];
  let last = 0;
  for (const match of text.matchAll(HIDDEN_CONTROL)) {
    const index = match.index ?? 0;
    if (index > last) segments.push({ kind: 'text', text: text.slice(last, index) });
    segments.push({ kind: 'control', code: match[0].charCodeAt(0) });
    last = index + match[0].length;
  }
  if (last < text.length) segments.push({ kind: 'text', text: text.slice(last) });
  return segments;
}

export const BODY_PREVIEW_LINES = 8;
export const BODY_PREVIEW_CHARS = 640;

/**
 * The opening of a long body, or null when it is short enough to show whole.
 * Collapsing only pays when it hides a real amount, so a body a line or a
 * few words over the preview shows whole.
 */
export function bodyPreview(body: string): { preview: string; lines: number } | null {
  const normalized = body.replace(/\r\n/g, '\n');
  const lines = normalized.split('\n');
  const overLines = lines.length > BODY_PREVIEW_LINES + 2;
  const overChars = normalized.length > BODY_PREVIEW_CHARS * 1.25;
  if (!overLines && !overChars) return null;
  let preview = lines.slice(0, BODY_PREVIEW_LINES).join('\n');
  if (preview.length > BODY_PREVIEW_CHARS) {
    const cut = preview.lastIndexOf(' ', BODY_PREVIEW_CHARS);
    preview = preview.slice(0, cut > BODY_PREVIEW_CHARS / 2 ? cut : BODY_PREVIEW_CHARS);
  }
  return { preview: preview.trimEnd(), lines: lines.length };
}

// ---------------------------------------------------------------------------
// Runs: the loop signal
// ---------------------------------------------------------------------------

/** Fewer than this many repeats reads as a double post, not a loop. */
export const MIN_FOLD_RUN = 3;

/** A body with outer whitespace trimmed and every inner run collapsed to one space. */
export function normalizeBody(body: string): string {
  return body.trim().replace(/\s+/g, ' ');
}

/**
 * Two messages belong to one run when the same author's same agent said the
 * same thing in the same place: same channel and thread, and bodies equal
 * once whitespace is collapsed and every number is masked, so "retry 3 of
 * 5" and "retry 4 of 5" match. Decisions never fold.
 */
function runKey(message: NetMessage): string | null {
  if (message.kind !== 'message') return null;
  return [
    message.channel,
    message.thread ?? '',
    message.author.user_id,
    message.author.agent ?? '',
    normalizeBody(message.body).replace(/\d+/g, '#'),
  ].join('\u0000');
}

export type NetListItem =
  | { kind: 'message'; message: NetMessage }
  | {
      kind: 'run';
      /** In list order. */
      messages: NetMessage[];
      /** Every body is the same text, not only the same up to numbers. */
      identical: boolean;
    };

/**
 * Fold runs of `minRun` or more consecutive near-identical messages into one
 * item, keeping list order. Anything that breaks the run (another author, a
 * different body, a decision) ends it.
 */
export function foldRuns(messages: NetMessage[], minRun = MIN_FOLD_RUN): NetListItem[] {
  const items: NetListItem[] = [];
  let index = 0;
  while (index < messages.length) {
    const key = runKey(messages[index]);
    let end = index + 1;
    if (key !== null) {
      while (end < messages.length && runKey(messages[end]) === key) end += 1;
    }
    const run = messages.slice(index, end);
    if (run.length >= minRun) {
      const first = normalizeBody(run[0].body);
      items.push({
        kind: 'run',
        messages: run,
        identical: run.every(message => normalizeBody(message.body) === first),
      });
    } else {
      for (const message of run) items.push({ kind: 'message', message });
    }
    index = end;
  }
  return items;
}

/** "in 5 seconds", "over 6 minutes": how long a run of messages took. */
export function runSpanPhrase(messages: NetMessage[]): string {
  const times = messages.map(message => Date.parse(message.created_at));
  const earliest = Math.min(...times);
  const latest = Math.max(...times);
  const seconds = (latest - earliest) / 1000;
  if (!Number.isFinite(seconds) || seconds < 1) return 'within a second';
  if (seconds < 90) return `in ${Math.round(seconds)} seconds`;
  return spanPhrase(new Date(earliest).toISOString(), new Date(latest).toISOString()) ?? '';
}

// ---------------------------------------------------------------------------
// Threads
// ---------------------------------------------------------------------------

/** Every message in the thread has been distilled into memory. */
export function isRemembered(thread: Pick<NetThread, 'reflected_through' | 'last_message_at'>) {
  if (!thread.reflected_through || !thread.last_message_at) return false;
  return Date.parse(thread.reflected_through) >= Date.parse(thread.last_message_at);
}

/** "over 6 minutes", the time between a thread's first and last message. */
export function spanPhrase(firstIso: string | null, lastIso: string | null): string | null {
  if (!firstIso || !lastIso) return null;
  const seconds = Math.max(0, (Date.parse(lastIso) - Date.parse(firstIso)) / 1000);
  if (!Number.isFinite(seconds) || seconds < 1) return null;
  if (seconds < 90) return `over ${Math.round(seconds)} seconds`;
  const minutes = seconds / 60;
  if (minutes < 90) return `over ${Math.round(minutes)} minutes`;
  const hours = minutes / 60;
  if (hours < 36) return `over ${Math.round(hours)} hours`;
  return `over ${Math.round(hours / 24)} days`;
}

export function plural(count: number, one: string, many = `${one}s`): string {
  return `${count.toLocaleString('en-US')} ${count === 1 ? one : many}`;
}

// ---------------------------------------------------------------------------
// Presence
// ---------------------------------------------------------------------------

export interface ChannelPresence {
  watching: NetWatcher[];
  recent: NetRecentAuthor[];
}

/**
 * Presence for one channel, or for the organization when `channel` is null:
 * one entry per reader (user, agent label and session) and per recent
 * author (user and agent label), most recent first.
 */
export function presenceFor(
  presence: NetPresenceResponse,
  channel: string | null
): ChannelPresence {
  const watching = new Map<string, NetWatcher>();
  for (const watcher of presence.watching) {
    if (channel && watcher.channel !== channel) continue;
    const key = `${watcher.user_id}\u0000${watcher.agent ?? ''}\u0000${watcher.session ?? ''}`;
    const seen = watching.get(key);
    if (!seen || seen.last_seen < watcher.last_seen) watching.set(key, watcher);
  }
  const recent = new Map<string, NetRecentAuthor>();
  for (const author of presence.recent) {
    if (channel && author.channel !== channel) continue;
    const key = `${author.user_id}\u0000${author.agent ?? ''}`;
    const seen = recent.get(key);
    if (!seen || seen.last_post_at < author.last_post_at) recent.set(key, author);
  }
  return {
    watching: [...watching.values()].sort((a, b) => b.last_seen.localeCompare(a.last_seen)),
    recent: [...recent.values()].sort((a, b) => b.last_post_at.localeCompare(a.last_post_at)),
  };
}

/** Readers per channel, for the channel list. */
export function watchingCounts(presence: NetPresenceResponse | null): Map<string, number> {
  const counts = new Map<string, number>();
  if (!presence) return counts;
  const seen = new Set<string>();
  for (const watcher of presence.watching) {
    const key = `${watcher.channel}\u0000${watcher.user_id}\u0000${watcher.agent ?? ''}\u0000${watcher.session ?? ''}`;
    if (seen.has(key)) continue;
    seen.add(key);
    counts.set(watcher.channel, (counts.get(watcher.channel) ?? 0) + 1);
  }
  return counts;
}
