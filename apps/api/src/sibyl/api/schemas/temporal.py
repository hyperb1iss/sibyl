"""Bi-temporal query request/response models."""

from typing import Literal

from pydantic import BaseModel, Field


class TemporalRequest(BaseModel):
    """Bi-temporal query request."""

    mode: Literal["history", "timeline", "conflicts"] = Field(
        default="history",
        description="Query mode: history (point-in-time), timeline (all versions), conflicts (superseded)",
    )
    entity_id: str | None = Field(
        default=None, description="Entity to query (required for history/timeline)"
    )
    as_of: str | None = Field(
        default=None,
        description="Point-in-time for history query (ISO date, e.g. 2025-03-15)",
    )
    include_expired: bool = Field(default=False, description="Include expired/invalidated edges")
    limit: int = Field(default=50, ge=1, le=200, description="Maximum edges to return")


class TemporalEdgeSchema(BaseModel):
    """An edge with bi-temporal metadata."""

    id: str
    name: str
    source_id: str
    source_name: str
    target_id: str
    target_name: str
    created_at: str | None = None  # When ingested into system
    expired_at: str | None = None  # When invalidated in system
    valid_at: str | None = None  # When fact became true in real world
    invalid_at: str | None = None  # When fact ceased to be true
    fact: str | None = None
    is_current: bool = True


class TemporalResponse(BaseModel):
    """Response from temporal query."""

    mode: str
    entity_id: str | None
    edges: list[TemporalEdgeSchema]
    total: int
    as_of: str | None = None
    message: str | None = None
