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
    usage_hint: str = (
        "Use this as the working context pack. Capture new decisions, plans, ideas, "
        "claims, procedures, and artifacts back into Sibyl as they emerge."
    )

    @property
    def items(self) -> list[ContextItem]:
        return [item for section in self.sections for item in section.items]
