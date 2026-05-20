from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException

from sibyl.api.routes.entities import (
    create_entities_bulk,
    create_entity,
    delete_entity,
    update_entity,
)
from sibyl.api.schemas import EntityBulkCreateRequest, EntityCreate, EntityUpdate
from sibyl.auth.errors import ProjectAccessDeniedError
from sibyl_core.auth import ProjectRole
from sibyl_core.models.entities import EntityType


def _request() -> MagicMock:
    request = MagicMock()
    request.headers = {}
    request.cookies = {}
    request.client = SimpleNamespace(host="127.0.0.1")
    return request


def _org() -> SimpleNamespace:
    return SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(user=SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000222")))


def _project_entity(*, name: str, description: str) -> SimpleNamespace:
    return SimpleNamespace(
        id="project_new",
        entity_type=EntityType.PROJECT,
        name=name,
        description=description,
        content=description,
        category=None,
        languages=[],
        tags=[],
        metadata={},
        source_file=None,
        created_at=None,
        updated_at=None,
    )


@asynccontextmanager
async def _locked_entity(*_args, **_kwargs):
    yield "lock-token"


@pytest.mark.asyncio
async def test_create_project_routes_through_runtime_project_record() -> None:
    org = _org()
    ctx = _ctx()
    entity = EntityCreate(
        name="Surreal Native",
        description="cut postgres loose",
        entity_type=EntityType.PROJECT,
    )
    add_result = SimpleNamespace(success=True, id="project_new", message="ok")
    runtime = SimpleNamespace(
        entity_manager=SimpleNamespace(
            get=AsyncMock(
                return_value=_project_entity(name=entity.name, description=entity.description)
            )
        )
    )

    with (
        patch("sibyl_core.tools.core.add", AsyncMock(return_value=add_result)),
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime", AsyncMock(return_value=runtime)
        ),
        patch("sibyl.api.routes.entities.broadcast_event", AsyncMock()),
        patch("sibyl.api.routes.entities.create_project_record", AsyncMock()) as create_project,
        patch("sibyl.api.routes.entities.log_audit_event", AsyncMock()) as audit_log,
    ):
        response = await create_entity(
            request=_request(),
            entity=entity,
            org=org,
            ctx=ctx,
            content_session=None,
            sync=False,
        )

    assert response.id == "project_new"
    runtime.entity_manager.get.assert_not_awaited()
    create_project.assert_awaited_once_with(
        organization_id=org.id,
        owner_user_id=ctx.user.id,
        graph_project_id="project_new",
        name="Surreal Native",
        description="cut postgres loose",
    )
    audit_log.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_entities_bulk_uses_runtime_bulk_create() -> None:
    org = _org()
    ctx = _ctx()
    batch = EntityBulkCreateRequest(
        entities=[
            EntityCreate(
                name="Session one",
                content="semantic memory content",
                entity_type=EntityType.SESSION,
                skip_conflicts=True,
                metadata={"source": "import"},
            ),
            EntityCreate(
                name="Session two",
                content="more semantic memory content",
                entity_type=EntityType.SESSION,
                skip_conflicts=True,
                metadata={"source": "import"},
            ),
        ]
    )
    runtime = SimpleNamespace(
        entity_manager=SimpleNamespace(
            create_direct_bulk=AsyncMock(return_value=["session_one", "session_two"])
        ),
        relationship_manager=SimpleNamespace(create_bulk=AsyncMock(return_value=(0, 0))),
    )

    with patch(
        "sibyl.api.routes.entities.get_entity_graph_runtime",
        AsyncMock(return_value=runtime),
    ):
        response = await create_entities_bulk(
            batch=batch,
            org=org,
            ctx=ctx,
            content_session=None,
        )

    assert response.created == 2
    assert [entity.id for entity in response.entities] == ["session_one", "session_two"]
    runtime.entity_manager.create_direct_bulk.assert_awaited_once()
    call = runtime.entity_manager.create_direct_bulk.await_args
    assert len(call.args[0]) == 2
    assert call.kwargs == {"generate_embeddings": True}


@pytest.mark.asyncio
async def test_create_entities_bulk_enqueues_memory_projection() -> None:
    org = _org()
    ctx = _ctx()
    batch = EntityBulkCreateRequest(
        entities=[
            EntityCreate(
                name="Session one",
                content="I bought a Samsung TV for the den.",
                entity_type=EntityType.SESSION,
                skip_conflicts=True,
                metadata={"source": "import"},
            )
        ]
    )
    runtime = SimpleNamespace(
        entity_manager=SimpleNamespace(create_direct_bulk=AsyncMock(return_value=["session_one"])),
        relationship_manager=SimpleNamespace(create_bulk=AsyncMock(return_value=(0, 0))),
    )

    with (
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime",
            AsyncMock(return_value=runtime),
        ),
        patch("sibyl.jobs.queue.enqueue_memory_projection", AsyncMock()) as enqueue_projection,
    ):
        await create_entities_bulk(
            batch=batch,
            org=org,
            ctx=ctx,
            content_session=None,
        )

    enqueue_projection.assert_awaited_once()
    payload, group_id = enqueue_projection.await_args.args
    assert group_id == str(org.id)
    assert payload[0]["content"] == "I bought a Samsung TV for the den."
    assert enqueue_projection.await_args.kwargs == {"created_source_ids": ["session_one"]}


@pytest.mark.asyncio
async def test_create_entities_bulk_requires_explicit_conflict_skip() -> None:
    org = _org()
    ctx = _ctx()
    batch = EntityBulkCreateRequest(
        entities=[
            EntityCreate(
                name="Session one",
                content="semantic memory content",
                entity_type=EntityType.SESSION,
            )
        ]
    )
    runtime = SimpleNamespace(
        entity_manager=SimpleNamespace(create_direct_bulk=AsyncMock(return_value=[])),
        relationship_manager=SimpleNamespace(create_bulk=AsyncMock(return_value=(0, 0))),
    )

    with (
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime",
            AsyncMock(return_value=runtime),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await create_entities_bulk(
            batch=batch,
            org=org,
            ctx=ctx,
            content_session=None,
        )

    assert exc.value.status_code == 400
    assert "skip_conflicts=true" in str(exc.value.detail)
    runtime.entity_manager.create_direct_bulk.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_entity_verifies_metadata_project_id_before_add() -> None:
    org = _org()
    ctx = _ctx()
    entity = EntityCreate(
        name="Scoped memory",
        content="Remember this only in a project the user can write.",
        entity_type=EntityType.DECISION,
        metadata={"project_id": "project_denied"},
    )
    add = AsyncMock()
    verify_access = AsyncMock(
        side_effect=ProjectAccessDeniedError(
            project_id="project_denied",
            required_role=ProjectRole.CONTRIBUTOR,
        )
    )

    with (
        patch("sibyl_core.tools.core.add", add),
        patch("sibyl.api.routes.entities.verify_entity_project_access", verify_access),
        pytest.raises(ProjectAccessDeniedError),
    ):
        await create_entity(
            request=_request(),
            entity=entity,
            org=org,
            ctx=ctx,
            content_session="session",
            sync=False,
        )

    verify_access.assert_awaited_once_with(
        "session",
        ctx,
        "project_denied",
        required_role=ProjectRole.CONTRIBUTOR,
        require_existing_project=True,
    )
    add.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_project_routes_through_runtime_project_record() -> None:
    org = _org()
    ctx = _ctx()
    existing = _project_entity(name="Old name", description="old")
    updated = _project_entity(name="New name", description="new")
    runtime = SimpleNamespace(
        entity_manager=SimpleNamespace(
            get=AsyncMock(return_value=existing),
            update=AsyncMock(return_value=updated),
        )
    )

    with (
        patch("sibyl.locks.entity_lock", _locked_entity),
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime", AsyncMock(return_value=runtime)
        ),
        patch(
            "sibyl.api.routes.entities.verify_entity_project_access", AsyncMock()
        ) as verify_access,
        patch("sibyl.api.routes.entities.broadcast_event", AsyncMock()),
        patch("sibyl.api.routes.entities.update_project_record", AsyncMock()) as update_project,
        patch("sibyl.api.routes.entities.log_audit_event", AsyncMock()) as audit_log,
    ):
        response = await update_entity(
            entity_id="project_new",
            update=EntityUpdate(name="New name", description="new"),
            request=_request(),
            org=org,
            ctx=ctx,
            content_session=None,
        )

    assert response.name == "New name"
    verify_access.assert_awaited_once_with(
        None,
        ctx,
        "project_new",
        required_role=ProjectRole.CONTRIBUTOR,
        require_existing_project=True,
    )
    update_project.assert_awaited_once_with(
        organization_id=org.id,
        graph_project_id="project_new",
        name="New name",
        description="new",
    )
    audit_log.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_project_routes_through_runtime_project_record() -> None:
    org = _org()
    ctx = _ctx()
    existing = _project_entity(name="Delete me", description="gone")
    runtime = SimpleNamespace(
        entity_manager=SimpleNamespace(
            get=AsyncMock(return_value=existing),
            delete=AsyncMock(return_value=True),
        )
    )

    with (
        patch("sibyl.locks.entity_lock", _locked_entity),
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime", AsyncMock(return_value=runtime)
        ),
        patch(
            "sibyl.api.routes.entities.verify_entity_project_access", AsyncMock()
        ) as verify_access,
        patch("sibyl.api.routes.entities.broadcast_event", AsyncMock()),
        patch("sibyl.api.routes.entities.delete_project_record", AsyncMock()) as delete_project,
        patch("sibyl.api.routes.entities.log_audit_event", AsyncMock()) as audit_log,
    ):
        await delete_entity(
            entity_id="project_new",
            request=_request(),
            org=org,
            ctx=ctx,
            content_session=None,
        )

    verify_access.assert_awaited_once_with(
        None,
        ctx,
        "project_new",
        required_role=ProjectRole.MAINTAINER,
        require_existing_project=True,
    )
    delete_project.assert_awaited_once_with(
        organization_id=org.id,
        graph_project_id="project_new",
    )
    audit_log.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_entity_sanitizes_raw_capture_scope_metadata() -> None:
    org = _org()
    ctx = _ctx()
    entity = EntityCreate(
        name="Scoped capture",
        content="Capture this.",
        entity_type=EntityType.DECISION,
        metadata={
            "capture_mode": "remember",
            "capture_surface": "dashboard",
            "memory_scope": "project",
            "scope_key": "project_forged",
            "principal_id": "victim",
            "project_id": "project_forged",
            "review_state": "accepted",
            "source_id": "source-forged",
            "raw_source_id": "raw-source-forged",
            "safe": "kept",
        },
    )
    add_result = SimpleNamespace(success=True, id="decision_1", message="ok")

    with (
        patch("sibyl.api.routes.entities.verify_entity_project_access", AsyncMock()),
        patch("sibyl_core.tools.core.add", AsyncMock(return_value=add_result)),
        patch("sibyl.api.routes.entities.get_entity_graph_runtime", AsyncMock()),
        patch("sibyl.api.routes.entities.broadcast_event", AsyncMock()),
        patch("sibyl.api.routes.entities.log_audit_event", AsyncMock()),
        patch("sibyl.api.routes.entities._archive_raw_capture", AsyncMock()) as archive_capture,
    ):
        await create_entity(
            request=_request(),
            entity=entity,
            org=org,
            ctx=ctx,
            content_session=None,
            sync=False,
        )

    sent_metadata = archive_capture.await_args.kwargs["metadata"]
    assert sent_metadata["capture_mode"] == "remember"
    assert sent_metadata["capture_surface"] == "dashboard"
    assert sent_metadata["safe"] == "kept"
    assert "memory_scope" not in sent_metadata
    assert "scope_key" not in sent_metadata
    assert "principal_id" not in sent_metadata
    assert "project_id" not in sent_metadata
    assert "review_state" not in sent_metadata
    assert "source_id" not in sent_metadata
    assert "raw_source_id" not in sent_metadata
