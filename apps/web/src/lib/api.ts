/**
 * API client for Sibyl backend.
 *
 * Uses fetch with React Query for data fetching and WebSocket for realtime updates.
 */

const API_BASE = '/api';

// =============================================================================
// Types (generated from OpenAPI will replace these)
// =============================================================================

// -----------------------------------------------------------------------------
// Metadata Types - Strongly typed entity metadata by entity type
// -----------------------------------------------------------------------------

/** Base metadata fields common to all entities */
export interface BaseMetadata {
  created_at?: string;
  updated_at?: string;
}

/** Task entity metadata */
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

/** Source (documentation) entity metadata */
export interface SourceMetadata extends BaseMetadata {
  crawl_status?: CrawlStatus;
  source_type?: SourceType;
  document_count?: number;
  total_tokens?: number;
  last_crawled?: string;
  url?: string;
  tags?: string[];
  crawl_error?: string;
  max_pages?: number;
  max_depth?: number;
}

/** Project entity metadata */
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

/** Search result metadata */
export interface SearchResultMetadata extends BaseMetadata {
  document_id?: string;
  source_id?: string;
  chunk_index?: number;
  section_path?: string;
}

/** Graph node metadata */
export interface GraphNodeMetadata extends BaseMetadata {
  entity_type?: string;
  [key: string]: unknown; // Allow additional fields
}

/** Type for task status values */
export type TaskStatus = 'backlog' | 'todo' | 'doing' | 'blocked' | 'review' | 'done' | 'archived';

/** Type for task priority values */
export type TaskPriority = 'critical' | 'high' | 'medium' | 'low' | 'someday';

/** Type for source crawl status */
export type CrawlStatus = 'pending' | 'in_progress' | 'completed' | 'failed' | 'partial';

/** Type for source types */
export type SourceType = 'website' | 'github' | 'local' | 'api_docs';

/** Maps entity types to their metadata types */
export type EntityMetadataMap = {
  task: TaskMetadata;
  source: SourceMetadata;
  project: ProjectMetadata;
  epic: EpicMetadata;
  // Generic entities use base metadata
  pattern: BaseMetadata;
  procedure: BaseMetadata;
  episode: BaseMetadata;
  rule: BaseMetadata;
  template: BaseMetadata;
  tool: BaseMetadata;
  topic: BaseMetadata;
  document: BaseMetadata;
};

export interface RelatedEntitySummary {
  id: string;
  name: string;
  entity_type: string;
  relationship: string;
  direction: 'outgoing' | 'incoming';
}

export interface Entity {
  id: string;
  entity_type: string;
  name: string;
  description: string;
  content: string;
  category: string | null;
  languages: string[];
  tags: string[];
  metadata: Record<string, unknown>;
  source_file: string | null;
  created_at: string | null;
  updated_at: string | null;
  related?: RelatedEntitySummary[] | null;
}

export interface EntityGetParams {
  include_summary?: boolean;
  related_limit?: number;
}

export interface EntityCreate {
  name: string;
  description?: string;
  content?: string;
  entity_type?: string;
  category?: string;
  languages?: string[];
  tags?: string[];
  metadata?: Record<string, unknown>;
}

export interface EntityUpdate {
  name?: string;
  description?: string;
  content?: string;
  category?: string;
  languages?: string[];
  tags?: string[];
  metadata?: Record<string, unknown>;
}

export interface EntityListResponse {
  entities: Entity[];
  total: number;
  page: number;
  page_size: number;
  has_more: boolean;
}

export interface RawCaptureSummary {
  id: string;
  entity_id: string | null;
  title: string;
  entity_type: string;
  tags: string[];
  metadata: Record<string, unknown>;
  capture_surface: string | null;
  review_state: 'pending' | 'deferred' | 'archived';
  created_by_user_id: string | null;
  created_at: string;
}

export interface RawCapture extends RawCaptureSummary {
  raw_content: string;
}

export interface RawCaptureListResponse {
  captures: RawCaptureSummary[];
  limit: number;
  offset: number;
  has_more: boolean;
}

export type RawCaptureReviewState = 'pending' | 'deferred' | 'archived';

export type MemoryScope =
  | 'private'
  | 'delegated'
  | 'project'
  | 'team'
  | 'organization'
  | 'shared'
  | 'public';

export interface MemoryAuditEvent {
  id: string;
  organization_id: string | null;
  user_id: string | null;
  action: string;
  memory_scope: string | null;
  scope_key: string | null;
  project_id: string | null;
  source_surface: string | null;
  source_ids: string[];
  source_ids_truncated: number | null;
  derived_ids: string[];
  derived_ids_truncated: number | null;
  policy_allowed: boolean | null;
  policy_reason: string | null;
  details: Record<string, unknown>;
  created_at: string | null;
}

export interface MemoryAuditListResponse {
  events: MemoryAuditEvent[];
  limit: number;
}

export interface MemorySpaceMember {
  id: string;
  organization_id: string;
  space_id: string;
  principal_type: string;
  principal_id: string;
  role: string;
  permissions: string[];
  expires_at: string | null;
  created_by_user_id: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface MemorySpace {
  id: string;
  organization_id: string;
  memory_scope: MemoryScope;
  scope_key: string | null;
  name: string;
  description: string | null;
  state: 'active' | 'disabled';
  disabled_reason: string | null;
  metadata: Record<string, unknown>;
  created_by_user_id: string;
  created_at: string | null;
  updated_at: string | null;
  members: MemorySpaceMember[];
}

export interface MemorySpaceListResponse {
  spaces: MemorySpace[];
}

export interface MemoryDerivedRecord {
  id: string;
  record_type: string;
  source_action: string;
}

export interface MemorySourceInspectResponse {
  id: string;
  organization_id: string;
  source_id: string;
  principal_id: string;
  agent_id: string | null;
  project_id: string | null;
  memory_scope: MemoryScope;
  scope_key: string | null;
  review_state: string;
  visibility: Record<string, unknown>;
  lifecycle: Record<string, unknown>;
  reflection_findings: Record<string, unknown>[];
  claim_records: Record<string, unknown>[];
  correction_history: Record<string, unknown>[];
  promotion_state: Record<string, unknown>;
  share_state: Record<string, unknown>;
  entity_type: string;
  title: string;
  raw_content: string | null;
  content_redacted: boolean;
  raw_content_length: number;
  tags: string[];
  metadata: Record<string, unknown>;
  provenance: Record<string, unknown>;
  capture_surface: string | null;
  captured_at: string | null;
  created_at: string | null;
  freshness_timestamps: Record<string, string | null>;
  transform_versions: Record<string, unknown>;
  policy_allowed: boolean;
  policy_reason: string;
  policy_metadata: Record<string, unknown>;
  derived_ids: string[];
  derived_types: string[];
  derived_records: MemoryDerivedRecord[];
  recent_audit_events: MemoryAuditEvent[];
  audit_event_count: number;
  available_actions: Record<string, unknown>[];
}

export type MemoryCorrectionAction =
  | 'delete'
  | 'hide'
  | 'mark_duplicate'
  | 'mark_sensitive'
  | 'mark_stale'
  | 'mark_wrong'
  | 'redact'
  | 'restore'
  | 'supersede';

export interface MemoryCorrectionRequest {
  action: MemoryCorrectionAction;
  reason?: string | null;
  replacement_source_id?: string | null;
  duplicate_of_source_id?: string | null;
  metadata?: Record<string, unknown>;
}

export interface MemoryCorrectionResponse {
  allowed: boolean;
  applied: boolean;
  source_id: string;
  action: string;
  reason: string;
  target_review_state: string;
  updated_review_state: string | null;
  affected_source_ids: string[];
  affected_derived_ids: string[];
  reversible: boolean;
  recall_impact: Record<string, unknown>;
  synthesis_impact: Record<string, unknown>;
  audit_action: string;
  policy_reasons: string[];
  metadata: Record<string, unknown>;
}

export interface SourceImportProgress {
  imported_count: number;
  skipped_count: number;
  dedupe_count: number;
  error_count: number;
  attachment_count: number;
  extraction_pending_count: number;
  raw_memory_count: number;
}

export type SourceImportStatus =
  | 'pending'
  | 'running'
  | 'paused'
  | 'completed'
  | 'failed'
  | 'canceled';

export interface SourceImportStatusResponse {
  import_id: string;
  adapter_name: string;
  adapter_version: string | null;
  source_identity: string | null;
  source_version: string | null;
  status: SourceImportStatus;
  privacy_class: string | null;
  target_memory_scope: MemoryScope | null;
  target_scope_key: string | null;
  checkpoint: Record<string, unknown> | null;
  progress: SourceImportProgress;
  raw_memory_ids: string[];
  dedupe_keys: string[];
  duplicate_dedupe_keys: string[];
  skipped_records: Record<string, unknown>[];
  errors: Record<string, unknown>[];
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

export interface SourceAdapter {
  name: string;
  version: string;
  source_type: string;
  display_name: string;
  capabilities: string[];
  default_privacy_class: string;
  transform_behavior: string;
  metadata_schema: Record<string, unknown>;
  supports_incremental: boolean;
}

export interface SourceAdapterListResponse {
  adapters: SourceAdapter[];
}

export interface SourceImportStartRequest {
  source_uri: string;
  adapter_name?: string;
  target_memory_scope?: MemoryScope;
  target_scope_key?: string | null;
  options?: Record<string, unknown>;
  batch_size?: number;
  promotion_preview_approved?: boolean;
}

export interface SourceImportResumeRequest {
  batch_size?: number | null;
  promotion_preview_approved?: boolean | null;
}

export type SynthesisOutputType =
  | 'documentation'
  | 'report'
  | 'briefing'
  | 'roadmap'
  | 'release_notes'
  | 'audit_packet'
  | 'custom';

export type SynthesisDepth = 'brief' | 'standard' | 'deep';
export type SynthesisRunStatus = 'planned' | 'drafting' | 'verified' | 'failed';
export type SynthesisVerificationStatus = 'pending' | 'gaps' | 'pass';
export type SynthesisArtifactFormat = 'markdown' | 'json';

export interface SynthesisSectionRequest {
  title: string;
  prompt?: string | null;
  required_source_ids?: string[];
}

export interface SynthesisRequest {
  goal: string;
  output_type?: SynthesisOutputType;
  audience?: string | null;
  depth?: SynthesisDepth;
  seed_query?: string | null;
  project?: string | null;
  domain?: string | null;
  entity_ids?: string[];
  decision_ids?: string[];
  task_ids?: string[];
  artifact_ids?: string[];
  required_sections?: SynthesisSectionRequest[];
  constraints?: string[];
  max_sections?: number;
  include_neighborhoods?: boolean;
}

export interface SynthesisSourceReference {
  id: string;
  type: string;
  name: string;
  content_preview: string;
  score: number;
  source: string | null;
  origin: string;
  relation: string | null;
  metadata: Record<string, unknown>;
}

export interface SynthesisGap {
  section_id: string;
  title: string;
  reason: string;
  query: string;
  missing_source_ids: string[];
}

export interface SynthesisOutlineSection {
  section_id: string;
  title: string;
  prompt: string;
  source_query: string;
  source_ids: string[];
  gaps: SynthesisGap[];
}

export interface SynthesisOutline {
  title: string;
  output_type: SynthesisOutputType;
  audience: string | null;
  sections: SynthesisOutlineSection[];
}

export interface SynthesisSourcePack {
  section_id: string;
  title: string;
  query: string;
  source_ids: string[];
  sources: SynthesisSourceReference[];
  hidden_count: number;
  redaction_count: number;
  correction_count: number;
  correction_reasons: Record<string, number>;
  freshness: Record<string, string | null>;
  unresolved_claims: string[];
}

export interface SynthesisVerification {
  status: SynthesisVerificationStatus;
  source_count: number;
  gap_count: number;
  gaps: SynthesisGap[];
}

export interface SynthesisPlanResponse {
  run_id: string;
  status: SynthesisRunStatus;
  request: SynthesisRequest;
  outline: SynthesisOutline;
  source_packs: SynthesisSourcePack[];
  verification: SynthesisVerification;
}

export interface SynthesisArtifact {
  artifact_id: string;
  format: SynthesisArtifactFormat;
  title: string;
  markdown: string;
  json_payload: Record<string, unknown>;
  source_ids: string[];
  section_source_ids: Record<string, string[]>;
  generated_text_hash: string;
  verification: SynthesisVerification;
  remembered_memory_id: string | null;
  remembered_source_id: string | null;
}

export interface SynthesisDraftRequest extends SynthesisRequest {
  output_format?: SynthesisArtifactFormat;
  remember?: boolean;
  memory_scope?: MemoryScope;
  scope_key?: string | null;
  tags?: string[];
}

export interface SynthesisDraftResponse extends SynthesisPlanResponse {
  artifact: SynthesisArtifact;
}

export interface SessionBundleContext {
  generated_at: string;
  org_slug: string | null;
  project_ids: string[];
  scope: 'all_projects' | 'project_selection';
}

export interface SessionTaskSummary {
  id: string;
  name: string;
  status: string;
  priority: string;
  feature: string | null;
  branch_name: string | null;
}

export interface SessionMemorySummary {
  id: string;
  name: string;
  entity_type: string | null;
  source: string | null;
  preview: string;
  document_id: string | null;
}

export interface SessionBundleResponse {
  context: SessionBundleContext;
  query: string | null;
  tasks: SessionTaskSummary[];
  relevant_entities: SessionMemorySummary[];
  remember_next: string;
}

export type EntitySortField = 'name' | 'created_at' | 'updated_at' | 'entity_type';
export type SortOrder = 'asc' | 'desc';

export interface SearchResult {
  id: string;
  type: string;
  name: string;
  content: string;
  score: number;
  source: string | null;
  url: string | null;
  result_origin: 'graph' | 'document';
  metadata: Record<string, unknown>;
}

export interface SearchResponse {
  results: SearchResult[];
  total: number;
  query: string;
  filters: Record<string, unknown>;
  graph_count?: number;
  document_count?: number;
  limit?: number;
  offset?: number;
  has_more?: boolean;
  actual_total?: number;
}

export interface GraphNode {
  id: string;
  type: string;
  label: string;
  color: string;
  size: number;
  x?: number;
  y?: number;
  metadata: Record<string, unknown>;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  type: string;
  label: string;
  weight: number;
}

export interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
  node_count: number;
  edge_count: number;
}

// Cluster types for bubble visualization
export interface Cluster {
  id: string;
  count: number;
  dominant_type: string;
  type_distribution: Record<string, number>;
  level: number;
}

export interface ClustersResponse {
  clusters: Cluster[];
  total_nodes: number;
  total_clusters: number;
}

export interface ClusterDetailResponse {
  cluster_id: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
  node_count: number;
  edge_count: number;
}

export interface GraphStatsResponse {
  total_nodes: number;
  total_edges: number;
  by_type: Record<string, number>;
}

// Hierarchical graph with cluster assignments for rich visualization
export type GraphResolution = 'overview' | 'detail';

export interface HierarchicalNode {
  id: string;
  name: string;
  type: string;
  label: string;
  color: string;
  summary: string;
  cluster_id: string;
  aggregate?: boolean;
  member_count?: number;
}

export interface HierarchicalEdge {
  source: string;
  target: string;
  type: string;
}

export interface HierarchicalCluster {
  id: string;
  member_count: number;
  displayed_member_count?: number;
  level: number;
  type_distribution: Record<string, number>;
  displayed_type_distribution?: Record<string, number>;
  dominant_type: string;
  displayed_dominant_type?: string;
}

export interface ClusterEdge {
  source: string;
  target: string;
  weight: number;
}

export interface HierarchicalGraphResponse {
  nodes: HierarchicalNode[];
  edges: HierarchicalEdge[];
  clusters: HierarchicalCluster[];
  cluster_edges: ClusterEdge[];
  total_nodes: number;
  total_edges: number;
  displayed_nodes?: number;
  displayed_edges?: number;
  resolution?: GraphResolution;
}

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

// =============================================================================
// Metrics Types
// =============================================================================

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

// =============================================================================
// Auth + Orgs
// =============================================================================

export interface AuthMeResponse {
  user: {
    id: string;
    github_id: number | null;
    email: string | null;
    name: string;
    avatar_url: string | null;
  };
  organization: { id: string; slug: string; name: string } | null;
  org_role: string | null;
}

export interface OrgSummary {
  id: string;
  slug: string;
  name: string;
  is_personal: boolean;
  role: string | null;
}

export interface OrgListResponse {
  orgs: OrgSummary[];
}

export interface OrgSwitchResponse {
  organization: { id: string; slug: string; name: string };
  access_token: string;
}

export interface OrgCreateRequest {
  name: string;
  slug?: string;
}

export interface OrgUpdateRequest {
  name?: string;
  slug?: string;
}

export interface OrgCreateResponse {
  organization: { id: string; slug: string; name: string };
  access_token: string;
}

export interface OrgGetResponse {
  organization: { id: string; slug: string; name: string };
  role: string;
}

export interface OrgMember {
  user: {
    id: string;
    github_id: number | null;
    email: string | null;
    name: string | null;
    avatar_url: string | null;
  };
  role: string;
  created_at: string;
}

export interface OrgMembersResponse {
  members: OrgMember[];
}

export interface OrgInvitation {
  id: string;
  email: string;
  role: string;
  created_at: string | null;
  expires_at: string | null;
  accept_url: string | null;
}

export interface OrgInvitationsResponse {
  invitations: OrgInvitation[];
}

export interface OrgInvitationCreateRequest {
  email: string;
  role: string;
  expires_days?: number;
}

export interface OrgInvitationCreateResponse {
  invitation: OrgInvitation;
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

// =============================================================================
// Security Types (Sessions, API Keys, OAuth)
// =============================================================================

export interface Session {
  id: string;
  user_agent: string | null;
  ip_address: string | null;
  created_at: string;
  expires_at: string;
  last_used_at: string | null;
  is_current: boolean;
}

export interface SessionsResponse {
  sessions: Session[];
}

export interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  scopes: string[];
  project_ids: string[];
  memory_space_ids: string[];
  last_used_at: string | null;
  expires_at: string | null;
  created_at: string | null;
}

export interface ApiKeysResponse {
  api_keys: ApiKey[];
}

export interface ApiKeyCreateRequest {
  name: string;
  scopes?: string[];
  project_ids?: string[];
  memory_space_ids?: string[];
  expires_in_days?: number;
}

export interface ApiKeyCreateResponse {
  api_key: ApiKey;
  key: string; // Full key, only shown once
}

export interface OAuthConnection {
  id: string;
  provider: string;
  provider_user_id: string;
  email: string | null;
  name: string | null;
  avatar_url: string | null;
  created_at: string;
}

export interface OAuthConnectionsResponse {
  connections: OAuthConnection[];
}

interface ApiKeyBackendRecord {
  id: string;
  name: string;
  prefix?: string;
  key_prefix?: string;
  scopes?: string[];
  project_ids?: string[];
  memory_space_ids?: string[];
  last_used_at?: string | null;
  expires_at?: string | null;
  created_at?: string | null;
}

interface ApiKeysBackendResponse {
  keys: ApiKeyBackendRecord[];
}

interface ApiKeyCreateBackendResponse extends ApiKeyBackendRecord {
  api_key: string;
}

interface OAuthConnectionBackendRecord {
  id: string;
  provider: string;
  provider_user_id: string;
  provider_email: string | null;
  connected_at: string;
}

function normalizeApiKey(record: ApiKeyBackendRecord): ApiKey {
  return {
    id: record.id,
    name: record.name,
    prefix: record.prefix ?? record.key_prefix ?? '',
    scopes: record.scopes ?? [],
    project_ids: record.project_ids ?? [],
    memory_space_ids: record.memory_space_ids ?? [],
    last_used_at: record.last_used_at ?? null,
    expires_at: record.expires_at ?? null,
    created_at: record.created_at ?? null,
  };
}

function normalizeOAuthConnection(record: OAuthConnectionBackendRecord): OAuthConnection {
  return {
    id: record.id,
    provider: record.provider,
    provider_user_id: record.provider_user_id,
    email: record.provider_email,
    name: null,
    avatar_url: null,
    created_at: record.connected_at,
  };
}

export interface PasswordChangeRequest {
  current_password: string;
  new_password: string;
}

// Onboarding checklist state
export interface OnboardingChecklist {
  connected_agent?: boolean;
  added_source?: boolean;
  tried_search?: boolean;
}

// User Preferences (flexible dict stored on user)
export interface UserPreferences {
  theme?: 'light' | 'dark' | 'system';
  locale?: string;
  timezone?: string;
  graphShowLabels?: boolean;
  graphDefaultZoom?: number;
  dashboardDefaultView?: 'grid' | 'list';
  notifyOnTaskAssigned?: boolean;
  notifyOnMention?: boolean;
  is_onboarded?: boolean; // Has user completed onboarding wizard
  onboarding_checklist?: OnboardingChecklist;
  [key: string]: unknown; // Allow additional preferences
}

export interface PreferencesResponse {
  preferences: UserPreferences;
}

// =============================================================================
// Task Types
// =============================================================================

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

// =============================================================================
// Source Types (Documentation Crawling)
// =============================================================================

export interface LocalSourceData {
  path: string;
  name: string;
  description: string;
  tags: string[];
}

export interface Source {
  id: string;
  name: string;
  description: string;
  url: string;
  source_type: SourceType;
  crawl_depth: number;
  crawl_patterns: string[];
  exclude_patterns: string[];
  crawl_status: CrawlStatus;
  last_crawled: string | null;
  document_count: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface SourceSummary {
  id: string;
  type: string;
  name: string;
  description: string;
  created_at?: string;
  updated_at?: string;
  metadata: {
    url?: string;
    source_type?: SourceType;
    crawl_status?: CrawlStatus;
    document_count?: number;
    total_tokens?: number;
    total_entities?: number;
    last_crawled?: string;
    crawl_error?: string;
    crawl_depth?: number;
    crawl_patterns?: string[];
    exclude_patterns?: string[];
    tags?: string[];
  };
}

export interface SourceListResponse {
  mode: string;
  entities: SourceSummary[];
  total: number;
  filters: Record<string, unknown>;
}

// Crawler API types (from /crawler endpoints)
export interface CrawlSource {
  id: string;
  name: string;
  url: string;
  source_type: SourceType;
  description: string | null;
  crawl_depth: number;
  crawl_status: CrawlStatus;
  document_count: number;
  chunk_count: number;
  last_crawled_at: string | null;
  last_error: string | null;
  created_at: string;
  include_patterns: string[];
  exclude_patterns: string[];
}

// =============================================================================
// RAG Search Types (Documentation Search)
// =============================================================================

export interface RAGSearchParams {
  query: string;
  source_id?: string;
  source_name?: string;
  match_count?: number;
  similarity_threshold?: number;
  return_mode?: 'chunks' | 'pages';
  include_context?: boolean;
}

export interface RAGChunkResult {
  chunk_id: string;
  document_id: string;
  source_id: string;
  source_name: string;
  url: string;
  title: string;
  content: string;
  context: string | null;
  similarity: number;
  chunk_type: 'text' | 'code' | 'heading' | 'list' | 'table';
  chunk_index: number;
  heading_path: string[];
  language: string | null;
}

export interface RAGPageResult {
  document_id: string;
  source_id: string;
  source_name: string;
  url: string;
  title: string;
  content: string;
  word_count: number;
  has_code: boolean;
  headings: string[];
  code_languages: string[];
  best_chunk_similarity: number;
}

export interface RAGSearchResponse {
  results: (RAGChunkResult | RAGPageResult)[];
  total: number;
  query: string;
  source_filter: string | null;
  return_mode: 'chunks' | 'pages';
}

export interface CodeExampleParams {
  query: string;
  language?: string;
  source_id?: string;
  match_count?: number;
}

export interface CodeExampleResult {
  chunk_id: string;
  document_id: string;
  source_id: string;
  source_name: string;
  url: string;
  title: string;
  code: string;
  context: string | null;
  language: string | null;
  similarity: number;
  heading_path: string[];
}

export interface CodeExampleResponse {
  examples: CodeExampleResult[];
  total: number;
  query: string;
  language_filter: string | null;
}

export interface FullPageResponse {
  document_id: string;
  source_id: string;
  source_name: string;
  url: string;
  title: string;
  content: string;
  raw_content: string | null;
  word_count: number;
  token_count: number;
  has_code: boolean;
  headings: string[];
  code_languages: string[];
  links: string[];
  crawled_at: string;
}

export interface DocumentUpdateRequest {
  title?: string;
  content?: string;
}

export interface DocumentRelatedEntity {
  id: string;
  name: string;
  entity_type: string;
  description: string;
  chunk_count: number;
}

export interface DocumentRelatedEntitiesResponse {
  document_id: string;
  entities: DocumentRelatedEntity[];
  total: number;
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
  include_database_dump?: boolean;
  include_graph?: boolean;
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

export interface CreateBackupRequest {
  include_database_dump?: boolean;
  include_graph?: boolean;
}

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

// =============================================================================
// API Functions
// =============================================================================

// Track if we're currently refreshing to prevent multiple refresh attempts
let isRefreshing = false;
let refreshPromise: Promise<boolean> | null = null;
let refreshCooldownUntil = 0;
let logoutPromise: Promise<void> | null = null;

/**
 * Try to refresh the access token using the refresh token cookie.
 * Returns true if refresh succeeded, false if it failed.
 */
async function tryRefreshToken(): Promise<boolean> {
  const now = Date.now();
  if (now < refreshCooldownUntil) {
    return false;
  }

  // If already refreshing, wait for that to complete
  if (isRefreshing && refreshPromise !== null) {
    return refreshPromise;
  }

  isRefreshing = true;
  refreshPromise = (async () => {
    try {
      const response = await fetch(`${API_BASE}/auth/refresh`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
      });
      if (response.ok) {
        refreshCooldownUntil = 0;
        return true;
      }

      const retryAfter = response.headers.get('Retry-After');
      if (response.status === 429 && retryAfter) {
        const retryAfterSeconds = Number(retryAfter);
        if (Number.isFinite(retryAfterSeconds)) {
          refreshCooldownUntil = Date.now() + retryAfterSeconds * 1000;
          return false;
        }

        const retryAt = Date.parse(retryAfter);
        if (!Number.isNaN(retryAt)) {
          refreshCooldownUntil = Math.max(Date.now() + 30_000, retryAt);
          return false;
        }
      }

      // Default cooldown to avoid hammering refresh on repeated 401s across many requests.
      refreshCooldownUntil = Date.now() + (response.status === 429 ? 60_000 : 30_000);
      return false;
    } catch {
      refreshCooldownUntil = Date.now() + 30_000;
      return false;
    } finally {
      isRefreshing = false;
      refreshPromise = null;
    }
  })();

  return refreshPromise;
}

async function bestEffortLogout(): Promise<void> {
  if (logoutPromise !== null) return logoutPromise;

  logoutPromise = (async () => {
    try {
      await fetch(`${API_BASE}/auth/logout`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        keepalive: true,
      });
    } catch {
      // Ignore network errors - we're already falling back to login.
    } finally {
      logoutPromise = null;
    }
  })();

  return logoutPromise;
}

/**
 * Redirect to login page with return URL.
 */
function redirectToLogin(): never {
  // Best-effort: clear cookies so middleware doesn't bounce `/login` back to `/`.
  void bestEffortLogout();

  const currentPath = window.location.pathname + window.location.search;
  window.location.href = `/login?next=${encodeURIComponent(currentPath)}`;
  // Return a promise that never resolves to prevent further execution
  return new Promise(() => {
    // Intentionally empty - blocks until page redirects
  }) as never;
}

async function fetchApi<T>(endpoint: string, options?: RequestInit): Promise<T> {
  const makeRequest = () =>
    fetch(`${API_BASE}${endpoint}`, {
      ...options,
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        ...options?.headers,
      },
    });

  const response = await makeRequest();

  if (!response.ok) {
    // Handle 401 - try to refresh token before redirecting to login
    if (response.status === 401 && typeof window !== 'undefined') {
      // Don't try to refresh if we're on login page or if this IS the refresh endpoint
      if (window.location.pathname !== '/login' && endpoint !== '/auth/refresh') {
        const refreshed = await tryRefreshToken();
        const retryResponse = await makeRequest();

        if (retryResponse.ok) {
          if (retryResponse.status === 204) {
            return undefined as T;
          }
          return retryResponse.json();
        }

        if (retryResponse.status === 401) {
          return redirectToLogin();
        }

        const error = await retryResponse.text();
        if (!refreshed) {
          throw new Error(error || `API error after refresh retry: ${retryResponse.status}`);
        }
        throw new Error(error || `API error: ${retryResponse.status}`);
      }
    }

    const error = await response.text();
    throw new Error(error || `API error: ${response.status}`);
  }

  // Handle 204 No Content (e.g., DELETE responses)
  if (response.status === 204) {
    return undefined as T;
  }

  return response.json();
}

// Entities
export const api = {
  // Entity CRUD
  entities: {
    list: (params?: {
      entity_type?: string;
      language?: string;
      category?: string;
      search?: string;
      project_ids?: string[];
      page?: number;
      page_size?: number;
      sort_by?: 'name' | 'created_at' | 'updated_at' | 'entity_type';
      sort_order?: 'asc' | 'desc';
    }) => {
      const searchParams = new URLSearchParams();
      if (params?.entity_type) searchParams.set('entity_type', params.entity_type);
      if (params?.language) searchParams.set('language', params.language);
      if (params?.category) searchParams.set('category', params.category);
      if (params?.search) searchParams.set('search', params.search);
      if (params?.project_ids?.length) {
        // FastAPI expects repeated query params for list
        for (const id of params.project_ids) {
          searchParams.append('project_ids', id);
        }
      }
      if (params?.page) searchParams.set('page', params.page.toString());
      if (params?.page_size) searchParams.set('page_size', params.page_size.toString());
      if (params?.sort_by) searchParams.set('sort_by', params.sort_by);
      if (params?.sort_order) searchParams.set('sort_order', params.sort_order);
      const query = searchParams.toString();
      return fetchApi<EntityListResponse>(`/entities${query ? `?${query}` : ''}`);
    },

    get: (id: string, params?: EntityGetParams) => {
      const searchParams = new URLSearchParams();
      if (params?.include_summary === false) searchParams.set('include_summary', 'false');
      if (params?.related_limit !== undefined) {
        searchParams.set('related_limit', params.related_limit.toString());
      }
      const query = searchParams.toString();
      return fetchApi<Entity>(`/entities/${id}${query ? `?${query}` : ''}`);
    },

    create: (entity: EntityCreate) =>
      fetchApi<Entity>('/entities', {
        method: 'POST',
        body: JSON.stringify(entity),
      }),

    update: (id: string, updates: EntityUpdate) =>
      fetchApi<Entity>(`/entities/${id}`, {
        method: 'PATCH',
        body: JSON.stringify(updates),
      }),

    delete: (id: string) =>
      fetchApi<void>(`/entities/${id}`, {
        method: 'DELETE',
      }),
  },

  rawCaptures: {
    list: (params?: {
      entity_type?: string;
      capture_surface?: string;
      review_state?: RawCaptureReviewState;
      limit?: number;
      offset?: number;
    }) => {
      const searchParams = new URLSearchParams();
      if (params?.entity_type) searchParams.set('entity_type', params.entity_type);
      if (params?.capture_surface) searchParams.set('capture_surface', params.capture_surface);
      if (params?.review_state) searchParams.set('review_state', params.review_state);
      if (params?.limit) searchParams.set('limit', params.limit.toString());
      if (params?.offset) searchParams.set('offset', params.offset.toString());
      const query = searchParams.toString();
      return fetchApi<RawCaptureListResponse>(`/entities/captures${query ? `?${query}` : ''}`);
    },

    get: (id: string) => fetchApi<RawCapture>(`/entities/captures/${encodeURIComponent(id)}`),
    updateReviewState: (id: string, review_state: RawCaptureReviewState) =>
      fetchApi<RawCapture>(`/entities/captures/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        body: JSON.stringify({ review_state }),
      }),
  },

  memory: {
    audit: {
      list: (params?: {
        action?: string;
        actor_user_id?: string;
        source_id?: string;
        derived_id?: string;
        memory_scope?: string;
        project_id?: string;
        policy_allowed?: boolean;
        limit?: number;
      }) => {
        const searchParams = new URLSearchParams();
        if (params?.action) searchParams.set('action', params.action);
        if (params?.actor_user_id) searchParams.set('actor_user_id', params.actor_user_id);
        if (params?.source_id) searchParams.set('source_id', params.source_id);
        if (params?.derived_id) searchParams.set('derived_id', params.derived_id);
        if (params?.memory_scope) searchParams.set('memory_scope', params.memory_scope);
        if (params?.project_id) searchParams.set('project_id', params.project_id);
        if (params?.policy_allowed !== undefined) {
          searchParams.set('policy_allowed', String(params.policy_allowed));
        }
        if (params?.limit) searchParams.set('limit', params.limit.toString());
        const query = searchParams.toString();
        return fetchApi<MemoryAuditListResponse>(`/memory/audit${query ? `?${query}` : ''}`);
      },
    },

    spaces: {
      list: () => fetchApi<MemorySpaceListResponse>('/memory/spaces'),
    },

    sourceImportStatus: (importId: string) =>
      fetchApi<SourceImportStatusResponse>(
        `/memory/source-imports/${encodeURIComponent(importId)}`
      ),

    inspect: {
      get: (sourceId: string) =>
        fetchApi<MemorySourceInspectResponse>(`/memory/inspect/${encodeURIComponent(sourceId)}`),
      previewCorrection: (sourceId: string, request: MemoryCorrectionRequest) =>
        fetchApi<MemoryCorrectionResponse>(
          `/memory/inspect/${encodeURIComponent(sourceId)}/corrections/preview`,
          {
            method: 'POST',
            body: JSON.stringify(request),
          }
        ),
      applyCorrection: (sourceId: string, request: MemoryCorrectionRequest) =>
        fetchApi<MemoryCorrectionResponse>(
          `/memory/inspect/${encodeURIComponent(sourceId)}/corrections`,
          {
            method: 'POST',
            body: JSON.stringify(request),
          }
        ),
    },
  },

  sourceImports: {
    adapters: () => fetchApi<SourceAdapterListResponse>('/sources/import-adapters'),
    start: (request: SourceImportStartRequest) =>
      fetchApi<SourceImportStatusResponse>('/sources/imports', {
        method: 'POST',
        body: JSON.stringify(request),
      }),
    get: (importId: string) =>
      fetchApi<SourceImportStatusResponse>(`/sources/imports/${encodeURIComponent(importId)}`),
    resume: (importId: string, request: SourceImportResumeRequest = {}) =>
      fetchApi<SourceImportStatusResponse>(
        `/sources/imports/${encodeURIComponent(importId)}/resume`,
        {
          method: 'POST',
          body: JSON.stringify(request),
        }
      ),
    cancel: (importId: string) =>
      fetchApi<SourceImportStatusResponse>(
        `/sources/imports/${encodeURIComponent(importId)}/cancel`,
        { method: 'POST' }
      ),
  },

  synthesis: {
    plan: (request: SynthesisRequest) =>
      fetchApi<SynthesisPlanResponse>('/synthesis/plan', {
        method: 'POST',
        body: JSON.stringify(request),
      }),
    draft: (request: SynthesisDraftRequest) =>
      fetchApi<SynthesisDraftResponse>('/synthesis/draft', {
        method: 'POST',
        body: JSON.stringify(request),
      }),
  },

  // Search
  search: {
    query: (params: {
      query: string;
      types?: string[];
      language?: string;
      category?: string;
      status?: string;
      project?: string;
      assignee?: string;
      since?: string;
      limit?: number;
      include_content?: boolean;
      include_documents?: boolean;
      include_graph?: boolean;
      use_enhanced?: boolean;
      boost_recent?: boolean;
    }) =>
      fetchApi<SearchResponse>('/search', {
        method: 'POST',
        body: JSON.stringify(params),
      }),

    explore: (params: {
      mode?: 'list' | 'related' | 'traverse';
      types?: string[];
      entity_id?: string;
      relationship_types?: string[];
      depth?: number;
      language?: string;
      category?: string;
      limit?: number;
    }) =>
      fetchApi<{
        mode: string;
        entities: unknown[];
        total: number;
        filters: Record<string, unknown>;
      }>('/search/explore', {
        method: 'POST',
        body: JSON.stringify(params),
      }),
  },

  // Graph
  graph: {
    nodes: (params?: { types?: string[]; limit?: number }) => {
      const searchParams = new URLSearchParams();
      if (params?.types) {
        for (const t of params.types) searchParams.append('types', t);
      }
      if (params?.limit) searchParams.set('limit', params.limit.toString());
      const query = searchParams.toString();
      return fetchApi<GraphNode[]>(`/graph/nodes${query ? `?${query}` : ''}`);
    },

    edges: (params?: { relationship_types?: string[]; limit?: number }) => {
      const searchParams = new URLSearchParams();
      if (params?.relationship_types) {
        for (const t of params.relationship_types) searchParams.append('relationship_types', t);
      }
      if (params?.limit) searchParams.set('limit', params.limit.toString());
      const query = searchParams.toString();
      return fetchApi<GraphEdge[]>(`/graph/edges${query ? `?${query}` : ''}`);
    },

    full: (params?: { types?: string[]; max_nodes?: number; max_edges?: number }) => {
      const searchParams = new URLSearchParams();
      if (params?.types) {
        for (const t of params.types) searchParams.append('types', t);
      }
      if (params?.max_nodes) searchParams.set('max_nodes', params.max_nodes.toString());
      if (params?.max_edges) searchParams.set('max_edges', params.max_edges.toString());
      const query = searchParams.toString();
      return fetchApi<GraphData>(`/graph/full${query ? `?${query}` : ''}`);
    },

    subgraph: (params: {
      entity_id: string;
      depth?: number;
      relationship_types?: string[];
      max_nodes?: number;
    }) =>
      fetchApi<GraphData>('/graph/subgraph', {
        method: 'POST',
        body: JSON.stringify(params),
      }),

    // Cluster endpoints for bubble visualization
    clusters: (params?: { refresh?: boolean }) => {
      const searchParams = new URLSearchParams();
      if (params?.refresh) searchParams.set('refresh', 'true');
      const query = searchParams.toString();
      return fetchApi<ClustersResponse>(`/graph/clusters${query ? `?${query}` : ''}`);
    },

    clusterDetail: (clusterId: string) =>
      fetchApi<ClusterDetailResponse>(`/graph/clusters/${encodeURIComponent(clusterId)}`),

    stats: () => fetchApi<GraphStatsResponse>('/graph/stats'),

    // Hierarchical graph with cluster assignments for rich visualization
    hierarchical: (params?: {
      max_nodes?: number;
      max_edges?: number;
      projects?: string[];
      types?: string[];
      refresh?: boolean;
      resolution?: GraphResolution;
      cluster_id?: string;
    }) => {
      const searchParams = new URLSearchParams();
      if (params?.max_nodes) searchParams.set('max_nodes', params.max_nodes.toString());
      if (params?.max_edges) searchParams.set('max_edges', params.max_edges.toString());
      if (params?.projects?.length) {
        for (const p of params.projects) searchParams.append('projects', p);
      }
      if (params?.types?.length) {
        for (const t of params.types) searchParams.append('types', t);
      }
      if (params?.refresh) searchParams.set('refresh', 'true');
      if (params?.resolution) searchParams.set('resolution', params.resolution);
      if (params?.cluster_id) searchParams.set('cluster_id', params.cluster_id);
      const query = searchParams.toString();
      return fetchApi<HierarchicalGraphResponse>(`/graph/hierarchical${query ? `?${query}` : ''}`);
    },
  },

  // Health check (public - no auth required)
  checkHealth: () => fetchApi<{ status: string }>('/health'),

  // Admin
  admin: {
    health: () => fetchApi<HealthResponse>('/admin/health'),
    stats: () => fetchApi<StatsResponse>('/admin/stats'),
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
  },

  telemetry: {
    summary: (params?: { window_seconds?: number; rollup_limit?: number }) => {
      const search = new URLSearchParams();
      if (params?.window_seconds) search.set('window_seconds', String(params.window_seconds));
      if (params?.rollup_limit !== undefined)
        search.set('rollup_limit', String(params.rollup_limit));
      const suffix = search.toString();
      return fetchApi<TelemetrySummaryResponse>(`/telemetry/summary${suffix ? `?${suffix}` : ''}`);
    },
  },

  jobs: {
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
      if (params?.source_limit !== undefined)
        search.set('source_limit', String(params.source_limit));
      if (params?.candidate_limit !== undefined) {
        search.set('candidate_limit', String(params.candidate_limit));
      }
      if (params?.archive_exceptions !== undefined) {
        search.set('archive_exceptions', String(params.archive_exceptions));
      }
      const suffix = search.toString();
      return fetchApi<MaintenanceJobResponse>(
        `/jobs/reflection-dream${suffix ? `?${suffix}` : ''}`,
        {
          method: 'POST',
        }
      );
    },
  },

  // Backup Management (per-org backup settings and archives)
  backups: {
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
  },

  auth: {
    me: () => fetchApi<AuthMeResponse>('/auth/me'),
    logout: () =>
      fetchApi<void>('/auth/logout', {
        method: 'POST',
      }),
  },

  // Security (sessions, API keys, OAuth connections, password)
  security: {
    // Sessions
    sessions: {
      list: async () => ({
        sessions: await fetchApi<Session[]>('/users/me/sessions'),
      }),
      revoke: (sessionId: string) =>
        fetchApi<void>(`/users/me/sessions/${sessionId}`, {
          method: 'DELETE',
        }).then(() => ({ success: true })),
      revokeAll: () =>
        fetchApi<{ revoked: number }>('/users/me/sessions', {
          method: 'DELETE',
        }),
    },

    // API Keys
    apiKeys: {
      list: async () => {
        const response = await fetchApi<ApiKeysBackendResponse>('/auth/api-keys');
        return { api_keys: response.keys.map(normalizeApiKey) };
      },
      create: async (data: ApiKeyCreateRequest) => {
        const response = await fetchApi<ApiKeyCreateBackendResponse>('/auth/api-keys', {
          method: 'POST',
          body: JSON.stringify({
            name: data.name,
            scopes: data.scopes,
            project_ids: data.project_ids,
            memory_space_ids: data.memory_space_ids,
            expires_days: data.expires_in_days,
          }),
        });
        return {
          api_key: normalizeApiKey(response),
          key: response.api_key,
        };
      },
      revoke: (keyId: string) =>
        fetchApi<{ success: boolean }>(`/auth/api-keys/${keyId}/revoke`, {
          method: 'POST',
        }),
    },

    // OAuth Connections
    connections: {
      list: async () => {
        const connections = await fetchApi<OAuthConnectionBackendRecord[]>('/users/me/connections');
        return { connections: connections.map(normalizeOAuthConnection) };
      },
      remove: (connectionId: string) =>
        fetchApi<void>(`/users/me/connections/${connectionId}`, {
          method: 'DELETE',
        }).then(() => ({ success: true })),
    },

    // Password
    changePassword: (data: PasswordChangeRequest) =>
      fetchApi<void>('/users/me/password', {
        method: 'POST',
        body: JSON.stringify(data),
      }).then(() => ({ success: true })),
  },

  // User Preferences
  preferences: {
    get: () => fetchApi<PreferencesResponse>('/users/me/preferences'),
    update: (preferences: Partial<UserPreferences>) =>
      fetchApi<PreferencesResponse>('/users/me/preferences', {
        method: 'PATCH',
        body: JSON.stringify({ preferences }),
      }),
  },

  session: {
    bundle: (params?: {
      query?: string;
      task_limit?: number;
      memory_limit?: number;
      project_ids?: string[];
    }) => {
      const searchParams = new URLSearchParams();
      if (params?.query) searchParams.set('query', params.query);
      if (params?.task_limit) searchParams.set('task_limit', String(params.task_limit));
      if (params?.memory_limit !== undefined) {
        searchParams.set('memory_limit', String(params.memory_limit));
      }
      if (params?.project_ids?.length) {
        for (const projectId of params.project_ids) {
          searchParams.append('project_ids', projectId);
        }
      }
      const suffix = searchParams.toString();
      return fetchApi<SessionBundleResponse>(`/session/bundle${suffix ? `?${suffix}` : ''}`);
    },
  },

  orgs: {
    list: () => fetchApi<OrgListResponse>('/orgs'),
    get: (slug: string) => fetchApi<OrgGetResponse>(`/orgs/${encodeURIComponent(slug)}`),
    create: (data: OrgCreateRequest) =>
      fetchApi<OrgCreateResponse>('/orgs', {
        method: 'POST',
        body: JSON.stringify(data),
      }),
    update: (slug: string, data: OrgUpdateRequest) =>
      fetchApi<{ organization: { id: string; slug: string; name: string } }>(
        `/orgs/${encodeURIComponent(slug)}`,
        {
          method: 'PATCH',
          body: JSON.stringify(data),
        }
      ),
    delete: (slug: string) =>
      fetchApi<void>(`/orgs/${encodeURIComponent(slug)}`, {
        method: 'DELETE',
      }),
    switch: (slug: string) =>
      fetchApi<OrgSwitchResponse>(`/orgs/${encodeURIComponent(slug)}/switch`, {
        method: 'POST',
      }),
    members: {
      list: (slug: string) =>
        fetchApi<OrgMembersResponse>(`/orgs/${encodeURIComponent(slug)}/members`),
      add: (slug: string, userId: string, role: string) =>
        fetchApi<{ user_id: string; role: string }>(`/orgs/${encodeURIComponent(slug)}/members`, {
          method: 'POST',
          body: JSON.stringify({ user_id: userId, role }),
        }),
      updateRole: (slug: string, userId: string, role: string) =>
        fetchApi<{ user_id: string; role: string }>(
          `/orgs/${encodeURIComponent(slug)}/members/${userId}`,
          {
            method: 'PATCH',
            body: JSON.stringify({ role }),
          }
        ),
      remove: (slug: string, userId: string) =>
        fetchApi<{ success: boolean }>(`/orgs/${encodeURIComponent(slug)}/members/${userId}`, {
          method: 'DELETE',
        }),
    },
    invitations: {
      list: (slug: string) =>
        fetchApi<OrgInvitationsResponse>(`/orgs/${encodeURIComponent(slug)}/invitations`),
      create: (slug: string, data: OrgInvitationCreateRequest) =>
        fetchApi<OrgInvitationCreateResponse>(`/orgs/${encodeURIComponent(slug)}/invitations`, {
          method: 'POST',
          body: JSON.stringify(data),
        }),
      delete: (slug: string, invitationId: string) =>
        fetchApi<{ success: boolean }>(
          `/orgs/${encodeURIComponent(slug)}/invitations/${invitationId}`,
          {
            method: 'DELETE',
          }
        ),
    },
  },

  // Tasks
  tasks: {
    list: (params?: { project?: string; project_ids?: string[]; status?: TaskStatus }) =>
      fetchApi<TaskListResponse>('/search/explore', {
        method: 'POST',
        body: JSON.stringify({
          mode: 'list',
          types: ['task'],
          project: params?.project,
          project_ids: params?.project_ids,
          status: params?.status,
          limit: 200,
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
  },

  // Projects (via explore endpoint)
  projects: {
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
      list: (projectId: string) =>
        fetchApi<ProjectMembersResponse>(`/projects/${projectId}/members`),
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
  },

  // Epics - feature grouping for tasks
  epics: {
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
  },

  // Sources (documentation crawling) - uses dedicated /sources endpoints
  sources: {
    list: () =>
      fetchApi<{ sources: CrawlSource[]; total: number }>('/sources').then(data => ({
        mode: 'list',
        entities: data.sources.map(s => ({
          id: s.id,
          type: 'source',
          name: s.name,
          description: s.description || '',
          created_at: s.created_at,
          updated_at: s.last_crawled_at || s.created_at,
          metadata: {
            url: s.url,
            source_type: s.source_type,
            crawl_status: s.crawl_status,
            document_count: s.document_count,
            last_crawled: s.last_crawled_at ?? undefined,
            crawl_depth: s.crawl_depth,
            crawl_patterns: s.include_patterns,
            exclude_patterns: s.exclude_patterns,
          },
        })),
        total: data.total,
        filters: {},
      })),

    get: (id: string) => fetchApi<CrawlSource>(`/sources/${id}`),

    create: (source: {
      name: string;
      url: string;
      description?: string;
      source_type?: SourceType;
      crawl_depth?: number;
      crawl_patterns?: string[];
      exclude_patterns?: string[];
    }) =>
      fetchApi<CrawlSource>('/sources', {
        method: 'POST',
        body: JSON.stringify({
          name: source.name,
          url: source.url,
          description: source.description || null,
          source_type: source.source_type || 'website',
          crawl_depth: source.crawl_depth || 2,
          include_patterns: source.crawl_patterns || [],
          exclude_patterns: source.exclude_patterns || [],
        }),
      }),

    delete: (id: string) =>
      fetchApi<void>(`/sources/${id}`, {
        method: 'DELETE',
      }),

    update: (
      id: string,
      updates: {
        name?: string;
        description?: string;
        crawl_depth?: number;
        include_patterns?: string[];
        exclude_patterns?: string[];
      }
    ) =>
      fetchApi<CrawlSource>(`/sources/${id}`, {
        method: 'PATCH',
        body: JSON.stringify(updates),
      }),

    // Trigger a crawl for a source
    crawl: (id: string, options?: { maxPages?: number; maxDepth?: number }) =>
      fetchApi<{ source_id: string; status: string; message: string }>(`/sources/${id}/ingest`, {
        method: 'POST',
        body: JSON.stringify({
          max_pages: options?.maxPages ?? 50,
          max_depth: options?.maxDepth ?? 3,
          generate_embeddings: true,
        }),
      }),

    // Get crawl status
    status: (id: string) =>
      fetchApi<{
        source_id: string;
        running: boolean;
        documents_crawled?: number;
        errors?: number;
      }>(`/sources/${id}/status`),

    // Preview URL metadata for better source naming
    preview: (url: string) =>
      fetchApi<{ url: string; title: string | null; suggested_name: string; domain: string }>(
        `/sources/preview?url=${encodeURIComponent(url)}`
      ),
  },

  // RAG (Documentation Search)
  rag: {
    // Vector similarity search on document chunks
    search: (params: RAGSearchParams) =>
      fetchApi<RAGSearchResponse>('/rag/search', {
        method: 'POST',
        body: JSON.stringify(params),
      }),

    // Hybrid search (vector + full-text)
    hybridSearch: (params: RAGSearchParams) =>
      fetchApi<RAGSearchResponse>('/rag/hybrid-search', {
        method: 'POST',
        body: JSON.stringify(params),
      }),

    // Code example search
    codeExamples: (params: CodeExampleParams) =>
      fetchApi<CodeExampleResponse>('/rag/code-examples', {
        method: 'POST',
        body: JSON.stringify(params),
      }),

    // Get full page content by ID
    getPage: (documentId: string) => fetchApi<FullPageResponse>(`/rag/pages/${documentId}`),

    // Update document title and/or content
    updateDocument: (documentId: string, updates: { title?: string; content?: string }) =>
      fetchApi<FullPageResponse>(`/rag/pages/${documentId}`, {
        method: 'PATCH',
        body: JSON.stringify(updates),
      }),

    // Get related entities for a document
    getDocumentEntities: (documentId: string) =>
      fetchApi<DocumentRelatedEntitiesResponse>(`/rag/pages/${documentId}/entities`),

    // Get full page content by URL
    getPageByUrl: (url: string) =>
      fetchApi<FullPageResponse>(`/rag/pages/by-url?url=${encodeURIComponent(url)}`),

    // List pages for a source
    listPages: (
      sourceId: string,
      params?: { limit?: number; offset?: number; has_code?: boolean; is_index?: boolean }
    ) => {
      const searchParams = new URLSearchParams();
      if (params?.limit) searchParams.set('limit', params.limit.toString());
      if (params?.offset) searchParams.set('offset', params.offset.toString());
      if (params?.has_code !== undefined) searchParams.set('has_code', params.has_code.toString());
      if (params?.is_index !== undefined) searchParams.set('is_index', params.is_index.toString());
      const query = searchParams.toString();
      return fetchApi<{
        source_id: string;
        source_name: string;
        pages: Array<{
          id: string;
          url: string;
          title: string;
          word_count: number;
          has_code: boolean;
          is_index: boolean;
        }>;
        total: number;
        has_more: boolean;
      }>(`/rag/sources/${sourceId}/pages${query ? `?${query}` : ''}`);
    },
  },

  // Metrics
  metrics: {
    // Get org-level metrics
    org: () => fetchApi<OrgMetricsResponse>('/metrics'),

    // Get lean project summaries
    projectsSummary: () => fetchApi<ProjectSummariesResponse>('/metrics/projects-summary'),

    // Get project-level metrics
    project: (projectId: string) =>
      fetchApi<ProjectMetricsResponse>(`/metrics/projects/${projectId}`),
  },

  // Setup wizard (no auth required - runs before first user exists)
  setup: {
    status: (validateKeys?: boolean) => {
      const query = validateKeys ? '?validate_keys=true' : '';
      return fetchApi<SetupStatus>(`/setup/status${query}`);
    },

    validateKeys: () => fetchApi<ApiKeyValidation>('/setup/validate-keys'),

    integration: () => fetchApi<IntegrationResponse>('/setup/integration'),
  },

  // System settings (no auth required during setup mode)
  settings: {
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
  },
};
