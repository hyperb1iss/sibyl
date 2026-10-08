import { fetchApi, type RequestOptions } from './transport';

/** Time windows the team activity endpoint aggregates over. */
export type TeamActivityWindow = '24h' | '7d' | '30d';

export type TeamActivityRole = 'owner' | 'admin' | 'member';

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
  actor_id: string;
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
  project_id: string | null;
  /** Every member, zero activity included, sorted by activity. */
  people: TeamActivityPerson[];
  /** Newest first, at most 100 items. */
  recent: TeamActivityItem[];
  truncated: boolean;
}

export interface TeamActivityParams {
  window: TeamActivityWindow;
  /** Omit for every project the viewer can read. */
  project_id?: string;
}

export const activityApi = {
  team: (params: TeamActivityParams, options?: RequestOptions) => {
    const query = new URLSearchParams({ window: params.window });
    if (params.project_id) query.set('project_id', params.project_id);
    return fetchApi<TeamActivityResponse>(`/activity/team?${query.toString()}`, {
      signal: options?.signal,
    });
  },
};
