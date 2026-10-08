import type {
  TeamActivityCounts,
  TeamActivityItem,
  TeamActivityPerson,
  TeamActivityResponse,
  TeamActivityWindow,
} from '@/lib/api/activity';

/** Sample data for stories and tests. Never imported by the app. */

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

const WINDOW_MS: Record<TeamActivityWindow, number> = {
  '24h': DAY,
  '7d': 7 * DAY,
  '30d': 30 * DAY,
};

export const FIXTURE_PROJECTS = [
  { id: 'project_core', name: 'Sibyl Core' },
  { id: 'project_web', name: 'Sibyl Web' },
  { id: 'project_ops', name: 'Homelab Ops' },
];

function counts(partial: Partial<TeamActivityCounts>): TeamActivityCounts {
  return {
    captures: 0,
    tasks_created: 0,
    tasks_completed: 0,
    decisions: 0,
    notes: 0,
    procedures: 0,
    other: 0,
    ...partial,
  };
}

export function fixturePeople(now: number): TeamActivityPerson[] {
  const ago = (ms: number) => new Date(now - ms).toISOString();
  return [
    {
      user_id: 'user_ada',
      name: 'Ada Lovelace',
      email: 'ada@example.com',
      avatar_url: null,
      role: 'owner',
      counts: counts({
        captures: 12,
        tasks_completed: 5,
        tasks_created: 3,
        decisions: 2,
        notes: 4,
        procedures: 1,
        other: 2,
      }),
      last_active_at: ago(25 * MINUTE),
    },
    {
      user_id: 'user_grace',
      name: 'Grace Hopper',
      email: 'grace@example.com',
      avatar_url: null,
      role: 'admin',
      counts: counts({
        captures: 6,
        tasks_completed: 4,
        tasks_created: 6,
        decisions: 1,
        procedures: 2,
      }),
      last_active_at: ago(HOUR),
    },
    {
      user_id: 'user_alan',
      name: 'Alan Turing',
      email: 'alan@example.com',
      avatar_url: null,
      role: 'member',
      counts: counts({ captures: 3, tasks_completed: 1, notes: 2 }),
      last_active_at: ago(5 * HOUR),
    },
    {
      user_id: 'user_katherine',
      name: 'Katherine Johnson',
      email: 'katherine@example.com',
      avatar_url: null,
      role: 'member',
      counts: counts({ captures: 1, decisions: 1 }),
      last_active_at: ago(DAY + 3 * HOUR),
    },
    {
      user_id: 'user_margaret',
      name: 'Margaret Hamilton',
      email: 'margaret@example.com',
      avatar_url: null,
      role: 'member',
      counts: counts({}),
      last_active_at: ago(12 * DAY),
    },
    {
      user_id: 'user_dennis',
      name: 'Dennis Ritchie',
      email: null,
      avatar_url: null,
      role: 'viewer',
      counts: counts({}),
      last_active_at: null,
    },
  ];
}

type ItemSeed = [
  kind: TeamActivityItem['kind'],
  title: string,
  actor: string,
  project: string | null,
  agoMs: number,
  entityType?: string,
];

const ITEM_SEEDS: ItemSeed[] = [
  [
    'capture',
    'Surreal 3.x rejects ORDER BY on a field missing from the projection',
    'user_ada',
    'project_core',
    25 * MINUTE,
  ],
  [
    'task_completed',
    'Gate the graph page on the resolved project scope',
    'user_grace',
    'project_web',
    HOUR,
  ],
  [
    'decision',
    'Keep the embedded pool clamped to one writer',
    'user_ada',
    'project_core',
    2 * HOUR,
  ],
  [
    'task_created',
    'Add cursor paging to the team activity feed',
    'user_grace',
    'project_web',
    3 * HOUR,
  ],
  ['note', 'Reranker weights drift after a passage rebuild', 'user_alan', 'project_core', 5 * HOUR],
  [
    'procedure',
    'Rotate provider keys without restarting workers',
    'user_grace',
    'project_ops',
    6 * HOUR,
  ],
  [
    'entity',
    'Coalesce websocket invalidations per query key',
    'user_ada',
    'project_web',
    DAY + 2 * HOUR,
    'pattern',
  ],
  [
    'capture',
    'Release dry run fails when the nightly SHA drifts',
    'user_katherine',
    null,
    DAY + 3 * HOUR,
  ],
  [
    'task_completed',
    'Scope stop-dev kills to the current checkout',
    'user_ada',
    'project_ops',
    DAY + 5 * HOUR,
  ],
  [
    'decision',
    'Open the team view on the shared project selector',
    'user_katherine',
    'project_web',
    2 * DAY + HOUR,
  ],
  [
    'capture',
    'Context packs lose agent diaries without a null project',
    'user_alan',
    'project_core',
    3 * DAY,
  ],
  ['task_created', 'Teach the CLI to restore hidden memories', 'user_ada', 'project_core', 3 * DAY],
  ['note', 'OrbStack forwards accept TCP after the VM dies', 'user_ada', 'project_ops', 4 * DAY],
  [
    'task_completed',
    'Pin the release workflow to the approved SHA',
    'user_grace',
    'project_ops',
    5 * DAY,
  ],
];

// The API builds hrefs; these mirror its shapes.
function hrefFor(kind: TeamActivityItem['kind'], id: string): string {
  if (kind === 'capture') return `/memory/captures?id=${id}`;
  if (kind === 'task_created' || kind === 'task_completed') return `/tasks/${id}`;
  return `/entities/${id}`;
}

const PROJECT_NAMES: Record<string, string> = Object.fromEntries(
  FIXTURE_PROJECTS.map(project => [project.id, project.name])
);

export function fixtureRecent(now: number): TeamActivityItem[] {
  const people = new Map(fixturePeople(now).map(person => [person.user_id, person]));
  return ITEM_SEEDS.map(([kind, title, actor, project, agoMs, entityType], index) => {
    const id = `item_${index + 1}`;
    return {
      kind,
      id,
      title,
      entity_type:
        entityType ?? (kind.startsWith('task') ? 'task' : kind === 'capture' ? null : kind),
      project_id: project,
      project_name: project ? (PROJECT_NAMES[project] ?? null) : null,
      actor_id: actor,
      actor_name: people.get(actor)?.name ?? null,
      actor_avatar_url: people.get(actor)?.avatar_url ?? null,
      at: new Date(now - agoMs).toISOString(),
      href: hrefFor(kind, id),
    };
  });
}

export function fixtureTeamActivity(
  now: number,
  window: TeamActivityWindow = '7d',
  overrides: Partial<TeamActivityResponse> = {}
): TeamActivityResponse {
  return {
    window: {
      since: new Date(now - WINDOW_MS[window]).toISOString(),
      until: new Date(now).toISOString(),
      label: window,
    },
    project_id: null,
    project_ids: null,
    actor_id: null,
    people: fixturePeople(now),
    recent: fixtureRecent(now),
    truncated: false,
    ...overrides,
  };
}

/** The answer with `actor_id` set: the feed holds only that member's events. */
export function fixtureActorActivity(
  now: number,
  actorId: string,
  window: TeamActivityWindow = '7d'
): TeamActivityResponse {
  return fixtureTeamActivity(now, window, {
    actor_id: actorId,
    recent: fixtureRecent(now).filter(item => item.actor_id === actorId),
  });
}

/** Everyone on the team, nobody active. */
export function fixtureQuietTeam(now: number, window: TeamActivityWindow = '7d') {
  return fixtureTeamActivity(now, window, {
    people: fixturePeople(now).map(person => ({ ...person, counts: counts({}) })),
    recent: [],
  });
}
