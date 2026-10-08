import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  FIXTURE_PROJECTS,
  fixtureQuietTeam,
  fixtureTeamActivity,
} from '@/components/team/team-activity.fixtures';
import type { TeamActivityResponse } from '@/lib/api/activity';
import { ProjectContextProvider } from '@/lib/project-context';
import { render, screen, waitFor, within } from '@/test/utils';

// A URL that moves when the page writes to it, like the real router.
const navigation = vi.hoisted(() => {
  let params = new URLSearchParams();
  const listeners = new Set<() => void>();
  return {
    replace: vi.fn(),
    get params() {
      return params;
    },
    set(search: string) {
      params = new URLSearchParams(search);
      for (const listener of listeners) listener();
    },
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
});

vi.mock('next/navigation', async () => {
  const { useSyncExternalStore } = await import('react');
  return {
    useRouter: () => ({ replace: navigation.replace, push: vi.fn() }),
    usePathname: () => '/team',
    useSearchParams: () =>
      useSyncExternalStore(
        navigation.subscribe,
        () => navigation.params,
        () => navigation.params
      ),
  };
});

import { TeamContent } from './team-content';

const NOW = Date.now();

const PROJECTS_RESPONSE = {
  mode: 'list',
  filters: {},
  total: FIXTURE_PROJECTS.length,
  has_more: false,
  entities: FIXTURE_PROJECTS.map(project => ({
    id: project.id,
    type: 'project',
    name: project.name,
    description: '',
    metadata: {},
  })),
};

const ME_RESPONSE = {
  user: {
    id: 'user_grace',
    github_id: null,
    email: 'grace@example.com',
    name: 'Grace Hopper',
    avatar_url: null,
  },
  organization: { id: 'org_1', slug: 'team', name: 'Team' },
  org_role: 'admin',
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

let activityResponse: (url: URL) => Response | Promise<Response>;
let fetchMock: ReturnType<typeof vi.fn>;

function activityRequests(): URL[] {
  return fetchMock.mock.calls
    .map(([input]) => new URL(String(input), 'http://localhost'))
    .filter(url => url.pathname === '/api/activity/team');
}

function lastActivityRequest(): URL | undefined {
  return activityRequests().at(-1);
}

/** Answer like the API: an actor_id request gets only that member's feed. */
function serve(response: TeamActivityResponse) {
  activityResponse = url => {
    const actor = url.searchParams.get('actor_id');
    if (!actor) return json(response);
    return json({
      ...response,
      actor_id: actor,
      recent: response.recent.filter(item => item.actor_id === actor),
    });
  };
}

function renderTeam(search = 'projects=all') {
  navigation.set(search);
  return render(
    <ProjectContextProvider>
      <TeamContent />
    </ProjectContextProvider>
  );
}

// jsdom in this runner has no usable localStorage, and the project context
// remembers "All projects" there, so each test gets its own empty store.
function createMemoryStorage(): Storage {
  const store = new Map<string, string>();
  return {
    get length() {
      return store.size;
    },
    clear: () => store.clear(),
    getItem: (key: string) => store.get(key) ?? null,
    key: (index: number) => [...store.keys()][index] ?? null,
    removeItem: (key: string) => {
      store.delete(key);
    },
    setItem: (key: string, value: string) => {
      store.set(key, String(value));
    },
  };
}

function feed() {
  return screen.getByRole('region', { name: /^Activity/ });
}

describe('TeamContent', () => {
  beforeEach(() => {
    navigation.replace.mockReset();
    navigation.replace.mockImplementation((href: string) => {
      navigation.set(href.split('?')[1] ?? '');
    });
    serve(fixtureTeamActivity(NOW));
    fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), 'http://localhost');
      if (url.pathname === '/api/activity/team') return activityResponse(url);
      if (url.pathname === '/api/search/explore') return json(PROJECTS_RESPONSE);
      if (url.pathname === '/api/auth/me') return json(ME_RESPONSE);
      return json({ detail: 'not found' }, 404);
    });
    vi.stubGlobal('fetch', fetchMock);
    vi.stubGlobal('localStorage', createMemoryStorage());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('renders people in the order the server sent, quiet members included', async () => {
    renderTeam();

    const people = await screen.findByRole('region', { name: /^People/ });
    const names = within(people)
      .getAllByRole('button')
      .map(button => button.textContent);
    expect(names).toEqual([
      'Ada Lovelace',
      'Grace Hopper',
      'Alan Turing',
      'Katherine Johnson',
      'Margaret Hamilton',
      'Dennis Ritchie',
    ]);

    const quiet = within(people)
      .getByRole('button', { name: 'Margaret Hamilton' })
      .closest('article');
    expect(quiet).toHaveAttribute('data-quiet');
    expect(within(quiet as HTMLElement).getByText('No activity in the last 7 days')).toBeVisible();

    const ada = within(people).getByRole('button', { name: 'Ada Lovelace' }).closest('article');
    expect(ada).not.toHaveAttribute('data-quiet');
    expect(within(ada as HTMLElement).getByText('29')).toBeVisible();
    expect(within(ada as HTMLElement).getByText('tasks done')).toBeVisible();

    expect(await screen.findByText('(you)')).toBeVisible();
    expect(screen.getByText('4 of 6 people active in the last 7 days')).toBeVisible();
  });

  it('defaults to seven days and refetches when the window changes', async () => {
    const { user } = renderTeam();
    await screen.findByRole('region', { name: /^People/ });

    expect(lastActivityRequest()?.searchParams.get('window')).toBe('7d');
    expect(screen.getByRole('button', { name: '7 days' })).toHaveAttribute('aria-pressed', 'true');

    await user.click(screen.getByRole('button', { name: '24h' }));

    const href = navigation.replace.mock.calls.at(-1)?.[0] as string;
    const url = new URL(href, 'http://localhost');
    expect(url.pathname).toBe('/team');
    expect(url.searchParams.get('window')).toBe('24h');
    expect(url.searchParams.get('projects')).toBe('all');
    await waitFor(() => expect(lastActivityRequest()?.searchParams.get('window')).toBe('24h'));
    expect(screen.getByRole('button', { name: '24h' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('keeps the current answer on screen while a new window loads', async () => {
    const { user } = renderTeam();
    await screen.findByRole('region', { name: /^People/ });

    // The 24h answer never arrives, so the page stays mid-switch.
    activityResponse = url =>
      url.searchParams.get('window') === '24h'
        ? new Promise<Response>(() => undefined)
        : json(fixtureTeamActivity(NOW));
    await user.click(screen.getByRole('button', { name: '24h' }));

    await waitFor(() => expect(lastActivityRequest()?.searchParams.get('window')).toBe('24h'));
    const people = screen.getByRole('region', { name: /^People/ });
    expect(people).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByText('Updating')).toBeVisible();
    expect(screen.queryByLabelText('Loading team activity')).not.toBeInTheDocument();
    // The summary still describes the data on screen, not the pending window.
    expect(screen.getByText('4 of 6 people active in the last 7 days')).toBeVisible();
  });

  it('opens a first visit on one project and never asks for every project', async () => {
    renderTeam('');

    await screen.findByRole('region', { name: /^People/ });
    expect(activityRequests().map(url => url.searchParams.get('project_id'))).toEqual([
      'project_core',
    ]);
  });

  it('scopes a shared project link without an unscoped request first', async () => {
    renderTeam('projects=project_web');

    await screen.findByRole('region', { name: /^People/ });
    expect(activityRequests().map(url => url.searchParams.get('project_id'))).toEqual([
      'project_web',
    ]);
    expect(screen.getByRole('combobox', { name: 'Project scope' })).toHaveTextContent('Sibyl Web');
  });

  it('reads the window from a shared link', async () => {
    renderTeam('projects=all&window=30d');

    await screen.findByRole('region', { name: /^People/ });
    expect(activityRequests().map(url => url.searchParams.get('window'))).toEqual(['30d']);
  });

  it('scopes the request to the project picked in the filter', async () => {
    const { user } = renderTeam();
    await screen.findByRole('region', { name: /^People/ });
    expect(lastActivityRequest()?.searchParams.has('project_id')).toBe(false);

    await user.click(screen.getByRole('combobox', { name: 'Project scope' }));
    await user.click(await screen.findByRole('option', { name: 'Sibyl Web' }));

    await waitFor(() =>
      expect(lastActivityRequest()?.searchParams.get('project_id')).toBe('project_web')
    );
    const href = navigation.replace.mock.calls.at(-1)?.[0] as string;
    expect(new URL(href, 'http://localhost').searchParams.get('projects')).toBe('project_web');

    await user.click(screen.getByRole('combobox', { name: 'Project scope' }));
    await user.click(await screen.findByRole('option', { name: 'All projects' }));

    await waitFor(() => expect(lastActivityRequest()?.searchParams.has('project_id')).toBe(false));
  });

  it('narrows the feed to one person on the server and back', async () => {
    const { user } = renderTeam();
    await screen.findByRole('region', { name: /^People/ });
    expect(within(feed()).getAllByRole('link')).toHaveLength(14);

    const alan = screen.getByRole('button', { name: 'Alan Turing' });
    await user.click(alan);

    expect(alan).toHaveAttribute('aria-pressed', 'true');
    await waitFor(() =>
      expect(lastActivityRequest()?.searchParams.get('actor_id')).toBe('user_alan')
    );
    expect(lastActivityRequest()?.searchParams.get('window')).toBe('7d');
    await waitFor(() => expect(feed().parentElement).not.toHaveAttribute('aria-busy'));
    expect(
      within(feed())
        .getAllByRole('link')
        .map(link => link.textContent)
    ).toEqual([
      'Reranker weights drift after a passage rebuild',
      'Context packs lose agent diaries without a null project',
    ]);

    await user.click(alan);
    expect(alan).toHaveAttribute('aria-pressed', 'false');
    await waitFor(() => expect(within(feed()).getAllByRole('link')).toHaveLength(14));
    expect(lastActivityRequest()?.searchParams.has('actor_id')).toBe(false);
  });

  it('narrows the feed at once while the person request loads', async () => {
    const { user } = renderTeam();
    await screen.findByRole('region', { name: /^People/ });

    // The member's answer never arrives: the page must not wait on it.
    activityResponse = url =>
      url.searchParams.has('actor_id')
        ? new Promise<Response>(() => undefined)
        : json(fixtureTeamActivity(NOW));
    await user.click(screen.getByRole('button', { name: 'Alan Turing' }));

    expect(within(feed()).getAllByRole('link')).toHaveLength(2);
    await waitFor(() =>
      expect(lastActivityRequest()?.searchParams.get('actor_id')).toBe('user_alan')
    );
    // Only the feed is waiting; the people grid is current.
    expect(feed().parentElement).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByRole('region', { name: /^People/ })).not.toHaveAttribute('aria-busy');
  });

  it('tells a person filter with nothing to show apart from an empty team', async () => {
    const { user } = renderTeam();
    await screen.findByRole('region', { name: /^People/ });

    await user.click(screen.getByRole('button', { name: 'Margaret Hamilton' }));

    expect(
      within(feed()).getByText('Nothing from Margaret Hamilton in the last 7 days')
    ).toBeVisible();
    await user.click(within(feed()).getByRole('button', { name: 'Show everyone' }));
    expect(within(feed()).getAllByRole('link')).toHaveLength(14);
  });

  it('offers a way back to everyone when a member request fails', async () => {
    const { user } = renderTeam();
    await screen.findByRole('region', { name: /^People/ });

    activityResponse = url =>
      url.searchParams.has('actor_id')
        ? json({ detail: 'boom' }, 500)
        : json(fixtureTeamActivity(NOW));
    await user.click(screen.getByRole('button', { name: 'Alan Turing' }));

    expect(await screen.findByText("Couldn't load team activity")).toBeVisible();
    await user.click(screen.getByRole('button', { name: 'Show everyone' }));

    expect(await screen.findByRole('region', { name: /^People/ })).toBeVisible();
    expect(within(feed()).getAllByRole('link')).toHaveLength(14);
    expect(lastActivityRequest()?.searchParams.has('actor_id')).toBe(false);
  });

  it('never shows a member answer as the team feed while deselecting', async () => {
    const { user } = renderTeam();
    await screen.findByRole('region', { name: /^People/ });
    const alan = screen.getByRole('button', { name: 'Alan Turing' });
    await user.click(alan);
    await user.click(screen.getByRole('button', { name: '30 days' }));
    await waitFor(() => expect(lastActivityRequest()?.searchParams.get('window')).toBe('30d'));
    await waitFor(() => expect(within(feed()).getAllByRole('link')).toHaveLength(2));

    // The team's 30-day answer is not cached yet and never arrives.
    activityResponse = url =>
      url.searchParams.has('actor_id')
        ? json(fixtureTeamActivity(NOW, '30d'))
        : new Promise<Response>(() => undefined);
    await user.click(alan);

    expect(screen.getByLabelText('Loading the team feed')).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: /^Activity/ })).not.toBeInTheDocument();
  });

  it('names actors and projects from the item, with fallbacks', async () => {
    const base = fixtureTeamActivity(NOW);
    const [first, second, third] = base.recent;
    serve({
      ...base,
      recent: [
        // Not in the people list, but the item carries its own names.
        {
          ...first,
          actor_id: 'user_former',
          actor_name: 'Former Teammate',
          actor_avatar_url: null,
          project_id: 'project_gone',
          project_name: 'Retired Project',
        },
        // No names on the item: fall back to the people list and project lookup.
        { ...second, actor_name: null, project_name: null },
        // Nothing anywhere names this actor.
        { ...third, actor_id: 'user_ghost', actor_name: null },
      ],
    });
    renderTeam();
    await screen.findByRole('region', { name: /^People/ });

    const rows = within(feed()).getAllByRole('listitem');
    expect(within(rows[0]).getByText('Former Teammate')).toBeVisible();
    expect(within(rows[0]).getByText('Retired Project')).toBeVisible();
    expect(within(rows[1]).getByText('Grace Hopper')).toBeVisible();
    expect(within(rows[1]).getByText('Sibyl Web')).toBeVisible();
    expect(within(rows[2]).getByText('Unknown member')).toBeVisible();
    expect(within(feed()).getAllByText('Unknown member')).toHaveLength(1);
  });

  it('badges viewers', async () => {
    renderTeam();
    const people = await screen.findByRole('region', { name: /^People/ });

    const dennis = within(people)
      .getByRole('button', { name: 'Dennis Ritchie' })
      .closest('article');
    expect(within(dennis as HTMLElement).getByText('Viewer')).toBeVisible();
  });

  it('asks for several selected projects in one request', async () => {
    renderTeam('projects=project_web,project_core');

    await screen.findByRole('region', { name: /^People/ });
    const requests = activityRequests();
    expect(requests).toHaveLength(1);
    expect(requests[0].searchParams.getAll('project_id')).toEqual(['project_core', 'project_web']);
  });

  it('says when the feed is cut at the latest 100', async () => {
    serve(fixtureTeamActivity(NOW, '7d', { truncated: true }));
    renderTeam();

    expect(await screen.findByText(/Showing the latest 100 team updates/)).toBeVisible();
  });

  it('leaves the truncation note off a complete feed', async () => {
    renderTeam();
    await screen.findByRole('region', { name: /^People/ });

    expect(screen.queryByText(/Showing the latest 100/)).not.toBeInTheDocument();
  });

  it('shows the empty state when nobody did anything', async () => {
    serve(fixtureQuietTeam(NOW));
    renderTeam();

    expect(await screen.findByText('No team activity in the last 7 days')).toBeVisible();
    // The team is still listed, every card quiet.
    const people = screen.getByRole('region', { name: /^People/ });
    expect(within(people).getAllByRole('article')).toHaveLength(6);
    expect(within(people).getAllByText(/^No activity in/)).toHaveLength(6);
  });

  it('shows an error with a retry that recovers', async () => {
    activityResponse = () => json({ detail: 'boom' }, 500);
    const { user } = renderTeam();

    expect(await screen.findByText("Couldn't load team activity")).toBeVisible();

    serve(fixtureTeamActivity(NOW));
    await user.click(screen.getByRole('button', { name: 'Retry' }));

    expect(await screen.findByRole('region', { name: /^People/ })).toBeVisible();
    expect(screen.queryByText("Couldn't load team activity")).not.toBeInTheDocument();
  });
});
