"""Pydantic schemas for API request/response models.

These map directly to TypeScript interfaces via OpenAPI generation.
"""

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from sibyl_core.models.context import ContextFacet, ContextIntent, ContextLayer
from sibyl_core.models.entities import EntityType, RelationshipType

# =============================================================================
# Entity Schemas
# =============================================================================


class EntityBase(BaseModel):
    """Base fields for all entities."""

    name: str = Field(..., max_length=200, description="Entity name/title")
    description: str = Field(default="", description="Short description")
    content: str = Field(default="", max_length=50000, description="Full content")
    category: str | None = Field(default=None, description="Category for organization")
    languages: list[str] = Field(default_factory=list, description="Programming languages")
    tags: list[str] = Field(default_factory=list, description="Searchable tags")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")


class EntityCreate(EntityBase):
    """Schema for creating a new entity."""

    entity_type: EntityType = Field(default=EntityType.EPISODE, description="Type of entity")
    related_to: list[str] | None = Field(
        default=None,
        description="Entity IDs to explicitly connect with RELATED_TO edges",
    )


class EntityUpdate(BaseModel):
    """Schema for updating an entity (all fields optional)."""

    name: str | None = Field(default=None, max_length=200)
    description: str | None = None
    content: str | None = Field(default=None, max_length=50000)
    category: str | None = None
    languages: list[str] | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None


class RelatedEntitySummary(BaseModel):
    """Summary of a related entity for embedding in responses."""

    id: str = Field(..., description="Entity ID")
    name: str = Field(..., description="Entity name")
    entity_type: str = Field(..., description="Entity type")
    relationship: str = Field(..., description="Relationship type connecting to this entity")
    direction: Literal["outgoing", "incoming"] = Field(..., description="Relationship direction")


class EntityResponse(EntityBase):
    """Full entity response with all fields."""

    id: str = Field(..., description="Unique entity ID")
    entity_type: EntityType = Field(..., description="Type of entity")
    source_file: str | None = Field(default=None, description="Source file path")
    created_at: datetime | None = Field(default=None, description="Creation timestamp")
    updated_at: datetime | None = Field(default=None, description="Last update timestamp")
    related: list[RelatedEntitySummary] | None = Field(
        default=None, description="Related entities (when requested via related_limit)"
    )

    model_config = {"from_attributes": True}


class EntityListResponse(BaseModel):
    """Paginated list of entities."""

    entities: list[EntityResponse]
    total: int
    page: int
    page_size: int
    has_more: bool


class RawCaptureSummary(BaseModel):
    """Summary view of a raw archived capture."""

    id: str = Field(..., description="Raw capture ID")
    entity_id: str | None = Field(default=None, description="Created graph entity ID")
    title: str = Field(..., description="Captured title")
    entity_type: str = Field(..., description="Captured entity type")
    tags: list[str] = Field(default_factory=list, description="Captured tags")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Original request metadata")
    capture_surface: str | None = Field(default=None, description="Where the capture originated")
    review_state: Literal["pending", "deferred", "archived", "promoted"] = Field(
        default="pending",
        description="Review queue state",
    )
    created_by_user_id: str | None = Field(
        default=None, description="User who initiated the capture"
    )
    created_at: datetime = Field(..., description="Archive creation timestamp")


class RawCaptureResponse(RawCaptureSummary):
    """Detailed raw capture response."""

    raw_content: str = Field(..., description="Verbatim captured content")


class RawCaptureListResponse(BaseModel):
    """Paginated raw capture list response."""

    captures: list[RawCaptureSummary]
    limit: int = Field(default=50, description="Results per page")
    offset: int = Field(default=0, description="Current offset")
    has_more: bool = Field(default=False, description="Whether more results exist")


class RawCaptureReviewUpdate(BaseModel):
    """Review-state update for a raw capture."""

    review_state: Literal["pending", "deferred", "archived", "promoted"] = Field(
        ...,
        description="Next review queue state",
    )


MemoryScopeLiteral = Literal[
    "private",
    "delegated",
    "project",
    "team",
    "organization",
    "shared",
    "public",
]
MemoryCorrectionActionLiteral = Literal[
    "delete",
    "hide",
    "mark_duplicate",
    "mark_sensitive",
    "mark_stale",
    "mark_wrong",
    "redact",
    "restore",
    "supersede",
]


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
    policy_reason: str | None = Field(default=None, description="Memory policy decision reason")


class RawMemoryRecallResponse(BaseModel):
    """Scoped raw memory recall response."""

    query: str = Field(..., description="Recall query")
    memories: list[RawMemoryResponse]
    limit: int
    policy_reason: str | None = Field(default=None, description="Memory policy decision reason")


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


MemorySpaceStateLiteral = Literal["active", "disabled"]


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


# =============================================================================
# Session Bundle Schemas
# =============================================================================


class SessionBundleContext(BaseModel):
    """Context metadata for a packaged wake-up bundle."""

    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    org_slug: str | None = Field(default=None, description="Active organization slug")
    project_ids: list[str] = Field(default_factory=list, description="Scoped project IDs")
    scope: Literal["all_projects", "project_selection"] = Field(
        default="all_projects",
        description="Whether the bundle is scoped to all projects or selected ones",
    )


class SessionTaskSummary(BaseModel):
    """Compact task representation for session bundles."""

    id: str = Field(..., description="Task ID")
    name: str = Field(..., description="Task title")
    status: str = Field(default="", description="Task status")
    priority: str = Field(default="", description="Task priority")
    feature: str | None = Field(default=None, description="Feature area")
    branch_name: str | None = Field(default=None, description="Attached branch name")


class SessionMemorySummary(BaseModel):
    """Compact relevant-memory representation for session bundles."""

    id: str = Field(..., description="Entity or document ID")
    name: str = Field(..., description="Entity title")
    entity_type: str | None = Field(default=None, description="Entity type")
    source: str | None = Field(default=None, description="Source document or path")
    preview: str = Field(default="", description="Short content preview")
    document_id: str | None = Field(default=None, description="Backing document ID")
    memory_scope: str | None = Field(default=None, description="Memory visibility scope")
    scope_key: str | None = Field(default=None, description="Project/team/shared scope key")


class SessionBundleResponse(BaseModel):
    """Packaged wake-up context for a session."""

    context: SessionBundleContext
    query: str | None = Field(default=None, description="Focus query used to gather memory")
    tasks: list[SessionTaskSummary] = Field(default_factory=list)
    relevant_entities: list[SessionMemorySummary] = Field(default_factory=list)
    remember_next: str = Field(default="", description="Single actionable wake-up suggestion")


# =============================================================================
# Search Schemas
# =============================================================================


class SearchRequest(BaseModel):
    """Unified search request - searches both knowledge graph AND documentation.

    By default, searches both stores and merges results by relevance.
    Use filters to narrow scope.
    """

    query: str = Field(..., min_length=1, description="Natural language search query")
    types: list[str] | None = Field(
        default=None,
        description="Filter by entity types. Options: pattern, rule, template, topic, "
        "episode, task, project, document. 'document' searches crawled docs.",
    )
    language: str | None = Field(default=None, description="Filter by programming language")
    category: str | None = Field(default=None, description="Filter by category")
    status: str | None = Field(default=None, description="Filter tasks by status")
    project: str | None = Field(default=None, description="Filter tasks by project ID")
    source: str | None = Field(default=None, description="Alias for source_name")
    source_id: str | None = Field(default=None, description="Filter documents by source ID")
    source_name: str | None = Field(default=None, description="Filter documents by source name")
    assignee: str | None = Field(default=None, description="Filter tasks by assignee name")
    since: str | None = Field(
        default=None, description="Filter by creation date (ISO: 2024-03-15 or relative: 7d, 2w)"
    )
    limit: int = Field(default=10, ge=1, le=50, description="Maximum results")
    offset: int = Field(default=0, ge=0, description="Offset for pagination")
    include_content: bool = Field(default=True, description="Include full content in results")
    include_documents: bool = Field(
        default=True, description="Include crawled documentation in search"
    )
    include_graph: bool = Field(default=True, description="Include knowledge graph entities")
    use_enhanced: bool = Field(default=True, description="Use enhanced retrieval with reranking")
    boost_recent: bool = Field(default=True, description="Boost recent results in ranking")


class SearchResult(BaseModel):
    """Single search result - unified across graph entities and documents."""

    id: str = Field(..., description="Entity or chunk ID")
    type: str = Field(..., description="Entity type (pattern, rule, episode, etc.) or 'document'")
    name: str = Field(..., description="Entity name or document title")
    content: str = Field(..., description="Matched content")
    score: float = Field(..., description="Relevance score (0-1)")
    source: str | None = Field(default=None, description="Source file path or documentation source")
    url: str | None = Field(default=None, description="URL for documents")
    result_origin: Literal["graph", "document"] = Field(
        default="graph", description="Whether result is from knowledge graph or documents"
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    """Unified search results response."""

    results: list[SearchResult]
    total: int
    query: str
    filters: dict[str, Any]
    graph_count: int = Field(default=0, description="Number of results from knowledge graph")
    document_count: int = Field(default=0, description="Number of results from documents")
    limit: int = Field(default=10, description="Results per page")
    offset: int = Field(default=0, description="Current offset")
    has_more: bool = Field(default=False, description="Whether more results exist")


# =============================================================================
# Context Pack Schemas
# =============================================================================


class ContextPackRequest(BaseModel):
    """Request for compiling a structured agent context pack."""

    goal: str = Field(..., min_length=1, description="Agent goal or user task")
    intent: ContextIntent = Field(default=ContextIntent.BUILD, description="How the agent will act")
    layer: ContextLayer = Field(
        default=ContextLayer.RECALL,
        description="Context depth: wake, recall, or deep_search",
    )
    domain: str | None = Field(default=None, description="Domain or category to bias retrieval")
    project: str | None = Field(default=None, description="Project ID to scope context")
    agent_id: str | None = Field(default=None, description="Agent diary identity to include")
    limit: int = Field(default=24, ge=1, le=50, description="Maximum total context items")
    include_related: bool = Field(default=True, description="Include one-hop related graph context")
    related_limit: int = Field(default=3, ge=0, le=5, description="Related items per context item")


class ContextPackRelatedItem(BaseModel):
    """One-hop graph neighbor for a selected memory."""

    id: str
    type: str
    name: str
    relationship: str
    direction: str
    distance: int = 1


class ContextPackItemQuality(BaseModel):
    """Source and freshness metadata for a selected memory."""

    origin: str | None = None
    source: str | None = None
    url: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    valid_at: str | None = None
    project_id: str | None = None


class ContextPackItem(BaseModel):
    """Single selected memory inside a context pack."""

    id: str
    type: str
    name: str
    content: str
    score: float
    facet: ContextFacet
    reason: str
    source: str | None = None
    quality: ContextPackItemQuality = Field(default_factory=ContextPackItemQuality)
    metadata: dict[str, Any] = Field(default_factory=dict)
    related: list[ContextPackRelatedItem] = Field(default_factory=list)


class ContextPackSection(BaseModel):
    """Grouped memories for one context facet."""

    facet: ContextFacet
    title: str
    items: list[ContextPackItem] = Field(default_factory=list)


class ContextPackResponse(BaseModel):
    """Structured context pack optimized for agent injection."""

    goal: str
    intent: ContextIntent
    layer: ContextLayer = ContextLayer.RECALL
    query: str
    domain: str | None = None
    project: str | None = None
    sections: list[ContextPackSection] = Field(default_factory=list)
    total_items: int = 0
    usage_hint: str
    markdown: str | None = Field(
        default=None,
        description="Compact Markdown rendering for agent prompt injection",
    )


class ReflectionRequest(BaseModel):
    """Request for reflecting raw notes into durable memory candidates."""

    content: str = Field(..., min_length=1, description="Raw session notes or conversation text")
    source_title: str = Field(default="Session reflection", description="Source/session title")
    intent: ContextIntent = Field(default=ContextIntent.GENERAL, description="Reflection intent")
    domain: str | None = Field(default=None, description="Domain or category for candidates")
    project: str | None = Field(default=None, description="Project ID to scope candidates")
    related_to: list[str] | None = Field(
        default=None, description="Entity IDs to link persisted candidates to"
    )
    task_ids: list[str] | None = Field(
        default=None,
        description="Task IDs to link persisted source and candidates to",
    )
    active_task: bool = Field(
        default=True,
        description="When persisting with a project, link to the single active doing task",
    )
    persist: bool = Field(default=False, description="Persist candidates into the graph")
    persist_source: bool = Field(
        default=True,
        description="When persisting, also store the raw source notes as a session memory",
    )
    persist_review: bool = Field(
        default=False,
        description="Store persisted output in the raw review queue instead of graph promotion",
    )
    limit: int = Field(default=12, ge=1, le=25, description="Maximum candidates")


class ReflectionCandidateResponse(BaseModel):
    """Single memory candidate produced by reflection."""

    kind: str
    title: str
    content: str
    reason: str
    confidence: float
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    raw_source_ids: list[str] = Field(default_factory=list)
    suggested_memory_scope: MemoryScopeLiteral | None = None
    suggested_scope_key: str | None = None
    review_state: str = "pending"
    persisted_id: str | None = None


class ReflectionResponse(BaseModel):
    """Reflection output with reviewable memory candidates."""

    source_title: str
    source_id: str | None = None
    intent: str
    domain: str | None = None
    project: str | None = None
    candidates: list[ReflectionCandidateResponse] = Field(default_factory=list)
    total_candidates: int = 0
    persisted_count: int = 0
    usage_hint: str
    markdown: str | None = Field(
        default=None,
        description="Compact Markdown rendering for agent review",
    )


# =============================================================================
# Explore Schemas
# =============================================================================


class ExploreRequest(BaseModel):
    """Explore/graph traversal request."""

    mode: Literal["list", "related", "traverse", "dependencies"] = Field(
        default="list", description="Exploration mode"
    )
    types: list[str] | None = Field(default=None, description="Entity types to explore")
    entity_id: str | None = Field(default=None, description="Starting entity for traversal")
    relationship_types: list[str] | None = Field(default=None, description="Filter relationships")
    depth: int = Field(default=1, ge=1, le=3, description="Traversal depth")
    language: str | None = None
    category: str | None = None
    project: str | None = Field(default=None, description="Filter by project ID (for tasks)")
    project_ids: list[str] | None = Field(
        default=None, description="Filter by multiple project IDs (for tasks and epics)"
    )
    epic: str | None = Field(default=None, description="Filter by epic ID (for tasks)")
    no_epic: bool = Field(default=False, description="Filter for tasks without an epic")
    status: str | None = Field(default=None, description="Filter by status (for tasks)")
    priority: str | None = Field(
        default=None,
        description="Filter by priority (for tasks): critical, high, medium, low, someday",
    )
    complexity: str | None = Field(
        default=None,
        description="Filter by complexity (for tasks): trivial, simple, medium, complex, epic",
    )
    feature: str | None = Field(default=None, description="Filter by feature area (for tasks)")
    tags: str | None = Field(
        default=None, description="Filter by tags (comma-separated, matches if task has ANY)"
    )
    include_archived: bool = Field(
        default=False, description="Include archived projects in results"
    )
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0, description="Offset for pagination")


class RelatedEntity(BaseModel):
    """Entity related through the graph."""

    id: str
    type: str
    name: str
    relationship: str
    direction: Literal["outgoing", "incoming"]
    distance: int = 1


class ExploreResponse(BaseModel):
    """Explore results response."""

    mode: str
    entities: list[dict[str, Any]]  # Can be EntitySummary or RelatedEntity
    total: int
    filters: dict[str, Any]
    limit: int = Field(default=50, description="Results per page")
    offset: int = Field(default=0, description="Current offset")
    has_more: bool = Field(default=False, description="Whether more results exist")
    actual_total: int | None = Field(default=None, description="Total matching before pagination")


# =============================================================================
# Temporal Query Schemas (Bi-temporal support)
# =============================================================================


class TemporalRequest(BaseModel):
    """Bi-temporal query request."""

    mode: Literal["history", "timeline", "conflicts"] = Field(
        default="history",
        description="Query mode: history (point-in-time), timeline (all versions), conflicts (superseded)",
    )
    entity_id: str | None = Field(
        default=None, description="Entity to query (required for history/timeline)"
    )
    as_of: str | None = Field(
        default=None,
        description="Point-in-time for history query (ISO date, e.g. 2025-03-15)",
    )
    include_expired: bool = Field(default=False, description="Include expired/invalidated edges")
    limit: int = Field(default=50, ge=1, le=200, description="Maximum edges to return")


class TemporalEdgeSchema(BaseModel):
    """An edge with bi-temporal metadata."""

    id: str
    name: str
    source_id: str
    source_name: str
    target_id: str
    target_name: str
    created_at: str | None = None  # When ingested into system
    expired_at: str | None = None  # When invalidated in system
    valid_at: str | None = None  # When fact became true in real world
    invalid_at: str | None = None  # When fact ceased to be true
    fact: str | None = None
    is_current: bool = True


class TemporalResponse(BaseModel):
    """Response from temporal query."""

    mode: str
    entity_id: str | None
    edges: list[TemporalEdgeSchema]
    total: int
    as_of: str | None = None
    message: str | None = None


# =============================================================================
# Graph Visualization Schemas
# =============================================================================


class GraphNode(BaseModel):
    """Node for graph visualization."""

    id: str = Field(..., description="Unique node ID")
    type: str = Field(..., description="Entity type")
    label: str = Field(..., description="Display label")
    color: str = Field(..., description="Node color (hex)")
    size: float = Field(default=1.0, description="Relative node size")
    x: float | None = Field(default=None, description="X position (if pre-computed)")
    y: float | None = Field(default=None, description="Y position (if pre-computed)")
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    """Edge for graph visualization."""

    id: str = Field(..., description="Unique edge ID")
    source: str = Field(..., description="Source node ID")
    target: str = Field(..., description="Target node ID")
    type: str = Field(..., description="Relationship type")
    label: str = Field(default="", description="Edge label")
    weight: float = Field(default=1.0, description="Edge weight/thickness")


class GraphData(BaseModel):
    """Full graph data for visualization."""

    nodes: list[GraphNode]
    edges: list[GraphEdge]
    node_count: int
    edge_count: int


class SubgraphRequest(BaseModel):
    """Request for subgraph around an entity."""

    entity_id: str = Field(..., description="Center entity ID")
    depth: int = Field(default=2, ge=1, le=4, description="Traversal depth")
    relationship_types: list[RelationshipType] | None = Field(
        default=None, description="Filter relationship types"
    )
    max_nodes: int = Field(default=100, ge=1, le=500, description="Maximum nodes to return")


# =============================================================================
# Admin Schemas
# =============================================================================


class HealthResponse(BaseModel):
    """Server health status."""

    status: Literal["healthy", "unhealthy", "unknown"]
    server_name: str
    uptime_seconds: int
    graph_connected: bool
    entity_counts: dict[str, int]
    errors: list[str]


class StatsResponse(BaseModel):
    """Knowledge graph statistics."""

    entity_counts: dict[str, int]
    total_entities: int
    relationship_counts: dict[str, int] | None = None
    total_relationships: int | None = None


# =============================================================================
# WebSocket Event Schemas
# =============================================================================


class WebSocketEvent(BaseModel):
    """Event sent over WebSocket for realtime updates."""

    event: Literal[
        "entity_created",
        "entity_updated",
        "entity_deleted",
        "search_complete",
        "ingest_progress",
        "ingest_complete",
        "health_update",
        "crawl_started",
        "crawl_progress",
        "crawl_complete",
    ]
    data: dict[str, Any]
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


# =============================================================================
# Crawler Schemas
# =============================================================================


class CrawlSourceCreate(BaseModel):
    """Create a crawl source."""

    name: str = Field(..., description="Human-readable name")
    url: str = Field(..., description="Base URL to crawl")
    source_type: Literal["website", "github", "local", "api_docs"] = Field(
        default="website", description="Type of documentation source"
    )
    description: str | None = Field(default=None, description="Optional description")
    crawl_depth: int = Field(default=2, ge=1, le=5, description="Maximum link depth")
    include_patterns: list[str] = Field(
        default_factory=list, description="URL patterns to include (regex)"
    )
    exclude_patterns: list[str] = Field(
        default_factory=list, description="URL patterns to exclude (regex)"
    )


class CrawlSourceUpdate(BaseModel):
    """Update a crawl source."""

    name: str | None = Field(default=None, description="Human-readable name")
    description: str | None = Field(default=None, description="Optional description")
    crawl_depth: int | None = Field(default=None, ge=1, le=5, description="Maximum link depth")
    include_patterns: list[str] | None = Field(default=None, description="URL patterns to include")
    exclude_patterns: list[str] | None = Field(default=None, description="URL patterns to exclude")


class CrawlSourceResponse(BaseModel):
    """Crawl source with status."""

    id: str
    name: str
    url: str
    source_type: str
    description: str | None = None
    crawl_depth: int
    crawl_status: str  # pending, in_progress, completed, failed, partial
    document_count: int
    chunk_count: int
    last_crawled_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime
    include_patterns: list[str] = Field(default_factory=list)
    exclude_patterns: list[str] = Field(default_factory=list)


class CrawlSourceListResponse(BaseModel):
    """List of crawl sources."""

    sources: list[CrawlSourceResponse]
    total: int


class CrawlDocumentResponse(BaseModel):
    """Crawled document summary."""

    id: str
    source_id: str
    url: str
    title: str
    word_count: int
    has_code: bool
    is_index: bool
    depth: int
    crawled_at: datetime
    headings: list[str] = Field(default_factory=list)
    code_languages: list[str] = Field(default_factory=list)
    # Only populated in detail view, not list view
    raw_content: str | None = None
    markdown_content: str | None = None  # Assembled from chunks


class CrawlDocumentListResponse(BaseModel):
    """List of crawled documents."""

    documents: list[CrawlDocumentResponse]
    total: int


class CrawlIngestRequest(BaseModel):
    """Request to start crawling a source."""

    max_pages: int = Field(default=50, ge=1, le=500, description="Maximum pages to crawl")
    max_depth: int = Field(default=3, ge=1, le=5, description="Maximum link depth")
    generate_embeddings: bool = Field(default=True, description="Generate embeddings for chunks")


class CrawlIngestResponse(BaseModel):
    """Response from starting a crawl."""

    source_id: str
    job_id: str | None = None  # Job ID for cancellation
    status: str  # queued, already_running, cancelled
    message: str


class CrawlStatsResponse(BaseModel):
    """Crawler statistics."""

    total_sources: int
    total_documents: int
    total_chunks: int
    chunks_with_embeddings: int
    sources_by_status: dict[str, int]


class CrawlHealthResponse(BaseModel):
    """Crawler health status."""

    relational_backend_enabled: bool
    relational_backend_healthy: bool
    relational_backend_version: str | None = None
    vector_extension_version: str | None = None
    crawl4ai_available: bool
    error: str | None = None


class LinkGraphRequest(BaseModel):
    """Request to link document chunks to the knowledge graph."""

    batch_size: int = Field(default=50, ge=1, le=200, description="Chunks per batch")
    dry_run: bool = Field(default=False, description="Preview without processing")
    create_new_entities: bool = Field(
        default=False,
        description="Create graph entities for unlinked extractions",
    )


class LinkGraphResponse(BaseModel):
    """Response from graph linking operation."""

    source_id: str | None = None  # None if processing all sources
    status: str  # completed, dry_run, error, no_chunks
    chunks_processed: int = 0
    chunks_remaining: int = 0  # Unprocessed chunks still pending
    entities_extracted: int = 0
    entities_linked: int = 0
    new_entities_created: int = 0
    sources_processed: list[str] = Field(default_factory=list)
    message: str | None = None
    error: str | None = None


class LinkGraphSourceStatus(BaseModel):
    """Pending graph-linking work for a single source."""

    source_id: str
    name: str
    pending: int


class LinkGraphStatusResponse(BaseModel):
    """Status of pending graph linking work."""

    total_chunks: int = 0
    chunks_with_entities: int = 0
    chunks_pending: int = 0
    sources: list[LinkGraphSourceStatus] = Field(default_factory=list)


# =============================================================================
# RAG Search Schemas
# =============================================================================


class RAGSearchRequest(BaseModel):
    """RAG search request for document chunks."""

    query: str = Field(..., min_length=1, description="Natural language search query")
    source_id: str | None = Field(default=None, description="Filter by source ID")
    source_name: str | None = Field(
        default=None, description="Filter by source name (partial match)"
    )
    match_count: int = Field(default=10, ge=1, le=100, description="Number of results")
    similarity_threshold: float = Field(
        default=0.5, ge=0.0, le=1.0, description="Minimum similarity score"
    )
    return_mode: Literal["chunks", "pages"] = Field(
        default="chunks", description="Return chunks or full pages"
    )
    include_context: bool = Field(default=True, description="Include contextual prefix in results")


class RAGChunkResult(BaseModel):
    """Single chunk result from RAG search."""

    chunk_id: str
    document_id: str
    source_id: str
    source_name: str
    url: str
    title: str
    content: str
    context: str | None = None
    similarity: float
    chunk_type: str
    chunk_index: int
    heading_path: list[str] = Field(default_factory=list)
    language: str | None = None


class RAGPageResult(BaseModel):
    """Full page result from RAG search."""

    document_id: str
    source_id: str
    source_name: str
    url: str
    title: str
    content: str
    word_count: int
    has_code: bool
    headings: list[str] = Field(default_factory=list)
    code_languages: list[str] = Field(default_factory=list)
    best_chunk_similarity: float


class RAGSearchResponse(BaseModel):
    """RAG search response."""

    results: list[RAGChunkResult | RAGPageResult]
    total: int
    query: str
    source_filter: str | None = None
    return_mode: str


class CodeExampleRequest(BaseModel):
    """Search for code examples."""

    query: str = Field(..., min_length=1, description="Search query for code")
    language: str | None = Field(default=None, description="Filter by programming language")
    source_id: str | None = Field(default=None, description="Filter by source")
    match_count: int = Field(default=10, ge=1, le=50, description="Number of results")


class CodeExampleResult(BaseModel):
    """Code example result."""

    chunk_id: str
    document_id: str
    source_id: str
    source_name: str
    url: str
    title: str
    code: str
    context: str | None = None
    language: str | None = None
    similarity: float
    heading_path: list[str] = Field(default_factory=list)


class CodeExampleResponse(BaseModel):
    """Code example search response."""

    examples: list[CodeExampleResult]
    total: int
    query: str
    language_filter: str | None = None


class FullPageRequest(BaseModel):
    """Request full page content."""

    document_id: str | None = Field(default=None, description="Get by document ID")
    url: str | None = Field(default=None, description="Get by URL")


class FullPageResponse(BaseModel):
    """Full page content response."""

    document_id: str
    source_id: str
    source_name: str
    url: str
    title: str
    content: str
    raw_content: str | None = None
    word_count: int
    token_count: int
    has_code: bool
    headings: list[str] = Field(default_factory=list)
    code_languages: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    crawled_at: datetime


class SourcePagesRequest(BaseModel):
    """Request to list pages for a source."""

    source_id: str = Field(..., description="Source ID")
    limit: int = Field(default=50, ge=1, le=200, description="Maximum pages")
    offset: int = Field(default=0, ge=0, description="Offset for pagination")
    has_code: bool | None = Field(default=None, description="Filter by code presence")
    is_index: bool | None = Field(default=None, description="Filter index pages")


class SourcePagesResponse(BaseModel):
    """List of pages for a source."""

    source_id: str
    source_name: str
    pages: list[CrawlDocumentResponse]
    total: int
    has_more: bool


class DocumentUpdateRequest(BaseModel):
    """Update a crawled document's content."""

    title: str | None = Field(default=None, max_length=512, description="New document title")
    content: str | None = Field(default=None, max_length=500000, description="New document content")


class DocumentRelatedEntity(BaseModel):
    """An entity related to a document through extraction."""

    id: str
    name: str
    entity_type: str
    description: str = ""
    chunk_count: int = Field(default=1, description="Number of chunks mentioning this entity")


class DocumentRelatedEntitiesResponse(BaseModel):
    """Related entities for a document."""

    document_id: str
    entities: list[DocumentRelatedEntity]
    total: int


# === Backup/Restore Schemas ===


class BackupDataSchema(BaseModel):
    """Graph backup data structure."""

    version: str
    created_at: str
    organization_id: str
    entity_count: int
    relationship_count: int
    entities: list[dict]
    relationships: list[dict]


class BackupResponse(BaseModel):
    """Response from backup operation."""

    success: bool
    entity_count: int
    relationship_count: int
    message: str
    duration_seconds: float
    backup_data: BackupDataSchema | None = None


class RestoreRequest(BaseModel):
    """Request to restore from backup."""

    backup_data: BackupDataSchema
    skip_existing: bool = True


class RestoreResponse(BaseModel):
    """Response from restore operation."""

    success: bool
    entities_restored: int
    relationships_restored: int
    entities_skipped: int
    relationships_skipped: int
    errors: list[str]
    duration_seconds: float


class BackfillRequest(BaseModel):
    """Request to backfill missing relationships."""

    dry_run: bool = Field(
        default=False, description="If true, report what would be done without making changes"
    )


class BackfillResponse(BaseModel):
    """Response from relationship backfill operation."""

    success: bool
    relationships_created: int
    tasks_without_project: int
    tasks_already_linked: int
    errors: list[str]
    duration_seconds: float
    dry_run: bool


class ProjectRecordBackfillRequest(BaseModel):
    """Request to backfill missing project control-plane records."""

    dry_run: bool = Field(
        default=True, description="If true, report missing records without creating them"
    )


class ProjectRecordBackfillItem(BaseModel):
    """Per-project result from project record backfill."""

    graph_project_id: str
    status: Literal["existing", "would_create", "created", "skipped", "failed"]
    reason: str | None = None


class ProjectRecordBackfillResponse(BaseModel):
    """Response from project record backfill operation."""

    success: bool
    dry_run: bool
    existing: int
    would_create: int
    created: int
    skipped: int
    failed: int
    projects: list[ProjectRecordBackfillItem]
    errors: list[str]
    duration_seconds: float


# =============================================================================
# Debug Schemas
# =============================================================================


class DebugQueryRequest(BaseModel):
    """Request for executing a read-only debug query."""

    cypher: str = Field(
        ...,
        description="Read-only graph query to execute (SurrealQL for Surreal runtime)",
    )
    params: dict[str, Any] = Field(default_factory=dict, description="Query parameters")


class DebugQueryResponse(BaseModel):
    """Response from debug query execution."""

    rows: list[dict[str, Any]] = Field(default_factory=list, description="Query result rows")
    row_count: int = Field(default=0, description="Number of rows returned")
    error: str | None = Field(default=None, description="Error message if query failed")


class DevStatusResponse(BaseModel):
    """Comprehensive developer status dashboard."""

    # Component health
    api_healthy: bool = Field(description="API server is healthy")
    worker_healthy: bool = Field(description="Worker process is running")
    graph_healthy: bool = Field(description="Graph runtime is reachable")
    queue_healthy: bool = Field(description="Job queue is healthy")
    coordination_backend: str = Field(description="Resolved coordination backend")
    coordination_status: str = Field(description="Coordination subsystem status")
    coordination_durable: bool = Field(description="Coordination state survives process restarts")
    coordination_error: str | None = Field(
        default=None, description="Coordination error or readiness message"
    )

    # Stats
    uptime_seconds: float = Field(default=0, description="Server uptime")
    entity_count: int = Field(default=0, description="Total entities in graph")
    queue_depth: int = Field(default=0, description="Jobs in queue")

    # Recent activity
    recent_errors: list[dict[str, Any]] = Field(
        default_factory=list, description="Recent error log entries"
    )


# =============================================================================
# Metrics Schemas
# =============================================================================


class TaskStatusDistribution(BaseModel):
    """Task counts by status."""

    backlog: int = 0
    todo: int = 0
    doing: int = 0
    blocked: int = 0
    review: int = 0
    done: int = 0


class TaskPriorityDistribution(BaseModel):
    """Task counts by priority."""

    critical: int = 0
    high: int = 0
    medium: int = 0
    low: int = 0
    someday: int = 0


class AssigneeStats(BaseModel):
    """Stats per assignee."""

    name: str
    total: int = 0
    completed: int = 0
    in_progress: int = 0


class TimeSeriesPoint(BaseModel):
    """Single point in a time series."""

    date: str  # ISO date string (YYYY-MM-DD)
    value: int


class ProjectMetrics(BaseModel):
    """Metrics for a single project."""

    project_id: str
    project_name: str
    total_tasks: int
    status_distribution: TaskStatusDistribution
    priority_distribution: TaskPriorityDistribution
    completion_rate: float  # 0-100
    assignees: list[AssigneeStats]
    tasks_created_last_7d: int
    tasks_completed_last_7d: int
    velocity_trend: list[TimeSeriesPoint]  # completions per day last 14 days


class ProjectMetricsResponse(BaseModel):
    """Response for project metrics."""

    metrics: ProjectMetrics


class ProjectSummary(BaseModel):
    """Task rollup for a single project in org-wide metrics."""

    id: str
    name: str
    total: int = 0
    completed: int = 0
    doing: int = 0
    blocked: int = 0
    review: int = 0
    todo: int = 0
    backlog: int = 0
    critical: int = 0
    high: int = 0
    overdue: int = 0
    completion_rate: float = 0.0


class ProjectSummariesResponse(BaseModel):
    """Lean response for project-summary views."""

    projects_summary: list[ProjectSummary]


class OrgMetricsResponse(BaseModel):
    """Organization-level metrics aggregating all projects."""

    total_projects: int
    total_tasks: int
    status_distribution: TaskStatusDistribution
    priority_distribution: TaskPriorityDistribution
    completion_rate: float
    top_assignees: list[AssigneeStats]
    tasks_created_last_7d: int
    tasks_completed_last_7d: int
    velocity_trend: list[TimeSeriesPoint]
    projects_summary: list[ProjectSummary]
