import type {
  NetAuthor,
  NetMessage,
  NetPresenceResponse,
  NetRecentPoster,
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
 * Characters that change how text around them displays, or hide text,
 * without being seen: C0 and C1 controls other than tab and newline, DEL,
 * zero-width characters (U+200B-U+200D, U+2060-U+2064, U+FEFF), the bidi
 * marks, embeddings, overrides and isolates that can make a line read
 * differently than it is written (U+061C, U+200E, U+200F, U+202A-U+202E,
 * U+2066-U+2069), and Unicode tag characters (U+E0000-U+E007F), which carry
 * text no font draws. Built from escapes so this source stays readable.
 */
const HIDDEN_CONTROL = new RegExp(
  '[\\u0000-\\u0008\\u000B-\\u001F\\u007F-\\u009F\\u061C\\u200B-\\u200F' +
    '\\u202A-\\u202E\\u2060-\\u2064\\u2066-\\u2069\\uFEFF\\u{E0000}-\\u{E007F}]',
  'gu'
);

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

const INVISIBLE_NAMES = new Map<number, string>([
  [0x061c, 'ALM'],
  [0x200b, 'ZWSP'],
  [0x200c, 'ZWNJ'],
  [0x200d, 'ZWJ'],
  [0x200e, 'LRM'],
  [0x200f, 'RLM'],
  [0x202a, 'LRE'],
  [0x202b, 'RLE'],
  [0x202c, 'PDF'],
  [0x202d, 'LRO'],
  [0x202e, 'RLO'],
  [0x2060, 'WJ'],
  [0x2066, 'LRI'],
  [0x2067, 'RLI'],
  [0x2068, 'FSI'],
  [0x2069, 'PDI'],
  [0xfeff, 'BOM'],
]);

/** The short name a hidden character goes by: ESC, RLO, ZWSP, TAG, or its code point. */
export function controlName(code: number): string {
  if (code < 0x20) return C0_NAMES[code];
  if (code === 0x7f) return 'DEL';
  if (code >= 0xe0000 && code <= 0xe007f) return 'TAG';
  return INVISIBLE_NAMES.get(code) ?? controlLabel(code);
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
    segments.push({ kind: 'control', code: match[0].codePointAt(0) ?? 0 });
    last = index + match[0].length;
  }
  if (last < text.length) segments.push({ kind: 'text', text: text.slice(last) });
  return segments;
}

/**
 * Text for an attribute (a title, an accessible name) that cannot hold a
 * rendered mark: each hidden character becomes its name in brackets.
 */
export function visibleText(text: string): string {
  return text.replace(HIDDEN_CONTROL, char => `[${controlName(char.codePointAt(0) ?? 0)}]`);
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
 * Two messages belong to one run when one agent session said exactly the
 * same thing in the same place: same channel, thread, author, agent label
 * and session, and bodies equal once whitespace is trimmed and collapsed.
 * Nothing looser: masking numbers would hide "12 failed" among "0 failed",
 * so a retry counter that moves stays visible as its own rows. Decisions
 * never fold.
 */
function runKey(message: NetMessage): string | null {
  if (message.kind !== 'message') return null;
  return [
    message.channel,
    message.thread ?? '',
    message.author.user_id,
    message.author.agent ?? '',
    message.author.session ?? '',
    normalizeBody(message.body),
  ].join('\u0000');
}

export type NetListItem =
  | { kind: 'message'; message: NetMessage }
  | {
      kind: 'run';
      /** In list order. */
      messages: NetMessage[];
    };

/**
 * Fold runs of `minRun` or more consecutive identical messages into one
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
      items.push({ kind: 'run', messages: run });
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
  /** One entry per user and agent label, most recently seen first. */
  listening: NetWatcher[];
  /** One entry per user and agent label, most recent post first. */
  recent: NetRecentPoster[];
}

function latestPer<T>(rows: T[], key: (row: T) => string, at: (row: T) => string): T[] {
  const latest = new Map<string, T>();
  for (const row of rows) {
    const seen = latest.get(key(row));
    if (!seen || at(seen) < at(row)) latest.set(key(row), row);
  }
  return [...latest.values()].sort((a, b) => at(b).localeCompare(at(a)));
}

/**
 * Who is listening to `channel` and who posted there in the last hour; with
 * a null channel, everyone across the organization. A reader of every
 * channel hears this one too, so it counts here. Several sessions of one
 * agent are one listener: the question is which agents would see a post.
 */
export function presenceFor(
  presence: NetPresenceResponse,
  channel: string | null
): ChannelPresence {
  const who = (row: { user_id: string; agent: string | null }) =>
    `${row.user_id}\u0000${row.agent ?? ''}`;
  return {
    listening: latestPer(
      presence.watching.filter(
        watcher => channel === null || watcher.channel === channel || watcher.channel === null
      ),
      who,
      watcher => watcher.last_seen
    ),
    recent: latestPer(
      presence.recent.filter(poster => channel === null || poster.channel === channel),
      who,
      poster => poster.last_post_at
    ),
  };
}

/** "Ada Lovelace (claude-code)", the way a listener reads in a tooltip. */
export function listenerLabel(listener: Pick<NetWatcher, 'name' | 'user_id' | 'agent'>): string {
  const name = authorName({ name: listener.name, email: null, user_id: listener.user_id });
  return listener.agent ? `${name} (${listener.agent})` : name;
}
