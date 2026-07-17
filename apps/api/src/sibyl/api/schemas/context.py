"""Context pack (structured agent context) request/response models."""

from typing import Any, Literal

from pydantic import BaseModel, Field

from sibyl_core.models.context import ContextFacet, ContextIntent, ContextLayer
from sibyl_core.retrieval.refinement import MAX_REFINEMENT_QUERIES

from .search import SearchResponse


class ContextEvidenceRequest(BaseModel):
    """Optional enhanced evidence search bundled with a context pack."""

    types: list[str] = Field(
        default_factory=lambda: ["session"],
        description="Entity types to include in the evidence pool",
    )
    limit: int = Field(default=24, ge=1, le=50, description="Maximum evidence results")
    content_max_chars: int = Field(
        default=500,
        ge=0,
        le=50_000,
        description="Maximum content characters returned per evidence result",
    )
    include_retrieval_diagnostics: bool = Field(
        default=False,
        description="Include authorized evidence ranking diagnostics",
    )
    retrieval_mode: Literal["fast", "accurate"] = Field(
        default="fast",
        description="Use one search or deterministic multi-step evidence refinement",
    )
    max_planned_queries: int = Field(
        default=3,
        ge=1,
        le=MAX_REFINEMENT_QUERIES,
        description="Maximum feedback searches across two accurate-mode refinement rounds",
    )


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
    audit: bool = Field(
        default=False,
        description="Include full retrieval metadata per item for pack auditing",
    )
    record_exposure: bool = Field(
        default=True,
        description="Record returned items as memory exposure signals",
    )
    markdown_token_budget: int | None = Field(
        default=None,
        ge=100,
        le=8000,
        description="Cap rendered markdown at roughly this many tokens",
    )
    evidence: ContextEvidenceRequest | None = Field(
        default=None,
        description="Run enhanced source-evidence retrieval alongside context compilation",
    )


class ContextPackRelatedItem(BaseModel):
    """One-hop graph neighbor for a selected memory."""

    id: str
    type: str
    name: str
    relationship: str
    direction: str
    distance: int = 1
    content: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


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
    usage_metadata: dict[str, Any] = Field(default_factory=dict)
    usage_hint: str
    markdown: str | None = Field(
        default=None,
        description="Compact Markdown rendering for agent prompt injection",
    )
    evidence: SearchResponse | None = Field(
        default=None,
        description="Enhanced evidence retrieved concurrently with the context pack",
    )
