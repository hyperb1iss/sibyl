"""Tests for the unified work-item transition service (audit H8).

These pin the contract both REST and MCP now share: a transition takes the
entity lock, performs the domain change through the core engine (tasks) or a
direct status write (epics), bumps project activity, and broadcasts — and is
serialized per work item by the lock.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sibyl.services import work_item_workflow as wiw
from sibyl.services.work_item_workflow import (
    WorkItemAction,
    transition_work_item,
)
from sibyl_core.models.entities import EntityType

GROUP_ID = "00000000-0000-0000-0000-0000000000aa"


def _task(*, status: str = "doing", name: str = "Ship it", **fields: object) -> SimpleNamespace:
    return SimpleNamespace(
        status=SimpleNamespace(value=status),
        name=name,
        branch_name=fields.get("branch_name"),
        pr_url=fields.get("pr_url"),
        model_dump=MagicMock(return_value={"id": "task-1", "status": status}),
    )


@asynccontextmanager
async def _granting_lock(*_args, **_kwargs):
    yield "lock-token"


class TestTaskTransition:
    @pytest.mark.asyncio
    async def test_start_task_locks_runs_engine_and_broadcasts(self) -> None:
        engine = SimpleNamespace(start_task=AsyncMock(return_value=_task(branch_name="feature/x")))
        runtime = SimpleNamespace(
            client=object(), entity_manager=MagicMock(), relationship_manager=MagicMock()
        )
        lock = MagicMock(side_effect=_granting_lock)
        broadcast = AsyncMock()

        with (
            patch.object(wiw, "entity_lock", lock),
            patch.object(wiw, "get_task_graph_runtime", AsyncMock(return_value=runtime)),
            patch.object(wiw, "TaskWorkflowEngine", return_value=engine),
            patch.object(wiw, "broadcast_event", broadcast),
        ):
            result = await transition_work_item(
                GROUP_ID, "task-1", WorkItemAction.START_TASK, payload={"assignee": "nova"}
            )

        # Lock taken for this exact work item.
        lock.assert_called_once()
        assert lock.call_args.args[0] == GROUP_ID
        assert lock.call_args.args[1] == "task-1"
        # Domain transition delegated to the core engine.
        engine.start_task.assert_awaited_once_with("task-1", "nova")
        # Broadcast fired with the canonical task payload.
        broadcast.assert_awaited_once()
        event_payload = broadcast.await_args.args[1]
        assert event_payload["id"] == "task-1"
        assert event_payload["entity_type"] == "task"
        assert event_payload["action"] == "start_task"
        assert event_payload["status"] == "doing"
        assert event_payload["branch_name"] == "feature/x"

        assert result.entity_type == EntityType.TASK
        assert result.response_data == {"status": "doing", "branch_name": "feature/x"}
        assert result.task_data == {"id": "task-1", "status": "doing"}

    @pytest.mark.asyncio
    async def test_block_task_response_keeps_reason_broadcast_keeps_blocker(self) -> None:
        engine = SimpleNamespace(block_task=AsyncMock(return_value=_task(status="blocked")))
        runtime = SimpleNamespace(
            client=object(), entity_manager=MagicMock(), relationship_manager=MagicMock()
        )
        broadcast = AsyncMock()

        with (
            patch.object(wiw, "entity_lock", MagicMock(side_effect=_granting_lock)),
            patch.object(wiw, "get_task_graph_runtime", AsyncMock(return_value=runtime)),
            patch.object(wiw, "TaskWorkflowEngine", return_value=engine),
            patch.object(wiw, "broadcast_event", broadcast),
        ):
            result = await transition_work_item(
                GROUP_ID, "task-1", WorkItemAction.BLOCK_TASK, payload={"reason": "waiting"}
            )

        engine.block_task.assert_awaited_once_with("task-1", "waiting")
        # REST contract divergence preserved: response carries reason, wire carries blocker.
        assert result.response_data == {"status": "blocked", "reason": "waiting"}
        assert broadcast.await_args.args[1]["blocker"] == "waiting"
        assert "reason" not in broadcast.await_args.args[1]

    @pytest.mark.asyncio
    async def test_broadcast_false_skips_event(self) -> None:
        engine = SimpleNamespace(unblock_task=AsyncMock(return_value=_task()))
        runtime = SimpleNamespace(
            client=object(), entity_manager=MagicMock(), relationship_manager=MagicMock()
        )
        broadcast = AsyncMock()

        with (
            patch.object(wiw, "entity_lock", MagicMock(side_effect=_granting_lock)),
            patch.object(wiw, "get_task_graph_runtime", AsyncMock(return_value=runtime)),
            patch.object(wiw, "TaskWorkflowEngine", return_value=engine),
            patch.object(wiw, "broadcast_event", broadcast),
        ):
            await transition_work_item(
                GROUP_ID, "task-1", WorkItemAction.UNBLOCK_TASK, broadcast=False
            )

        broadcast.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_domain_transition_runs_inside_the_lock(self) -> None:
        """The engine call must happen while the lock is held, not after."""
        events: list[str] = []

        @asynccontextmanager
        async def recording_lock(*_args, **_kwargs):
            events.append("lock_acquire")
            try:
                yield "token"
            finally:
                events.append("lock_release")

        async def record_start(*_args, **_kwargs):
            events.append("engine_start_task")
            return _task()

        engine = SimpleNamespace(start_task=record_start)
        runtime = SimpleNamespace(
            client=object(), entity_manager=MagicMock(), relationship_manager=MagicMock()
        )

        with (
            patch.object(wiw, "entity_lock", recording_lock),
            patch.object(wiw, "get_task_graph_runtime", AsyncMock(return_value=runtime)),
            patch.object(wiw, "TaskWorkflowEngine", return_value=engine),
            patch.object(wiw, "broadcast_event", AsyncMock()),
        ):
            await transition_work_item(GROUP_ID, "task-1", WorkItemAction.START_TASK)

        assert events == ["lock_acquire", "engine_start_task", "lock_release"]


class TestEpicTransition:
    @pytest.mark.asyncio
    async def test_complete_epic_writes_status_bumps_activity_and_broadcasts(self) -> None:
        epic = SimpleNamespace(id="epic-1", name="Epic Nova", metadata={"project_id": "proj-9"})
        update_entity = AsyncMock()
        broadcast = AsyncMock()

        with (
            patch.object(wiw, "entity_lock", MagicMock(side_effect=_granting_lock)),
            patch.object(wiw, "update_graph_entity", update_entity),
            patch.object(wiw, "broadcast_event", broadcast),
        ):
            result = await transition_work_item(
                GROUP_ID,
                "epic-1",
                WorkItemAction.COMPLETE_EPIC,
                payload={"learnings": "thin seam"},
                entity=epic,
            )

        # Status write + completed_date + learnings, then a project-activity bump.
        status_write = update_entity.await_args_list[0]
        assert status_write.args[:2] == (GROUP_ID, "epic-1")
        assert status_write.args[2]["status"] == "completed"
        assert status_write.args[2]["learnings"] == "thin seam"
        assert "completed_date" in status_write.args[2]

        activity_write = update_entity.await_args_list[1]
        assert activity_write.args[:2] == (GROUP_ID, "proj-9")
        assert "last_activity_at" in activity_write.args[2]

        broadcast.assert_awaited_once()
        assert broadcast.await_args.args[1]["entity_type"] == "epic"
        assert broadcast.await_args.args[1]["status"] == "completed"
        assert result.response_data == {"status": "completed", "learnings": "thin seam"}

    @pytest.mark.asyncio
    async def test_archive_epic_without_project_skips_activity_bump(self) -> None:
        epic = SimpleNamespace(id="epic-1", name="Epic Nova", metadata={})
        update_entity = AsyncMock()

        with (
            patch.object(wiw, "entity_lock", MagicMock(side_effect=_granting_lock)),
            patch.object(wiw, "update_graph_entity", update_entity),
            patch.object(wiw, "broadcast_event", AsyncMock()),
        ):
            result = await transition_work_item(
                GROUP_ID, "epic-1", WorkItemAction.ARCHIVE_EPIC, entity=epic
            )

        # Only the status write; no project to touch.
        update_entity.assert_awaited_once()
        assert update_entity.await_args.args[2] == {"status": "archived"}
        assert result.status == "archived"


class TestLockSerialization:
    @pytest.mark.asyncio
    async def test_concurrent_transitions_on_same_item_are_serialized(self) -> None:
        """Two overlapping transitions on one work item must not run their
        domain bodies concurrently — the real in-process lock serializes them."""
        concurrency = 0
        peak = 0

        async def slow_start(*_args, **_kwargs):
            nonlocal concurrency, peak
            concurrency += 1
            peak = max(peak, concurrency)
            await asyncio.sleep(0.02)
            concurrency -= 1
            return _task()

        engine = SimpleNamespace(start_task=slow_start)
        runtime = SimpleNamespace(
            client=object(), entity_manager=MagicMock(), relationship_manager=MagicMock()
        )

        # Real entity_lock (local backend), real TaskWorkflowEngine swapped for the stub.
        with (
            patch.object(wiw, "get_task_graph_runtime", AsyncMock(return_value=runtime)),
            patch.object(wiw, "TaskWorkflowEngine", return_value=engine),
            patch.object(wiw, "broadcast_event", AsyncMock()),
        ):
            await asyncio.gather(
                transition_work_item(GROUP_ID, "task-shared", WorkItemAction.START_TASK),
                transition_work_item(GROUP_ID, "task-shared", WorkItemAction.START_TASK),
            )

        assert peak == 1, "lock should prevent overlapping domain transitions"

    @pytest.mark.asyncio
    async def test_distinct_items_transition_in_parallel(self) -> None:
        """Different work items hold different locks, so they may overlap."""
        concurrency = 0
        peak = 0
        both_in = asyncio.Event()

        async def slow_start(*_args, **_kwargs):
            nonlocal concurrency, peak
            concurrency += 1
            peak = max(peak, concurrency)
            if concurrency == 2:
                both_in.set()
            await asyncio.wait_for(both_in.wait(), timeout=0.5)
            concurrency -= 1
            return _task()

        engine = SimpleNamespace(start_task=slow_start)
        runtime = SimpleNamespace(
            client=object(), entity_manager=MagicMock(), relationship_manager=MagicMock()
        )

        with (
            patch.object(wiw, "get_task_graph_runtime", AsyncMock(return_value=runtime)),
            patch.object(wiw, "TaskWorkflowEngine", return_value=engine),
            patch.object(wiw, "broadcast_event", AsyncMock()),
        ):
            await asyncio.gather(
                transition_work_item(GROUP_ID, "task-a", WorkItemAction.START_TASK),
                transition_work_item(GROUP_ID, "task-b", WorkItemAction.START_TASK),
            )

        assert peak == 2, "distinct work items must not serialize against each other"
