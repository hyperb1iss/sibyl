"""The completion record a task status change keeps true."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sibyl_core.tasks.completion import completion_updates, status_edit_completion_updates

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
CARRIED = {"status": "doing", "migration": {"tool": "sibyl migrate to-team"}}


def test_a_move_into_done_stamps_the_instant_and_the_actor() -> None:
    assert completion_updates("doing", "done", actor_id="bob", now=NOW) == {
        "completed_at": NOW.isoformat(),
        "completed_by": "bob",
    }


@pytest.mark.parametrize("previous", ["archived", "blocked", "todo"])
def test_an_actorless_move_into_done_clears_any_earlier_finisher(previous: str) -> None:
    """A stale completed_by left by an old completion must not take the new one."""
    updates = completion_updates(previous, "done", actor_id=None, now=NOW)
    assert updates == {"completed_at": NOW.isoformat(), "completed_by": None}


def test_done_to_done_rewrites_nothing() -> None:
    assert completion_updates("done", "done", actor_id="bob") == {}


def test_a_reopen_clears_and_an_archive_keeps() -> None:
    assert completion_updates("done", "doing", actor_id="bob") == {
        "completed_at": None,
        "completed_by": None,
    }
    assert completion_updates("done", "archived", actor_id="bob") == {}


def test_a_later_edit_of_a_carried_task_records_its_completion() -> None:
    """The row's migration stamp is forever; only the migration's own write is marked."""
    updates = status_edit_completion_updates(CARRIED, "doing", "done", actor_id="bob")
    assert updates["completed_by"] == "bob"
    assert updates["completed_at"]


def test_the_migrations_own_mirror_write_credits_nobody() -> None:
    updates = status_edit_completion_updates(
        CARRIED, "doing", "done", actor_id="migrator", mirrors_source=True
    )
    assert updates == {"completed_at": None, "completed_by": None}


def test_the_mirror_mark_is_ignored_on_a_task_the_migration_did_not_carry() -> None:
    updates = status_edit_completion_updates(
        {"status": "doing"}, "doing", "done", actor_id="bob", mirrors_source=True
    )
    assert updates["completed_by"] == "bob"
    assert updates["completed_at"]
