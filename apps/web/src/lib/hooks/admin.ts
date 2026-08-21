'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import type {
  AdminAuditListResponse,
  AIModelKind,
  LLMProviderName,
  LLMSurface,
  StatsResponse,
  UpdateLLMSurfaceRequest,
  UpdateSettingsRequest,
} from '../api/admin';
import { adminApi, backupsApi, jobsApi, settingsApi, setupApi, telemetryApi } from '../api/admin';
import { TIMING } from '../constants/app';
import { queryKeys } from './query-keys';
import { useWebSocketStatus } from './realtime';

export function useHealth() {
  return useQuery({
    queryKey: queryKeys.admin.health,
    queryFn: adminApi.health,
    refetchInterval: TIMING.HEALTH_CHECK_INTERVAL,
  });
}

export function useStats(initialData?: StatsResponse) {
  return useQuery({
    queryKey: queryKeys.admin.stats,
    queryFn: adminApi.stats,
    initialData,
    staleTime: 5 * TIMING.STALE_TIME,
    refetchOnWindowFocus: false,
  });
}

export function useAdminAudit(
  params?: Parameters<typeof adminApi.audit.list>[0],
  options?: { enabled?: boolean; initialData?: AdminAuditListResponse }
) {
  return useQuery({
    queryKey: queryKeys.admin.audit(params),
    queryFn: () => adminApi.audit.list(params),
    enabled: options?.enabled ?? true,
    initialData: options?.initialData,
    placeholderData: previousData => previousData,
  });
}

export function useTelemetrySummary(params?: Parameters<typeof telemetryApi.summary>[0]) {
  return useQuery({
    queryKey: queryKeys.telemetry.summary(params),
    queryFn: () => telemetryApi.summary(params),
    refetchInterval: TIMING.STALE_TIME,
    staleTime: TIMING.STALE_TIME,
  });
}

export function useSetupStatus(options?: { validateKeys?: boolean; enabled?: boolean }) {
  return useQuery({
    queryKey: queryKeys.setup.status,
    queryFn: () => setupApi.status(options?.validateKeys),
    enabled: options?.enabled ?? true,
    staleTime: 30000, // Cache for 30 seconds
    retry: false, // Don't retry on failure (server might be down)
  });
}

/**
 * Validate API keys are configured and working.
 */
export function useValidateApiKeys(options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: queryKeys.setup.validation,
    queryFn: () => setupApi.validateKeys(),
    enabled: options?.enabled ?? true,
    staleTime: 60000, // Cache for 1 minute
    retry: 1, // One retry on timeout
  });
}

/**
 * Get the integration payload for connecting Sibyl to a CLI or MCP client.
 *
 * Returns the CLI install command, per-client MCP configs, and the agent
 * prompt snippet. Single source of truth behind the connect surfaces.
 */
export function useIntegration(options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: queryKeys.setup.integration,
    queryFn: () => setupApi.integration(),
    enabled: options?.enabled ?? true,
    staleTime: Infinity, // Server URL and snippets are stable
  });
}

// =============================================================================
// Settings Hooks
// =============================================================================

/**
 * Get system settings (API key configuration status).
 */
export function useSettings(options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: queryKeys.settings.all,
    queryFn: () => settingsApi.get(),
    enabled: options?.enabled ?? true,
    staleTime: 30000, // Cache for 30 seconds
  });
}

/**
 * Update system settings (save API keys to database).
 */
export function useUpdateSettings() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (request: UpdateSettingsRequest) => settingsApi.update(request),
    onSuccess: () => {
      // Invalidate settings and setup status queries
      queryClient.invalidateQueries({ queryKey: queryKeys.settings.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.setup.status });
      queryClient.invalidateQueries({ queryKey: queryKeys.setup.validation });
    },
  });
}

/**
 * Delete a system setting from the database.
 */
export function useDeleteSetting() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (key: string) => settingsApi.delete(key),
    onSuccess: () => {
      // Invalidate settings and setup status queries
      queryClient.invalidateQueries({ queryKey: queryKeys.settings.all });
      queryClient.invalidateQueries({ queryKey: queryKeys.setup.status });
      queryClient.invalidateQueries({ queryKey: queryKeys.setup.validation });
    },
  });
}

export function useLLMSettings(options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: queryKeys.settings.llm,
    queryFn: () => settingsApi.ai.getLLMSettings(),
    enabled: options?.enabled ?? true,
    staleTime: 30000,
  });
}

export function useLLMRegistry(kind: AIModelKind = 'llm', options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: queryKeys.settings.registry(kind),
    queryFn: () => settingsApi.ai.getRegistry(kind),
    enabled: options?.enabled ?? true,
    staleTime: 300000,
  });
}

export function useUpdateLLMSurface() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ surface, request }: { surface: LLMSurface; request: UpdateLLMSurfaceRequest }) =>
      settingsApi.ai.updateLLMSurface(surface, request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.settings.llm });
      queryClient.invalidateQueries({ queryKey: queryKeys.settings.all });
    },
  });
}

export function useTestLLMSurface() {
  return useMutation({
    mutationFn: (surface: LLMSurface) => settingsApi.ai.testLLMSurface(surface),
  });
}

export function useTestProviderKey() {
  return useMutation({
    mutationFn: (provider: LLMProviderName) => settingsApi.ai.testProviderKey(provider),
  });
}

export function useTestAIModel() {
  return useMutation({
    mutationFn: (modelAlias: string) => settingsApi.ai.testModel(modelAlias),
  });
}

// =============================================================================
// Backup Management Hooks
// =============================================================================

/**
 * Get backup settings for the current organization.
 */
export function useBackupSettings(options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: queryKeys.backups.settings,
    queryFn: () => backupsApi.settings.get(),
    enabled: options?.enabled ?? true,
    staleTime: 30000,
  });
}

/**
 * Update backup settings for the current organization.
 */
export function useUpdateBackupSettings() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (data: Parameters<typeof backupsApi.settings.update>[0]) =>
      backupsApi.settings.update(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.backups.settings });
    },
  });
}

/**
 * List all backups for the current organization.
 */
export function useBackups(options?: { enabled?: boolean; limit?: number; offset?: number }) {
  const enabled = options?.enabled ?? true;
  const wsStatus = useWebSocketStatus(enabled);

  return useQuery({
    queryKey: queryKeys.backups.list,
    queryFn: () => backupsApi.list(options?.limit ?? 50, options?.offset ?? 0),
    enabled,
    staleTime: 10000,
    refetchInterval: wsStatus === 'connected' ? false : 30000,
  });
}

export function useJobs(options?: { enabled?: boolean; function?: string; limit?: number }) {
  return useQuery({
    queryKey: queryKeys.jobs.list({
      function: options?.function,
      limit: options?.limit ?? 25,
    }),
    queryFn: () =>
      jobsApi.list({
        function: options?.function,
        limit: options?.limit ?? 25,
      }),
    enabled: options?.enabled ?? true,
    staleTime: 5000,
    refetchInterval: 15000,
  });
}

export function useRunMaintenanceJob() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ action }: { action: 'consolidate' | 'forget' | 'reflect' }) => {
      if (action === 'consolidate') return jobsApi.runConsolidation();
      if (action === 'reflect') return jobsApi.runReflectionDream({ dry_run: true });
      return jobsApi.runPriorityDecay();
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.jobs.all });
    },
  });
}

/**
 * Get details of a specific backup.
 */
export function useBackup(backupId: string, options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: queryKeys.backups.detail(backupId),
    queryFn: () => backupsApi.get(backupId),
    enabled: (options?.enabled ?? true) && !!backupId,
    staleTime: 10000,
  });
}

/**
 * Create a new backup.
 */
export function useCreateBackup() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (data?: Parameters<typeof backupsApi.create>[0]) => backupsApi.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.backups.list });
    },
  });
}

/**
 * Delete a backup.
 */
export function useDeleteBackup() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (backupId: string) => backupsApi.delete(backupId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.backups.list });
    },
  });
}

/**
 * Trigger backup cleanup.
 */
export function useBackupCleanup() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (retentionDays?: number) => backupsApi.cleanup(retentionDays),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.backups.list });
    },
  });
}

/**
 * Get status of a backup job.
 */
export function useBackupJobStatus(jobId: string, options?: { enabled?: boolean }) {
  const enabled = (options?.enabled ?? true) && !!jobId;
  const wsStatus = useWebSocketStatus(enabled);

  return useQuery({
    queryKey: queryKeys.backups.jobStatus(jobId),
    queryFn: () => backupsApi.jobStatus(jobId),
    enabled,
    staleTime: 2000,
    refetchInterval: query => {
      if (wsStatus === 'connected') {
        return false;
      }
      const status = query.state.data?.status;
      if (status === 'complete' || status === 'not_found') {
        return false;
      }
      return 3000;
    },
  });
}
