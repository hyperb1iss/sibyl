"""Stable type contracts shared by MCP tool domains."""

from typing import Annotated, Literal

from pydantic import Field

from sibyl_core.models.relations import DECLARABLE_PREDICATE_HELP

MemoryKind = Literal[
    "episode",
    "decision",
    "plan",
    "idea",
    "claim",
    "artifact",
    "procedure",
    "domain",
    "session",
    "pattern",
    "rule",
]
SynthesisOutputKind = Literal[
    "documentation",
    "report",
    "briefing",
    "roadmap",
    "release_notes",
    "audit_packet",
    "handbook",
    "custom",
]
SynthesisDepthKind = Literal["brief", "standard", "deep"]
DeclaredRelatedTo = Annotated[list[str] | None, Field(description=DECLARABLE_PREDICATE_HELP)]
SynthesisArtifactKind = Literal["markdown", "json"]
