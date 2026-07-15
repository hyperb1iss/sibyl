"""Contracts for source-backed operational experience."""

from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sibyl_core.models.entities import Entity, Relationship

MAX_OPERATIONAL_OBSERVATIONS = 1_024
MAX_EVIDENCE_PARTS_PER_OBSERVATION = 256
MAX_OPERATIONAL_EVIDENCE_PARTS = 4_096
MAX_OPERATIONAL_EVIDENCE_PART_CHARS = 1_000_000
MAX_OPERATIONAL_EVIDENCE_TOTAL_CHARS = 64 * 1024 * 1024


class OperationalEvidencePart(BaseModel):
    """One bounded, ordered part of an observation's source evidence."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    content: str = Field(max_length=MAX_OPERATIONAL_EVIDENCE_PART_CHARS)
    content_type: str = "text/plain"
    metadata: dict[str, Any] = Field(default_factory=dict)


class OperationalObservation(BaseModel):
    """An immutable observation captured before or after an action."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    ordinal: int = Field(ge=0)
    uri: str | None = None
    action: str | None = None
    reasoning: str | None = None
    evidence: tuple[OperationalEvidencePart, ...] = Field(
        min_length=1,
        max_length=MAX_EVIDENCE_PARTS_PER_OBSERVATION,
    )
    image_refs: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)


class OperationalExperience(BaseModel):
    """A goal-directed sequence with its original observations intact."""

    model_config = ConfigDict(frozen=True)

    source_id: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    outcome: str | None = None
    start_uri: str | None = None
    observations: tuple[OperationalObservation, ...] = Field(
        min_length=1,
        max_length=MAX_OPERATIONAL_OBSERVATIONS,
    )
    project_id: str | None = None
    scope_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_evidence_budget(self) -> Self:
        evidence_parts = [
            evidence for observation in self.observations for evidence in observation.evidence
        ]
        if len(evidence_parts) > MAX_OPERATIONAL_EVIDENCE_PARTS:
            raise ValueError("operational experience exceeds the total evidence part limit")
        if (
            sum(len(evidence.content) for evidence in evidence_parts)
            > MAX_OPERATIONAL_EVIDENCE_TOTAL_CHARS
        ):
            raise ValueError("operational experience exceeds the total evidence content limit")
        return self


class OperationalExperienceManifest(BaseModel):
    """Stable projection inventory used for replay and stale-record cleanup."""

    model_config = ConfigDict(frozen=True)

    source_id: str
    schema_version: int
    content_hash: str
    manifest_entity_id: str
    entity_ids: tuple[str, ...]
    relationship_ids: tuple[str, ...]


class OperationalExperienceProjection(BaseModel):
    """Deterministic graph payload produced from operational evidence."""

    model_config = ConfigDict(frozen=True)

    entities: tuple[Entity, ...]
    relationships: tuple[Relationship, ...]
    manifest: OperationalExperienceManifest


class OperationalExperienceWriteResult(BaseModel):
    """Result of replaying an operational experience into the graph."""

    model_config = ConfigDict(frozen=True)

    projection: OperationalExperienceProjection
    written_entity_ids: tuple[str, ...]
    written_relationship_ids: tuple[str, ...]
    deleted_entity_ids: tuple[str, ...]
    deleted_relationship_ids: tuple[str, ...]
