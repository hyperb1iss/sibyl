"""Tests for task workflow routes."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException

from sibyl.api.routes.tasks import (
    CompleteTaskRequest,
    CreateTaskRequest,
    complete_task,
    create_task,
    list_notes,
)
from sibyl_core.auth import MemoryPolicyContext, OrganizationRole, ProjectRole


@pytest.mark.asyncio
async def test_create_task_requires_registered_project_before_runtime() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    user = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000222"))
    auth = SimpleNamespace()
    verify_access = AsyncMock(
        side_effect=HTTPException(status_code=404, detail="Project not found")
    )
    runtime = AsyncMock()

    with (
        patch("sibyl.api.routes.tasks.verify_entity_project_access", verify_access),
        patch("sibyl.api.routes.tasks.get_task_graph_runtime", runtime),
        pytest.raises(HTTPException) as exc,
    ):
        await create_task(
            request=CreateTaskRequest(title="Scoped task", project_id="project_missing"),
            org=org,
            user=user,
            auth=auth,
        )

    assert exc.value.status_code == 404
    verify_access.assert_awaited_once_with(
        None,
        auth,
        "project_missing",
        required_role=ProjectRole.CONTRIBUTOR,
        require_existing_project=True,
    )
    runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_task_writes_relationships_concurrently() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    user = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000222"))
    auth = SimpleNamespace()
    all_started = asyncio.Event()
    started: list[str] = []
    completed: list[str] = []

    async def create_relationship(relationship):
        started.append(relationship.target_id)
        if len(started) == 3:
            all_started.set()
        await asyncio.wait_for(all_started.wait(), timeout=0.5)
        completed.append(relationship.target_id)
        return relationship.id

    runtime = SimpleNamespace(
        entity_manager=SimpleNamespace(create_direct=AsyncMock(return_value="task-123")),
        relationship_manager=SimpleNamespace(create=create_relationship),
    )

    with (
        patch("sibyl.api.routes.tasks.verify_entity_project_access", AsyncMock()),
        patch("sibyl.api.routes.tasks.get_task_graph_runtime", AsyncMock(return_value=runtime)),
        patch("sibyl.api.routes.tasks.broadcast_event", AsyncMock()),
    ):
        response = await create_task(
            request=CreateTaskRequest(
                title="Parallel task",
                project_id="project-1",
                depends_on=["task-a", "task-b"],
            ),
            org=org,
            user=user,
            auth=auth,
        )

    assert response.task_id == "task-123"
    assert started == ["project-1", "task-a", "task-b"]
    assert set(completed) == {"project-1", "task-a", "task-b"}


class TestCompleteTaskRoute:
    @pytest.mark.asyncio
    async def test_complete_task_enqueues_episode_and_procedure_jobs(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        auth = SimpleNamespace(
            to_memory_policy_context=MagicMock(
                side_effect=lambda **kwargs: MemoryPolicyContext(
                    actor_user_id="user-1",
                    organization_id=str(org.id),
                    organization_role=OrganizationRole.MEMBER,
                    **kwargs,
                )
            ),
        )
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
            patch(
                "sibyl.api.routes.tasks._verify_task_access",
                AsyncMock(return_value=SimpleNamespace(metadata={}, project_id="proj-1")),
            ),
            patch(
                "sibyl.api.routes.tasks.list_accessible_project_graph_ids",
                AsyncMock(return_value={"proj-1", "proj-2"}),
            ),
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
        auth.to_memory_policy_context.assert_called_once_with(
            memory_space="project",
            scope_key="proj-1",
            project_id="proj-1",
            accessible_projects={"proj-1", "proj-2"},
            source_surface="task_learning_job",
        )
        episode_enqueue.assert_awaited_once_with(
            {"id": "task-123", "title": "Ship the thing"},
            str(org.id),
            policy_context={
                "actor_user_id": "user-1",
                "organization_id": str(org.id),
                "organization_role": "member",
                "accessible_projects": ["proj-1", "proj-2"],
                "accessible_delegations": None,
                "delegated_authority": None,
                "agent_id": None,
                "project_id": "proj-1",
                "memory_space": "project",
                "scope_key": "proj-1",
                "source_surface": "task_learning_job",
            },
        )
        procedure_enqueue.assert_awaited_once_with(
            {"id": "task-123", "title": "Ship the thing"},
            str(org.id),
            policy_context={
                "actor_user_id": "user-1",
                "organization_id": str(org.id),
                "organization_role": "member",
                "accessible_projects": ["proj-1", "proj-2"],
                "accessible_delegations": None,
                "delegated_authority": None,
                "agent_id": None,
                "project_id": "proj-1",
                "memory_space": "project",
                "scope_key": "proj-1",
                "source_surface": "task_learning_job",
            },
        )
        assert response.action == "complete_task"
        assert response.data["status"] == "done"


class TestListNotesRoute:
    @pytest.mark.asyncio
    async def test_list_notes_reuses_access_guard_instead_of_reloading_task(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        auth = SimpleNamespace()
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
