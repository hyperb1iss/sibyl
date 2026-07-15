"""Contracts for source-backed operational experience."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from sibyl_core.models.entities import Entity, Relationship


class OperationalObservation(BaseModel):
    """An immutable observation captured before or after an action."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    ordinal: int = Field(ge=0)
    uri: str | None = None
    action: str | None = None
    reasoning: str | None = None
    evidence: str = ""
    image_refs: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)


class OperationalExperience(BaseModel):
    """A goal-directed sequence with its original observations intact."""

    model_config = ConfigDict(frozen=True)

    source_id: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    outcome: str | None = None
    start_uri: str | None = None
    observations: tuple[OperationalObservation, ...] = Field(min_length=1)
    project_id: str | None = None
    scope_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


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
