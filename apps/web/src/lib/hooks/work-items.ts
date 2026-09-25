'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import type {
  CreateNoteRequest,
  EpicStatus,
  OrgMetricsResponse,
  ProjectMetricsResponse,
  ProjectRole,
  ProjectSummariesResponse,
  TaskListResponse,
  TaskStatus,
  TaskSummary,
} from '../api/work-items';
import {
  epicsApi,
  LEGACY_TASK_PAGE_SIZE,
  metricsApi,
  projectsApi,
  TASK_PAGE_SIZE,
  tasksApi,
} from '../api/work-items';
import { TIMING } from '../constants/app';
import { queryKeys } from './query-keys';

export function useTasks(
  params?: {
    project?: string;
    project_ids?: string[];
    status?: TaskStatus;
  },
  options?: { enabled?: boolean; initialData?: TaskListResponse }
) {
  const normalized =
    params && (params.project || params.project_ids?.length || params.status)
      ? {
          ...(params.project ? { project: params.project } : {}),
          ...(params.project_ids?.length ? { project_ids: [...params.project_ids] } : {}),
          ...(params.status ? { status: params.status } : {}),
        }
      : undefined;

  return useQuery({
    queryKey: queryKeys.tasks.list(normalized),
    queryFn: () => fetchAllTasks(normalized),
    enabled: options?.enabled ?? true,
    initialData: options?.initialData,
  });
}

/** Hard stop so a runaway has_more can never loop forever. */
const MAX_TASK_PAGES = 25;

/** The server rejected the page size itself (an API still on the 200-row cap). */
function pageSizeRejected(error: unknown): boolean {
  return error instanceof Error && error.message.includes('"body.limit"');
}

/**
 * Page through the explore list until the server reports no more rows.
 * The board and the project views bucket every task client-side, so a
 * single capped page silently dropped everything past the newest rows and
 * the column counts disagreed with the dashboard. An older API that still
 * caps pages at 200 rejects the first request; the pager drops to that page
 * size instead of leaving the board empty during a rolling upgrade.
 *
 * The explore list filters some rows after its database window, so a page
 * can come back empty while the server still reports more. Offset widens
 * that window, so an empty page advances by the page size rather than
 * stopping; the page cap is what terminates the loop. When the cap is what
 * ends it, the response says so instead of claiming the set is complete.
 */
export async function fetchAllTasks(
  params?: Parameters<typeof tasksApi.list>[0]
): Promise<TaskListResponse> {
  const entities: TaskSummary[] = [];
  let response: TaskListResponse | undefined;
  let pageSize = TASK_PAGE_SIZE;
  let offset = 0;
  let truncated = false;
  for (let page = 0; page < MAX_TASK_PAGES; page++) {
    try {
      response = await tasksApi.list(params, { limit: pageSize, offset });
    } catch (error) {
      if (pageSize !== LEGACY_TASK_PAGE_SIZE && pageSizeRejected(error)) {
        pageSize = LEGACY_TASK_PAGE_SIZE;
        page -= 1;
        continue;
      }
      throw error;
    }
    entities.push(...response.entities);
    offset += response.entities.length || pageSize;
    if (!response.has_more) break;
    truncated = page === MAX_TASK_PAGES - 1;
  }
  return {
    mode: response?.mode ?? 'list',
    filters: response?.filters ?? {},
    entities,
    total: entities.length,
    actual_total: response?.actual_total ?? entities.length,
    has_more: truncated,
  };
}

export function useTask(id: string) {
  return useQuery({
    queryKey: queryKeys.tasks.detail(id),
    queryFn: () => tasksApi.get(id),
    enabled: !!id,
  });
}

export function useTaskUpdateStatus() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ id, status }: { id: string; status: TaskStatus }) =>
      tasksApi.updateStatus(id, status),
    onSuccess: (_data, { id }) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.tasks.detail(id) });
      queryClient.invalidateQueries({ queryKey: queryKeys.tasks.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.entities.detail(id) });
      queryClient.invalidateQueries({ queryKey: ['metrics'] });
    },
  });
}

// =============================================================================
// Task Notes Hooks
// =============================================================================

export function useTaskNotes(taskId: string, options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: queryKeys.tasks.notes(taskId),
    queryFn: () => tasksApi.notes.list(taskId),
    enabled: (options?.enabled ?? true) && !!taskId,
  });
}

export function useAddTaskNote() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ taskId, data }: { taskId: string; data: CreateNoteRequest }) =>
      tasksApi.notes.create(taskId, data),
    onSuccess: (_data, { taskId }) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.tasks.notes(taskId) });
    },
  });
}

// =============================================================================
// Project Hooks
// =============================================================================

export function useProjects(
  options?: { includeArchived?: boolean; enabled?: boolean },
  initialData?: TaskListResponse
) {
  const includeArchived = options?.includeArchived ?? false;
  return useQuery({
    queryKey: queryKeys.projects.list(includeArchived),
    queryFn: () => projectsApi.list({ includeArchived }),
    enabled: options?.enabled ?? true,
    staleTime: TIMING.STALE_TIME,
    initialData,
  });
}

export function useProjectMembers(projectId: string, options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: queryKeys.projects.members(projectId),
    queryFn: () => projectsApi.members.list(projectId),
    enabled: options?.enabled ?? !!projectId,
    retry: false,
    staleTime: TIMING.STALE_TIME,
  });
}

export function useUpdateProjectMemberRole() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      projectId,
      userId,
      role,
    }: {
      projectId: string;
      userId: string;
      role: ProjectRole;
    }) => projectsApi.members.updateRole(projectId, userId, role),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.projects.members(variables.projectId) });
    },
  });
}

export function useRemoveProjectMember() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ projectId, userId }: { projectId: string; userId: string }) =>
      projectsApi.members.remove(projectId, userId),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.projects.members(variables.projectId) });
    },
  });
}

// =============================================================================
// Epic Hooks
// =============================================================================

export function useEpics(params?: {
  project?: string;
  project_ids?: string[];
  status?: EpicStatus;
}) {
  const normalized =
    params && (params.project || params.project_ids?.length || params.status)
      ? {
          ...(params.project ? { project: params.project } : {}),
          ...(params.project_ids?.length ? { project_ids: [...params.project_ids] } : {}),
          ...(params.status ? { status: params.status } : {}),
        }
      : undefined;

  return useQuery({
    queryKey: queryKeys.epics.list(normalized),
    queryFn: () => epicsApi.list(normalized),
    staleTime: TIMING.STALE_TIME,
  });
}

export function useEpic(id: string) {
  return useQuery({
    queryKey: queryKeys.epics.detail(id),
    queryFn: () => epicsApi.get(id),
    enabled: !!id,
    staleTime: TIMING.STALE_TIME,
  });
}

export function useEpicTasks(epicId: string) {
  return useQuery({
    queryKey: queryKeys.epics.tasks(epicId),
    queryFn: () => epicsApi.tasks(epicId),
    enabled: !!epicId,
    staleTime: TIMING.STALE_TIME,
  });
}

/**
 * Fetch org-level metrics (aggregated across all projects).
 */
export function useOrgMetrics(initialData?: OrgMetricsResponse) {
  return useQuery({
    queryKey: queryKeys.metrics.org,
    queryFn: metricsApi.org,
    initialData,
    staleTime: TIMING.STALE_TIME,
  });
}

/** Fetch lean project summaries for the projects page. */
export function useProjectSummaries(initialData?: ProjectSummariesResponse) {
  return useQuery({
    queryKey: queryKeys.metrics.projectsSummary,
    queryFn: metricsApi.projectsSummary,
    initialData,
    staleTime: TIMING.STALE_TIME,
  });
}

/**
 * Fetch project-level metrics.
 */
export function useProjectMetrics(projectId: string, initialData?: ProjectMetricsResponse) {
  return useQuery({
    queryKey: queryKeys.metrics.project(projectId),
    queryFn: () => metricsApi.project(projectId),
    initialData,
    enabled: Boolean(projectId),
    staleTime: TIMING.STALE_TIME,
  });
}
