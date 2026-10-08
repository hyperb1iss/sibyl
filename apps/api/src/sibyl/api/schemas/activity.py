"""Team activity response models."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ActivityWindowLabel = Literal["24h", "7d", "30d"]
ActivityKind = Literal[
    "capture",
    "task_created",
    "task_completed",
    "decision",
    "note",
    "procedure",
    "entity",
]
TeamMemberRole = Literal["owner", "admin", "member", "viewer"]


class ActivityWindow(BaseModel):
    """The half-open interval ``[since, until)`` the counts cover."""

    since: datetime
    until: datetime
    label: ActivityWindowLabel


class ActivityCounts(BaseModel):
    """What one member did in the window, among rows the caller may read."""

    captures: int = 0
    tasks_created: int = 0
    tasks_completed: int = 0
    decisions: int = 0
    notes: int = 0
    procedures: int = 0
    other: int = 0


class TeamActivityPerson(BaseModel):
    """One organization member and their activity in the window."""

    user_id: str
    name: str
    email: str | None = None
    avatar_url: str | None = None
    role: TeamMemberRole
    counts: ActivityCounts = Field(default_factory=ActivityCounts)
    last_active_at: datetime | None = None


class TeamActivityItem(BaseModel):
    """One readable event in the window, attributed to a member."""

    kind: ActivityKind
    id: str
    title: str
    entity_type: str | None = None
    project_id: str | None = None
    actor_id: str
    at: datetime
    href: str


class TeamActivityResponse(BaseModel):
    """Per-member activity for the caller's current organization."""

    window: ActivityWindow
    project_id: str | None = None
    people: list[TeamActivityPerson]
    recent: list[TeamActivityItem]
    truncated: bool = False
