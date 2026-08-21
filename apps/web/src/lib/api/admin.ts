import { fetchApi, fetchApiBlob } from './transport';

export interface AdminAuditEvent {
  id: string;
  organization_id: string | null;
  user_id: string | null;
  action: string;
  resource: string | null;
  ip_address: string | null;
  user_agent: string | null;
  details: Record<string, unknown>;
  created_at: string | null;
}

export interface AdminAuditListResponse {
  events: AdminAuditEvent[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
}

export interface AdminAuditParams {
  user_id?: string;
  action?: string;
  resource?: string;
  start_time?: string;
  end_time?: string;
  limit?: number;
  offset?: number;
}

export type AdminAuditExportFormat = 'csv' | 'json';

export interface HealthResponse {
  status: 'healthy' | 'unhealthy' | 'unknown';
  server_name: string;
  uptime_seconds: number;
  graph_connected: boolean;
  entity_counts: Record<string, number>;
  errors: string[];
}

export interface StatsResponse {
  entity_counts: Record<string, number>;
  total_entities: number;
}

export interface TelemetryDurationSummary {
  count: number;
  errors: number;
  slow: number;
  error_rate: number;
  avg_ms: number;
  p50_ms: number;
  p95_ms: number;
  p99_ms: number;
  max_ms: number;
}

export interface TelemetryTrendPoint {
  timestamp: string;
  api_p95_ms: number;
  surreal_p95_ms: number;
  memory_p95_ms: number;
  llm_p95_ms: number;
  error_rate: number;
  request_count: number;
  query_count: number;
  memory_count: number;
  llm_count: number;
}

export interface TelemetryEvent {
  timestamp: string;
  category: string;
  status: string;
  duration_ms: number | null;
  value: number;
  labels: Record<string, string>;
}

export interface TelemetryMetric {
  kind: string;
  name: string;
  labels: Record<string, string>;
  value?: number | null;
  count?: number | null;
  sum?: number | null;
  min?: number | null;
  max?: number | null;
  avg?: number | null;
  p50?: number | null;
  p95?: number | null;
  p99?: number | null;
}

export interface TelemetrySummaryResponse {
  generated_at: string;
  window_seconds: number;
  uptime_seconds: number;
  summaries: Record<string, TelemetryDurationSummary>;
  trends: TelemetryTrendPoint[];
  recent_events: TelemetryEvent[];
  metrics: TelemetryMetric[];
  rollups: Record<string, unknown>[];
}

// =============================================================================
// Setup Wizard Types
// =============================================================================

export interface SetupStatus {
  needs_setup: boolean;
  has_users: boolean;
  has_orgs: boolean;
  setup_complete: boolean;
  public_signups_enabled: boolean;
  openai_configured: boolean;
  anthropic_configured: boolean;
  gemini_configured: boolean;
  openai_valid: boolean | null;
  anthropic_valid: boolean | null;
  gemini_valid: boolean | null;
}

export interface ApiKeyValidation {
  openai_valid: boolean;
  anthropic_valid: boolean;
  gemini_valid: boolean;
  openai_error: string | null;
  anthropic_error: string | null;
  gemini_error: string | null;
}

/** One way to wire Sibyl into an MCP-capable agent. */
export interface McpClientConfig {
  id: string;
  label: string;
  /** "command" to run in a terminal, or "config" to paste into a file. */
  kind: 'command' | 'config';
  /** Syntax hint for rendering. */
  language: 'bash' | 'json' | 'toml';
  snippet: string;
  /** Where a "config" snippet belongs, when applicable. */
  target: string | null;
}

/** Everything a user needs to connect Sibyl to a CLI or MCP client. */
export interface IntegrationResponse {
  server_url: string;
  mcp_url: string;
  cli_install: string;
  cli_install_alt: string;
  mcp_clients: McpClientConfig[];
  prompt_snippet: string;
}

export function isSetupAlreadyInitializedError(error: unknown): boolean {
  if (!(error instanceof Error)) {
    return false;
  }
  return (
    error.message.includes('setup_already_initialized') ||
    error.message.includes('Setup is complete')
  );
}

// =============================================================================
// Settings Types
// =============================================================================

export interface SettingInfo {
  configured: boolean;
  source: 'database' | 'environment' | 'none';
  is_secret: boolean;
  masked: string | null;
  value: string | null;
}

export interface SettingsResponse {
  settings: Record<string, SettingInfo>;
}

export interface UpdateSettingsRequest {
  openai_api_key?: string;
  anthropic_api_key?: string;
  gemini_api_key?: string;
  embedding_provider?: 'openai' | 'gemini';
  embedding_model?: string;
  embedding_dimensions?: number;
  graph_embedding_provider?: 'openai' | 'gemini';
  graph_embedding_model?: string;
  graph_embedding_dimensions?: number;
}

export interface UpdateSettingsResponse {
  updated: string[];
  validation: Record<string, { valid: boolean; error: string | null }>;
}

export interface DeleteSettingResponse {
  deleted: boolean;
  key: string;
}

export type LLMProviderName = 'anthropic' | 'gemini' | 'openai';
export type LLMSurface = 'default' | 'crawler' | 'memory' | 'synthesis';
export type AIModelKind = 'llm' | 'embedding';
export type LLMConfigSource = 'env' | 'db' | 'default';
export type LLMValidationStatus =
  | 'valid'
  | 'invalid_key'
  | 'network'
  | 'rate_limited'
  | 'model_not_found'
  | 'permission_denied';

export interface LLMConfigValueField {
  value: string | number | null;
  source: LLMConfigSource;
  locked_by_env: boolean;
  env_var: string | null;
}

export interface LLMSecretConfigField {
  configured: boolean;
  source: LLMConfigSource;
  locked_by_env: boolean;
  env_var: string | null;
  masked: string | null;
}

export interface LLMSurfaceSettings {
  surface: LLMSurface;
  provider: LLMConfigValueField;
  model: LLMConfigValueField;
  temperature: LLMConfigValueField;
  max_tokens: LLMConfigValueField;
  timeout_seconds: LLMConfigValueField;
  api_key: LLMSecretConfigField;
  cached_at: string | null;
}

export interface LLMSettingsResponse {
  scope: 'instance_wide';
  surfaces: Record<LLMSurface, LLMSurfaceSettings>;
}

export interface UpdateLLMSurfaceRequest {
  provider?: LLMProviderName;
  model?: string;
  temperature?: number;
  max_tokens?: number;
  timeout_seconds?: number;
}

export interface UpdateLLMSurfaceResponse {
  scope: 'instance_wide';
  surface: LLMSurfaceSettings;
  warning: string | null;
}

export interface AIModelEntry {
  alias: string;
  snapshot: string;
  kind: AIModelKind;
  provider: string;
  provider_model_id: string;
  pydantic_ai_model_class: string;
  use_cases: string[];
  capabilities: string[];
  max_output_tokens: number | null;
  embedding_dimensions: number | null;
  default_temperature: number | null;
  input_cost_per_mtok_usd: number;
  output_cost_per_mtok_usd: number | null;
  cost_source_url: string;
  last_verified_at: string;
  deprecated_after: string | null;
  warning: string | null;
}

export interface AIRegistryResponse {
  entries: AIModelEntry[];
}

export interface LLMTestResult {
  surface: LLMSurface;
  provider: LLMProviderName;
  model: string;
  status: LLMValidationStatus;
  valid: boolean;
  latency_ms: number;
  parsed_output: Record<string, unknown> | null;
  input_tokens: number | null;
  output_tokens: number | null;
  error: string | null;
}

export interface ProviderKeyTestResult {
  provider: LLMProviderName;
  model: string;
  status: LLMValidationStatus;
  valid: boolean;
  latency_ms: number;
  input_tokens: number | null;
  output_tokens: number | null;
  error: string | null;
}

export interface ModelAvailabilityTestResult {
  provider: LLMProviderName;
  requested_model: string;
  resolved_model: string | null;
  status: LLMValidationStatus;
  valid: boolean;
  latency_ms: number;
  input_tokens: number | null;
  output_tokens: number | null;
  error: string | null;
}

// Backup/Restore Types
export interface BackupData {
  version: string;
  created_at: string;
  organization_id: string;
  entity_count: number;
  relationship_count: number;
  entities: Record<string, unknown>[];
  relationships: Record<string, unknown>[];
}

export interface BackupResponse {
  success: boolean;
  entity_count: number;
  relationship_count: number;
  message: string;
  duration_seconds: number;
  backup_data: BackupData | null;
}

export interface RestoreResponse {
  success: boolean;
  entities_restored: number;
  relationships_restored: number;
  entities_skipped: number;
  relationships_skipped: number;
  errors: string[];
  duration_seconds: number;
}

// Backup Management Types (per-org backup settings and archives)
export type BackupStatus = 'pending' | 'in_progress' | 'completed' | 'failed';

export interface BackupSettingsResponse {
  enabled: boolean;
  schedule: string;
  retention_days: number;
  include_database_dump: boolean;
  include_graph: boolean;
  database_dump_supported: boolean;
  archive_contents: string[];
  last_backup_at: string | null;
  last_backup_id: string | null;
}

export interface BackupSettingsUpdate {
  enabled?: boolean;
  schedule?: string;
  retention_days?: number;
}

export interface BackupInfo {
  id: string;
  backup_id: string;
  status: BackupStatus;
  filename: string | null;
  size_bytes: number;
  entity_count: number;
  relationship_count: number;
  duration_seconds: number;
  triggered_by: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  error: string | null;
}

export interface BackupListResponse {
  backups: BackupInfo[];
  total: number;
}

export type CreateBackupRequest = Record<string, never>;

export interface CreateBackupResponse {
  id: string;
  backup_id: string;
  job_id: string;
  status: string;
  message: string;
  archive_contents: string[];
}

export interface BackupJobStatus {
  job_id: string;
  function: string;
  status: string;
  enqueue_time: string | null;
  start_time: string | null;
  finish_time: string | null;
  result: unknown;
  error: string | null;
}

export interface CleanupResponse {
  job_id: string;
  message: string;
}

export type BackgroundJobStatus = 'queued' | 'in_progress' | 'complete' | 'deferred' | 'not_found';

export interface BackgroundJobSummary {
  job_id: string;
  function: string;
  status: BackgroundJobStatus;
  enqueue_time: string | null;
  start_time: string | null;
  finish_time: string | null;
  error: string | null;
}

export interface BackgroundJobListResponse {
  jobs: BackgroundJobSummary[];
  total: number;
  error?: string;
}

export interface MaintenanceJobResponse {
  job_id: string;
  function: 'consolidate_org' | 'priority_decay' | 'run_reflection_dream_cycle';
  status: 'queued';
  message: string;
}

function adminAuditSearchParams(params?: AdminAuditParams): URLSearchParams {
  const searchParams = new URLSearchParams();
  if (params?.user_id) searchParams.set('user_id', params.user_id);
  if (params?.action) searchParams.set('action', params.action);
  if (params?.resource) searchParams.set('resource', params.resource);
  if (params?.start_time) searchParams.set('start_time', params.start_time);
  if (params?.end_time) searchParams.set('end_time', params.end_time);
  if (params?.limit) searchParams.set('limit', params.limit.toString());
  if (params?.offset) searchParams.set('offset', params.offset.toString());
  return searchParams;
}

export const checkHealth = () => fetchApi<{ status: string }>('/health');

export const adminApi = {
  health: () => fetchApi<HealthResponse>('/admin/health'),
  stats: () => fetchApi<StatsResponse>('/admin/stats'),
  audit: {
    list: (params?: AdminAuditParams) => {
      const searchParams = adminAuditSearchParams(params);
      const query = searchParams.toString();
      return fetchApi<AdminAuditListResponse>(`/admin/audit${query ? `?${query}` : ''}`);
    },
    export: (params: AdminAuditParams & { format: AdminAuditExportFormat }) => {
      const searchParams = adminAuditSearchParams(params);
      searchParams.set('format', params.format);
      return fetchApiBlob(`/admin/audit/export?${searchParams.toString()}`);
    },
  },
  backup: () =>
    fetchApi<BackupResponse>('/admin/backup', {
      method: 'POST',
    }),
  restore: (backupData: BackupData, skipExisting = true) =>
    fetchApi<RestoreResponse>('/admin/restore', {
      method: 'POST',
      body: JSON.stringify({
        backup_data: backupData,
        skip_existing: skipExisting,
      }),
    }),
};

export const telemetryApi = {
  summary: (params?: { window_seconds?: number; rollup_limit?: number }) => {
    const search = new URLSearchParams();
    if (params?.window_seconds) search.set('window_seconds', String(params.window_seconds));
    if (params?.rollup_limit !== undefined) search.set('rollup_limit', String(params.rollup_limit));
    const suffix = search.toString();
    return fetchApi<TelemetrySummaryResponse>(`/telemetry/summary${suffix ? `?${suffix}` : ''}`);
  },
};

export const jobsApi = {
  list: (params?: { function?: string; limit?: number }) => {
    const search = new URLSearchParams();
    if (params?.function) search.set('function', params.function);
    if (params?.limit) search.set('limit', String(params.limit));
    const suffix = search.toString();
    return fetchApi<BackgroundJobListResponse>(`/jobs${suffix ? `?${suffix}` : ''}`);
  },
  runConsolidation: () =>
    fetchApi<MaintenanceJobResponse>('/jobs/consolidation', {
      method: 'POST',
    }),
  runPriorityDecay: () =>
    fetchApi<MaintenanceJobResponse>('/jobs/forgetting', {
      method: 'POST',
    }),
  runReflectionDream: (params?: {
    dry_run?: boolean;
    source_limit?: number;
    candidate_limit?: number;
    archive_exceptions?: boolean;
  }) => {
    const search = new URLSearchParams();
    if (params?.dry_run !== undefined) search.set('dry_run', String(params.dry_run));
    if (params?.source_limit !== undefined) search.set('source_limit', String(params.source_limit));
    if (params?.candidate_limit !== undefined) {
      search.set('candidate_limit', String(params.candidate_limit));
    }
    if (params?.archive_exceptions !== undefined) {
      search.set('archive_exceptions', String(params.archive_exceptions));
    }
    const suffix = search.toString();
    return fetchApi<MaintenanceJobResponse>(`/jobs/reflection-dream${suffix ? `?${suffix}` : ''}`, {
      method: 'POST',
    });
  },
};

export const backupsApi = {
  settings: {
    get: () => fetchApi<BackupSettingsResponse>('/backups/settings'),
    update: (data: BackupSettingsUpdate) =>
      fetchApi<BackupSettingsResponse>('/backups/settings', {
        method: 'PATCH',
        body: JSON.stringify(data),
      }),
  },
  list: (limit = 50, offset = 0) =>
    fetchApi<BackupListResponse>(`/backups?limit=${limit}&offset=${offset}`),
  get: (backupId: string) => fetchApi<BackupInfo>(`/backups/${backupId}`),
  create: (data?: CreateBackupRequest) =>
    fetchApi<CreateBackupResponse>('/backups', {
      method: 'POST',
      body: JSON.stringify(data ?? {}),
    }),
  delete: (backupId: string) =>
    fetchApi<{ deleted: boolean; backup_id: string }>(`/backups/${backupId}`, {
      method: 'DELETE',
    }),
  download: (backupId: string) => `/api/backups/${backupId}/download`,
  cleanup: (retentionDays?: number) =>
    fetchApi<CleanupResponse>('/backups/cleanup', {
      method: 'POST',
      body: JSON.stringify(retentionDays ? { retention_days: retentionDays } : {}),
    }),
  jobStatus: (jobId: string) => fetchApi<BackupJobStatus>(`/backups/jobs/${jobId}`),
};

export const setupApi = {
  status: (validateKeys?: boolean) => {
    const query = validateKeys ? '?validate_keys=true' : '';
    return fetchApi<SetupStatus>(`/setup/status${query}`);
  },

  validateKeys: () => fetchApi<ApiKeyValidation>('/setup/validate-keys'),

  integration: () => fetchApi<IntegrationResponse>('/setup/integration'),
};

export const settingsApi = {
  get: () => fetchApi<SettingsResponse>('/settings'),

  update: (request: UpdateSettingsRequest) =>
    fetchApi<UpdateSettingsResponse>('/settings', {
      method: 'PATCH',
      body: JSON.stringify(request),
    }),

  delete: (key: string) =>
    fetchApi<DeleteSettingResponse>(`/settings/${key}`, {
      method: 'DELETE',
    }),

  ai: {
    getLLMSettings: () => fetchApi<LLMSettingsResponse>('/settings/ai/llm'),

    updateLLMSurface: (surface: LLMSurface, request: UpdateLLMSurfaceRequest) =>
      fetchApi<UpdateLLMSurfaceResponse>(`/settings/ai/llm/${surface}`, {
        method: 'PUT',
        body: JSON.stringify(request),
      }),

    testLLMSurface: (surface: LLMSurface) =>
      fetchApi<LLMTestResult>(`/settings/ai/llm/${surface}/test`, {
        method: 'POST',
      }),

    testProviderKey: (provider: LLMProviderName) =>
      fetchApi<ProviderKeyTestResult>(`/settings/ai/keys/${provider}/test`, {
        method: 'POST',
      }),

    testModel: (modelAlias: string) =>
      fetchApi<ModelAvailabilityTestResult>(`/settings/ai/models/${modelAlias}/test`, {
        method: 'POST',
      }),

    getRegistry: (kind?: AIModelKind) => {
      const query = kind ? `?kind=${encodeURIComponent(kind)}` : '';
      return fetchApi<AIRegistryResponse>(`/settings/ai/registry${query}`);
    },
  },
};
