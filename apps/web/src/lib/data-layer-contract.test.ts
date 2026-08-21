import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

import { api } from './api';
import * as hooks from './hooks';

const API_KEYS = [
  'entities',
  'rawCaptures',
  'memory',
  'sourceImports',
  'synthesis',
  'search',
  'graph',
  'checkHealth',
  'admin',
  'telemetry',
  'jobs',
  'backups',
  'auth',
  'security',
  'preferences',
  'profile',
  'session',
  'orgs',
  'tasks',
  'projects',
  'epics',
  'sources',
  'rag',
  'metrics',
  'setup',
  'settings',
] as const;

const HOOK_EXPORTS = [
  'queryKeys',
  'useAddOrgMember',
  'useAddProjectMember',
  'useAddTaskNote',
  'useAdminAudit',
  'useAllCrawlProgress',
  'useApiKeys',
  'useApplyMemoryCorrection',
  'useAuthProviders',
  'useBackup',
  'useBackupCleanup',
  'useBackupJobStatus',
  'useBackups',
  'useBackupSettings',
  'useCancelCrawl',
  'useCancelSourceImport',
  'useChangePassword',
  'useCodeExamples',
  'useCrawlProgress',
  'useCrawlSource',
  'useCreateApiKey',
  'useCreateBackup',
  'useCreateEntity',
  'useCreateOrg',
  'useCreateOrgInvitation',
  'useCreateSource',
  'useDeleteBackup',
  'useDeleteEntity',
  'useDeleteOrg',
  'useDeleteOrgInvitation',
  'useDeleteSetting',
  'useDeleteSource',
  'useDocumentEntities',
  'useEntities',
  'useEntity',
  'useEpic',
  'useEpicManage',
  'useEpics',
  'useEpicTasks',
  'useFullPage',
  'useHealth',
  'useHierarchicalGraph',
  'useIntegration',
  'useJobs',
  'useLLMRegistry',
  'useLLMSettings',
  'useMe',
  'useMediaQuery',
  'useMemoryAudit',
  'useMemorySourceImport',
  'useMemorySourceInspect',
  'useMemorySpaces',
  'useOnboardingProgress',
  'useOrg',
  'useOrgInvitations',
  'useOrgMembers',
  'useOrgMetrics',
  'useOrgs',
  'usePreferences',
  'usePreviewMemoryCorrection',
  'useProject',
  'useProjectMembers',
  'useProjectMetrics',
  'useProjects',
  'useProjectSummaries',
  'useRAGHybridSearch',
  'useRawCapture',
  'useRawCaptures',
  'useRealtimeUpdates',
  'useRemoveOrgMember',
  'useRemoveProjectMember',
  'useResumeSourceImport',
  'useRevokeAllSessions',
  'useRevokeApiKey',
  'useRevokeSession',
  'useRunMaintenanceJob',
  'useSearch',
  'useSessionBundle',
  'useSessions',
  'useSettings',
  'useSetupStatus',
  'useSource',
  'useSourceImportAdapters',
  'useSourcePages',
  'useSources',
  'useStartSourceImport',
  'useStats',
  'useSwitchOrg',
  'useSyncSource',
  'useSynthesisDraft',
  'useSynthesisPlan',
  'useTask',
  'useTaskManage',
  'useTaskNotes',
  'useTasks',
  'useTaskUpdateStatus',
  'useTelemetrySummary',
  'useTestAIModel',
  'useTestLLMSurface',
  'useTestProviderKey',
  'useUpdateBackupSettings',
  'useUpdateDocument',
  'useUpdateEntity',
  'useUpdateLLMSurface',
  'useUpdateOrg',
  'useUpdateOrgMemberRole',
  'useUpdatePreferences',
  'useUpdateProjectMemberRole',
  'useUpdateRawCaptureReviewState',
  'useUpdateSettings',
  'useUpdateSource',
  'useValidateApiKeys',
  'useWebSocketStatus',
] as const;

const LIB_DIR = dirname(fileURLToPath(import.meta.url));
const MODULE_IMPORT = /\b(?:import|export)\s+(?:type\s+)?(?:[^'";]*?\s+from\s+)?['"]([^'"]+)['"]/g;

function moduleFiles(): string[] {
  return ['api.ts', 'hooks.ts', 'api', 'hooks'].flatMap(entry => {
    const path = join(LIB_DIR, entry);
    return entry.endsWith('.ts')
      ? [path]
      : readdirSync(path)
          .filter(file => file.endsWith('.ts'))
          .map(file => join(path, file));
  });
}

function localDependencies(file: string, files: Set<string>): string[] {
  const source = readFileSync(file, 'utf8');
  return [...source.matchAll(MODULE_IMPORT)]
    .map(match => match[1])
    .filter(specifier => specifier.startsWith('.'))
    .map(specifier => resolve(dirname(file), `${specifier}.ts`))
    .filter(candidate => files.has(candidate));
}

function findCycle(graph: Map<string, string[]>): string[] | null {
  const visited = new Set<string>();
  const active = new Set<string>();
  const stack: string[] = [];

  function visit(node: string): string[] | null {
    if (active.has(node)) return [...stack.slice(stack.indexOf(node)), node];
    if (visited.has(node)) return null;

    visited.add(node);
    active.add(node);
    stack.push(node);
    for (const dependency of graph.get(node) ?? []) {
      const cycle = visit(dependency);
      if (cycle) return cycle;
    }
    stack.pop();
    active.delete(node);
    return null;
  }

  for (const node of graph.keys()) {
    const cycle = visit(node);
    if (cycle) return cycle;
  }
  return null;
}

describe('web data-layer contract', () => {
  it('preserves the public API namespace and hooks export set', () => {
    expect(Object.keys(api)).toEqual(API_KEYS);
    expect(Object.keys(hooks).sort()).toEqual([...HOOK_EXPORTS].sort());
  });

  it('keeps compatibility barrels explicit and out of domain ownership', () => {
    const apiBarrel = readFileSync(join(LIB_DIR, 'api.ts'), 'utf8');
    const hooksBarrel = readFileSync(join(LIB_DIR, 'hooks.ts'), 'utf8');
    expect(apiBarrel).not.toMatch(/export\s+\*/);
    expect(hooksBarrel).not.toMatch(/export\s+\*/);

    for (const directory of ['api', 'hooks']) {
      for (const file of readdirSync(join(LIB_DIR, directory)).filter(file =>
        file.endsWith('.ts')
      )) {
        const source = readFileSync(join(LIB_DIR, directory, file), 'utf8');
        expect(source).not.toMatch(/from ['"]\.\.\/(?:api|hooks)['"]/);
      }
    }
  });

  it('has no static import or re-export cycles', () => {
    const files = new Set(moduleFiles());
    const graph = new Map([...files].map(file => [file, localDependencies(file, files)]));
    expect(findCycle(graph)).toBeNull();
  });
});
