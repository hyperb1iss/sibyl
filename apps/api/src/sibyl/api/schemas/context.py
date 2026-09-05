"""Context pack (structured agent context) request/response models."""

from typing import Any, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from sibyl_core.models.context import ContextFacet, ContextIntent, ContextLayer
from sibyl_core.tools.context import DEFAULT_MARKDOWN_TOKEN_BUDGET

from .search import SearchResponse


class ContextEvidenceRequest(BaseModel):
    """Optional enhanced evidence search bundled with a context pack."""

    types: list[str] = Field(
        default_factory=lambda: ["session"],
        description="Entity types to include in the evidence pool",
    )
    limit: int = Field(default=24, ge=1, le=50, description="Maximum evidence results")
    char_budget: int | None = Field(
        default=None,
        ge=1,
        le=500_000,
        description=(
            "Bound composed evidence by characters of returned content instead of item count"
        ),
    )
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
    retrieval_mode: Literal["fast", "naive"] = Field(
        default="fast",
        description=(
            "Use one search (fast), or the naive-strong control arm (naive). "
            "The accurate mode was removed in 1.4; select fast explicitly. "
            "EXPERIMENTAL: naive uses BM25, dense KNN, and plain RRF with no traversal, synthesis, "
            "query planning, or coverage ranking, and governs the whole pack."
        ),
    )
    reserve_distilled_notes: bool = Field(
        default=True,
        description="Reserve a typed lane for distilled operational notes",
    )
    operational_note_dedupe_mode: Literal["source", "source_kind"] = Field(
        default="source",
        description=(
            "Dedupe distilled notes by source for baseline parity or by source and note kind"
        ),
    )
    operational_note_lane_mode: Literal["reserved", "additive"] = Field(
        default="reserved",
        description=(
            "Reserve note capacity inside the raw pack or add notes without changing raw order"
        ),
    )
    knn_type_overfetch: int = Field(
        default=0,
        ge=0,
        le=32,
        description=(
            "When >0 and a type filter is set, typed vector reads walk an "
            "untyped KNN pool this many times the candidate budget and filter "
            "types outside the HNSW bracket (a selective predicate beside the "
            "bracket forces a 10-15x deeper walk); a full head is exactly the "
            "typed KNN head, a shortfall falls back to the classic form. "
            "0 keeps the classic typed query."
        ),
    )

    @field_validator("retrieval_mode", mode="before")
    @classmethod
    def reject_removed_accurate_mode(cls, value: object) -> object:
        if value == "accurate":
            raise ValueError(
                "retrieval_mode=accurate was removed in Sibyl 1.4; "
                "set retrieval_mode=fast (or omit it). Replay historical accurate runs "
                "against their pinned Sibyl version."
            )
        return value

    @model_validator(mode="after")
    def require_note_lane_for_explicit_composition(self) -> Self:
        composition_fields = {
            "operational_note_dedupe_mode",
            "operational_note_lane_mode",
        }
        if not self.reserve_distilled_notes and self.model_fields_set & composition_fields:
            raise ValueError(
                "operational note composition modes require reserve_distilled_notes=true"
            )
        if self.operational_note_lane_mode == "additive" and self.char_budget is None:
            raise ValueError("operational_note_lane_mode=additive requires char_budget")
        return self


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
        default=DEFAULT_MARKDOWN_TOKEN_BUDGET,
        ge=100,
        le=32_000,
        description="Size rendered markdown to roughly this many tokens",
    )
    evidence: ContextEvidenceRequest | None = Field(
        default=None,
        description="Run enhanced source-evidence retrieval alongside context compilation",
    )
    knn_type_overfetch: int = Field(
        default=0,
        ge=0,
        le=32,
        description=(
            "When >0 and a type filter is set, typed vector reads walk an "
            "untyped KNN pool this many times the candidate budget and filter "
            "types outside the HNSW bracket (a selective predicate beside the "
            "bracket forces a 10-15x deeper walk); a full head is exactly the "
            "typed KNN head, a shortfall falls back to the classic form. "
            "0 keeps the classic typed query."
        ),
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
