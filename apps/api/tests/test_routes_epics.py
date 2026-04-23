from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch
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
    async def test_start_epic_routes_updates_through_graph_runtime(self) -> None:
        epic = _epic()
        update_entity = AsyncMock()
        broadcast = AsyncMock()

        with (
            patch("sibyl.api.routes.epics._verify_epic_access", AsyncMock(return_value=epic)),
            patch("sibyl.api.routes.epics.update_graph_entity", update_entity),
            patch("sibyl.api.routes.epics.broadcast_event", broadcast),
        ):
            response = await start_epic("epic-1", org=_org(), ctx=_ctx())

        assert response.action == "start_epic"
        assert response.data["status"] == "in_progress"
        assert update_entity.await_args_list[0] == call(
            str(_org().id),
            "epic-1",
            {"status": "in_progress"},
        )
        project_touch = update_entity.await_args_list[1]
        assert project_touch.args[:2] == (str(_org().id), "project-9")
        assert "last_activity_at" in project_touch.args[2]
        broadcast.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_complete_epic_captures_learnings_via_graph_update(self) -> None:
        epic = _epic()
        update_entity = AsyncMock()

        with (
            patch("sibyl.api.routes.epics._verify_epic_access", AsyncMock(return_value=epic)),
            patch("sibyl.api.routes.epics.update_graph_entity", update_entity),
            patch("sibyl.api.routes.epics.broadcast_event", AsyncMock()),
        ):
            response = await complete_epic(
                "epic-1",
                org=_org(),
                ctx=_ctx(),
                request=CompleteEpicRequest(learnings="Keep the seam thin"),
            )

        assert response.action == "complete_epic"
        assert response.data["learnings"] == "Keep the seam thin"
        first_update = update_entity.await_args_list[0]
        assert first_update.args[:2] == (str(_org().id), "epic-1")
        assert first_update.args[2]["status"] == "completed"
        assert first_update.args[2]["learnings"] == "Keep the seam thin"
        assert "completed_date" in first_update.args[2]

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
