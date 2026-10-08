"""The completion record a task status change keeps true.

A task's ``completed_at`` and ``completed_by`` say when it reached done and
who took it there. Every writer that changes a task's status asks this module
what else to write, against the status it read inside the same lock or
revision as the write, so the record moves only on a real transition:

- into done, it is stamped with the instant and the actor; with no known
  actor ``completed_by`` is cleared, so an earlier finisher never takes credit
  for a later completion;
- done to done is not a transition, so a second completion rewrites nothing;
- out of done to an open status, it is cleared, so a reopened task does not
  keep a finisher it no longer has;
- done to archived keeps it, since archiving does not undo the work.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

DONE = "done"
ARCHIVED = "archived"


def status_text(value: object) -> str:
    return str(getattr(value, "value", value) or "").strip().lower()


def completion_updates(
    current_status: object,
    next_status: object,
    *,
    actor_id: str | None,
    record: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """What a write moving a task from ``current_status`` to ``next_status`` adds.

    ``record=False`` marks a move into done that nobody on this server made
    (the migration mirroring its source): the record is cleared, so the done
    credits nobody. A reopen clears it either way.
    """
    current = status_text(current_status)
    target = status_text(next_status)
    if not target or target == current:
        return {}
    if target == DONE:
        if not record:
            return {"completed_at": None, "completed_by": None}
        # ISO text, as the graph stores it, so the update also broadcasts as is.
        return {
            "completed_at": (now or datetime.now(UTC)).isoformat(),
            "completed_by": actor_id or None,
        }
    if current == DONE and target != ARCHIVED:
        return {"completed_at": None, "completed_by": None}
    return {}


def carried_by_migration(metadata: Mapping[str, Any] | None) -> bool:
    """Whether ``sibyl migrate to-team`` carried this row from another server."""
    return bool((metadata or {}).get("migration"))


def status_edit_completion_updates(
    current_metadata: Mapping[str, Any] | None,
    current_status: object,
    next_status: object,
    *,
    actor_id: str | None,
    mirrors_source: bool = False,
) -> dict[str, Any]:
    """Completion fields for a plain status edit (task PATCH, MCP update_task).

    ``mirrors_source`` marks the one write that is not a completion: the
    migration keeping a carried task's status in step with its source. It is
    honoured only on a row the migration carried, and only for that write;
    every other edit of a carried task, from the CLI, MCP or the board,
    records its completion like any other task.
    """
    return completion_updates(
        current_status,
        next_status,
        actor_id=actor_id,
        record=not (mirrors_source and carried_by_migration(current_metadata)),
    )


__all__ = [
    "carried_by_migration",
    "completion_updates",
    "status_edit_completion_updates",
    "status_text",
]
