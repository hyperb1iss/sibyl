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
  TaskPriority,
  TaskStatus,
} from '../api/work-items';
import { epicsApi, metricsApi, projectsApi, tasksApi } from '../api/work-items';
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
    queryFn: () => tasksApi.list(normalized),
    enabled: options?.enabled ?? true,
    initialData: options?.initialData,
  });
}

export function useTask(id: string) {
  return useQuery({
    queryKey: queryKeys.tasks.detail(id),
    queryFn: () => tasksApi.get(id),
    enabled: !!id,
  });
}

export function useTaskManage() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      action,
      entity_id,
      params,
    }: {
      action:
        | 'start_task'
        | 'block_task'
        | 'unblock_task'
        | 'submit_review'
        | 'complete_task'
        | 'archive';
      entity_id: string;
      params?: {
        assignee?: string;
        blocker?: string;
        reason?: string;
        commit_shas?: string[];
        pr_url?: string;
        actual_hours?: number;
        learnings?: string;
      };
    }) => {
      // Route to RESTful endpoints based on action
      switch (action) {
        case 'start_task':
          return tasksApi.start(
            entity_id,
            params?.assignee ? { assignee: params.assignee } : undefined
          );
        case 'block_task':
          return tasksApi.block(entity_id, params?.blocker || params?.reason || 'Blocked');
        case 'unblock_task':
          return tasksApi.unblock(entity_id);
        case 'submit_review':
          return tasksApi.review(entity_id, {
            pr_url: params?.pr_url,
            commit_shas: params?.commit_shas,
          });
        case 'complete_task':
          return tasksApi.complete(entity_id, {
            actual_hours: params?.actual_hours,
            learnings: params?.learnings,
          });
        case 'archive':
          return tasksApi.archive(
            entity_id,
            params?.reason ? { reason: params.reason } : undefined
          );
        default:
          throw new Error(`Unknown action: ${action}`);
      }
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.tasks.all });
      queryClient.invalidateQueries({ queryKey: ['metrics'] });
    },
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

export function useProject(id: string) {
  return useQuery({
    queryKey: queryKeys.projects.detail(id),
    queryFn: () => projectsApi.get(id),
    enabled: !!id,
    staleTime: TIMING.STALE_TIME,
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

export function useAddProjectMember() {
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
    }) => projectsApi.members.add(projectId, userId, role),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.projects.members(variables.projectId) });
    },
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

export function useEpicManage() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      action,
      entity_id,
      params,
    }: {
      action: 'start_epic' | 'complete_epic' | 'archive_epic' | 'update_epic';
      entity_id: string;
      params?: {
        learnings?: string;
        reason?: string;
        status?: EpicStatus;
        priority?: TaskPriority;
        title?: string;
        description?: string;
        assignees?: string[];
        tags?: string[];
      };
    }) => {
      // Route to RESTful endpoints based on action
      switch (action) {
        case 'start_epic':
          return epicsApi.start(entity_id);
        case 'complete_epic':
          return epicsApi.complete(
            entity_id,
            params?.learnings ? { learnings: params.learnings } : undefined
          );
        case 'archive_epic':
          return epicsApi.archive(
            entity_id,
            params?.reason ? { reason: params.reason } : undefined
          );
        case 'update_epic':
          return epicsApi.update(entity_id, {
            status: params?.status,
            priority: params?.priority,
            title: params?.title,
            description: params?.description,
            assignees: params?.assignees,
            tags: params?.tags,
          });
        default:
          throw new Error(`Unknown action: ${action}`);
      }
    },
    onSuccess: () => {
      // Invalidate epics list and related queries
      queryClient.invalidateQueries({ queryKey: queryKeys.epics.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.tasks.all });
    },
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
