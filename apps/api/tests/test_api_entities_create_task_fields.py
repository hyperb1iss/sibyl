from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from sibyl.api.routes.entities import create_entity
from sibyl.api.schemas import EntityCreate
from sibyl_core.auth import ProjectRole
from sibyl_core.models.entities import EntityType


@pytest.mark.asyncio
async def test_entities_create_passes_task_fields_to_add() -> None:
    org = MagicMock()
    org.id = uuid4()

    request = MagicMock()
    request.headers = {}
    request.cookies = {}

    content_session = AsyncMock()
    ctx = MagicMock()

    entity = EntityCreate(
        name="Test task",
        description="",
        content="do it",
        entity_type=EntityType.TASK,
        related_to=["decision_123"],
        metadata={
            "project_id": "project_123",
            "epic_id": "epic_456",
            "priority": "high",
            "assignees": ["alice"],
            "technologies": ["python"],
            "depends_on": ["task_a", "task_b"],
        },
    )

    add_result = MagicMock()
    add_result.success = True
    add_result.id = "task_new"
    add_result.message = "ok"

    related_target = MagicMock()
    related_target.entity_type = EntityType.DECISION
    related_target.project_id = None
    related_target.metadata = {}
    runtime = MagicMock()
    runtime.entity_manager = MagicMock()
    runtime.entity_manager.get = AsyncMock(return_value=related_target)

    with (
        patch("sibyl_core.tools.core.add", AsyncMock(return_value=add_result)) as add,
        patch("sibyl.api.routes.entities.broadcast_event", AsyncMock()),
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime",
            AsyncMock(return_value=runtime),
        ),
        patch(
            "sibyl.api.routes.entities.verify_entity_project_access", AsyncMock()
        ) as verify_access,
    ):
        resp = await create_entity(
            request=request,
            entity=entity,
            org=org,
            ctx=ctx,
            content_session=content_session,
            sync=False,
        )

    assert resp.id == "task_new"
    verify_access.assert_awaited_once_with(
        content_session,
        ctx,
        "project_123",
        required_role=ProjectRole.CONTRIBUTOR,
        require_existing_project=True,
    )
    add.assert_awaited_once()
    _, kwargs = add.call_args
    assert kwargs["project"] == "project_123"
    assert kwargs["epic"] == "epic_456"
    assert kwargs["technologies"] == ["python"]
    assert kwargs["depends_on"] == ["task_a", "task_b"]
    assert kwargs["related_to"] == ["decision_123"]


@pytest.mark.asyncio
async def test_entities_create_rejects_missing_related_to_target() -> None:
    org = MagicMock()
    org.id = uuid4()

    request = MagicMock()
    request.headers = {}
    request.cookies = {}

    content_session = AsyncMock()
    ctx = MagicMock()

    entity = EntityCreate(
        name="Test task",
        description="",
        content="do it",
        entity_type=EntityType.TASK,
        related_to=["decision_missing"],
    )

    runtime = MagicMock()
    runtime.entity_manager = MagicMock()
    runtime.entity_manager.get = AsyncMock(side_effect=KeyError("not found"))

    with (
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime",
            AsyncMock(return_value=runtime),
        ),
        patch("sibyl_core.tools.core.add", AsyncMock()) as add,
        patch("sibyl.api.routes.entities.broadcast_event", AsyncMock()),
        pytest.raises(HTTPException) as exc,
    ):
        await create_entity(
            request=request,
            entity=entity,
            org=org,
            ctx=ctx,
            content_session=content_session,
            sync=False,
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "Related entity not found: decision_missing"
    add.assert_not_awaited()


@pytest.mark.asyncio
async def test_entities_create_validates_the_target_behind_a_predicate() -> None:
    """A declared predicate rides on the id string and must not reach the lookup.

    Validation resolves `supersedes:decision_123` to `decision_123`, so the
    existence and project-access checks run against the entity the edge will
    actually point at. The declaration itself travels intact to `add()`, which
    is where the predicate becomes the edge type.
    """
    org = MagicMock()
    org.id = uuid4()

    request = MagicMock()
    request.headers = {}
    request.cookies = {}

    content_session = AsyncMock()
    ctx = MagicMock()

    entity = EntityCreate(
        name="Reconsidered decision",
        description="",
        content="the newer call",
        entity_type=EntityType.DECISION,
        related_to=["supersedes:decision_123", "decision_456"],
    )

    add_result = MagicMock()
    add_result.success = True
    add_result.id = "decision_new"
    add_result.message = "ok"

    related_target = MagicMock()
    related_target.entity_type = EntityType.DECISION
    related_target.project_id = None
    related_target.metadata = {}
    runtime = MagicMock()
    runtime.entity_manager = MagicMock()
    runtime.entity_manager.get = AsyncMock(return_value=related_target)

    with (
        patch("sibyl_core.tools.core.add", AsyncMock(return_value=add_result)) as add,
        patch("sibyl.api.routes.entities.broadcast_event", AsyncMock()),
        patch(
            "sibyl.api.routes.entities.get_entity_graph_runtime",
            AsyncMock(return_value=runtime),
        ),
    ):
        resp = await create_entity(
            request=request,
            entity=entity,
            org=org,
            ctx=ctx,
            content_session=content_session,
            sync=False,
        )

    assert resp.id == "decision_new"
    assert [call.args[0] for call in runtime.entity_manager.get.await_args_list] == [
        "decision_123",
        "decision_456",
    ]
    _, kwargs = add.call_args
    assert kwargs["related_to"] == ["supersedes:decision_123", "decision_456"]
