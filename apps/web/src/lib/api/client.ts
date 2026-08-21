import {
  adminApi,
  backupsApi,
  checkHealth,
  jobsApi,
  settingsApi,
  setupApi,
  telemetryApi,
} from './admin';
import { authApi, orgsApi, preferencesApi, profileApi, securityApi } from './auth';
import { entitiesApi, graphApi } from './graph';
import { memoryApi, rawCapturesApi, sessionApi, sourceImportsApi, synthesisApi } from './memory';
import { ragApi, searchApi } from './search';
import { sourcesApi } from './sources';
import { epicsApi, metricsApi, projectsApi, tasksApi } from './work-items';

export const api = {
  entities: entitiesApi,
  rawCaptures: rawCapturesApi,
  memory: memoryApi,
  sourceImports: sourceImportsApi,
  synthesis: synthesisApi,
  search: searchApi,
  graph: graphApi,
  checkHealth,
  admin: adminApi,
  telemetry: telemetryApi,
  jobs: jobsApi,
  backups: backupsApi,
  auth: authApi,
  security: securityApi,
  preferences: preferencesApi,
  profile: profileApi,
  session: sessionApi,
  orgs: orgsApi,
  tasks: tasksApi,
  projects: projectsApi,
  epics: epicsApi,
  sources: sourcesApi,
  rag: ragApi,
  metrics: metricsApi,
  setup: setupApi,
  settings: settingsApi,
};
