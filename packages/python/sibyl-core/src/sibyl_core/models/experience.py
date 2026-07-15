"""Contracts for source-backed operational experience."""

import json
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sibyl_core.models.entities import Entity, Relationship

MAX_OPERATIONAL_OBSERVATIONS = 1_024
MAX_EVIDENCE_PARTS_PER_OBSERVATION = 256
MAX_OPERATIONAL_EVIDENCE_PARTS = 4_096
MAX_OPERATIONAL_EVIDENCE_PART_CHARS = 1_000_000
MAX_OPERATIONAL_EVIDENCE_TOTAL_CHARS = 64 * 1024 * 1024
MAX_OPERATIONAL_FIELD_CHARS = 18_000
MAX_OPERATIONAL_URI_CHARS = 8_192
MAX_OPERATIONAL_IDENTIFIER_CHARS = 2_048
MAX_OPERATIONAL_IMAGE_REFS = 256
MAX_OPERATIONAL_AUXILIARY_JSON_CHARS = 8 * 1024 * 1024
OperationalImageRef = Annotated[str, Field(max_length=MAX_OPERATIONAL_URI_CHARS)]


class OperationalEvidencePart(BaseModel):
    """One bounded, ordered part of an observation's source evidence."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1, max_length=MAX_OPERATIONAL_IDENTIFIER_CHARS)
    content: str = Field(max_length=MAX_OPERATIONAL_EVIDENCE_PART_CHARS)
    content_type: str = Field(default="text/plain", max_length=256)
    metadata: dict[str, Any] = Field(default_factory=dict)


class OperationalObservation(BaseModel):
    """An immutable observation captured before or after an action."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1, max_length=MAX_OPERATIONAL_IDENTIFIER_CHARS)
    ordinal: int = Field(ge=0)
    uri: str | None = Field(default=None, max_length=MAX_OPERATIONAL_URI_CHARS)
    action: str | None = Field(default=None, max_length=MAX_OPERATIONAL_FIELD_CHARS)
    reasoning: str | None = Field(default=None, max_length=MAX_OPERATIONAL_FIELD_CHARS)
    evidence: tuple[OperationalEvidencePart, ...] = Field(
        min_length=1,
        max_length=MAX_EVIDENCE_PARTS_PER_OBSERVATION,
    )
    image_refs: tuple[OperationalImageRef, ...] = Field(
        default=(),
        max_length=MAX_OPERATIONAL_IMAGE_REFS,
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class OperationalExperience(BaseModel):
    """A goal-directed sequence with its original observations intact."""

    model_config = ConfigDict(frozen=True)

    source_id: str = Field(min_length=1, max_length=MAX_OPERATIONAL_IDENTIFIER_CHARS)
    goal: str = Field(min_length=1, max_length=MAX_OPERATIONAL_FIELD_CHARS)
    outcome: str | None = Field(default=None, max_length=MAX_OPERATIONAL_FIELD_CHARS)
    start_uri: str | None = Field(default=None, max_length=MAX_OPERATIONAL_URI_CHARS)
    observations: tuple[OperationalObservation, ...] = Field(
        min_length=1,
        max_length=MAX_OPERATIONAL_OBSERVATIONS,
    )
    project_id: str | None = Field(default=None, max_length=MAX_OPERATIONAL_IDENTIFIER_CHARS)
    scope_key: str | None = Field(default=None, max_length=MAX_OPERATIONAL_IDENTIFIER_CHARS)
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
        auxiliary_payload = self.model_dump(mode="json")
        for observation in auxiliary_payload["observations"]:
            for evidence in observation["evidence"]:
                evidence["content"] = ""
        auxiliary_chars = len(json.dumps(auxiliary_payload, sort_keys=True, separators=(",", ":")))
        if auxiliary_chars > MAX_OPERATIONAL_AUXILIARY_JSON_CHARS:
            raise ValueError("operational experience exceeds the auxiliary payload limit")
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
    embedding_backfill_required: bool = False
