"""Classify and roll up team activity from rows the caller may already read.

Nothing here reads storage or decides visibility. The activity route hands
over rows that already passed the reader filters the entity and capture lists
apply, and this module decides what each row says about who did what inside
the window: which member it credits, which kind of event it is, and where the
web app shows it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast
from urllib.parse import quote
from uuid import UUID

from sibyl.api.schemas.activity import (
    ActivityCounts,
    ActivityKind,
    ActivityWindow,
    ActivityWindowLabel,
    TeamActivityItem,
    TeamActivityPerson,
    TeamActivityResponse,
    TeamMemberRole,
)
from sibyl.persistence.content_common import RawCaptureRecord

WINDOW_SPANS: Final[dict[str, timedelta]] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
DEFAULT_WINDOW: Final = "7d"
RECENT_LIMIT: Final = 100

# Rows the server derives rather than rows a person authored. Counting them
# would credit the projection pipeline's output to whoever wrote the source,
# and count one memory several times over:
#   topic      mention nodes the memory projection mints for each name it sees
#   passage    spans the passage projection cuts from a longer memory
#   community  clusters written by community detection
#   document   pages the crawler ingested from a documentation source
# Learning episodes and procedures written from task learnings carry no author,
# so they credit nobody without being listed here.
DERIVED_ENTITY_TYPES: Final = frozenset({"topic", "passage", "community", "document"})
# Categories the server stamps on the rows it derives itself; the graph
# migration leaves them behind for the same reason.
DERIVED_CATEGORIES: Final = frozenset(
    {"memory_projection", "passage_projection", "memory_fact_projection"}
)
# Reflection (the dream cycle) consolidates memories into new decisions,
# procedures and tasks under the principal whose memories it read. That is
# synthesis output, not something the member did in the window.
REFLECTION_CAPTURE_MODES: Final = frozenset({"reflect"})
REFLECTION_CAPTURE_SURFACES: Final = frozenset(
    {"reflection", "reflection_candidate", "reflection_source", "synthesis_artifact"}
)
# `sibyl migrate to-team` replays another server's memories as raw captures on
# this surface and re-creates its graph rows with a `migration` stamp. That is
# work carried over in bulk on the day of the move, not new work.
MIGRATION_CAPTURE_SURFACES: Final = frozenset({"migration"})
_UNCOUNTED_CAPTURE_SURFACES: Final = REFLECTION_CAPTURE_SURFACES | MIGRATION_CAPTURE_SURFACES

_TYPED_KINDS: Final[dict[str, ActivityKind]] = {
    "decision": "decision",
    "note": "note",
    "procedure": "procedure",
}
_COUNT_FIELDS: Final[dict[ActivityKind, str]] = {
    "capture": "captures",
    "task_created": "tasks_created",
    "task_completed": "tasks_completed",
    "decision": "decisions",
    "note": "notes",
    "procedure": "procedures",
    "entity": "other",
}
_ROLES: Final = frozenset({"owner", "admin", "member", "viewer"})


@dataclass(frozen=True, slots=True)
class Window:
    label: ActivityWindowLabel
    since: datetime
    until: datetime

    def holds(self, instant: datetime) -> bool:
        return self.since <= instant < self.until


@dataclass(frozen=True, slots=True)
class Member:
    user_id: str
    name: str
    email: str | None
    avatar_url: str | None
    role: TeamMemberRole


@dataclass(frozen=True, slots=True)
class ActivityEvent:
    kind: ActivityKind
    id: str
    title: str
    entity_type: str | None
    project_id: str | None
    actor_id: str
    at: datetime
    href: str


def resolve_window(label: str, *, now: datetime) -> Window:
    span = WINDOW_SPANS[label]
    until = utc(now)
    if until is None:
        raise ValueError("now must be a datetime")
    return Window(label=cast("ActivityWindowLabel", label), since=until - span, until=until)


def utc(value: object) -> datetime | None:
    """Read a stored instant as an aware UTC datetime.

    The content plane decodes naive UTC, the graph decodes aware values, and
    work-item metadata keeps ISO text; all three compare in one window here.
    """
    if hasattr(value, "dt") and isinstance(getattr(value, "dt", None), str):
        value = value.dt
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def member_key(value: object) -> str | None:
    """The canonical id a stored actor names, or None when it names no user."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return str(UUID(text))
    except ValueError:
        return None


def members_from_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, Member]:
    """Members keyed by canonical user id, from the organization member listing."""
    members: dict[str, Member] = {}
    for row in rows:
        user = row.get("user")
        if not isinstance(user, Mapping):
            continue
        user_id = member_key(user.get("id"))
        if user_id is None:
            continue
        role = str(row.get("role") or "member").strip().lower()
        email = _text(user.get("email"))
        members[user_id] = Member(
            user_id=user_id,
            name=_text(user.get("name")) or email or user_id,
            email=email,
            avatar_url=_text(user.get("avatar_url")),
            role=cast("TeamMemberRole", role if role in _ROLES else "member"),
        )
    return members


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_member(members: Mapping[str, Member], *candidates: object) -> Member | None:
    for candidate in candidates:
        key = member_key(candidate)
        if key is not None and key in members:
            return members[key]
    return None


def _metadata(row: Any) -> Mapping[str, Any]:
    metadata = getattr(row, "metadata", None)
    return metadata if isinstance(metadata, Mapping) else {}


def entity_type_value(entity: Any) -> str:
    entity_type = getattr(entity, "entity_type", None)
    return str(getattr(entity_type, "value", entity_type) or "").strip().lower()


def derived_entity(entity: Any) -> bool:
    """Whether this row is something other than a member's new work.

    True for rows the server derived, rows reflection synthesized, and rows a
    migration carried over from another server.
    """
    metadata = _metadata(entity)
    if metadata.get("migration"):
        return True
    if entity_type_value(entity) in DERIVED_ENTITY_TYPES:
        return True
    category = getattr(entity, "category", None) or metadata.get("category")
    if str(category or "").strip().lower() in DERIVED_CATEGORIES:
        return True
    if str(metadata.get("capture_mode") or "").strip().lower() in REFLECTION_CAPTURE_MODES:
        return True
    if str(metadata.get("capture_surface") or "").strip().lower() in _UNCOUNTED_CAPTURE_SURFACES:
        return True
    return metadata.get("reflection_identity") is not None


def _entity_status(entity: Any) -> str:
    status = _metadata(entity).get("status") or getattr(entity, "status", None)
    return str(getattr(status, "value", status) or "").strip().lower()


def completion_actor(entity: Any, members: Mapping[str, Member]) -> Member | None:
    """Who moved a done task to done, or None when no field says so faithfully.

    The complete transition and an edit that sets a task to done both stamp
    ``completed_by`` with ``completed_at``; a row naming a non-member credits
    nobody. Rows finished before that stamp existed fall back as follows. One
    with ``completed_at`` came through the complete transition, which never
    recorded an actor, and its ``modified_by`` is whoever last edited it, so
    it credits nobody rather than a guess. One without ``completed_at`` was
    set to done by an edit, which stamped ``modified_by`` with the status;
    that is the most faithful field it has, though a later edit moves it.
    """
    metadata = _metadata(entity)
    completed_by = metadata.get("completed_by")
    if completed_by:
        return _first_member(members, completed_by)
    if metadata.get("completed_at"):
        return None
    return _first_member(
        members, getattr(entity, "modified_by", None) or metadata.get("modified_by")
    )


def entity_href(entity: Any) -> str:
    entity_id = quote(str(getattr(entity, "id", "")), safe="")
    entity_type = entity_type_value(entity)
    if entity_type == "task":
        return f"/tasks/{entity_id}"
    if entity_type == "epic":
        return f"/epics/{entity_id}"
    if entity_type == "project":
        return f"/projects?id={entity_id}"
    if entity_type == "note" and (task_id := _text(_metadata(entity).get("task_id"))):
        return f"/tasks/{quote(task_id, safe='')}"
    return f"/entities/{entity_id}"


def capture_href(capture_id: str) -> str:
    return f"/memory/captures?id={quote(capture_id, safe='')}"


def entity_events(
    entity: Any,
    *,
    window: Window,
    members: Mapping[str, Member],
    project_id: str | None,
) -> list[ActivityEvent]:
    """The created and completed events one readable graph row contributes."""
    events: list[ActivityEvent] = []
    entity_type = entity_type_value(entity)
    metadata = _metadata(entity)
    created_at = utc(getattr(entity, "created_at", None))

    def event(kind: ActivityKind, actor: Member, at: datetime) -> ActivityEvent:
        return ActivityEvent(
            kind=kind,
            id=str(entity.id),
            title=str(getattr(entity, "name", "") or entity.id),
            entity_type=entity_type or None,
            project_id=project_id,
            actor_id=actor.user_id,
            at=at,
            href=entity_href(entity),
        )

    if created_at is not None and window.holds(created_at) and not derived_entity(entity):
        # created_by is the column the write path sets; principal_id is the
        # authorship the scope stamp takes from the authenticated writer, and
        # it is the only author an asynchronously created memory carries.
        author = _first_member(
            members, getattr(entity, "created_by", None), metadata.get("principal_id")
        )
        if author is not None:
            kind: ActivityKind = (
                "task_created" if entity_type == "task" else _TYPED_KINDS.get(entity_type, "entity")
            )
            events.append(event(kind, author, created_at))

    if entity_type == "task" and _entity_status(entity) == "done":
        completed_at = utc(metadata.get("completed_at")) or utc(getattr(entity, "updated_at", None))
        if completed_at is not None and window.holds(completed_at):
            actor = completion_actor(entity, members)
            if actor is not None:
                events.append(event("task_completed", actor, completed_at))
    return events


def standalone_capture(capture: RawCaptureRecord) -> bool:
    """Whether a raw capture is its own act rather than a mirror of a graph row.

    ``remember`` writes the verbatim raw memory, then the graph row, then an
    archive sidecar of that row, and stamps ``projected_capture_id`` on the
    raw memory. The graph row is counted under its own type, so the sidecar
    and the stamped raw memory would count the same act twice.
    """
    metadata = capture.metadata or {}
    if capture.deleted_at is not None or capture.entity_type != "raw_memory":
        return False
    if metadata.get("projected_capture_id"):
        return False
    surface = capture.capture_surface or metadata.get("capture_surface")
    return str(surface or "").strip().lower() not in _UNCOUNTED_CAPTURE_SURFACES


def capture_project_id(capture: RawCaptureRecord) -> str | None:
    return _text(capture.project_id) or _text((capture.metadata or {}).get("project_id"))


def capture_event(
    capture: RawCaptureRecord,
    *,
    window: Window,
    members: Mapping[str, Member],
) -> ActivityEvent | None:
    if not standalone_capture(capture):
        return None
    at = utc(capture.created_at)
    if at is None or not window.holds(at):
        return None
    # A raw capture's principal_id is the authenticated writer the capture
    # route stamped; nothing else about the row names a person.
    actor = _first_member(members, capture.principal_id)
    if actor is None:
        return None
    capture_id = str(capture.id)
    return ActivityEvent(
        kind="capture",
        id=capture_id,
        title=capture.title.strip() or "Untitled capture",
        entity_type=capture.entity_type or None,
        project_id=capture_project_id(capture),
        actor_id=actor.user_id,
        at=at,
        href=capture_href(capture_id),
    )


def summarize(
    events: Iterable[ActivityEvent],
    *,
    members: Mapping[str, Member],
    window: Window,
    project_id: str | None,
    recent_limit: int = RECENT_LIMIT,
) -> TeamActivityResponse:
    """Every member with their counts, and the newest events across everyone."""
    counts: dict[str, dict[str, int]] = {user_id: {} for user_id in members}
    last_active: dict[str, datetime] = {}
    ordered: list[ActivityEvent] = []
    for item in events:
        if item.actor_id not in members:
            continue
        field = _COUNT_FIELDS[item.kind]
        tally = counts[item.actor_id]
        tally[field] = tally.get(field, 0) + 1
        if item.actor_id not in last_active or item.at > last_active[item.actor_id]:
            last_active[item.actor_id] = item.at
        ordered.append(item)

    people = [
        TeamActivityPerson(
            user_id=member.user_id,
            name=member.name,
            email=member.email,
            avatar_url=member.avatar_url,
            role=member.role,
            counts=ActivityCounts(**counts[member.user_id]),
            last_active_at=last_active.get(member.user_id),
        )
        for member in members.values()
    ]
    people.sort(
        key=lambda person: (
            -sum(counts[person.user_id].values()),
            person.name.casefold(),
            person.user_id,
        )
    )
    ordered.sort(key=lambda item: (item.at, item.kind, item.id), reverse=True)
    return TeamActivityResponse(
        window=ActivityWindow(since=window.since, until=window.until, label=window.label),
        project_id=project_id,
        people=people,
        recent=[
            TeamActivityItem(
                kind=item.kind,
                id=item.id,
                title=item.title,
                entity_type=item.entity_type,
                project_id=item.project_id,
                actor_id=item.actor_id,
                at=item.at,
                href=item.href,
            )
            for item in ordered[:recent_limit]
        ],
        truncated=len(ordered) > recent_limit,
    )
