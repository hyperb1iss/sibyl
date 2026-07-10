"""Tests for task workflow routes."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException

from sibyl.api.idempotency import idempotency_request_hash, replay_idempotent_response
from sibyl.api.routes.tasks import (
    CompleteTaskRequest,
    CreateNoteRequest,
    CreateTaskRequest,
    TaskActionResponse,
    UpdateTaskRequest,
    complete_task,
    create_note,
    create_task,
    list_notes,
    update_task,
)
from sibyl.persistence.content_common import ApiIdempotencyRecord
from sibyl.services.work_item_workflow import WorkItemAction, WorkItemTransition
from sibyl_core.auth import MemoryPolicyContext, OrganizationRole, ProjectRole
from sibyl_core.models.entities import EntityType


def _request(*, idempotency_key: str | None = None) -> MagicMock:
    request = MagicMock()
    request.headers = {}
    if idempotency_key:
        request.headers["Idempotency-Key"] = idempotency_key
    return request


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
            http_request=_request(),
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
            http_request=_request(),
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


@pytest.mark.asyncio
async def test_create_task_returns_404_when_native_epic_lookup_misses() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    user = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000222"))
    auth = SimpleNamespace()
    runtime = SimpleNamespace(
        entity_manager=SimpleNamespace(
            get=AsyncMock(side_effect=KeyError("epic-missing")),
            create_direct=AsyncMock(),
        ),
        relationship_manager=SimpleNamespace(create=AsyncMock()),
    )

    with (
        patch("sibyl.api.routes.tasks.verify_entity_project_access", AsyncMock()),
        patch("sibyl.api.routes.tasks.get_task_graph_runtime", AsyncMock(return_value=runtime)),
        pytest.raises(HTTPException) as exc,
    ):
        await create_task(
            http_request=_request(),
            request=CreateTaskRequest(
                title="Missing epic task",
                project_id="project-1",
                epic_id="epic-missing",
            ),
            org=org,
            user=user,
            auth=auth,
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "Epic not found: epic-missing"
    runtime.entity_manager.create_direct.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_task_replays_saved_idempotent_response() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    user = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000222"))
    auth = SimpleNamespace()
    request = CreateTaskRequest(title="Replay task", project_id="project-1")
    payload = {"body": request.model_dump(mode="json")}
    record = ApiIdempotencyRecord(
        organization_id=org.id,
        principal_id=str(user.id),
        idempotency_key="idem-task",
        method="POST",
        path="/tasks",
        request_hash=idempotency_request_hash(payload),
        response_status_code=200,
        response_body={
            "success": True,
            "action": "create",
            "task_id": "task_saved",
            "message": "Task created successfully",
            "data": {"project_id": "project-1"},
        },
    )

    with (
        patch("sibyl.api.routes.tasks.verify_entity_project_access", AsyncMock()),
        patch(
            "sibyl.api.idempotency.content_runtime.get_api_idempotency_record",
            AsyncMock(return_value=record),
        ),
        patch("sibyl.api.routes.tasks.get_task_graph_runtime", AsyncMock()) as runtime,
    ):
        response = await create_task(
            http_request=_request(idempotency_key="idem-task"),
            request=request,
            org=org,
            user=user,
            auth=auth,
        )

    assert response.task_id == "task_saved"
    runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_task_rejects_idempotency_key_payload_mismatch() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    user = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000222"))
    auth = SimpleNamespace()
    record = ApiIdempotencyRecord(
        organization_id=org.id,
        principal_id=str(user.id),
        idempotency_key="idem-task",
        method="POST",
        path="/tasks",
        request_hash="different-request",
        response_status_code=200,
        response_body={
            "success": True,
            "action": "create",
            "task_id": "task_saved",
            "message": "Task created successfully",
        },
    )

    with (
        patch("sibyl.api.routes.tasks.verify_entity_project_access", AsyncMock()),
        patch(
            "sibyl.api.idempotency.content_runtime.get_api_idempotency_record",
            AsyncMock(return_value=record),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await create_task(
            http_request=_request(idempotency_key="idem-task"),
            request=CreateTaskRequest(title="Different task", project_id="project-1"),
            org=org,
            user=user,
            auth=auth,
        )

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_idempotency_replay_marks_mutation_receipt() -> None:
    org_id = UUID("00000000-0000-0000-0000-000000000111")
    payload = {"body": {"expected_revision": 1}}
    record = ApiIdempotencyRecord(
        organization_id=org_id,
        principal_id="user-1",
        idempotency_key="idem-replay",
        method="POST",
        path="/tasks/task-1/start",
        request_hash=idempotency_request_hash(payload),
        response_status_code=200,
        response_body={
            "success": True,
            "action": "start_task",
            "task_id": "task-1",
            "message": "Task started",
            "data": {"status": "doing", "revision": 2},
            "mutation_receipt": {
                "operation_id": "idem-replay",
                "applied": True,
                "revision": 2,
                "affected_records": ["entity:task-1"],
                "idempotency_key": "idem-replay",
                "replayed": False,
            },
        },
    )

    with patch(
        "sibyl.api.idempotency.content_runtime.get_api_idempotency_record",
        AsyncMock(return_value=record),
    ):
        response = await replay_idempotent_response(
            _request(idempotency_key="idem-replay"),
            organization_id=org_id,
            principal_id="user-1",
            method="POST",
            path="/tasks/task-1/start",
            payload=payload,
            response_model=TaskActionResponse,
            content_session=None,
        )

    assert response is not None
    assert response.mutation_receipt is not None
    assert response.mutation_receipt.replayed is True


@pytest.mark.asyncio
async def test_update_task_sync_allows_title_description_with_actor_attribution() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    user = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000222"))
    auth = SimpleNamespace()
    updated = SimpleNamespace(name="Renamed task", metadata={})
    entity_manager = SimpleNamespace(update=AsyncMock(return_value=updated))
    runtime = SimpleNamespace(entity_manager=entity_manager, relationship_manager=None)

    with (
        patch("sibyl.api.routes.tasks._verify_task_access", AsyncMock()),
        patch("sibyl.api.routes.tasks.get_task_graph_runtime", AsyncMock(return_value=runtime)),
        patch("sibyl.api.routes.tasks.broadcast_event", AsyncMock()),
    ):
        response = await update_task(
            task_id="task-123",
            request=UpdateTaskRequest(title="Renamed task", description="Updated body"),
            sync=True,
            org=org,
            user=user,
            auth=auth,
        )

    assert response.success is True
    entity_manager.update.assert_awaited_once_with(
        "task-123",
        {
            "modified_by": str(user.id),
            "name": "Renamed task",
            "description": "Updated body",
        },
    )


@pytest.mark.asyncio
async def test_update_task_async_allows_dependency_only_update() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    user = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000222"))
    auth = SimpleNamespace()
    enqueue = AsyncMock(return_value="job-123")

    with (
        patch("sibyl.api.routes.tasks._verify_task_access", AsyncMock()),
        patch("sibyl.jobs.queue.enqueue_update_task", enqueue),
    ):
        response = await update_task(
            task_id="task-123",
            request=UpdateTaskRequest(add_depends_on=["task-dep"]),
            sync=False,
            org=org,
            user=user,
            auth=auth,
        )

    assert response.success is True
    enqueue.assert_awaited_once_with(
        "task-123",
        {"modified_by": str(user.id)},
        str(org.id),
        epic_id=None,
        new_status=None,
        add_depends_on=["task-dep"],
        remove_depends_on=[],
    )


@pytest.mark.asyncio
async def test_create_task_saves_idempotent_response_after_success() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    user = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000222"))
    auth = SimpleNamespace()
    runtime = SimpleNamespace(
        entity_manager=SimpleNamespace(create_direct=AsyncMock(return_value="task-123")),
        relationship_manager=SimpleNamespace(create=AsyncMock(return_value="rel-123")),
    )
    save_record = AsyncMock(side_effect=lambda _session, *, record: record)

    with (
        patch("sibyl.api.routes.tasks.verify_entity_project_access", AsyncMock()),
        patch(
            "sibyl.api.idempotency.content_runtime.get_api_idempotency_record",
            AsyncMock(return_value=None),
        ),
        patch(
            "sibyl.api.idempotency.content_runtime.save_api_idempotency_record",
            save_record,
        ),
        patch("sibyl.api.routes.tasks.get_task_graph_runtime", AsyncMock(return_value=runtime)),
        patch("sibyl.api.routes.tasks.broadcast_event", AsyncMock()),
    ):
        response = await create_task(
            http_request=_request(idempotency_key="idem-task"),
            request=CreateTaskRequest(title="Saved task", project_id="project-1"),
            org=org,
            user=user,
            auth=auth,
        )

    assert response.task_id == "task-123"
    assert save_record.await_count == 2
    saved = save_record.await_args.kwargs["record"]
    assert saved.organization_id == org.id
    assert saved.principal_id == str(user.id)
    assert saved.idempotency_key == "idem-task"
    assert saved.method == "POST"
    assert saved.path == "/tasks"
    assert saved.response_body["task_id"] == "task-123"


class TestCompleteTaskRoute:
    @pytest.mark.asyncio
    async def test_complete_task_enqueues_episode_and_procedure_jobs(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        auth = SimpleNamespace(
            user_id="user-1",
            to_memory_policy_context=MagicMock(
                side_effect=lambda **kwargs: MemoryPolicyContext(
                    actor_user_id="user-1",
                    organization_id=str(org.id),
                    organization_role=OrganizationRole.MEMBER,
                    **kwargs,
                )
            ),
        )
        request = CompleteTaskRequest(
            actual_hours=2.5,
            learnings="Capture the pattern",
            expected_revision=1,
        )
        transition_result = WorkItemTransition(
            action=WorkItemAction.COMPLETE_TASK,
            item_id="task-123",
            entity_type=EntityType.TASK,
            status="done",
            name="Ship the thing",
            revision=2,
            fields={"learnings": "Capture the pattern"},
            task_data={"id": "task-123", "title": "Ship the thing"},
        )
        transition = AsyncMock(return_value=transition_result)
        episode_enqueue = AsyncMock(return_value="learning_episode:task-123")
        procedure_enqueue = AsyncMock(return_value="learning_procedure:task-123")
        save_record = AsyncMock(side_effect=lambda _session, *, record: record)

        with (
            patch(
                "sibyl.api.routes.tasks._verify_task_access",
                AsyncMock(return_value=SimpleNamespace(metadata={}, project_id="proj-1")),
            ),
            patch(
                "sibyl.api.routes.tasks.list_accessible_project_graph_ids",
                AsyncMock(return_value={"proj-1", "proj-2"}),
            ),
            patch("sibyl.api.routes.tasks.transition_work_item", transition),
            patch("sibyl.jobs.queue.enqueue_create_learning_episode", episode_enqueue),
            patch("sibyl.jobs.queue.enqueue_create_learning_procedure", procedure_enqueue),
            patch(
                "sibyl.api.idempotency.content_runtime.get_api_idempotency_record",
                AsyncMock(return_value=None),
            ),
            patch(
                "sibyl.api.idempotency.content_runtime.save_api_idempotency_record",
                save_record,
            ),
        ):
            response = await complete_task(
                "task-123",
                http_request=_request(idempotency_key="idem-complete"),
                org=org,
                auth=auth,
                request=request,
            )

        transition.assert_awaited_once_with(
            str(org.id),
            "task-123",
            WorkItemAction.COMPLETE_TASK,
            payload={"actual_hours": 2.5, "learnings": "Capture the pattern"},
            expected_revision=1,
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
        assert response.mutation_receipt is not None
        assert response.mutation_receipt.applied is True
        assert response.mutation_receipt.revision == 2
        assert response.mutation_receipt.idempotency_key == "idem-complete"
        assert response.mutation_receipt.affected_records == ["entity:task-123"]
        assert save_record.await_count == 2
        saved = save_record.await_args.kwargs["record"]
        assert saved.path == "/tasks/task-123/complete"
        assert saved.response_body["mutation_receipt"]["revision"] == 2
        assert saved.response_body["action"] == "complete_task"

    @pytest.mark.asyncio
    async def test_complete_task_records_cited_memories(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        auth = SimpleNamespace(
            user_id="user-1",
            to_memory_policy_context=MagicMock(
                side_effect=lambda **kwargs: MemoryPolicyContext(
                    actor_user_id="user-1",
                    organization_id=str(org.id),
                    organization_role=OrganizationRole.MEMBER,
                    **kwargs,
                )
            ),
        )
        request = CompleteTaskRequest(cited_ids=["decision-1", "raw_memory:raw-1"])
        transition_result = WorkItemTransition(
            action=WorkItemAction.COMPLETE_TASK,
            item_id="task-123",
            entity_type=EntityType.TASK,
            status="done",
            name="Ship the thing",
            fields={},
            task_data={"id": "task-123", "title": "Ship the thing", "project_id": "proj-1"},
        )
        transition = AsyncMock(return_value=transition_result)
        record_citations = AsyncMock(
            return_value={
                "cited_count": 2,
                "coverage_complete": True,
                "stamped_count": 2,
            }
        )

        with (
            patch(
                "sibyl.api.routes.tasks._verify_task_access",
                AsyncMock(return_value=SimpleNamespace(metadata={}, project_id="proj-1")),
            ),
            patch(
                "sibyl.api.routes.tasks.list_accessible_project_graph_ids",
                AsyncMock(return_value={"proj-1"}),
            ),
            patch("sibyl.api.routes.tasks.transition_work_item", transition),
            patch(
                "sibyl.api.idempotency.content_runtime.get_api_idempotency_record",
                AsyncMock(return_value=None),
            ),
            patch(
                "sibyl.api.idempotency.content_runtime.save_api_idempotency_record",
                AsyncMock(),
            ),
            patch(
                "sibyl_core.tools.usage_citation.record_cited_item_usages",
                record_citations,
            ),
        ):
            response = await complete_task(
                "task-123",
                http_request=_request(),
                org=org,
                auth=auth,
                request=request,
            )

        record_citations.assert_awaited_once_with(
            ["decision-1", "raw_memory:raw-1"],
            organization_id=str(org.id),
            principal_id="user-1",
            project_id="proj-1",
            source_surface="task_complete",
            request_metadata={
                "actual_hours": None,
                "has_learnings": False,
                "task_id": "task-123",
            },
        )
        assert response.data["citation_usage"]["stamped_count"] == 2

    @pytest.mark.asyncio
    async def test_complete_task_replays_saved_idempotent_response(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        auth = SimpleNamespace(user_id="user-1")
        request = CompleteTaskRequest(actual_hours=2.5, learnings="Already captured")
        payload = {"body": request.model_dump(mode="json")}
        record = ApiIdempotencyRecord(
            organization_id=org.id,
            principal_id="user-1",
            idempotency_key="idem-complete",
            method="POST",
            path="/tasks/task-123/complete",
            request_hash=idempotency_request_hash(payload),
            response_status_code=200,
            response_body={
                "success": True,
                "action": "complete_task",
                "task_id": "task-123",
                "message": "Task completed with learnings captured",
                "data": {"status": "done", "learnings": "Already captured"},
            },
        )

        with (
            patch(
                "sibyl.api.routes.tasks._verify_task_access",
                AsyncMock(return_value=SimpleNamespace(metadata={}, project_id="proj-1")),
            ),
            patch(
                "sibyl.api.idempotency.content_runtime.get_api_idempotency_record",
                AsyncMock(return_value=record),
            ),
            patch("sibyl.api.routes.tasks.get_task_graph_runtime", AsyncMock()) as runtime,
        ):
            response = await complete_task(
                "task-123",
                http_request=_request(idempotency_key="idem-complete"),
                org=org,
                auth=auth,
                request=request,
            )

        assert response.task_id == "task-123"
        assert response.message == "Task completed with learnings captured"
        runtime.assert_not_awaited()


class TestNotesRoute:
    @pytest.mark.asyncio
    async def test_create_note_replays_saved_idempotent_response(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        user = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000222"))
        auth = SimpleNamespace()
        request = CreateNoteRequest(content="Already saved")
        payload = {"body": request.model_dump(mode="json")}
        record = ApiIdempotencyRecord(
            organization_id=org.id,
            principal_id=str(user.id),
            idempotency_key="idem-note",
            method="POST",
            path="/tasks/task-123/notes",
            request_hash=idempotency_request_hash(payload),
            response_status_code=200,
            response_body={
                "id": "note_saved",
                "task_id": "task-123",
                "content": "Already saved",
                "author_type": "user",
                "author_name": "",
                "created_at": "2026-05-15T00:00:00+00:00",
                "status": None,
            },
        )

        with (
            patch(
                "sibyl.api.routes.tasks._verify_task_access",
                AsyncMock(return_value=SimpleNamespace(metadata={}, project_id="proj-1")),
            ),
            patch(
                "sibyl.api.idempotency.content_runtime.get_api_idempotency_record",
                AsyncMock(return_value=record),
            ),
            patch("sibyl.api.routes.tasks.get_task_graph_runtime", AsyncMock()) as runtime,
        ):
            response = await create_note(
                "task-123",
                http_request=_request(idempotency_key="idem-note"),
                request=request,
                org=org,
                user=user,
                auth=auth,
            )

        assert response.id == "note_saved"
        assert response.content == "Already saved"
        runtime.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_note_saves_idempotent_response_after_success(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        user = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000222"))
        auth = SimpleNamespace()
        manager = SimpleNamespace(
            get=AsyncMock(return_value=SimpleNamespace(id="task-123")),
            create_direct=AsyncMock(return_value="note-123"),
        )
        runtime = SimpleNamespace(
            entity_manager=manager,
            relationship_manager=SimpleNamespace(create=AsyncMock(return_value="rel-123")),
        )
        save_record = AsyncMock(side_effect=lambda _session, *, record: record)

        with (
            patch(
                "sibyl.api.routes.tasks._verify_task_access",
                AsyncMock(return_value=SimpleNamespace(metadata={}, project_id="proj-1")),
            ),
            patch("sibyl.jobs.pending.is_pending", AsyncMock(return_value=False)),
            patch("sibyl.api.routes.tasks.get_task_graph_runtime", AsyncMock(return_value=runtime)),
            patch(
                "sibyl.api.idempotency.content_runtime.get_api_idempotency_record",
                AsyncMock(return_value=None),
            ),
            patch(
                "sibyl.api.idempotency.content_runtime.save_api_idempotency_record",
                save_record,
            ),
            patch("sibyl.api.routes.tasks.broadcast_event", AsyncMock()),
        ):
            response = await create_note(
                "task-123",
                http_request=_request(idempotency_key="idem-note"),
                request=CreateNoteRequest(content="Save me"),
                org=org,
                user=user,
                auth=auth,
            )

        assert response.task_id == "task-123"
        assert response.content == "Save me"
        manager.create_direct.assert_awaited_once()
        runtime.relationship_manager.create.assert_awaited_once()
        assert save_record.await_count == 2
        saved = save_record.await_args.kwargs["record"]
        assert saved.organization_id == org.id
        assert saved.principal_id == str(user.id)
        assert saved.idempotency_key == "idem-note"
        assert saved.path == "/tasks/task-123/notes"
        assert saved.response_body["content"] == "Save me"

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
