from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from sibyl.api.routes.context import context_pack
from sibyl.api.schemas import ContextPackRequest
from sibyl.auth.errors import ProjectAccessDeniedError
from sibyl_core.models.context import ContextIntent, ContextPack


def _pack() -> ContextPack:
    return ContextPack(
        goal="ship faster",
        intent=ContextIntent.BUILD,
        query="ship faster",
        domain=None,
        project=None,
        sections=[],
        total_items=0,
    )


class TestContextPackRoute:
    @pytest.mark.asyncio
    async def test_context_pack_scopes_to_accessible_projects(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        ctx = SimpleNamespace()

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ) as list_projects,
            patch(
                "sibyl_core.tools.context.compile_context", AsyncMock(return_value=_pack())
            ) as compile_context,
        ):
            response = await context_pack(
                request=ContextPackRequest(goal="ship faster"),
                org=org,
                ctx=ctx,
            )

        list_projects.assert_awaited_once_with(ctx)
        assert response.goal == "ship faster"
        assert response.markdown is not None
        assert response.markdown.startswith("# Sibyl Context Pack")
        assert compile_context.await_args.kwargs["accessible_projects"] == {"proj_1"}
        assert compile_context.await_args.kwargs["project"] is None
        assert compile_context.await_args.kwargs["include_related"] is True
        assert compile_context.await_args.kwargs["related_limit"] == 3

    @pytest.mark.asyncio
    async def test_context_pack_uses_requested_accessible_project(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch(
                "sibyl_core.tools.context.compile_context", AsyncMock(return_value=_pack())
            ) as compile_context,
        ):
            await context_pack(
                request=ContextPackRequest(goal="ship faster", project="proj_1"),
                org=org,
                ctx=SimpleNamespace(),
            )

        assert compile_context.await_args.kwargs["project"] == "proj_1"
        assert compile_context.await_args.kwargs["accessible_projects"] is None

    @pytest.mark.asyncio
    async def test_context_pack_rejects_inaccessible_project(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch("sibyl_core.tools.context.compile_context", AsyncMock()) as compile_context,
            pytest.raises(ProjectAccessDeniedError) as exc,
        ):
            await context_pack(
                request=ContextPackRequest(goal="ship faster", project="proj_2"),
                org=org,
                ctx=SimpleNamespace(),
            )

        compile_context.assert_not_awaited()
        assert exc.value.status_code == 403
