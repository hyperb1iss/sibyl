"""Models for compiling precise context packs for agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ContextIntent(StrEnum):
    BUILD = "build"
    PLAN = "plan"
    IDEATE = "ideate"
    RESEARCH = "research"
    REVIEW = "review"
    DEBUG = "debug"
    DECIDE = "decide"
    LEARN = "learn"
    GENERAL = "general"


class ContextLayer(StrEnum):
    WAKE = "wake"
    RECALL = "recall"
    DEEP_SEARCH = "deep_search"


class ContextFacet(StrEnum):
    ACTIVE_WORK = "active_work"
    PRIOR_ART = "prior_art"
    ARTIFACTS = "artifacts"
    CONSTRAINTS = "constraints"
    DECISIONS = "decisions"
    DOMAIN = "domain"
    GOTCHAS = "gotchas"
    IDEATION = "ideation"
    PLANNING = "planning"
    PROCEDURES = "procedures"
    RECENT_MEMORY = "recent_memory"
    VERIFICATION = "verification"


@dataclass(frozen=True)
class ContextRelatedItem:
    id: str
    type: str
    name: str
    relationship: str
    direction: str
    distance: int = 1
    content: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    source_revision: int | None = None


@dataclass(frozen=True)
class ContextItemQualityMetadata:
    origin: str | None = None
    source: str | None = None
    url: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    valid_at: str | None = None
    project_id: str | None = None


@dataclass(frozen=True)
class ContextItem:
    id: str
    type: str
    name: str
    content: str
    score: float
    facet: ContextFacet
    reason: str
    source: str | None = None
    quality: ContextItemQualityMetadata = field(default_factory=ContextItemQualityMetadata)
    metadata: dict[str, Any] = field(default_factory=dict)
    related: list[ContextRelatedItem] = field(default_factory=list)
    source_revision: int | None = None


@dataclass(frozen=True)
class ContextSection:
    facet: ContextFacet
    title: str
    items: list[ContextItem] = field(default_factory=list)


@dataclass(frozen=True)
class ContextPack:
    goal: str
    intent: ContextIntent
    query: str
    domain: str | None
    project: str | None
    sections: list[ContextSection]
    total_items: int
    layer: ContextLayer = ContextLayer.RECALL
    usage_metadata: dict[str, Any] = field(default_factory=dict)
    usage_hint: str = (
        "Use this as the working context pack. Capture new decisions, plans, ideas, "
        "claims, procedures, and artifacts back into Sibyl as they emerge."
    )

    @property
    def items(self) -> list[ContextItem]:
        return [item for section in self.sections for item in section.items]


@dataclass(frozen=True)
class ContextRenderSpan:
    """A source-bound range of the exact UTF-8 Markdown output.

    Item spans cover the full Markdown block, including its provenance labels.
    Field spans additionally map visible text back to renderer input bytes.
    """

    item_index: int
    record_id: str
    record_type: str
    source_alias: str | None
    source_revision: int | None
    revision_status: str
    field: str
    start_byte: int
    end_byte: int
    input_sha256: str
    transform: str
    input_start_byte: int | None = None
    input_end_byte: int | None = None


@dataclass(frozen=True)
class ContextRenderDisposition:
    """Disposition of one selected top-level item, not its omitted neighbors."""

    item_index: int
    record_id: str
    state: str
    reason: str
    content_state: str


@dataclass(frozen=True)
class ContextRenderOptions:
    max_items: int = 8
    items_per_section: int = 3
    max_content_chars: int = 280
    include_related: bool = True
    token_budget: int | None = None


@dataclass(frozen=True)
class ContextRenderReceipt:
    """Receipt for exact API Markdown bytes, before CLI display formatting.

    Spans bind typed source_revision values, never similarly named item metadata.
    Dispositions cover selected top-level items only. request_id is an optional
    correlation label, which may be client supplied; it is not an attestation.
    """

    schema_version: str
    markdown_sha256: str
    markdown_bytes: int
    selected_items: int
    options: ContextRenderOptions
    spans: list[ContextRenderSpan]
    dispositions: list[ContextRenderDisposition]
    request_id: str | None = None


@dataclass(frozen=True)
class RenderedContextPack:
    markdown: str
    receipt: ContextRenderReceipt
