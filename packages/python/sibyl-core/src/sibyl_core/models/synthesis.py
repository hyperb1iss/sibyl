"""Models for source-grounded synthesis planning."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class SynthesisOutputType(StrEnum):
    DOCUMENTATION = "documentation"
    REPORT = "report"
    BRIEFING = "briefing"
    ROADMAP = "roadmap"
    RELEASE_NOTES = "release_notes"
    AUDIT_PACKET = "audit_packet"
    CUSTOM = "custom"


class SynthesisDepth(StrEnum):
    BRIEF = "brief"
    STANDARD = "standard"
    DEEP = "deep"


class SynthesisRunStatus(StrEnum):
    PLANNED = "planned"
    DRAFTING = "drafting"
    VERIFIED = "verified"
    FAILED = "failed"


class SynthesisVerificationStatus(StrEnum):
    PENDING = "pending"
    GAPS = "gaps"
    PASS = "pass"


class SynthesisArtifactFormat(StrEnum):
    MARKDOWN = "markdown"
    JSON = "json"


@dataclass(frozen=True)
class SynthesisSectionRequest:
    title: str
    prompt: str | None = None
    required_source_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SynthesisRequest:
    goal: str
    output_type: SynthesisOutputType = SynthesisOutputType.DOCUMENTATION
    audience: str | None = None
    depth: SynthesisDepth = SynthesisDepth.STANDARD
    seed_query: str | None = None
    project: str | None = None
    domain: str | None = None
    entity_ids: list[str] = field(default_factory=list)
    decision_ids: list[str] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)
    artifact_ids: list[str] = field(default_factory=list)
    required_sections: list[SynthesisSectionRequest] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    max_sections: int = 6
    include_neighborhoods: bool = True


@dataclass(frozen=True)
class SynthesisSourceReference:
    id: str
    type: str
    name: str
    content_preview: str = ""
    score: float = 0.0
    source: str | None = None
    origin: str = "graph"
    relation: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SynthesisGap:
    section_id: str
    title: str
    reason: str
    query: str
    missing_source_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SynthesisOutlineSection:
    section_id: str
    title: str
    prompt: str
    source_query: str
    source_ids: list[str] = field(default_factory=list)
    gaps: list[SynthesisGap] = field(default_factory=list)

    @property
    def source_count(self) -> int:
        return len(self.source_ids)


@dataclass(frozen=True)
class SynthesisOutline:
    title: str
    output_type: SynthesisOutputType
    audience: str | None
    sections: list[SynthesisOutlineSection] = field(default_factory=list)


@dataclass(frozen=True)
class SynthesisSourcePack:
    section_id: str
    title: str
    query: str
    source_ids: list[str] = field(default_factory=list)
    sources: list[SynthesisSourceReference] = field(default_factory=list)
    hidden_count: int = 0
    redaction_count: int = 0
    freshness: dict[str, str | None] = field(default_factory=dict)
    unresolved_claims: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SynthesisVerification:
    status: SynthesisVerificationStatus
    source_count: int
    gap_count: int
    gaps: list[SynthesisGap] = field(default_factory=list)


@dataclass(frozen=True)
class SynthesisArtifact:
    artifact_id: str
    format: SynthesisArtifactFormat
    title: str
    markdown: str
    json_payload: dict[str, Any]
    source_ids: list[str]
    section_source_ids: dict[str, list[str]]
    generated_text_hash: str
    verification: SynthesisVerification
    remembered_memory_id: str | None = None
    remembered_source_id: str | None = None


@dataclass(frozen=True)
class SynthesisRun:
    run_id: str
    status: SynthesisRunStatus
    request: SynthesisRequest
    outline: SynthesisOutline
    source_packs: list[SynthesisSourcePack]
    verification: SynthesisVerification


__all__ = [
    "SynthesisArtifact",
    "SynthesisArtifactFormat",
    "SynthesisDepth",
    "SynthesisGap",
    "SynthesisOutline",
    "SynthesisOutlineSection",
    "SynthesisOutputType",
    "SynthesisRequest",
    "SynthesisRun",
    "SynthesisRunStatus",
    "SynthesisSectionRequest",
    "SynthesisSourcePack",
    "SynthesisSourceReference",
    "SynthesisVerification",
    "SynthesisVerificationStatus",
]
