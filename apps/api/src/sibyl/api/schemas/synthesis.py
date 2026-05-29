"""Synthesis planning/drafting and reflection request/response models."""

from typing import Any

from pydantic import BaseModel, Field

from sibyl_core.models.context import ContextIntent
from sibyl_core.models.synthesis import (
    SynthesisArtifactFormat,
    SynthesisDepth,
    SynthesisOutputType,
    SynthesisRunStatus,
    SynthesisVerificationStatus,
)

from .common import MemoryScopeLiteral


class SynthesisSectionPlanRequest(BaseModel):
    """Requested outline section for source-grounded synthesis."""

    title: str = Field(..., min_length=1, max_length=200)
    prompt: str | None = Field(default=None, max_length=2000)
    required_source_ids: list[str] = Field(default_factory=list, max_length=50)


class SynthesisPlanRequest(BaseModel):
    """Request for a deterministic source-aware synthesis plan."""

    goal: str = Field(..., min_length=1, max_length=1000)
    output_type: SynthesisOutputType = Field(default=SynthesisOutputType.DOCUMENTATION)
    audience: str | None = Field(default=None, max_length=500)
    depth: SynthesisDepth = Field(default=SynthesisDepth.STANDARD)
    seed_query: str | None = Field(default=None, max_length=1000)
    project: str | None = Field(default=None, max_length=500)
    domain: str | None = Field(default=None, max_length=200)
    entity_ids: list[str] = Field(default_factory=list, max_length=100)
    decision_ids: list[str] = Field(default_factory=list, max_length=100)
    task_ids: list[str] = Field(default_factory=list, max_length=100)
    artifact_ids: list[str] = Field(default_factory=list, max_length=100)
    required_sections: list[SynthesisSectionPlanRequest] = Field(
        default_factory=list,
        max_length=12,
    )
    constraints: list[str] = Field(default_factory=list, max_length=50)
    max_sections: int = Field(default=6, ge=1, le=12)
    include_neighborhoods: bool = True


class SynthesisSourceReferenceResponse(BaseModel):
    """Source reference selected for a synthesis plan."""

    id: str
    type: str
    name: str
    content_preview: str = ""
    score: float = 0.0
    source: str | None = None
    origin: str = "graph"
    relation: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SynthesisGapResponse(BaseModel):
    """Section-level synthesis source gap."""

    section_id: str
    title: str
    reason: str
    query: str
    missing_source_ids: list[str] = Field(default_factory=list)


class SynthesisOutlineSectionResponse(BaseModel):
    """Planned synthesis outline section."""

    section_id: str
    title: str
    prompt: str
    source_query: str
    source_ids: list[str] = Field(default_factory=list)
    gaps: list[SynthesisGapResponse] = Field(default_factory=list)


class SynthesisOutlineResponse(BaseModel):
    """Source-aware synthesis outline."""

    title: str
    output_type: SynthesisOutputType
    audience: str | None = None
    sections: list[SynthesisOutlineSectionResponse] = Field(default_factory=list)


class SynthesisSourcePackResponse(BaseModel):
    """Placeholder source pack for a planned section."""

    section_id: str
    title: str
    query: str
    source_ids: list[str] = Field(default_factory=list)
    sources: list[SynthesisSourceReferenceResponse] = Field(default_factory=list)
    hidden_count: int = 0
    redaction_count: int = 0
    correction_count: int = 0
    correction_reasons: dict[str, int] = Field(default_factory=dict)
    freshness: dict[str, str | None] = Field(default_factory=dict)
    unresolved_claims: list[str] = Field(default_factory=list)


class SynthesisVerificationResponse(BaseModel):
    """Planning-time synthesis verification summary."""

    status: SynthesisVerificationStatus
    source_count: int = 0
    gap_count: int = 0
    gaps: list[SynthesisGapResponse] = Field(default_factory=list)


class SynthesisPlanResponse(BaseModel):
    """Deterministic synthesis planning response before drafting."""

    run_id: str
    status: SynthesisRunStatus
    request: SynthesisPlanRequest
    outline: SynthesisOutlineResponse
    source_packs: list[SynthesisSourcePackResponse] = Field(default_factory=list)
    verification: SynthesisVerificationResponse


class SynthesisDraftRequest(SynthesisPlanRequest):
    """Request for a drafted, verified synthesis artifact."""

    output_format: SynthesisArtifactFormat = Field(default=SynthesisArtifactFormat.MARKDOWN)
    remember: bool = Field(default=False, description="Persist the generated artifact")
    memory_scope: MemoryScopeLiteral = Field(default="private", description="Artifact memory scope")
    scope_key: str | None = Field(default=None, description="Artifact scope key")
    tags: list[str] = Field(default_factory=list, max_length=50)


class SynthesisArtifactResponse(BaseModel):
    """Generated source-grounded synthesis artifact."""

    artifact_id: str
    format: SynthesisArtifactFormat
    title: str
    markdown: str
    json_payload: dict[str, Any] = Field(default_factory=dict)
    source_ids: list[str] = Field(default_factory=list)
    section_source_ids: dict[str, list[str]] = Field(default_factory=dict)
    generated_text_hash: str
    verification: SynthesisVerificationResponse
    remembered_memory_id: str | None = None
    remembered_source_id: str | None = None


class SynthesisDraftResponse(SynthesisPlanResponse):
    """Synthesis response with a drafted artifact."""

    artifact: SynthesisArtifactResponse


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
    claim_records: list[dict[str, Any]] = Field(default_factory=list)
    reflection_findings: list[dict[str, Any]] = Field(default_factory=list)
    relationship_records: list[dict[str, Any]] = Field(default_factory=list)
    sensitivity_flags: list[str] = Field(default_factory=list)


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
