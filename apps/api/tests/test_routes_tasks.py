"""Tests for task workflow routes."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from sibyl.api.routes.tasks import CompleteTaskRequest, complete_task, list_notes


class TestCompleteTaskRoute:
    @pytest.mark.asyncio
    async def test_complete_task_enqueues_episode_and_procedure_jobs(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        auth = SimpleNamespace(ctx=SimpleNamespace(), session=AsyncMock())
        request = CompleteTaskRequest(actual_hours=2.5, learnings="Capture the pattern")
        completed_task = SimpleNamespace(
            id="task-123",
            name="Ship the thing",
            status=SimpleNamespace(value="done"),
            model_dump=MagicMock(return_value={"id": "task-123", "title": "Ship the thing"}),
        )
        runtime = SimpleNamespace(
            client=object(),
            entity_manager=MagicMock(),
            relationship_manager=MagicMock(),
        )
        workflow = SimpleNamespace(
            complete_task=AsyncMock(return_value=completed_task),
        )
        episode_enqueue = AsyncMock(return_value="learning_episode:task-123")
        procedure_enqueue = AsyncMock(return_value="learning_procedure:task-123")

        @asynccontextmanager
        async def locked_entity(*_args, **_kwargs):
            yield object()

        with (
            patch("sibyl.api.routes.tasks._verify_task_access", AsyncMock()),
            patch("sibyl.api.routes.tasks.entity_lock", locked_entity),
            patch("sibyl.api.routes.tasks.get_task_graph_runtime", AsyncMock(return_value=runtime)),
            patch("sibyl.api.routes.tasks.TaskWorkflowEngine", return_value=workflow),
            patch("sibyl.jobs.queue.enqueue_create_learning_episode", episode_enqueue),
            patch("sibyl.jobs.queue.enqueue_create_learning_procedure", procedure_enqueue),
            patch("sibyl.api.routes.tasks.broadcast_event", AsyncMock()),
        ):
            response = await complete_task(
                "task-123",
                org=org,
                auth=auth,
                request=request,
            )

        workflow.complete_task.assert_awaited_once_with(
            "task-123", 2.5, "Capture the pattern", create_episode=False
        )
        episode_enqueue.assert_awaited_once_with(
            {"id": "task-123", "title": "Ship the thing"},
            str(org.id),
        )
        procedure_enqueue.assert_awaited_once_with(
            {"id": "task-123", "title": "Ship the thing"},
            str(org.id),
        )
        assert response.action == "complete_task"
        assert response.data["status"] == "done"


class TestListNotesRoute:
    @pytest.mark.asyncio
    async def test_list_notes_reuses_access_guard_instead_of_reloading_task(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        auth = SimpleNamespace(ctx=SimpleNamespace(), session=AsyncMock())
        manager = MagicMock()
        manager.get = AsyncMock()
        manager.get_notes_for_task = AsyncMock(return_value=[])
        runtime = SimpleNamespace(entity_manager=manager)

        with (
            patch(
                "sibyl.api.routes.tasks._verify_task_access",
                AsyncMock(return_value=SimpleNamespace(id="task-123")),
            ),
            patch("sibyl.api.routes.tasks.get_task_graph_runtime", AsyncMock(return_value=runtime)),
        ):
            response = await list_notes("task-123", org=org, auth=auth)

        assert response.count == 0
        manager.get.assert_not_awaited()
        manager.get_notes_for_task.assert_awaited_once_with("task-123", limit=50)
