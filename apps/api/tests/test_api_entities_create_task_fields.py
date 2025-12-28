from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from sibyl.api.routes.entities import create_entity
from sibyl.api.schemas import EntityCreate
from sibyl.models.entities import EntityType


@pytest.mark.asyncio
async def test_entities_create_passes_task_fields_to_add() -> None:
    org = MagicMock()
    org.id = uuid4()

    request = MagicMock()
    request.headers = {}
    request.cookies = {}

    session = AsyncMock()
    ctx = MagicMock()

    entity = EntityCreate(
        name="Test task",
        description="",
        content="do it",
        entity_type=EntityType.TASK,
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

    with (
        patch("sibyl.tools.core.add", AsyncMock(return_value=add_result)) as add,
        patch("sibyl.api.routes.entities.broadcast_event", AsyncMock()),
    ):
        resp = await create_entity(
            request=request,
            entity=entity,
            org=org,
            ctx=ctx,
            session=session,
            sync=False,
        )

    assert resp.id == "task_new"
    add.assert_awaited_once()
    _, kwargs = add.call_args
    assert kwargs["project"] == "project_123"
    assert kwargs["epic"] == "epic_456"
    assert kwargs["technologies"] == ["python"]
    assert kwargs["depends_on"] == ["task_a", "task_b"]

