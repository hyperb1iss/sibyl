"""Explore / graph-traversal request/response models."""

from typing import Any, Literal

from pydantic import BaseModel, Field


class ExploreRequest(BaseModel):
    """Explore/graph traversal request."""

    mode: Literal["list", "related", "traverse", "dependencies"] = Field(
        default="list", description="Exploration mode"
    )
    types: list[str] | None = Field(default=None, description="Entity types to explore")
    entity_id: str | None = Field(default=None, description="Starting entity for traversal")
    relationship_types: list[str] | None = Field(default=None, description="Filter relationships")
    depth: int = Field(default=1, ge=1, le=3, description="Traversal depth")
    language: str | None = None
    category: str | None = None
    project: str | None = Field(default=None, description="Filter by project ID (for tasks)")
    project_ids: list[str] | None = Field(
        default=None, description="Filter by multiple project IDs (for tasks and epics)"
    )
    epic: str | None = Field(default=None, description="Filter by epic ID (for tasks)")
    no_epic: bool = Field(default=False, description="Filter for tasks without an epic")
    status: str | None = Field(default=None, description="Filter by status (for tasks)")
    priority: str | None = Field(
        default=None,
        description="Filter by priority (for tasks): critical, high, medium, low, someday",
    )
    complexity: str | None = Field(
        default=None,
        description="Filter by complexity (for tasks): trivial, simple, medium, complex, epic",
    )
    feature: str | None = Field(default=None, description="Filter by feature area (for tasks)")
    tags: str | None = Field(
        default=None, description="Filter by tags (comma-separated, matches if task has ANY)"
    )
    include_archived: bool = Field(
        default=False, description="Include archived projects in results"
    )
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0, description="Offset for pagination")


class RelatedEntity(BaseModel):
    """Entity related through the graph."""

    id: str
    type: str
    name: str
    relationship: str
    direction: Literal["outgoing", "incoming"]
    distance: int = 1


class ExploreResponse(BaseModel):
    """Explore results response."""

    mode: str
    entities: list[dict[str, Any]]  # Can be EntitySummary or RelatedEntity
    total: int
    filters: dict[str, Any]
    limit: int = Field(default=50, description="Results per page")
    offset: int = Field(default=0, description="Current offset")
    has_more: bool = Field(default=False, description="Whether more results exist")
    actual_total: int | None = Field(default=None, description="Total matching before pagination")
