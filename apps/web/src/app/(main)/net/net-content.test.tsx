import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type {
  NetChannelsResponse,
  NetDirectoryResponse,
  NetFeedResponse,
  NetMessage,
  NetPresenceResponse,
  NetThreadsResponse,
} from '@/lib/api/net';
import { render, screen, waitFor, within } from '@/test/utils';

const navigation = vi.hoisted(() => {
  const router = { replace: vi.fn(), push: vi.fn() };
  return { params: new URLSearchParams(), router };
});

vi.mock('next/navigation', () => ({
  // One router, like Next's: effects that depend on it run once, not per render.
  useRouter: () => navigation.router,
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

// Ada's agent stuck retrying, with Grace's agent cutting in once: the fold
// covers the six repeats and leaves the interruption and what follows alone.
const LOOP = [
  ...Array.from({ length: 6 }, (_, n) =>
    message({
      channel: '@grace',
      thread: 'deploy:staging',
      body: 'Deploy check still red on staging. Retrying.',
      created_at: `2026-11-05T10:10:0${n}Z`,
      author: { ...ADA, agent: 'claude-code', session: 'ada-deploy-loop' },
    })
  ),
  message(
    {
      channel: '@grace',
      thread: 'deploy:staging',
      body: 'Deploy check still red on staging. Retrying.',
      created_at: '2026-11-05T10:10:07Z',
    },
    GRACE,
    'codex'
  ),
  message({
    channel: '@grace',
    thread: 'deploy:staging',
    body: 'Deploy check still red on staging. Retrying.',
    created_at: '2026-11-05T10:10:08Z',
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

/** A thread key written backwards behind a right-to-left override. */
const SPOOFED_THREAD = '\u202e24#ppa/emca:hg';

const BUSY_THREADS: NetThreadsResponse = {
  threads: Array.from({ length: 11 }, (_, n) => ({
    channel: '#busy',
    thread: n === 10 ? SPOOFED_THREAD : `task:${n + 1}`,
    project_id: null,
    message_count: 1,
    first_message_at: '2026-11-05T10:00:00Z',
    last_message_at: `2026-11-05T10:${String(59 - n).padStart(2, '0')}:00Z`,
    reflected_through: null,
    reflection_source_id: null,
    reflection_error: null,
    reflection_skipped: [],
  })),
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
      session: 'session-grace-1',
      last_seen: '2026-11-05T10:07:30Z',
    },
    // A dispatcher reading every channel listens on #reviews and @ada alike.
    {
      channel: null,
      user_id: 'u-ada',
      name: 'Ada Lovelace',
      agent: 'herald',
      session: 'session-ada-9',
      last_seen: '2026-11-05T10:07:00Z',
    },
  ],
  recent: [
    {
      channel: '#reviews',
      user_id: 'u-ada',
      name: 'Ada Lovelace',
      agent: 'claude-code',
      last_post_at: '2026-11-05T10:06:00Z',
    },
  ],
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
/**
 * A viewer with no active organization: no inbox, so nothing about the
 * viewer shapes the page. The handles block below signs in as Ada.
 */
const NO_ORG_ME = {
  user: { id: 'u-viewer', github_id: null, email: null, name: 'Viewer', avatar_url: null },
  organization: null,
  org_role: null,
  handle: null,
  inbox: null,
};

/** What /auth/me says. */
let me: Record<string, unknown>;
let preferences: Record<string, unknown>;
let preferenceWrites: Record<string, unknown>[];
/** What GET /net/directory answers. */
let directory: () => Response;
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
    presence = () => json({ watching: [], recent: [] });
    me = NO_ORG_ME;
    preferences = {};
    preferenceWrites = [];
    directory = () => json({ members: [] });
    navigation.router.replace.mockReset();
    fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'http://localhost');
      switch (url.pathname) {
        case '/api/auth/me':
          return json(me);
        case '/api/users/me/preferences': {
          if (init?.method === 'PATCH') {
            const update = (
              JSON.parse(String(init.body)) as { preferences: Record<string, unknown> }
            ).preferences;
            preferenceWrites.push(update);
            preferences = { ...preferences, ...update };
          }
          return json({ preferences });
        }
        case '/api/net/directory':
          return directory();
        case '/api/net/channels':
          return json(channels);
        case '/api/net/feed': {
          const channel = url.searchParams.get('channel');
          const thread = url.searchParams.get('thread');
          if (!channel) return json(feed([...REVIEW_THREAD, ...INBOX]));
          if (channel === '#reviews') return json(feed(thread ? REVIEW_THREAD : REVIEW_THREAD));
          if (channel === '@ada') return json(feed(INBOX));
          if (channel === '@grace') return json(feed(LOOP));
          if (channel === '#busy')
            return json(
              feed([
                message(
                  {
                    channel: '#busy',
                    thread: SPOOFED_THREAD,
                    body: 'Approved.',
                    created_at: '2026-11-05T10:09:00Z',
                  },
                  { ...GRACE, name: 'Grace\u200bHopper' },
                  'co\u202edex'
                ),
              ])
            );
          return json(feed([]));
        }
        case '/api/net/threads': {
          const asked = url.searchParams.get('channel');
          if (asked === '#reviews') return json(THREADS);
          if (asked === '#busy') return json(BUSY_THREADS);
          return json({ threads: [] });
        }
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
    const marks = within(hostile as HTMLElement).getAllByTitle(/^Hidden character/);
    expect(marks.map(mark => mark.getAttribute('title'))).toEqual([
      'Hidden character U+001B',
      'Hidden character U+001B',
      'Hidden character U+202E',
    ]);
  });

  it('draws an emoji joined by a ZWJ in a body, but marks the joiner in a name', async () => {
    const coder = '\u{1F469}\u200d\u{1F4BB}';
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), 'http://localhost');
      if (url.pathname === '/api/net/channels') return json(CHANNELS);
      if (url.pathname === '/api/net/feed')
        return json(
          feed([
            message(
              {
                channel: '@ada',
                body: `Pairing on it ${coder} now`,
                created_at: '2026-11-05T10:09:00Z',
              },
              { ...GRACE, name: `Grace ${coder}` },
              'codex'
            ),
          ])
        );
      if (url.pathname === '/api/auth/me') return json(NO_ORG_ME);
      if (url.pathname === '/api/net/presence') return json({ watching: [], recent: [] });
      if (url.pathname === '/api/net/threads') return json({ threads: [] });
      return json({ detail: 'Not Found' }, 404);
    });
    renderNet('channel=%40ada');

    const row = await screen.findByRole('article', { name: /Message from Grace/ });
    expect(within(row).getByText(`Pairing on it ${coder} now`)).toBeInTheDocument();
    // The one mark in the row is in the author's name.
    expect(within(row).getAllByTitle('Hidden character U+200D')).toHaveLength(1);
    expect(row).toHaveAttribute('aria-label', 'Message from Grace \u{1F469}[ZWJ]\u{1F4BB}');
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

  it('leaves the page whole when presence cannot be read', async () => {
    presence = () => json({ detail: 'presence registry unavailable' }, 503);
    renderNet('channel=%23reviews');

    await screen.findByRole('region', { name: '#reviews' });
    await waitFor(() => expect(requests('/api/net/presence').length).toBeGreaterThan(0));
    expect(
      await screen.findByRole('article', { name: 'Decision from Ada Lovelace' })
    ).toBeVisible();
    expect(screen.queryByText(/listening/i)).toBeNull();
    expect(screen.queryByText(/reading this channel/)).toBeNull();
  });

  it('says plainly when no agent is listening', async () => {
    renderNet('channel=%23reviews');

    expect(
      await screen.findByText('No agent is reading this channel right now.')
    ).toBeInTheDocument();
    const nav = screen.getByRole('navigation', { name: 'Channels' });
    expect(within(nav).queryByText(/listening/)).toBeNull();
    expect(screen.getByText('listening', { selector: 'dt' }).nextSibling).toHaveTextContent('0');
  });

  it('shows who is listening and who posted, counting readers of every channel', async () => {
    presence = () => json(PRESENCE);
    renderNet('channel=%23reviews');

    // Grace reads #reviews; Ada's dispatcher reads everything, so both hear it,
    // and Ada's dispatcher alone hears @ada.
    const nav = await screen.findByRole('navigation', { name: 'Channels' });
    const reviews = within(nav).getByRole('link', { name: /reviews/ });
    expect(await within(reviews).findByText('2 listening')).toHaveAttribute(
      'title',
      'Grace Hopper (codex)\nAda Lovelace (herald)'
    );
    expect(
      within(within(nav).getByRole('link', { name: /ada/ })).getByText('1 listening')
    ).toBeInTheDocument();

    const stream = screen.getByRole('region', { name: '#reviews' });
    const header = stream.querySelector('header') as HTMLElement;
    const listening = within(header).getByText('Listening').nextElementSibling as HTMLElement;
    expect(within(listening).getByText('Grace Hopper')).toBeInTheDocument();
    expect(within(listening).getByText('herald')).toBeInTheDocument();
    const posted = within(header).getByText('Posted in the last hour')
      .nextElementSibling as HTMLElement;
    expect(within(posted).getByText('claude-code')).toBeInTheDocument();
    expect(screen.getByText('listening', { selector: 'dt' })).toBeInTheDocument();
    // No status vocabulary and no plumbing.
    expect(document.body.textContent).not.toMatch(/online|away|session-/i);
  });

  it("shows who is listening on a thread's channel", async () => {
    presence = () => json(PRESENCE);
    renderNet('channel=%23reviews&thread=gh%3Aacme%2Fapp%2342');

    const thread = await screen.findByRole('region', { name: 'gh:acme/app#42' });
    expect(await within(thread).findByText('Listening')).toBeInTheDocument();
    expect(within(thread).getByText('Posted in the last hour')).toBeInTheDocument();
  });

  it('folds a run of repeats into one quiet row that expands to each message', async () => {
    const { user } = renderNet('channel=%40grace');

    const stream = await screen.findByRole('region', { name: '@grace' });
    const rows = await within(stream).findAllByRole('article');
    // Newest first: Ada once more, Grace cutting in, then Ada's six folded.
    expect(rows.map(row => row.getAttribute('aria-label'))).toEqual([
      'Message from Ada Lovelace',
      'Message from Grace Hopper',
      'Ada Lovelace repeated a message 6 times',
    ]);
    const run = rows[2];
    expect(within(run).getByText('×6')).toBeInTheDocument();
    expect(within(run).getByText('Same message 6 times in 5 seconds')).toBeInTheDocument();
    expect(within(run).getAllByText(/Deploy check still red/)).toHaveLength(1);

    await user.click(within(run).getByRole('button', { name: 'Show each' }));
    const each = within(run).getByRole('list', { name: '6 messages from Ada Lovelace' });
    expect(within(each).getAllByRole('listitem')).toHaveLength(6);
    expect(within(run).getByRole('button', { name: 'Fold' })).toHaveAttribute(
      'aria-expanded',
      'true'
    );
    // The day still counts every message.
    expect(within(stream).getByRole('heading', { name: /8$/ })).toBeInTheDocument();
  });

  it('shows a spoofed thread key, name and agent as written, marks and all', async () => {
    channels = {
      channels: [
        ...CHANNELS.channels,
        {
          channel: '#busy',
          message_count: 1,
          thread_count: 11,
          last_message_at: '2026-11-05T10:59:00Z',
        },
      ],
    };
    const { user } = renderNet(`channel=%23busy&thread=${encodeURIComponent(SPOOFED_THREAD)}`);

    const heading = await screen.findByRole('heading', { level: 2, name: /emca:hg/ });
    // Isolated, with the override drawn as a mark instead of reversing the key.
    const isolate = heading.querySelector('bdi');
    expect(isolate).not.toBeNull();
    expect(within(isolate as HTMLElement).getByTitle('Hidden character U+202E')).toHaveTextContent(
      'RLO'
    );
    expect(heading.textContent).toBe('RLO24#ppa/emca:hg');

    const row = await screen.findByRole('article', { name: 'Message from Grace[ZWSP]Hopper' });
    expect(within(row).getByTitle('Hidden character U+200B')).toBeInTheDocument();
    expect(row.querySelectorAll('bdi').length).toBeGreaterThanOrEqual(2);

    // The thread list folds past eight; its toggle says whether it is open.
    const nav = screen.getByRole('navigation', { name: 'Channels' });
    const more = await within(nav).findByRole('button', { name: /Show .* more thread/ });
    expect(more).toHaveAttribute('aria-expanded', 'false');
    expect(within(nav).getByRole('link', { name: /emca:hg/ })).toHaveAttribute(
      'title',
      '[RLO]24#ppa/emca:hg'
    );
    await user.click(more);
    expect(within(nav).getByRole('button', { name: 'Show fewer threads' })).toHaveAttribute(
      'aria-expanded',
      'true'
    );
  });

  it('reads a channel named in any case as the lowercase channel it is', async () => {
    renderNet('channel=%40ADA');

    expect(await screen.findByRole('region', { name: '@ada' })).toBeInTheDocument();
    expect(requests('/api/net/feed').at(-1)?.searchParams.get('channel')).toBe('@ada');
  });

  describe('with member handles', () => {
    const ADA_ME = {
      user: {
        id: 'u-ada',
        github_id: null,
        email: 'ada@example.com',
        name: 'Ada Lovelace',
        avatar_url: null,
      },
      organization: { id: 'org-1', slug: 'acme', name: 'Acme' },
      org_role: 'owner',
      handle: 'ada',
      inbox: '@ada',
    };

    beforeEach(() => {
      me = ADA_ME;
      const members: NetDirectoryResponse = {
        members: [
          {
            user_id: 'u-ada',
            name: 'Ada Lovelace',
            handle: 'ada',
            inbox: '@ada',
            listening: 0,
            last_post_at: null,
          },
          {
            user_id: 'u-grace',
            name: 'Grace Hopper',
            handle: 'grace',
            inbox: '@grace',
            listening: 1,
            last_post_at: '2026-11-05T10:07:00Z',
          },
        ],
      };
      directory = () => json(members);
    });

    it('lands on your inbox when something new is waiting', async () => {
      renderNet();

      await waitFor(() =>
        expect(navigation.router.replace).toHaveBeenCalledWith('/net?channel=%40ada')
      );
    });

    it('lands on every channel once the inbox has been read', async () => {
      // Seen through the newest message in the inbox fixture.
      preferences = { net_inbox_seen: { 'org-1:@ada': '2026-11-05T10:08:00Z' } };
      renderNet();

      expect(await screen.findByRole('region', { name: 'All channels' })).toBeInTheDocument();
      const nav = screen.getByRole('navigation', { name: 'Channels' });
      const yours = within(nav).getByRole('link', { name: /Your inbox/ });
      expect(yours).toHaveAttribute('href', '/net?channel=%40ada');
      expect(within(yours).queryByText(/new$/)).toBeNull();
      expect(navigation.router.replace).not.toHaveBeenCalled();
    });

    it('pins your inbox first even before anyone writes to it', async () => {
      me = { ...ADA_ME, handle: 'hedy', inbox: '@hedy' };
      renderNet('channel=%40hedy');

      const nav = await screen.findByRole('navigation', { name: 'Channels' });
      const links = within(nav).getAllByRole('link');
      expect(links[0]).toHaveTextContent('Your inbox');
      expect(links[0]).toHaveTextContent('nothing yet');
      expect(await screen.findByText('Nothing in your inbox yet')).toBeInTheDocument();
      expect(screen.getByText(/sibyl net post @hedy/)).toBeInTheDocument();
    });

    it('counts what is new since your last visit, never your own posts', async () => {
      // Grace wrote at 10:07, after the last visit; Ada's own 10:08 post does not count.
      preferences = { net_inbox_seen: { 'org-1:@ada': '2026-11-05T10:06:00Z' } };
      renderNet('channel=%23reviews');

      const nav = await screen.findByRole('navigation', { name: 'Channels' });
      expect(await within(nav).findByText('1 new')).toBeInTheDocument();
    });

    it('marks the inbox seen through its newest message when you open it', async () => {
      preferences = { theme: 'neon', net_inbox_seen: { 'other-org:@ada': '2026-01-01T00:00:00Z' } };
      renderNet('channel=%40ada');

      expect(
        await screen.findByRole('heading', { level: 2, name: /Your inbox/ })
      ).toBeInTheDocument();
      await waitFor(() =>
        expect(preferenceWrites.at(-1)).toEqual({
          net_inbox_seen: {
            'other-org:@ada': '2026-01-01T00:00:00Z',
            'org-1:@ada': '2026-11-05T10:08:00Z',
          },
        })
      );
    });

    it("shows each author's handle beside their name, linking to their inbox", async () => {
      renderNet('channel=%23reviews');

      const row = await screen.findByRole('article', { name: 'Decision from Ada Lovelace' });
      expect(await within(row).findByRole('link', { name: '@ada' })).toHaveAttribute(
        'href',
        '/net?channel=%40ada'
      );
    });

    it("turns @me in a shared link into the reader's own inbox", async () => {
      renderNet('channel=%40me&thread=gh%3Aacme%2Fapp%2342');

      await waitFor(() =>
        expect(navigation.router.replace).toHaveBeenCalledWith(
          '/net?channel=%40ada&thread=gh%3Aacme%2Fapp%2342'
        )
      );
    });
  });

  it('gives a viewer with no active organization no inbox, and every channel', async () => {
    renderNet();

    expect(await screen.findByRole('region', { name: 'All channels' })).toBeInTheDocument();
    const nav = screen.getByRole('navigation', { name: 'Channels' });
    expect(within(nav).queryByText('Your inbox')).toBeNull();
    expect(navigation.router.replace).not.toHaveBeenCalled();
  });

  it('leaves handles off, and the page whole, when the directory cannot be read', async () => {
    me = {
      ...NO_ORG_ME,
      organization: { id: 'org-1', slug: 'acme', name: 'Acme' },
      handle: 'hedy',
      inbox: '@hedy',
    };
    directory = () => json({ detail: 'directory unavailable' }, 503);
    renderNet('channel=%23reviews');

    const row = await screen.findByRole('article', { name: 'Decision from Ada Lovelace' });
    await waitFor(() => expect(requests('/api/net/directory').length).toBeGreaterThan(0));
    expect(within(row).queryByRole('link', { name: '@ada' })).toBeNull();
    // The viewer's own inbox comes from /auth/me, so it still leads the list.
    const nav = screen.getByRole('navigation', { name: 'Channels' });
    expect(within(nav).getAllByRole('link')[0]).toHaveTextContent('Your inbox');
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
