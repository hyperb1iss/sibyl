import { describe, expect, it } from 'vitest';
import type { NetMessage, NetPresenceResponse } from '@/lib/api/net';
import {
  BODY_PREVIEW_LINES,
  bodyPreview,
  bodySegments,
  controlName,
  foldRuns,
  isRemembered,
  listenerLabel,
  MIN_FOLD_RUN,
  netHref,
  normalizeBody,
  presenceFor,
  runSpanPhrase,
  spanPhrase,
  visibleText,
} from './net-format';
import { shellChannel } from './net-panes';

describe('bodySegments', () => {
  it('keeps newlines and tabs as text and pulls hidden controls out as marks', () => {
    const segments = bodySegments('red \u001b[31mtext\u0007\nnext\tline\r\n‮evil');
    expect(segments).toEqual([
      { kind: 'text', text: 'red ' },
      { kind: 'control', code: 0x1b },
      { kind: 'text', text: '[31mtext' },
      { kind: 'control', code: 0x07 },
      { kind: 'text', text: '\nnext\tline\n' },
      { kind: 'control', code: 0x202e },
      { kind: 'text', text: 'evil' },
    ]);
  });

  it('leaves markup alone: it is text, never parsed', () => {
    expect(bodySegments('<script>alert(1)</script>')).toEqual([
      { kind: 'text', text: '<script>alert(1)</script>' },
    ]);
  });

  it('marks zero-width, bidi mark and tag characters too', () => {
    const tags = String.fromCodePoint(0xe0041, 0xe0042);
    const codes = bodySegments(`a\u200bb\u200c\u200d\u2060\ufeff\u200e\u200f\u061c${tags}z`)
      .filter(segment => segment.kind === 'control')
      .map(segment => (segment.kind === 'control' ? controlName(segment.code) : ''));
    expect(codes).toEqual(['ZWSP', 'ZWNJ', 'ZWJ', 'WJ', 'BOM', 'LRM', 'RLM', 'ALM', 'TAG', 'TAG']);
  });

  it('lets a joiner inside an emoji sequence through in a body, and marks it anywhere else', () => {
    const coder = '\u{1F469}\u200d\u{1F4BB}'; // woman, ZWJ, laptop
    const flag = '\u{1F3F3}\ufe0f\u200d\u{1F308}'; // white flag, VS16, ZWJ, rainbow
    const toned = '\u{1F469}\u{1F3FD}\u200d\u{1F4BB}'; // with a skin-tone modifier
    for (const emoji of [coder, flag, toned]) {
      expect(bodySegments(`shipped ${emoji} done`, { emojiJoins: true })).toEqual([
        { kind: 'text', text: `shipped ${emoji} done` },
      ]);
    }
    const marked = (text: string) =>
      bodySegments(text, { emojiJoins: true }).filter(segment => segment.kind === 'control');
    // Between letters, at either edge of an emoji, or at the ends: still marked.
    expect(marked('ad\u200dmin')).toHaveLength(1);
    expect(marked('\u{1F469}\u200dx')).toHaveLength(1);
    expect(marked('x\u200d\u{1F4BB}')).toHaveLength(1);
    expect(marked('\u200d\u{1F4BB}')).toHaveLength(1);
    expect(marked('\u{1F469}\u200d')).toHaveLength(1);
    // Identity strings never let it through, emoji or not.
    expect(bodySegments(coder).filter(segment => segment.kind === 'control')).toHaveLength(1);
    expect(visibleText(`Ada ${coder}`)).toBe('Ada \u{1F469}[ZWJ]\u{1F4BB}');
  });

  it('spells hidden characters out for attributes that cannot hold a mark', () => {
    expect(visibleText('\u202e24#ppa/emca:hg')).toBe('[RLO]24#ppa/emca:hg');
    expect(visibleText('Ada\u200bLovelace')).toBe('Ada[ZWSP]Lovelace');
    expect(visibleText('gh:acme/app#42')).toBe('gh:acme/app#42');
  });

  it('names controls the way a terminal person reads them', () => {
    expect(controlName(0x1b)).toBe('ESC');
    expect(controlName(0x07)).toBe('BEL');
    expect(controlName(0x0d)).toBe('CR');
    expect(controlName(0x7f)).toBe('DEL');
    expect(controlName(0x202e)).toBe('RLO');
    expect(controlName(0x85)).toBe('U+0085');
  });
});

describe('bodyPreview', () => {
  it('shows short bodies whole, including one a line or two past the preview', () => {
    expect(bodyPreview('one line')).toBeNull();
    const nearly = Array.from({ length: BODY_PREVIEW_LINES + 2 }, (_, i) => `line ${i}`);
    expect(bodyPreview(nearly.join('\n'))).toBeNull();
  });

  it('cuts a long body to its first lines and reports how many there are', () => {
    const lines = Array.from({ length: 30 }, (_, i) => `line ${i}`);
    const preview = bodyPreview(lines.join('\n'));
    expect(preview?.lines).toBe(30);
    expect(preview?.preview.split('\n')).toHaveLength(BODY_PREVIEW_LINES);
  });

  it('cuts one very long line at a word boundary', () => {
    const preview = bodyPreview('word '.repeat(400));
    expect(preview?.lines).toBe(1);
    expect(preview?.preview.length).toBeLessThanOrEqual(640);
    expect(preview?.preview.endsWith('word')).toBe(true);
  });
});

describe('netHref', () => {
  it('encodes channel and thread, and ignores a thread without a channel', () => {
    expect(netHref()).toBe('/net');
    expect(netHref('#reviews')).toBe('/net?channel=%23reviews');
    expect(netHref('@ada', 'gh:acme/app#42')).toBe(
      '/net?channel=%40ada&thread=gh%3Aacme%2Fapp%2342'
    );
    expect(netHref(null, 'orphan')).toBe('/net');
  });
});

describe('shellChannel', () => {
  it('quotes a room, since # starts a shell comment', () => {
    expect(shellChannel('@ada')).toBe('@ada');
    expect(shellChannel('#reviews')).toBe("'#reviews'");
  });
});

describe('isRemembered', () => {
  it('is true once distillation reaches the last message', () => {
    const last = '2026-11-05T10:00:00Z';
    expect(isRemembered({ reflected_through: last, last_message_at: last })).toBe(true);
    expect(isRemembered({ reflected_through: '2026-11-05T09:59:00Z', last_message_at: last })).toBe(
      false
    );
    expect(isRemembered({ reflected_through: null, last_message_at: last })).toBe(false);
  });
});

describe('spanPhrase', () => {
  it('describes how long a thread has been going', () => {
    expect(spanPhrase('2026-11-05T10:00:00Z', '2026-11-05T10:00:40Z')).toBe('over 40 seconds');
    expect(spanPhrase('2026-11-05T10:00:00Z', '2026-11-05T10:06:00Z')).toBe('over 6 minutes');
    expect(spanPhrase('2026-11-05T10:00:00Z', '2026-11-05T10:00:00Z')).toBeNull();
  });
});

describe('presence', () => {
  const presence: NetPresenceResponse = {
    watching: [
      {
        channel: '@ada',
        user_id: 'u-ada',
        name: 'Ada',
        agent: 'claude-code',
        session: 's1',
        last_seen: '2026-11-05T10:00:00Z',
      },
      // A second session of the same agent is the same listener.
      {
        channel: '@ada',
        user_id: 'u-ada',
        name: 'Ada',
        agent: 'claude-code',
        session: 's2',
        last_seen: '2026-11-05T10:01:00Z',
      },
      // A dispatcher reading every channel hears each of them.
      {
        channel: null,
        user_id: 'u-grace',
        name: 'Grace',
        agent: 'herald',
        session: 'd1',
        last_seen: '2026-11-05T10:00:30Z',
      },
      {
        channel: '#reviews',
        user_id: 'u-grace',
        name: 'Grace',
        agent: 'codex',
        session: null,
        last_seen: '2026-11-05T09:59:00Z',
      },
    ],
    recent: [
      {
        channel: '#reviews',
        user_id: 'u-grace',
        name: 'Grace',
        agent: 'codex',
        last_post_at: '2026-11-05T09:50:00Z',
      },
      {
        channel: '@ada',
        user_id: 'u-grace',
        name: 'Grace',
        agent: 'codex',
        last_post_at: '2026-11-05T09:55:00Z',
      },
    ],
  };

  function listeners(channel: string | null): string[] {
    return presenceFor(presence, channel).listening.map(listenerLabel);
  }

  it('counts a reader of every channel under each channel, newest first', () => {
    expect(listeners('@ada')).toEqual(['Ada (claude-code)', 'Grace (herald)']);
    expect(listeners('#reviews')).toEqual(['Grace (herald)', 'Grace (codex)']);
    expect(listeners('@nobody')).toEqual(['Grace (herald)']);
  });

  it("collapses an agent's sessions into one listener, keeping its latest read", () => {
    const ada = presenceFor(presence, '@ada').listening[0];
    expect(ada.last_seen).toBe('2026-11-05T10:01:00Z');
    expect(listeners(null)).toEqual(['Ada (claude-code)', 'Grace (herald)', 'Grace (codex)']);
  });

  it('lists recent posters for the channel asked about', () => {
    expect(presenceFor(presence, '#reviews').recent.map(listenerLabel)).toEqual(['Grace (codex)']);
    expect(presenceFor(presence, '#deploys').recent).toEqual([]);
  });
});

describe('foldRuns', () => {
  let sequence = 0;
  function msg(
    body: string,
    overrides: Partial<NetMessage> & { user?: string; agent?: string | null } = {}
  ): NetMessage {
    sequence += 1;
    const { user = 'u-ada', agent = 'claude-code', ...rest } = overrides;
    return {
      id: `m-${sequence}`,
      channel: '@grace',
      thread: 'deploy:staging',
      kind: 'message',
      body,
      project_id: null,
      api_key_id: null,
      decision_memory_id: null,
      created_at: new Date(Date.UTC(2026, 10, 5, 10, 0, sequence)).toISOString(),
      cursor: `c-${sequence}`,
      author: { user_id: user, name: user, email: null, agent, session: null },
      ...rest,
    };
  }

  function shape(items: ReturnType<typeof foldRuns>): (string | number)[] {
    return items.map(item => (item.kind === 'run' ? item.messages.length : item.message.body));
  }

  it('folds a run of the same message from one agent into one item', () => {
    const items = foldRuns(
      Array.from({ length: 6 }, () => msg('Deploy check still red on staging. Retrying.'))
    );
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ kind: 'run' });
    expect(shape(items)).toEqual([6]);
  });

  it('ignores outer and repeated whitespace', () => {
    expect(normalizeBody('  Retrying.\n\n now  ')).toBe('Retrying. now');
    const items = foldRuns([msg('Retrying now'), msg('Retrying   now\n'), msg(' Retrying\nnow')]);
    expect(items).toEqual([expect.objectContaining({ kind: 'run' })]);
  });

  it('never folds messages that differ only in their numbers', () => {
    // Masking digits would bury the one row that matters among the rest.
    const failures = foldRuns([
      msg('0 failed'),
      msg('0 failed'),
      msg('12 failed'),
      msg('0 failed'),
      msg('0 failed'),
    ]);
    expect(shape(failures)).toEqual(['0 failed', '0 failed', '12 failed', '0 failed', '0 failed']);
    const approvals = foldRuns([
      msg('PR 12 approved'),
      msg('PR 13 approved'),
      msg('PR 14 approved'),
    ]);
    expect(approvals.every(item => item.kind === 'message')).toBe(true);
  });

  it('keeps two sessions of one agent apart', () => {
    const items = foldRuns([
      msg('Retrying.', {
        author: {
          user_id: 'u-ada',
          name: 'u-ada',
          email: null,
          agent: 'claude-code',
          session: 's1',
        },
      }),
      msg('Retrying.', {
        author: {
          user_id: 'u-ada',
          name: 'u-ada',
          email: null,
          agent: 'claude-code',
          session: 's2',
        },
      }),
      msg('Retrying.', {
        author: {
          user_id: 'u-ada',
          name: 'u-ada',
          email: null,
          agent: 'claude-code',
          session: 's1',
        },
      }),
    ]);
    expect(items.every(item => item.kind === 'message')).toBe(true);
  });

  it('leaves a run shorter than the minimum alone', () => {
    expect(MIN_FOLD_RUN).toBe(3);
    expect(shape(foldRuns([msg('Retrying.'), msg('Retrying.')]))).toEqual([
      'Retrying.',
      'Retrying.',
    ]);
  });

  it('does not fold a mixed sequence', () => {
    const items = foldRuns([
      // Two agents trading the same line: no run belongs to one author.
      msg('Still red.', { user: 'u-ada' }),
      msg('Still red.', { user: 'u-grace' }),
      msg('Still red.', { user: 'u-ada' }),
      msg('Still red.', { user: 'u-grace' }),
      // Same author, different words.
      msg('Opened the PR.'),
      msg('Rebased on main.'),
      msg('CI is green.'),
      // Same words, different agents of one person.
      msg('Looking.', { agent: 'claude-code' }),
      msg('Looking.', { agent: 'codex' }),
      msg('Looking.', { agent: null }),
      // Same words, different threads.
      msg('Retrying.', { thread: 'a' }),
      msg('Retrying.', { thread: 'b' }),
      msg('Retrying.', { thread: null }),
      // Same words, different channels.
      msg('Ping.', { channel: '@ada' }),
      msg('Ping.', { channel: '@grace' }),
      msg('Ping.', { channel: '#reviews' }),
    ]);
    expect(items.every(item => item.kind === 'message')).toBe(true);
    expect(items).toHaveLength(16);
  });

  it('never folds a decision, and a decision ends the run around it', () => {
    const items = foldRuns([
      msg('Ship it.'),
      msg('Ship it.'),
      msg('Ship it.', { kind: 'decision', decision_memory_id: 'raw_1' }),
      msg('Ship it.'),
      msg('Ship it.'),
    ]);
    expect(items.every(item => item.kind === 'message')).toBe(true);
  });

  it('keeps list order and folds only the repeating stretch', () => {
    const items = foldRuns([
      msg('Starting the deploy check.'),
      msg('Still red. Retrying.'),
      msg('Still red. Retrying.'),
      msg('Still red. Retrying.'),
      msg('Still red. Retrying.', { user: 'u-grace', agent: 'codex' }),
    ]);
    expect(shape(items)).toEqual(['Starting the deploy check.', 3, 'Still red. Retrying.']);
  });

  it('says how long a run took, in either list order', () => {
    const run = [msg('x'), msg('x'), msg('x'), msg('x'), msg('x'), msg('x')];
    expect(runSpanPhrase(run)).toBe('in 5 seconds');
    expect(runSpanPhrase([...run].reverse())).toBe('in 5 seconds');
    const sameInstant = { created_at: '2026-11-05T10:00:00.000Z' };
    expect(runSpanPhrase([msg('x', sameInstant), msg('x', sameInstant)])).toBe('within a second');
  });
});
