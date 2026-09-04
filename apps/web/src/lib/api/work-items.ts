import type { Entity } from './graph';
import type { BaseMetadata } from './shared';
import { fetchApi } from './transport';

export interface TaskMetadata extends BaseMetadata {
  status?: TaskStatus;
  priority?: TaskPriority;
  project_id?: string;
  epic_id?: string;
  due_date?: string;
  feature?: string;
  tags?: string[];
  assignees?: string[];
  branch_name?: string;
  pr_url?: string;
  estimated_hours?: number;
  actual_hours?: number;
  technologies?: string[];
  blocker_reason?: string;
  learnings?: string;
  task_order?: number;
}

export interface ProjectMetadata extends BaseMetadata {
  status?: 'active' | 'archived' | 'paused';
  repository_url?: string;
  technologies?: string[];
  tech_stack?: string[]; // Alias for technologies
  features?: string[];
  last_activity_at?: string;
  task_count?: number;
}

/** Epic entity metadata */
export interface EpicMetadata extends BaseMetadata {
  priority?: TaskPriority;
  project_id?: string;
  status?: 'planning' | 'in_progress' | 'blocked' | 'completed' | 'archived';
  total_tasks?: number;
  completed_tasks?: number;
  in_progress_tasks?: number;
  blocked_tasks?: number;
  in_review_tasks?: number;
  completion_pct?: number;
}

export type TaskStatus = 'backlog' | 'todo' | 'doing' | 'blocked' | 'review' | 'done' | 'archived';

/** Type for task priority values */
export type TaskPriority = 'critical' | 'high' | 'medium' | 'low' | 'someday';

export interface TaskStatusDistribution {
  backlog: number;
  todo: number;
  doing: number;
  blocked: number;
  review: number;
  done: number;
}

export interface TaskPriorityDistribution {
  critical: number;
  high: number;
  medium: number;
  low: number;
  someday: number;
}

export interface AssigneeStats {
  name: string;
  total: number;
  completed: number;
  in_progress: number;
}

export interface TimeSeriesPoint {
  date: string;
  value: number;
}

export interface ProjectMetrics {
  project_id: string;
  project_name: string;
  total_tasks: number;
  status_distribution: TaskStatusDistribution;
  priority_distribution: TaskPriorityDistribution;
  completion_rate: number;
  assignees: AssigneeStats[];
  tasks_created_last_7d: number;
  tasks_completed_last_7d: number;
  velocity_trend: TimeSeriesPoint[];
}

export interface ProjectMetricsResponse {
  metrics: ProjectMetrics;
}

export interface ProjectSummary {
  id: string;
  name: string;
  total: number;
  completed: number;
  doing: number;
  blocked: number;
  review: number;
  todo: number;
  backlog: number;
  critical: number;
  high: number;
  overdue: number;
  completion_rate: number;
}

export interface ProjectSummariesResponse {
  projects_summary: ProjectSummary[];
}

export interface OrgMetricsResponse {
  total_projects: number;
  total_tasks: number;
  status_distribution: TaskStatusDistribution;
  priority_distribution: TaskPriorityDistribution;
  completion_rate: number;
  top_assignees: AssigneeStats[];
  tasks_created_last_7d: number;
  tasks_completed_last_7d: number;
  velocity_trend: TimeSeriesPoint[];
  projects_summary: ProjectSummary[];
}

export type ProjectRole =
  | 'project_owner'
  | 'project_maintainer'
  | 'project_contributor'
  | 'project_viewer';

export interface ProjectMember {
  user: {
    id: string;
    email: string | null;
    name: string | null;
    avatar_url: string | null;
  };
  role: ProjectRole;
  is_owner: boolean;
  created_at: string;
}

export interface ProjectMembersResponse {
  members: ProjectMember[];
  can_manage: boolean;
}

export interface Task {
  id: string;
  title: string;
  description: string;
  status: TaskStatus;
  priority: TaskPriority;
  task_order: number;
  project_id: string | null;
  feature: string | null;
  assignees: string[];
  due_date: string | null;
  technologies: string[];
  domain: string | null;
  branch_name: string | null;
  pr_url: string | null;
  created_at: string | null;
  updated_at: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface TaskListResponse {
  mode: string;
  entities: TaskSummary[];
  total: number;
  filters: Record<string, unknown>;
  has_more?: boolean;
  actual_total?: number | null;
}

export interface TaskSummary {
  id: string;
  type: string;
  name: string;
  description: string;
  metadata: {
    status?: TaskStatus;
    priority?: TaskPriority;
    project_id?: string;
    assignees?: string[];
    [key: string]: unknown;
  };
}

export interface Project {
  id: string;
  title: string;
  description: string;
  status: 'planning' | 'active' | 'on_hold' | 'completed' | 'archived';
  repository_url: string | null;
  features: string[];
  tech_stack: string[];
  total_tasks: number;
  completed_tasks: number;
  in_progress_tasks: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface TaskActionResponse {
  success: boolean;
  action: string;
  task_id: string;
  message: string;
  data: Record<string, unknown>;
}

export interface EpicActionResponse {
  success: boolean;
  action: string;
  epic_id: string;
  message: string;
  data: Record<string, unknown>;
}

// =============================================================================
// Epic Types
// =============================================================================

export type EpicStatus = 'planning' | 'in_progress' | 'blocked' | 'completed' | 'archived';

export interface Epic {
  id: string;
  title: string;
  description: string;
  project_id: string;
  status: EpicStatus;
  priority: TaskPriority;
  assignees: string[];
  tags: string[];
  start_date: string | null;
  target_date: string | null;
  completed_date: string | null;
  total_tasks: number;
  completed_tasks: number;
  learnings: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface EpicListResponse {
  mode: string;
  entities: EpicSummary[];
  total: number;
  filters: Record<string, unknown>;
}

export interface EpicSummary {
  id: string;
  type: string;
  name: string;
  description: string;
  metadata: {
    status?: EpicStatus;
    priority?: TaskPriority;
    project_id?: string;
    assignees?: string[];
    total_tasks?: number;
    completed_tasks?: number;
    in_progress_tasks?: number;
    blocked_tasks?: number;
    in_review_tasks?: number;
    completion_pct?: number;
    [key: string]: unknown;
  };
}

export interface EpicProgress {
  total_tasks: number;
  completed_tasks: number;
  in_progress_tasks: number;
  blocked_tasks: number;
  in_review_tasks: number;
  completion_pct: number;
}

// =============================================================================
// Task Notes Types
// =============================================================================

export type AuthorType = 'agent' | 'user';

export interface Note {
  id: string;
  task_id: string;
  content: string;
  author_type: AuthorType;
  author_name: string;
  created_at: string;
}

export interface NotesListResponse {
  notes: Note[];
  count: number;
}

export interface CreateNoteRequest {
  content: string;
  author_type?: AuthorType;
  author_name?: string;
}

/** Explore list pages are capped server-side at 1000 rows. */
export const TASK_PAGE_SIZE = 1000;
/** The cap an API from before 1.3.1 still enforces. */
export const LEGACY_TASK_PAGE_SIZE = 200;

export const tasksApi = {
  list: (
    params?: { project?: string; project_ids?: string[]; status?: TaskStatus },
    page?: { limit?: number; offset?: number }
  ) =>
    fetchApi<TaskListResponse>('/search/explore', {
      method: 'POST',
      body: JSON.stringify({
        mode: 'list',
        types: ['task'],
        project: params?.project,
        project_ids: params?.project_ids,
        status: params?.status,
        limit: page?.limit ?? TASK_PAGE_SIZE,
        offset: page?.offset ?? 0,
      }),
    }),

  get: (id: string) => fetchApi<Entity>(`/entities/${id}`),

  // RESTful task workflow endpoints
  start: (id: string, params?: { assignee?: string }) =>
    fetchApi<TaskActionResponse>(`/tasks/${id}/start`, {
      method: 'POST',
      body: params ? JSON.stringify(params) : undefined,
    }),

  block: (id: string, reason: string) =>
    fetchApi<TaskActionResponse>(`/tasks/${id}/block`, {
      method: 'POST',
      body: JSON.stringify({ reason }),
    }),

  unblock: (id: string) =>
    fetchApi<TaskActionResponse>(`/tasks/${id}/unblock`, {
      method: 'POST',
    }),

  review: (id: string, params?: { pr_url?: string; commit_shas?: string[] }) =>
    fetchApi<TaskActionResponse>(`/tasks/${id}/review`, {
      method: 'POST',
      body: params ? JSON.stringify(params) : undefined,
    }),

  complete: (id: string, params?: { actual_hours?: number; learnings?: string }) =>
    fetchApi<TaskActionResponse>(`/tasks/${id}/complete`, {
      method: 'POST',
      body: params ? JSON.stringify(params) : undefined,
    }),

  archive: (id: string, params?: { reason?: string }) =>
    fetchApi<TaskActionResponse>(`/tasks/${id}/archive`, {
      method: 'POST',
      body: params ? JSON.stringify(params) : undefined,
    }),

  updateStatus: (id: string, status: TaskStatus) =>
    fetchApi<Entity>(`/entities/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ metadata: { status } }),
    }),

  // Task Notes
  notes: {
    list: (taskId: string, limit = 50) =>
      fetchApi<NotesListResponse>(`/tasks/${taskId}/notes?limit=${limit}`),

    create: (taskId: string, data: CreateNoteRequest) =>
      fetchApi<Note>(`/tasks/${taskId}/notes`, {
        method: 'POST',
        body: JSON.stringify(data),
      }),
  },
};

export const projectsApi = {
  list: (options?: { includeArchived?: boolean }) =>
    fetchApi<TaskListResponse>('/search/explore', {
      method: 'POST',
      body: JSON.stringify({
        mode: 'list',
        types: ['project'],
        limit: 100,
        include_archived: options?.includeArchived ?? false,
      }),
    }),

  get: (id: string) => fetchApi<Entity>(`/entities/${id}`),

  members: {
    list: (projectId: string) => fetchApi<ProjectMembersResponse>(`/projects/${projectId}/members`),
    add: (projectId: string, userId: string, role: ProjectRole) =>
      fetchApi<{ user_id: string; role: string }>(`/projects/${projectId}/members`, {
        method: 'POST',
        body: JSON.stringify({ user_id: userId, role }),
      }),
    updateRole: (projectId: string, userId: string, role: ProjectRole) =>
      fetchApi<{ user_id: string; role: string }>(`/projects/${projectId}/members/${userId}`, {
        method: 'PATCH',
        body: JSON.stringify({ role }),
      }),
    remove: (projectId: string, userId: string) =>
      fetchApi<{ success: boolean }>(`/projects/${projectId}/members/${userId}`, {
        method: 'DELETE',
      }),
  },
};

export const epicsApi = {
  list: (params?: { project?: string; project_ids?: string[]; status?: EpicStatus }) =>
    fetchApi<EpicListResponse>('/search/explore', {
      method: 'POST',
      body: JSON.stringify({
        mode: 'list',
        types: ['epic'],
        project: params?.project,
        project_ids: params?.project_ids,
        status: params?.status,
        limit: 200,
      }),
    }),

  get: (id: string) => fetchApi<Entity>(`/entities/${id}`),

  tasks: (id: string) =>
    fetchApi<TaskListResponse>('/search/explore', {
      method: 'POST',
      body: JSON.stringify({
        mode: 'list',
        types: ['task'],
        epic: id,
        limit: 200,
      }),
    }),

  // RESTful epic workflow endpoints
  start: (id: string) =>
    fetchApi<EpicActionResponse>(`/epics/${id}/start`, {
      method: 'POST',
    }),

  complete: (id: string, params?: { learnings?: string }) =>
    fetchApi<EpicActionResponse>(`/epics/${id}/complete`, {
      method: 'POST',
      body: params ? JSON.stringify(params) : undefined,
    }),

  archive: (id: string, params?: { reason?: string }) =>
    fetchApi<EpicActionResponse>(`/epics/${id}/archive`, {
      method: 'POST',
      body: params ? JSON.stringify(params) : undefined,
    }),

  update: (
    id: string,
    params: {
      status?: EpicStatus;
      priority?: TaskPriority;
      title?: string;
      description?: string;
      assignees?: string[];
      tags?: string[];
    }
  ) =>
    fetchApi<EpicActionResponse>(`/epics/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(params),
    }),
};

export const metricsApi = {
  // Get org-level metrics
  org: () => fetchApi<OrgMetricsResponse>('/metrics'),

  // Get lean project summaries
  projectsSummary: () => fetchApi<ProjectSummariesResponse>('/metrics/projects-summary'),

  // Get project-level metrics
  project: (projectId: string) =>
    fetchApi<ProjectMetricsResponse>(`/metrics/projects/${projectId}`),
};
