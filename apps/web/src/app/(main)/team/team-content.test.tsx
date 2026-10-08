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

let activityResponse: () => Response;
let fetchMock: ReturnType<typeof vi.fn>;

function activityRequests(): URL[] {
  return fetchMock.mock.calls
    .map(([input]) => new URL(String(input), 'http://localhost'))
    .filter(url => url.pathname === '/api/activity/team');
}

function lastActivityRequest(): URL | undefined {
  return activityRequests().at(-1);
}

function serve(response: TeamActivityResponse) {
  activityResponse = () => json(response);
}

function renderTeam(search = 'projects=all') {
  navigation.set(search);
  return render(
    <ProjectContextProvider>
      <TeamContent />
    </ProjectContextProvider>
  );
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
      if (url.pathname === '/api/activity/team') return activityResponse();
      if (url.pathname === '/api/search/explore') return json(PROJECTS_RESPONSE);
      if (url.pathname === '/api/auth/me') return json(ME_RESPONSE);
      return json({ detail: 'not found' }, 404);
    });
    vi.stubGlobal('fetch', fetchMock);
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

  it('narrows the feed to one person and back', async () => {
    const { user } = renderTeam();
    await screen.findByRole('region', { name: /^People/ });
    expect(within(feed()).getAllByRole('link')).toHaveLength(14);

    const alan = screen.getByRole('button', { name: 'Alan Turing' });
    await user.click(alan);

    expect(alan).toHaveAttribute('aria-pressed', 'true');
    const links = within(feed()).getAllByRole('link');
    expect(links.map(link => link.textContent)).toEqual([
      'Reranker weights drift after a passage rebuild',
      'Context packs lose agent diaries without a null project',
    ]);
    expect(within(feed()).getByText('2 of 14')).toBeVisible();

    await user.click(alan);
    expect(alan).toHaveAttribute('aria-pressed', 'false');
    expect(within(feed()).getAllByRole('link')).toHaveLength(14);
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
