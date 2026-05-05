from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from sibyl.api.routes.epics import _verify_epic_access
from sibyl.api.routes.tasks import _verify_task_access
from sibyl_core.auth import ProjectRole
from sibyl_core.models.entities import EntityType


@pytest.mark.asyncio
async def test_verify_task_access_uses_knowledge_read_adapter() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    ctx = SimpleNamespace()
    entity = SimpleNamespace(metadata={"project_id": "project-1"})
    service = AsyncMock()
    service.get_entity.return_value = entity
    authorize = AsyncMock()

    with (
        patch("sibyl.api.routes.tasks.get_knowledge_read_adapter", AsyncMock(return_value=service)),
        patch("sibyl.api.routes.tasks.verify_entity_project_access", authorize),
    ):
        await _verify_task_access("task-1", org, ctx)

    service.get_entity.assert_awaited_once_with("task-1")
    authorize.assert_awaited_once_with(
        None,
        ctx,
        "project-1",
        required_role=ProjectRole.CONTRIBUTOR,
    )


@pytest.mark.asyncio
async def test_verify_epic_access_uses_knowledge_read_adapter() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    ctx = SimpleNamespace()
    epic = SimpleNamespace(entity_type=EntityType.EPIC, metadata={"project_id": "project-9"})
    service = AsyncMock()
    service.get_entity.return_value = epic
    authorize = AsyncMock()

    with (
        patch("sibyl.api.routes.epics.get_knowledge_read_adapter", AsyncMock(return_value=service)),
        patch("sibyl.api.routes.epics.verify_entity_project_access", authorize),
    ):
        result = await _verify_epic_access("epic-1", org, ctx)

    assert result is epic
    service.get_entity.assert_awaited_once_with("epic-1")
    authorize.assert_awaited_once_with(
        ctx=ctx,
        entity_project_id="project-9",
        required_role=ProjectRole.CONTRIBUTOR,
    )
