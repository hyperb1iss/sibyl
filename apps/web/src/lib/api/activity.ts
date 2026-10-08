import { fetchApi, type RequestOptions } from './transport';

/** Time windows the team activity endpoint aggregates over. */
export type TeamActivityWindow = '24h' | '7d' | '30d';

export type TeamActivityRole = 'owner' | 'admin' | 'member' | 'viewer';

export type TeamActivityKind =
  | 'capture'
  | 'task_created'
  | 'task_completed'
  | 'decision'
  | 'note'
  | 'procedure'
  | 'entity';

/** Per-person totals inside the window. `other` counts `entity` items. */
export interface TeamActivityCounts {
  captures: number;
  tasks_created: number;
  tasks_completed: number;
  decisions: number;
  notes: number;
  procedures: number;
  other: number;
}

export interface TeamActivityPerson {
  user_id: string;
  name: string;
  email: string | null;
  avatar_url: string | null;
  role: TeamActivityRole;
  counts: TeamActivityCounts;
  last_active_at: string | null;
}

export interface TeamActivityItem {
  kind: TeamActivityKind;
  id: string;
  title: string;
  entity_type: string | null;
  project_id: string | null;
  /** Set when the caller can read the project. */
  project_name: string | null;
  actor_id: string;
  /** The member's display name, as the people list shows it. */
  actor_name: string | null;
  actor_avatar_url: string | null;
  at: string;
  /** App path the item opens at. */
  href: string;
}

export interface TeamActivityRange {
  since: string;
  until: string;
  label: TeamActivityWindow;
}

export interface TeamActivityResponse {
  window: TeamActivityRange;
  /** The project filter when exactly one project was requested. */
  project_id: string | null;
  /** Every requested project; activity in any of them counts. */
  project_ids: string[] | null;
  /** When set, `recent` holds only this member's events. */
  actor_id: string | null;
  /** Every member, zero activity included, sorted by activity. */
  people: TeamActivityPerson[];
  /** Newest first, at most 100 items, one member's when `actor_id` is set. */
  recent: TeamActivityItem[];
  truncated: boolean;
}

export interface TeamActivityParams {
  window: TeamActivityWindow;
  /** Activity in any of these projects. Omit for every project the viewer can read. */
  project_ids?: string[];
  /** Narrow `recent` to one member; `people` still lists everyone. */
  actor_id?: string;
}

export const activityApi = {
  team: (params: TeamActivityParams, options?: RequestOptions) => {
    const query = new URLSearchParams({ window: params.window });
    for (const projectId of params.project_ids ?? []) query.append('project_id', projectId);
    if (params.actor_id) query.set('actor_id', params.actor_id);
    return fetchApi<TeamActivityResponse>(`/activity/team?${query.toString()}`, {
      signal: options?.signal,
    });
  },
};
