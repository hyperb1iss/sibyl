"""Session bundle (wake-up context) request/response models."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field


class SessionBundleContext(BaseModel):
    """Context metadata for a packaged wake-up bundle."""

    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    org_slug: str | None = Field(default=None, description="Active organization slug")
    project_ids: list[str] = Field(default_factory=list, description="Scoped project IDs")
    scope: Literal["all_projects", "project_selection"] = Field(
        default="all_projects",
        description="Whether the bundle is scoped to all projects or selected ones",
    )


class SessionTaskSummary(BaseModel):
    """Compact task representation for session bundles."""

    id: str = Field(..., description="Task ID")
    name: str = Field(..., description="Task title")
    status: str = Field(default="", description="Task status")
    priority: str = Field(default="", description="Task priority")
    feature: str | None = Field(default=None, description="Feature area")
    branch_name: str | None = Field(default=None, description="Attached branch name")


class SessionMemorySummary(BaseModel):
    """Compact relevant-memory representation for session bundles."""

    id: str = Field(..., description="Entity or document ID")
    name: str = Field(..., description="Entity title")
    entity_type: str | None = Field(default=None, description="Entity type")
    source: str | None = Field(default=None, description="Source document or path")
    preview: str = Field(default="", description="Short content preview")
    document_id: str | None = Field(default=None, description="Backing document ID")
    memory_scope: str | None = Field(default=None, description="Memory visibility scope")
    scope_key: str | None = Field(default=None, description="Project/team/shared scope key")


class SessionBundleResponse(BaseModel):
    """Packaged wake-up context for a session."""

    context: SessionBundleContext
    query: str | None = Field(default=None, description="Focus query used to gather memory")
    tasks: list[SessionTaskSummary] = Field(default_factory=list)
    relevant_entities: list[SessionMemorySummary] = Field(default_factory=list)
    remember_next: str = Field(default="", description="Single actionable wake-up suggestion")
