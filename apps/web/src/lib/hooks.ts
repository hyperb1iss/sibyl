'use client';

/**
 * Public React Query hook contract.
 *
 * Hook implementations live in `./hooks/*`. Explicit exports keep the legacy
 * surface stable without introducing runtime forwarding wrappers.
 */

export {
  useAdminAudit,
  useBackup,
  useBackupCleanup,
  useBackupJobStatus,
  useBackupSettings,
  useBackups,
  useCreateBackup,
  useDeleteBackup,
  useDeleteSetting,
  useHealth,
  useIntegration,
  useJobs,
  useLLMRegistry,
  useLLMSettings,
  useRunMaintenanceJob,
  useSettings,
  useSetupStatus,
  useStats,
  useTelemetrySummary,
  useTestAIModel,
  useTestLLMSurface,
  useTestProviderKey,
  useUpdateBackupSettings,
  useUpdateLLMSurface,
  useUpdateSettings,
  useValidateApiKeys,
} from './hooks/admin';
export {
  useAddOrgMember,
  useApiKeys,
  useAuthProviders,
  useChangePassword,
  useCreateApiKey,
  useCreateOrg,
  useCreateOrgInvitation,
  useDeleteOrg,
  useDeleteOrgInvitation,
  useMe,
  useOnboardingProgress,
  useOrg,
  useOrgInvitations,
  useOrgMembers,
  useOrgs,
  usePreferences,
  useRemoveOrgMember,
  useRevokeAllSessions,
  useRevokeApiKey,
  useRevokeSession,
  useSessions,
  useSwitchOrg,
  useUpdateOrg,
  useUpdateOrgMemberRole,
  useUpdatePreferences,
} from './hooks/auth';
export {
  useCreateEntity,
  useDeleteEntity,
  useEntities,
  useEntity,
  useHierarchicalGraph,
  useUpdateEntity,
} from './hooks/graph';
export {
  useApplyMemoryCorrection,
  useCancelSourceImport,
  useMemoryAudit,
  useMemorySourceImport,
  useMemorySourceInspect,
  useMemorySpaces,
  usePreviewMemoryCorrection,
  useRawCapture,
  useRawCaptures,
  useResumeSourceImport,
  useSessionBundle,
  useSourceImportAdapters,
  useStartSourceImport,
  useSynthesisDraft,
  useSynthesisPlan,
  useUpdateRawCaptureReviewState,
} from './hooks/memory';
export { queryKeys } from './hooks/query-keys';
export { useRealtimeUpdates, useWebSocketStatus } from './hooks/realtime';
export {
  useCodeExamples,
  useDocumentEntities,
  useFullPage,
  useRAGHybridSearch,
  useSearch,
  useSourcePages,
  useUpdateDocument,
} from './hooks/search';
export { useMediaQuery } from './hooks/shared';
export type { CrawlProgressData } from './hooks/sources';
export {
  useAllCrawlProgress,
  useCancelCrawl,
  useCrawlProgress,
  useCrawlSource,
  useCreateSource,
  useDeleteSource,
  useSource,
  useSources,
  useSyncSource,
  useUpdateSource,
} from './hooks/sources';
export {
  useAddProjectMember,
  useAddTaskNote,
  useEpic,
  useEpicManage,
  useEpics,
  useEpicTasks,
  useOrgMetrics,
  useProject,
  useProjectMembers,
  useProjectMetrics,
  useProjectSummaries,
  useProjects,
  useRemoveProjectMember,
  useTask,
  useTaskManage,
  useTaskNotes,
  useTasks,
  useTaskUpdateStatus,
  useUpdateProjectMemberRole,
} from './hooks/work-items';
