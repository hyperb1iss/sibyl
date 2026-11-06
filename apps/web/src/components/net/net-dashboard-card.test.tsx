import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@/test/utils';
import { busiestChannel, NetDashboardCard } from './net-dashboard-card';

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function message(channel: string, user: string, at: string) {
  return {
    id: `${channel}-${at}`,
    channel,
    thread: null,
    kind: 'message',
    body: 'hi',
    project_id: null,
    api_key_id: null,
    decision_memory_id: null,
    created_at: at,
    cursor: 'c',
    author: { user_id: user, name: user, email: null, agent: 'codex', session: null },
  };
}

const RECENT = [
  message('#reviews', 'u-grace', '2026-11-05T10:00:00Z'),
  message('#reviews', 'u-grace', '2026-11-05T10:01:00Z'),
  message('@ada', 'u-grace', '2026-11-05T10:02:00Z'),
];

let me: Record<string, unknown>;
let channels: {
  channel: string;
  message_count: number;
  thread_count: number;
  last_message_at: string;
}[];

describe('NetDashboardCard', () => {
  beforeEach(() => {
    me = {
      user: { id: 'u-ada', github_id: null, email: null, name: 'Ada', avatar_url: null },
      organization: { id: 'org-1', slug: 'acme', name: 'Acme' },
      org_role: 'owner',
      handle: 'ada',
      inbox: '@ada',
    };
    channels = [
      {
        channel: '#reviews',
        message_count: 2,
        thread_count: 0,
        last_message_at: '2026-11-05T10:01:00Z',
      },
      {
        channel: '@ada',
        message_count: 1,
        thread_count: 1,
        last_message_at: '2026-11-05T10:02:00Z',
      },
    ];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = new URL(String(input), 'http://localhost');
        switch (url.pathname) {
          case '/api/auth/me':
            return json(me);
          case '/api/users/me/preferences':
            return json({ preferences: {} });
          case '/api/net/channels':
            return json({ channels });
          case '/api/net/feed': {
            const channel = url.searchParams.get('channel');
            const messages = channel ? RECENT.filter(row => row.channel === channel) : RECENT;
            return json({ messages, next_since: 'end', more: false });
          }
          case '/api/net/threads':
            return json({
              threads: [
                {
                  channel: '@ada',
                  thread: 'gh:acme/app#42',
                  project_id: null,
                  message_count: 1,
                  first_message_at: '2026-11-05T10:02:00Z',
                  last_message_at: '2026-11-05T10:02:00Z',
                  reflected_through: null,
                  reflection_source_id: null,
                  reflection_error: null,
                  reflection_skipped: [],
                },
              ],
            });
          case '/api/net/presence':
            return json({
              watching: [
                {
                  channel: null,
                  user_id: 'u-grace',
                  name: 'Grace',
                  agent: 'codex',
                  session: 's',
                  last_seen: '2026-11-05T10:02:00Z',
                },
              ],
              recent: [],
            });
          default:
            return json({ detail: 'Not Found' }, 404);
        }
      })
    );
  });

  afterEach(() => vi.unstubAllGlobals());

  it("points at your inbox's newest thread, the busiest channel and who is listening", async () => {
    render(<NetDashboardCard />);

    const card = await screen.findByRole('region', { name: 'Agent chatter' });
    const inbox = await within(card).findByRole('link', { name: /Your inbox/ });
    expect(inbox).toHaveAttribute('href', '/net?channel=%40ada&thread=gh%3Aacme%2Fapp%2342');
    expect(await within(inbox).findByText('1 new')).toBeInTheDocument();
    const busiest = await within(card).findByRole('link', { name: /Most active/ });
    expect(busiest).toHaveAttribute('href', '/net?channel=%23reviews');
    expect(busiest).toHaveTextContent('2 of the last 3');
    expect(
      await within(card).findByText('1 listening across the organization')
    ).toBeInTheDocument();
    expect(within(card).getByRole('link', { name: /Open Net/ })).toHaveAttribute('href', '/net');
  });

  it('leaves the inbox out for a viewer with no active organization', async () => {
    me = { ...me, organization: null, org_role: null, handle: null, inbox: null };
    render(<NetDashboardCard />);

    const card = await screen.findByRole('region', { name: 'Agent chatter' });
    expect(await within(card).findByRole('link', { name: /Most active/ })).toBeInTheDocument();
    expect(within(card).queryByText('Your inbox')).toBeNull();
  });

  it('says plainly when agents have not talked yet', async () => {
    me = { ...me, organization: null, org_role: null, handle: null, inbox: null };
    channels = [];
    render(<NetDashboardCard />);

    expect(await screen.findByText(/No agent traffic yet/)).toBeInTheDocument();
  });
});

describe('NetDashboardCard on the first-run screen', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('stays out of the way until agents have something to show', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(String(input), 'http://localhost').pathname;
      if (path === '/api/auth/me')
        return json({
          user: { id: 'u-ada' },
          organization: null,
          org_role: null,
          handle: null,
          inbox: null,
        });
      if (path === '/api/net/channels') return json({ channels: [] });
      return json({ detail: 'Not Found' }, 404);
    });
    vi.stubGlobal('fetch', fetchMock);
    const { container } = render(<NetDashboardCard hideWhenQuiet />);

    await vi.waitFor(() =>
      expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/net/channels'))).toBe(
        true
      )
    );
    await new Promise(resolve => setTimeout(resolve, 20));
    expect(container).toBeEmptyDOMElement();
  });
});

describe('busiestChannel', () => {
  it('picks the channel with the most recent messages', () => {
    expect(busiestChannel(RECENT as never)).toEqual({ channel: '#reviews', count: 2 });
    expect(busiestChannel([])).toBeNull();
  });
});
