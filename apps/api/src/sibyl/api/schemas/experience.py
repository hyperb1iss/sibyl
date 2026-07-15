"""Operational experience capture request and response models."""

from typing import Any

from pydantic import BaseModel, Field

from sibyl_core.models.experience import OperationalExperience


class OperationalExperienceCaptureRequest(BaseModel):
    """Request to persist raw operational evidence and typed projections."""

    experience: OperationalExperience
    defer_embeddings: bool = Field(
        default=True,
        description="Persist lexical records first and queue embedding backfill",
    )


class OperationalExperienceCaptureResponse(BaseModel):
    """Replay-safe operational experience write receipt."""

    source_id: str
    manifest_id: str
    content_hash: str
    written_entities: int
    written_relationships: int
    deleted_entities: int
    deleted_relationships: int
    entity_ids: list[str] = Field(default_factory=list)
    relationship_ids: list[str] = Field(default_factory=list)
    background_jobs: dict[str, Any] = Field(default_factory=dict)
