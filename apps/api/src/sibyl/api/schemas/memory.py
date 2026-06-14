"""Memory: raw memory, audit events, memory spaces, source inspection,
corrections, reflection promotion/autonomy, and sharing models."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from .common import (
    MemoryCorrectionActionLiteral,
    MemoryScopeLiteral,
    MemorySpaceStateLiteral,
)


class RawMemoryRememberRequest(BaseModel):
    """Request to store verbatim memory before extraction."""

    title: str = Field(default="", max_length=300, description="Optional human title")
    raw_content: str = Field(..., min_length=1, max_length=500000, description="Verbatim memory")
    source_id: str | None = Field(default=None, description="Stable source/provenance ID")
    memory_scope: MemoryScopeLiteral = Field(default="private", description="Retrieval scope")
    scope_key: str | None = Field(default=None, description="Project/team/shared scope key")
    diary: bool = Field(default=False, description="Store as a private agent diary entry")
    agent_id: str | None = Field(default=None, description="Agent identity for diary entries")
    project_id: str | None = Field(default=None, description="Project associated with this diary")
    tags: list[str] = Field(default_factory=list, description="Searchable tags")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Auxiliary metadata")
    provenance: dict[str, Any] = Field(default_factory=dict, description="Source provenance")
    capture_surface: str = Field(default="api", description="Capture surface")


class RawMemoryRecallRequest(BaseModel):
    """Request to recall raw memories through a scoped search."""

    query: str = Field(..., min_length=1, description="Full-text recall query")
    memory_scope: MemoryScopeLiteral = Field(default="private", description="Retrieval scope")
    scope_key: str | None = Field(default=None, description="Project/team/shared scope key")
    diary: bool = Field(default=False, description="Recall private agent diary entries")
    agent_id: str | None = Field(default=None, description="Agent identity to recall")
    project_id: str | None = Field(default=None, description="Project diary filter")
    participants: list[str] = Field(
        default_factory=list,
        description="Participant identifiers to filter imported records",
    )
    labels: list[str] = Field(
        default_factory=list,
        description="Adapter labels/tags to filter imported records",
    )
    thread_id: str | None = Field(default=None, description="Imported thread identifier")
    occurred_after: datetime | None = Field(
        default=None,
        description="Earliest source occurrence timestamp",
    )
    occurred_before: datetime | None = Field(
        default=None,
        description="Latest source occurrence timestamp",
    )
    as_of: datetime | None = Field(
        default=None,
        description="Point-in-time validity timestamp for recalled memories",
    )
    limit: int = Field(default=10, ge=1, le=50, description="Maximum memories to return")


class RawMemoryResponse(BaseModel):
    """Raw memory response."""

    id: str = Field(..., description="Raw memory ID")
    organization_id: str = Field(..., description="Organization ID")
    source_id: str = Field(..., description="Source/provenance ID")
    principal_id: str = Field(..., description="Principal who captured or owns the memory")
    memory_scope: MemoryScopeLiteral = Field(..., description="Retrieval scope")
    scope_key: str | None = Field(default=None, description="Project/team/shared scope key")
    title: str = Field(default="", description="Human title")
    raw_content: str = Field(..., description="Verbatim memory")
    tags: list[str] = Field(default_factory=list, description="Searchable tags")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Auxiliary metadata")
    provenance: dict[str, Any] = Field(default_factory=dict, description="Source provenance")
    capture_surface: str | None = Field(default=None, description="Capture surface")
    captured_at: datetime | None = Field(default=None, description="Capture timestamp")
    created_at: datetime | None = Field(default=None, description="Creation timestamp")
    score: float = Field(default=0.0, description="Recall score")
    snippet: str | None = Field(default=None, description="Highlighted recall snippet")
    policy_reason: str | None = Field(default=None, description="Memory policy decision reason")


class RawMemoryRecallResponse(BaseModel):
    """Scoped raw memory recall response."""

    query: str = Field(..., description="Recall query")
    memories: list[RawMemoryResponse]
    limit: int
    policy_reason: str | None = Field(default=None, description="Memory policy decision reason")
    source_degraded: bool = Field(default=False, description="Whether recall sources degraded")
    source_failure_count: int = Field(default=0, description="Failed recall source count")
    source_failures: list[dict[str, str]] = Field(
        default_factory=list,
        description="Recall source failures",
    )


class MemoryAuditEventResponse(BaseModel):
    """Inspectable memory audit event."""

    id: str = Field(..., description="Audit event UUID")
    organization_id: str | None = Field(default=None, description="Organization ID")
    user_id: str | None = Field(default=None, description="Actor user ID")
    action: str = Field(..., description="Audit action")
    memory_scope: str | None = Field(default=None, description="Memory scope")
    scope_key: str | None = Field(default=None, description="Scope key")
    project_id: str | None = Field(default=None, description="Project graph ID")
    source_surface: str | None = Field(default=None, description="Source surface")
    source_ids: list[str] = Field(default_factory=list, description="Source IDs")
    source_ids_truncated: int | None = Field(default=None, description="Hidden source ID count")
    derived_ids: list[str] = Field(default_factory=list, description="Derived IDs")
    derived_ids_truncated: int | None = Field(default=None, description="Hidden derived ID count")
    policy_allowed: bool | None = Field(default=None, description="Policy allow/deny state")
    policy_reason: str | None = Field(default=None, description="Policy or outcome reason")
    details: dict[str, Any] = Field(default_factory=dict, description="Bounded event details")
    created_at: datetime | None = Field(default=None, description="Audit event timestamp")


class MemoryAuditListResponse(BaseModel):
    """Memory audit list response."""

    events: list[MemoryAuditEventResponse]
    limit: int


class MemorySpaceCreateRequest(BaseModel):
    """Request to create a persisted memory space."""

    memory_scope: MemoryScopeLiteral = Field(default="private", description="Memory scope")
    scope_key: str | None = Field(default=None, max_length=500, description="Scope key")
    name: str = Field(..., min_length=1, max_length=200, description="Human name")
    description: str | None = Field(default=None, max_length=2000, description="Description")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Auxiliary metadata")


class MemorySpaceUpdateRequest(BaseModel):
    """Request to update memory-space metadata."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    state: MemorySpaceStateLiteral | None = None
    metadata: dict[str, Any] | None = None


class MemorySpaceMemberCreateRequest(BaseModel):
    """Request to grant a principal access to a memory space."""

    principal_type: str = Field(..., min_length=1, max_length=50)
    principal_id: str = Field(..., min_length=1, max_length=500)
    role: str = Field(default="reader", min_length=1, max_length=50)
    permissions: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None


class MemorySpaceAccessPreviewRequest(BaseModel):
    """Request to preview effective memory-space recall."""

    target_principal_type: Literal["user", "agent", "delegated"] = "agent"
    target_principal_id: str = Field(..., min_length=1, max_length=500)
    additional_space_ids: list[str] = Field(default_factory=list, max_length=25)
    limit: int = Field(default=50, ge=1, le=200)


class MemorySpaceMemberResponse(BaseModel):
    """Persisted memory-space membership."""

    id: str
    organization_id: str
    space_id: str
    principal_type: str
    principal_id: str
    role: str
    permissions: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None
    created_by_user_id: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class MemorySpaceResponse(BaseModel):
    """Persisted memory space with optional membership details."""

    id: str
    organization_id: str
    memory_scope: MemoryScopeLiteral
    scope_key: str | None = None
    name: str
    description: str | None = None
    state: MemorySpaceStateLiteral
    disabled_reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict, description="Auxiliary metadata")
    created_by_user_id: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    members: list[MemorySpaceMemberResponse] = Field(default_factory=list)


class MemorySpaceListResponse(BaseModel):
    """Memory-space list response."""

    spaces: list[MemorySpaceResponse]


class MemorySpaceAccessPreviewResponse(BaseModel):
    """Non-mutating effective recall preview for memory spaces."""

    allowed: bool
    reason: str
    target_principal_type: str
    target_principal_id: str
    memory_space_ids: list[str] = Field(default_factory=list)
    visible_source_ids: list[str] = Field(default_factory=list)
    denied_source_ids: list[str] = Field(default_factory=list)
    missing_source_ids: list[str] = Field(default_factory=list)
    redacted_count: int = 0
    hidden_but_relevant_count: int = 0
    policy_reasons: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict, description="Auxiliary metadata")


class MemoryDerivedRecordResponse(BaseModel):
    """Derived memory record summarized from audit receipts."""

    id: str = Field(..., description="Derived record ID")
    record_type: str = Field(..., description="Derived record type")
    source_action: str = Field(..., description="Audit action that exposed the record")


class MemorySourceInspectResponse(BaseModel):
    """Owner/admin memory source inspection response."""

    id: str = Field(..., description="Raw memory record ID")
    organization_id: str = Field(..., description="Organization ID")
    source_id: str = Field(..., description="Source/provenance ID")
    principal_id: str = Field(..., description="Principal who captured or owns the memory")
    agent_id: str | None = Field(default=None, description="Agent identity for diary memory")
    project_id: str | None = Field(default=None, description="Associated project ID")
    memory_scope: MemoryScopeLiteral = Field(..., description="Retrieval scope")
    scope_key: str | None = Field(default=None, description="Project/team/shared scope key")
    review_state: str = Field(..., description="Review queue state")
    visibility: dict[str, Any] = Field(
        default_factory=dict,
        description="Visibility summary for the requesting actor",
    )
    lifecycle: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured lifecycle state for this memory",
    )
    reflection_findings: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Structured reflection findings that affected this memory",
    )
    claim_records: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Source-grounded claim records derived from this memory",
    )
    correction_history: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Correction and lifecycle events known for this source",
    )
    promotion_state: dict[str, Any] = Field(
        default_factory=dict,
        description="Promotion state and related promotion receipts",
    )
    share_state: dict[str, Any] = Field(
        default_factory=dict,
        description="Share preview or share state and related receipts",
    )
    entity_type: str = Field(..., description="Memory entity type")
    title: str = Field(default="", description="Human title")
    raw_content: str | None = Field(default=None, description="Verbatim memory when readable")
    content_redacted: bool = Field(..., description="Whether raw content was redacted")
    raw_content_length: int = Field(..., description="Length of the raw memory content")
    tags: list[str] = Field(default_factory=list, description="Searchable tags")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Auxiliary metadata")
    provenance: dict[str, Any] = Field(default_factory=dict, description="Source provenance")
    capture_surface: str | None = Field(default=None, description="Capture surface")
    captured_at: datetime | None = Field(default=None, description="Capture timestamp")
    created_at: datetime | None = Field(default=None, description="Creation timestamp")
    freshness_timestamps: dict[str, datetime | None] = Field(
        default_factory=dict,
        description="Named source freshness timestamps",
    )
    transform_versions: dict[str, Any] = Field(
        default_factory=dict,
        description="Transform, adapter, and extraction version metadata",
    )
    policy_allowed: bool = Field(..., description="Whether content was readable by policy")
    policy_reason: str = Field(..., description="Policy reason for content visibility")
    policy_metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Inspectable policy metadata",
    )
    derived_ids: list[str] = Field(default_factory=list, description="Derived record IDs")
    derived_types: list[str] = Field(default_factory=list, description="Derived record types")
    derived_records: list[MemoryDerivedRecordResponse] = Field(
        default_factory=list,
        description="Derived records summarized from audit receipts",
    )
    recent_audit_events: list[MemoryAuditEventResponse] = Field(
        default_factory=list,
        description="Recent memory audit receipts mentioning this source",
    )
    audit_event_count: int = Field(..., description="Number of included audit receipts")
    available_actions: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Safe next actions and whether a preview step is required",
    )


class MemoryCorrectionRequest(BaseModel):
    """Request to preview or apply a memory correction/lifecycle change."""

    action: MemoryCorrectionActionLiteral
    reason: str | None = Field(default=None, max_length=2000)
    replacement_source_id: str | None = Field(default=None, max_length=500)
    duplicate_of_source_id: str | None = Field(default=None, max_length=500)
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Audit-only metadata recorded with the correction receipt",
    )


class MemoryCorrectionResponse(BaseModel):
    """Memory correction preview/apply response."""

    allowed: bool
    applied: bool = False
    source_id: str
    action: str
    reason: str
    target_review_state: str
    updated_review_state: str | None = None
    lifecycle: dict[str, Any] = Field(default_factory=dict)
    reflection_finding: dict[str, Any] | None = None
    affected_source_ids: list[str] = Field(default_factory=list)
    affected_derived_ids: list[str] = Field(default_factory=list)
    reversible: bool
    recall_impact: dict[str, Any] = Field(default_factory=dict)
    synthesis_impact: dict[str, Any] = Field(default_factory=dict)
    audit_action: str
    policy_reasons: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReflectionPromotionRequest(BaseModel):
    """Request to promote a reviewed reflection candidate into native memory."""

    candidate_id: str = Field(..., description="Raw review candidate capture ID")
    promote_to_scope: MemoryScopeLiteral | None = Field(
        default=None,
        description="Explicit target memory scope for promotion",
    )
    promote_to_scope_key: str | None = Field(
        default=None,
        description="Project/delegation key for scoped promotion targets",
    )
    domain: str | None = Field(default=None, description="Optional category/domain override")
    project: str | None = Field(default=None, description="Project relation for promoted memory")
    related_to: list[str] = Field(
        default_factory=list,
        description="Additional graph entity IDs to relate to the promoted memory",
    )


class ReflectionAutonomyRequest(ReflectionPromotionRequest):
    """Request to let the autonomy engine review a reflection candidate."""

    dry_run: bool = Field(
        default=False,
        description="Return the automatic decision without applying a safe promotion",
    )
    confidence_threshold: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Optional confidence threshold override for rollout gates",
    )


class ReflectionPromotionResponse(BaseModel):
    """Promotion outcome with stable policy deny reasons."""

    success: bool
    candidate_id: str
    promoted_id: str | None = None
    reason: str
    review_state: str
    memory_scope: MemoryScopeLiteral | None = None
    scope_key: str | None = None
    raw_source_ids: list[str] = Field(default_factory=list)
    policy_reasons: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryScopeInputResponse(BaseModel):
    """Source memory scope included in a preview decision."""

    id: str
    memory_scope: MemoryScopeLiteral
    scope_key: str | None = None


class ReflectionPromotionPreviewResponse(BaseModel):
    """Dry-run promotion outcome with policy and source metadata."""

    allowed: bool
    candidate_id: str
    reason: str
    review_state: str
    promote_to_scope: MemoryScopeLiteral | None = None
    promote_to_scope_key: str | None = None
    raw_source_ids: list[str] = Field(default_factory=list)
    policy_reasons: list[str] = Field(default_factory=list)
    input_scopes: list[MemoryScopeInputResponse] = Field(default_factory=list)
    source_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReflectionAutonomyResponse(BaseModel):
    """Automatic review decision and optional promotion receipt."""

    outcome: Literal["auto_promote", "exception", "skip"]
    recommended_action: Literal["promote", "route_to_review", "skip"]
    applied: bool = False
    dry_run: bool = False
    candidate_id: str
    reason: str
    review_state: str
    promote_to_scope: MemoryScopeLiteral | None = None
    promote_to_scope_key: str | None = None
    promoted_id: str | None = None
    raw_source_ids: list[str] = Field(default_factory=list)
    policy_reasons: list[str] = Field(default_factory=list)
    exception_reasons: list[str] = Field(default_factory=list)
    confidence: float | None = None
    confidence_threshold: float
    preview: ReflectionPromotionPreviewResponse
    promotion: ReflectionPromotionResponse | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReflectionReviewDrainRequest(BaseModel):
    """Bulk automatic review drain for pending reflection candidates."""

    dry_run: bool = Field(
        default=True,
        description="Evaluate the pending queue without applying promotions or archives",
    )
    limit: int = Field(default=50, ge=1, le=200, description="Maximum candidates to process")
    promote_to_scope: MemoryScopeLiteral | None = Field(
        default=None,
        description="Explicit target memory scope for promotion",
    )
    promote_to_scope_key: str | None = Field(
        default=None,
        description="Project/delegation key for scoped promotion targets",
    )
    domain: str | None = Field(default=None, description="Optional category/domain override")
    project: str | None = Field(default=None, description="Project relation for promoted memory")
    related_to: list[str] = Field(
        default_factory=list,
        description="Additional graph entity IDs to relate to promoted memories",
    )
    confidence_threshold: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Optional confidence threshold override for rollout gates",
    )
    archive_exceptions: bool = Field(
        default=False,
        description="Archive terminal exception candidates when their reasons are allowlisted",
    )
    archive_exception_reasons: list[str] = Field(
        default_factory=lambda: ["duplicate_candidate", "stale_candidate"],
        description="Exception reasons eligible for automatic archive when applying",
    )


class ReflectionReviewDrainItem(BaseModel):
    """Per-candidate result from a bulk reflection review drain."""

    candidate_id: str
    outcome: Literal["auto_promote", "exception", "skip", "error"]
    recommended_action: Literal["promote", "route_to_review", "skip", "error"]
    applied: bool = False
    archived: bool = False
    dry_run: bool = True
    reason: str
    review_state: str
    promoted_id: str | None = None
    raw_source_ids: list[str] = Field(default_factory=list)
    policy_reasons: list[str] = Field(default_factory=list)
    exception_reasons: list[str] = Field(default_factory=list)
    confidence: float | None = None
    error: str | None = None


class ReflectionReviewDrainResponse(BaseModel):
    """Bulk reflection review drain summary."""

    dry_run: bool
    limit: int
    scanned_count: int
    auto_promote_count: int = 0
    applied_count: int = 0
    archived_count: int = 0
    exception_count: int = 0
    skip_count: int = 0
    failed_count: int = 0
    results: list[ReflectionReviewDrainItem] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemorySharePreviewRequest(BaseModel):
    """Request to preview memory sharing without enabling a write."""

    source_ids: list[str] = Field(..., min_length=1, description="Raw memory IDs to preview")
    target_scope: MemoryScopeLiteral = Field(..., description="Intended share target scope")
    target_scope_key: str | None = Field(default=None, description="Target project/team/shared key")
    recipient_organization_id: str | None = Field(
        default=None,
        description="Optional recipient organization for future cross-org sharing",
    )
    project_id: str | None = Field(default=None, description="Project associated with the preview")


class MemorySharePreviewResponse(BaseModel):
    """Dry-run share outcome with redaction and source metadata."""

    allowed: bool
    reason: str
    target_scope: MemoryScopeLiteral | None = None
    target_scope_key: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    visible_source_ids: list[str] = Field(default_factory=list)
    denied_source_ids: list[str] = Field(default_factory=list)
    missing_source_ids: list[str] = Field(default_factory=list)
    redacted_count: int = 0
    hidden_but_relevant_count: int = 0
    policy_reasons: list[str] = Field(default_factory=list)
    input_scopes: list[MemoryScopeInputResponse] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
