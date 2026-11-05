import { describe, expect, it } from 'vitest';
import type { NetMessage, NetPresenceResponse } from '@/lib/api/net';
import {
  BODY_PREVIEW_LINES,
  bodyPreview,
  bodySegments,
  controlName,
  foldRuns,
  isRemembered,
  MIN_FOLD_RUN,
  netHref,
  normalizeBody,
  presenceFor,
  runSpanPhrase,
  spanPhrase,
  watchingCounts,
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
      {
        channel: '@ada',
        user_id: 'u-ada',
        name: 'Ada',
        agent: 'claude-code',
        session: 's1',
        last_seen: '2026-11-05T10:01:00Z',
      },
      {
        channel: '@ada',
        user_id: 'u-ada',
        name: 'Ada',
        agent: 'claude-code',
        session: 's2',
        last_seen: '2026-11-05T09:59:00Z',
      },
      {
        channel: '#reviews',
        user_id: 'u-grace',
        name: 'Grace',
        agent: 'codex',
        session: null,
        last_seen: '2026-11-05T10:00:30Z',
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
    ],
  };

  it('counts each reading session once per channel', () => {
    expect(Object.fromEntries(watchingCounts(presence))).toEqual({ '@ada': 2, '#reviews': 1 });
    expect(watchingCounts(null).size).toBe(0);
  });

  it('narrows to a channel, newest first, keeping the latest sighting', () => {
    const ada = presenceFor(presence, '@ada');
    expect(ada.watching.map(watcher => [watcher.session, watcher.last_seen])).toEqual([
      ['s1', '2026-11-05T10:01:00Z'],
      ['s2', '2026-11-05T09:59:00Z'],
    ]);
    expect(ada.recent).toEqual([]);
    expect(presenceFor(presence, null).watching).toHaveLength(3);
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
    expect(items[0]).toMatchObject({ kind: 'run', identical: true });
    expect(shape(items)).toEqual([6]);
  });

  it('ignores outer and repeated whitespace', () => {
    expect(normalizeBody('  Retrying.\n\n now  ')).toBe('Retrying. now');
    const items = foldRuns([msg('Retrying now'), msg('Retrying   now\n'), msg(' Retrying\nnow')]);
    expect(items).toEqual([expect.objectContaining({ kind: 'run', identical: true })]);
  });

  it('folds messages that differ only in their numbers, and says they are not identical', () => {
    const items = foldRuns([msg('Retry 1 of 5'), msg('Retry 2 of 5'), msg('Retry 13 of 15')]);
    expect(items).toEqual([expect.objectContaining({ kind: 'run', identical: false })]);
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
