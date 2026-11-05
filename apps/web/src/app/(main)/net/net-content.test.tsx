import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type {
  NetChannelsResponse,
  NetFeedResponse,
  NetMessage,
  NetPresenceResponse,
  NetThreadsResponse,
} from '@/lib/api/net';
import { render, screen, waitFor, within } from '@/test/utils';

const navigation = vi.hoisted(() => ({ params: new URLSearchParams() }));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => '/net',
  useSearchParams: () => navigation.params,
}));

import { NetContent } from './net-content';

const ADA = { user_id: 'u-ada', name: 'Ada Lovelace', email: 'ada@example.com' };
const GRACE = { user_id: 'u-grace', name: 'Grace Hopper', email: 'grace@example.com' };

let sequence = 0;
function message(
  overrides: Partial<NetMessage> & { body: string; created_at: string },
  author: typeof ADA = ADA,
  agent = 'claude-code'
): NetMessage {
  sequence += 1;
  return {
    id: `msg-${sequence}`,
    channel: '#reviews',
    thread: null,
    kind: 'message',
    project_id: null,
    api_key_id: null,
    decision_memory_id: null,
    cursor: `CURSOR-${sequence}-opaque`,
    author: { ...author, agent, session: `session-${sequence}` },
    ...overrides,
  };
}

const HOSTILE =
  '<script>window.__pwned = true</script>\n<img src=x onerror="window.__pwned = true">\nANSI \u001b[31mred\u001b[0m and ‮evil';

const REVIEW_THREAD = [
  message({
    thread: 'gh:acme/app#42',
    body: 'Opened the migration PR.',
    created_at: '2026-11-05T10:00:00Z',
  }),
  message(
    {
      thread: 'gh:acme/app#42',
      body: 'Then the bridge buys nothing.\nCan we drop it?',
      created_at: '2026-11-05T10:02:00Z',
    },
    GRACE,
    'codex'
  ),
  message({
    thread: 'gh:acme/app#42',
    kind: 'decision',
    decision_memory_id: 'raw_decision_1',
    body: 'Ship the migration in two phases; drop the reverse bridge.',
    created_at: '2026-11-05T10:06:00Z',
  }),
];

const INBOX = [
  message(
    { channel: '@ada', thread: 'probe:render', body: HOSTILE, created_at: '2026-11-05T10:07:00Z' },
    GRACE,
    'codex'
  ),
  message({
    channel: '@ada',
    body: Array.from({ length: 30 }, (_, n) => `- release note item ${n + 1}`).join('\n'),
    created_at: '2026-11-05T10:08:00Z',
  }),
];

const CHANNELS: NetChannelsResponse = {
  channels: [
    { channel: '@ada', message_count: 2, thread_count: 1, last_message_at: '2026-11-05T10:08:00Z' },
    {
      channel: '#reviews',
      message_count: 3,
      thread_count: 1,
      last_message_at: '2026-11-05T10:06:00Z',
    },
  ],
};

const THREADS: NetThreadsResponse = {
  threads: [
    {
      channel: '#reviews',
      thread: 'gh:acme/app#42',
      project_id: 'project_acme',
      message_count: 3,
      first_message_at: '2026-11-05T10:00:00Z',
      last_message_at: '2026-11-05T10:06:00Z',
      reflected_through: '2026-11-05T10:06:00Z',
      reflection_source_id: 'source-transcript-1',
      reflection_error: null,
      reflection_skipped: ['u-grace: api key revoked'],
    },
  ],
};

const PRESENCE: NetPresenceResponse = {
  watching: [
    {
      channel: '#reviews',
      user_id: 'u-grace',
      name: 'Grace Hopper',
      agent: 'codex',
      session: 's-1',
      last_seen: '2026-11-05T10:07:30Z',
    },
    {
      channel: '#reviews',
      user_id: 'u-ada',
      name: 'Ada Lovelace',
      agent: 'claude-code',
      session: 's-2',
      last_seen: '2026-11-05T10:07:00Z',
    },
  ],
  recent: [],
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function feed(messages: NetMessage[]): NetFeedResponse {
  return { messages, next_since: 'END-CURSOR', more: false };
}

let channels: NetChannelsResponse;
let presence: Response | (() => Response);
let fetchMock: ReturnType<typeof vi.fn>;

function requests(path: string): URL[] {
  return fetchMock.mock.calls
    .map(([input]) => new URL(String(input), 'http://localhost'))
    .filter(url => url.pathname === path);
}

function renderNet(search = '') {
  navigation.params = new URLSearchParams(search);
  return render(<NetContent />);
}

describe('NetContent', () => {
  beforeEach(() => {
    channels = CHANNELS;
    presence = () => json({ detail: 'Not Found' }, 404);
    fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), 'http://localhost');
      switch (url.pathname) {
        case '/api/net/channels':
          return json(channels);
        case '/api/net/feed': {
          const channel = url.searchParams.get('channel');
          const thread = url.searchParams.get('thread');
          if (!channel) return json(feed([...REVIEW_THREAD, ...INBOX]));
          if (channel === '#reviews') return json(feed(thread ? REVIEW_THREAD : REVIEW_THREAD));
          if (channel === '@ada') return json(feed(INBOX));
          return json(feed([]));
        }
        case '/api/net/threads':
          return json(url.searchParams.get('channel') === '#reviews' ? THREADS : { threads: [] });
        case '/api/net/presence':
          return typeof presence === 'function' ? presence() : presence;
        case '/api/search/explore':
          return json({
            mode: 'list',
            filters: {},
            total: 1,
            has_more: false,
            entities: [
              {
                id: 'project_acme',
                type: 'project',
                name: 'acme/app',
                description: '',
                metadata: {},
              },
            ],
          });
        default:
          return json({ detail: 'Not Found' }, 404);
      }
    });
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    delete (window as { __pwned?: boolean }).__pwned;
  });

  it('lists channels and shows every channel newest first, read only', async () => {
    renderNet();

    const nav = await screen.findByRole('navigation', { name: 'Channels' });
    expect(within(nav).getByRole('link', { name: /All channels/ })).toHaveAttribute(
      'aria-current',
      'page'
    );
    expect(within(nav).getByRole('link', { name: /ada/ })).toHaveAttribute(
      'href',
      '/net?channel=%40ada'
    );

    const stream = await screen.findByRole('region', { name: 'All channels' });
    const rows = await within(stream).findAllByRole('article');
    expect(rows.map(row => row.getAttribute('aria-label'))).toEqual([
      'Message from Ada Lovelace',
      'Message from Grace Hopper',
      'Decision from Ada Lovelace',
      'Message from Grace Hopper',
      'Message from Ada Lovelace',
    ]);
    // Nowhere to type: the page reads, agents write from the CLI.
    expect(screen.queryByRole('textbox')).toBeNull();
    // Feed cursors are plumbing.
    expect(document.body.textContent).not.toMatch(/CURSOR|END-CURSOR/);
    expect(requests('/api/net/feed')[0].searchParams.get('channel')).toBeNull();
  });

  it('renders a hostile body as inert text with its control characters marked', async () => {
    renderNet('channel=%40ada');

    const stream = await screen.findByRole('region', { name: '@ada' });
    const hostile = (await within(stream).findAllByRole('article')).find(row =>
      row.textContent?.includes('<script>')
    );
    expect(hostile).toBeDefined();
    expect(hostile?.querySelector('script, img')).toBeNull();
    expect(hostile?.textContent).toContain('<img src=x onerror="window.__pwned = true">');
    expect((window as { __pwned?: boolean }).__pwned).toBeUndefined();
    const marks = within(hostile as HTMLElement).getAllByTitle(/^Control character/);
    expect(marks.map(mark => mark.getAttribute('title'))).toEqual([
      'Control character U+001B',
      'Control character U+001B',
      'Control character U+202E',
    ]);
  });

  it('opens a long body collapsed and expands it on request', async () => {
    const { user } = renderNet('channel=%40ada');

    const toggle = await screen.findByRole('button', { name: 'Show all 30 lines' });
    expect(screen.queryByText(/release note item 30/)).toBeNull();
    await user.click(toggle);
    expect(screen.getByText(/release note item 30/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Show less' })).toHaveAttribute(
      'aria-expanded',
      'true'
    );
  });

  it('marks a decision and links the memory it was saved as', async () => {
    renderNet('channel=%23reviews');

    const decision = await screen.findByRole('article', { name: 'Decision from Ada Lovelace' });
    expect(within(decision).getByText('Decision')).toBeInTheDocument();
    expect(within(decision).getByRole('link', { name: /Saved to memory/ })).toHaveAttribute(
      'href',
      '/memory/captures?id=raw_decision_1'
    );
    expect(within(decision).getByRole('link', { name: 'gh:acme/app#42' })).toHaveAttribute(
      'href',
      '/net?channel=%23reviews&thread=gh%3Aacme%2Fapp%2342'
    );
    const nav = screen.getByRole('navigation', { name: 'Channels' });
    expect(
      await within(nav).findByRole('list', { name: 'Threads in #reviews' })
    ).toBeInTheDocument();
  });

  it('reads a thread oldest first with distillation behind a disclosure', async () => {
    const { user } = renderNet('channel=%23reviews&thread=gh%3Aacme%2Fapp%2342');

    const thread = await screen.findByRole('region', { name: 'gh:acme/app#42' });
    const rows = await within(thread).findAllByRole('article');
    expect(rows.map(row => row.getAttribute('aria-label'))).toEqual([
      'Message from Ada Lovelace',
      'Message from Grace Hopper',
      'Decision from Ada Lovelace',
    ]);
    expect(
      await within(thread).findByText(
        /3 messages between Ada Lovelace and Grace Hopper over 6 minutes/
      )
    ).toBeInTheDocument();
    expect(within(thread).getByText('Remembered')).toBeInTheDocument();
    expect(await within(thread).findByText('acme/app')).toBeInTheDocument();

    expect(within(thread).queryByText(/api key revoked/)).toBeNull();
    await user.click(within(thread).getByRole('button', { name: 'Details' }));
    expect(within(thread).getByText('u-grace: api key revoked')).toBeInTheDocument();

    const feedRequest = requests('/api/net/feed').at(-1);
    expect(feedRequest?.searchParams.get('thread')).toBe('gh:acme/app#42');
    expect(feedRequest?.searchParams.get('limit')).toBe('500');
  });

  it('hides presence when the server does not serve it', async () => {
    renderNet('channel=%23reviews');

    await screen.findByRole('region', { name: '#reviews' });
    await waitFor(() => expect(requests('/api/net/presence')).toHaveLength(1));
    expect(screen.queryByText(/watching/)).toBeNull();
  });

  it('shows who is watching a channel when presence is served', async () => {
    presence = () => json(PRESENCE);
    const { user } = renderNet('channel=%23reviews');

    const nav = await screen.findByRole('navigation', { name: 'Channels' });
    expect(await within(nav).findByText('2 watching')).toBeInTheDocument();
    const toggle = await screen.findByRole('button', { name: /2 watching/ });
    await user.click(toggle);
    expect(screen.getByRole('heading', { name: 'Watching now' })).toBeInTheDocument();
    expect(screen.getAllByText('Grace Hopper').length).toBeGreaterThan(0);
    // Sessions are plumbing.
    expect(document.body.textContent).not.toContain('s-1');
  });

  it('teaches the CLI when the organization has no traffic', async () => {
    channels = { channels: [] };
    renderNet();

    expect(await screen.findByText('No agent traffic yet')).toBeInTheDocument();
    expect(screen.getByText(/sibyl net post @ada/)).toBeInTheDocument();
    expect(screen.getByText('sibyl net watch -c @ada')).toBeInTheDocument();
    expect(screen.queryByRole('navigation', { name: 'Channels' })).toBeNull();
  });

  it('teaches the CLI in a channel nobody has posted to', async () => {
    renderNet('channel=%40bob');

    expect(await screen.findByText('Nothing in @bob yet')).toBeInTheDocument();
    expect(screen.getByText(/sibyl net post @bob/)).toBeInTheDocument();
  });
});
