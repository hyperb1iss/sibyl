from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException

from sibyl.api.routes.epics import (
    CompleteEpicRequest,
    UpdateEpicRequest,
    complete_epic,
    start_epic,
    update_epic,
)
from sibyl.services.work_item_workflow import WorkItemAction, WorkItemTransition
from sibyl_core.models.entities import EntityType


def _transition(action: WorkItemAction, status: str, **fields: object) -> WorkItemTransition:
    return WorkItemTransition(
        action=action,
        item_id="epic-1",
        entity_type=EntityType.EPIC,
        status=status,
        name="Epic Nova",
        fields=fields,
    )


def _org() -> SimpleNamespace:
    return SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))


def _ctx() -> SimpleNamespace:
    return SimpleNamespace()


def _epic() -> SimpleNamespace:
    return SimpleNamespace(
        id="epic-1",
        name="Epic Nova",
        metadata={"project_id": "project-9"},
    )


class TestEpicRoutes:
    @pytest.mark.asyncio
    async def test_start_epic_routes_through_workflow_service(self) -> None:
        epic = _epic()
        transition = AsyncMock(return_value=_transition(WorkItemAction.START_EPIC, "in_progress"))

        with (
            patch("sibyl.api.routes.epics._verify_epic_access", AsyncMock(return_value=epic)),
            patch("sibyl.api.routes.epics.transition_work_item", transition),
        ):
            response = await start_epic("epic-1", org=_org(), ctx=_ctx())

        assert response.action == "start_epic"
        assert response.data["status"] == "in_progress"
        transition.assert_awaited_once_with(
            str(_org().id),
            "epic-1",
            WorkItemAction.START_EPIC,
            entity=epic,
        )

    @pytest.mark.asyncio
    async def test_complete_epic_captures_learnings_via_workflow_service(self) -> None:
        epic = _epic()
        transition = AsyncMock(
            return_value=_transition(
                WorkItemAction.COMPLETE_EPIC, "completed", learnings="Keep the seam thin"
            )
        )

        with (
            patch("sibyl.api.routes.epics._verify_epic_access", AsyncMock(return_value=epic)),
            patch("sibyl.api.routes.epics.transition_work_item", transition),
        ):
            response = await complete_epic(
                "epic-1",
                org=_org(),
                ctx=_ctx(),
                request=CompleteEpicRequest(learnings="Keep the seam thin"),
            )

        assert response.action == "complete_epic"
        assert response.data["status"] == "completed"
        assert response.data["learnings"] == "Keep the seam thin"
        transition.assert_awaited_once_with(
            str(_org().id),
            "epic-1",
            WorkItemAction.COMPLETE_EPIC,
            payload={"learnings": "Keep the seam thin"},
            entity=epic,
        )

    @pytest.mark.asyncio
    async def test_update_epic_rejects_empty_updates_before_graph_write(self) -> None:
        epic = _epic()
        update_entity = AsyncMock()

        with (
            patch("sibyl.api.routes.epics._verify_epic_access", AsyncMock(return_value=epic)),
            patch("sibyl.api.routes.epics.update_graph_entity", update_entity),
            pytest.raises(HTTPException, match="No fields to update") as exc_info,
        ):
            await update_epic(
                "epic-1",
                request=UpdateEpicRequest(),
                org=_org(),
                ctx=_ctx(),
            )

        assert exc_info.value.status_code == 400
        update_entity.assert_not_awaited()
