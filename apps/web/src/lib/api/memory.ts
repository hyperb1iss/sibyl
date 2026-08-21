import { fetchApi } from './transport';

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
  target_lifecycle_state: string;
  target_lifecycle_flags: string[];
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
  | 'handbook'
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

export const rawCapturesApi = {
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
};

export const memoryApi = {
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
    fetchApi<SourceImportStatusResponse>(`/memory/source-imports/${encodeURIComponent(importId)}`),

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
};

export const sourceImportsApi = {
  adapters: () => fetchApi<SourceAdapterListResponse>('/ingestion/import-adapters'),
  start: (request: SourceImportStartRequest) =>
    fetchApi<SourceImportStatusResponse>('/ingestion/imports', {
      method: 'POST',
      body: JSON.stringify(request),
    }),
  get: (importId: string) =>
    fetchApi<SourceImportStatusResponse>(`/ingestion/imports/${encodeURIComponent(importId)}`),
  resume: (importId: string, request: SourceImportResumeRequest = {}) =>
    fetchApi<SourceImportStatusResponse>(
      `/ingestion/imports/${encodeURIComponent(importId)}/resume`,
      {
        method: 'POST',
        body: JSON.stringify(request),
      }
    ),
  cancel: (importId: string) =>
    fetchApi<SourceImportStatusResponse>(
      `/ingestion/imports/${encodeURIComponent(importId)}/cancel`,
      { method: 'POST' }
    ),
};

export const synthesisApi = {
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
};

export const sessionApi = {
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
};
