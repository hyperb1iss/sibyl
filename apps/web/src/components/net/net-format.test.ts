import { describe, expect, it } from 'vitest';
import type { NetPresenceResponse } from '@/lib/api/net';
import {
  BODY_PREVIEW_LINES,
  bodyPreview,
  bodySegments,
  controlName,
  isRemembered,
  netHref,
  presenceFor,
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
