from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from sibyl.api.routes.context import context_pack
from sibyl.api.schemas import ContextPackRequest, ReflectionRequest
from sibyl.auth.errors import ProjectAccessDeniedError
from sibyl_core.models.context import ContextIntent, ContextPack
from sibyl_core.models.reflection import ReflectionCandidate, ReflectionPack


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


def _reflection_pack() -> ReflectionPack:
    return ReflectionPack(
        source_title="Planning",
        source_id="session_1",
        intent="build",
        domain="sibyl",
        project="proj_1",
        candidates=[
            ReflectionCandidate(
                kind="decision",
                title="Decision: Use reflect",
                content="We decided to add reflect.",
                reason="captures a choice",
                confidence=0.86,
            )
        ],
        total_candidates=1,
    )


class TestReflectRoute:
    @pytest.mark.asyncio
    async def test_reflect_scopes_to_accessible_project(self) -> None:
        from sibyl.api.routes.context import reflect_context

        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch(
                "sibyl_core.tools.core.reflect_memory",
                AsyncMock(return_value=_reflection_pack()),
            ) as reflect_memory,
        ):
            response = await reflect_context(
                request=ReflectionRequest(
                    content="We decided to add reflect.",
                    source_title="Planning",
                    intent=ContextIntent.BUILD,
                    project="proj_1",
                    persist=True,
                ),
                org=org,
                ctx=SimpleNamespace(),
            )

        assert response.source_title == "Planning"
        assert response.source_id == "session_1"
        assert response.markdown is not None
        assert response.persisted_count == 0
        assert reflect_memory.await_args.kwargs["organization_id"] == str(org.id)
        assert reflect_memory.await_args.kwargs["project"] == "proj_1"
        assert reflect_memory.await_args.kwargs["persist"] is True
        assert reflect_memory.await_args.kwargs["persist_source"] is True

    @pytest.mark.asyncio
    async def test_reflect_rejects_inaccessible_project(self) -> None:
        from sibyl.api.routes.context import reflect_context

        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch("sibyl_core.tools.core.reflect_memory", AsyncMock()) as reflect_memory,
            pytest.raises(ProjectAccessDeniedError) as exc,
        ):
            await reflect_context(
                request=ReflectionRequest(content="notes", project="proj_2"),
                org=org,
                ctx=SimpleNamespace(),
            )

        reflect_memory.assert_not_awaited()
        assert exc.value.status_code == 403
