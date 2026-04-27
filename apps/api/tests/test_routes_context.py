from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from sibyl.api.routes.context import context_pack
from sibyl.api.schemas import ContextPackRequest, ReflectionRequest
from sibyl.auth.errors import ProjectAccessDeniedError
from sibyl_core.models.context import (
    ContextFacet,
    ContextIntent,
    ContextItem,
    ContextItemQualityMetadata,
    ContextLayer,
    ContextPack,
    ContextSection,
)
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


def _pack_with_quality() -> ContextPack:
    return ContextPack(
        goal="ship faster",
        intent=ContextIntent.BUILD,
        query="ship faster",
        domain=None,
        project=None,
        sections=[
            ContextSection(
                facet=ContextFacet.DECISIONS,
                title="Decisions",
                items=[
                    ContextItem(
                        id="decision_1",
                        type="decision",
                        name="Use context packs",
                        content="Agents should receive precise grouped memory.",
                        score=0.91,
                        facet=ContextFacet.DECISIONS,
                        reason="decision records a choice",
                        source="Northstar",
                        quality=ContextItemQualityMetadata(
                            origin="graph",
                            source="docs/architecture/SIBYL_NORTHSTAR.md",
                            project_id="project-sibyl",
                        ),
                    )
                ],
            )
        ],
        total_items=1,
    )


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(user_id="user-123")


class TestContextPackRoute:
    @pytest.mark.asyncio
    async def test_context_pack_scopes_to_accessible_projects(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        ctx = _ctx()

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
        assert response.layer == ContextLayer.RECALL
        assert response.markdown is not None
        assert response.markdown.startswith("# Sibyl Context Pack")
        assert compile_context.await_args.kwargs["accessible_projects"] == {"proj_1"}
        assert compile_context.await_args.kwargs["layer"] == ContextLayer.RECALL
        assert compile_context.await_args.kwargs["principal_id"] == "user-123"
        assert compile_context.await_args.kwargs["project"] is None
        assert compile_context.await_args.kwargs["include_related"] is True
        assert compile_context.await_args.kwargs["related_limit"] == 3

    @pytest.mark.asyncio
    async def test_context_pack_preserves_quality_metadata(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch(
                "sibyl_core.tools.context.compile_context",
                AsyncMock(return_value=_pack_with_quality()),
            ),
        ):
            response = await context_pack(
                request=ContextPackRequest(goal="ship faster"),
                org=org,
                ctx=_ctx(),
            )

        item = response.sections[0].items[0]
        assert item.quality.origin == "graph"
        assert item.quality.source == "docs/architecture/SIBYL_NORTHSTAR.md"
        assert item.quality.project_id == "project-sibyl"

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
                ctx=_ctx(),
            )

        assert compile_context.await_args.kwargs["project"] == "proj_1"
        assert compile_context.await_args.kwargs["accessible_projects"] is None

    @pytest.mark.asyncio
    async def test_context_pack_passes_requested_layer(self) -> None:
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
                request=ContextPackRequest(goal="ship faster", layer=ContextLayer.WAKE),
                org=org,
                ctx=_ctx(),
            )

        assert compile_context.await_args.kwargs["layer"] == ContextLayer.WAKE

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
                ctx=_ctx(),
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
            patch(
                "sibyl_core.tools.core.explore",
                AsyncMock(return_value=SimpleNamespace(entities=[])),
            ) as explore,
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
        assert reflect_memory.await_args.kwargs["related_to"] is None
        assert reflect_memory.await_args.kwargs["persist"] is True
        assert reflect_memory.await_args.kwargs["persist_source"] is True
        explore.assert_awaited_once_with(
            mode="list",
            types=["task"],
            project="proj_1",
            status="doing",
            limit=2,
            organization_id=str(org.id),
        )

    @pytest.mark.asyncio
    async def test_reflect_links_explicit_and_single_active_task(self) -> None:
        from sibyl.api.routes.context import reflect_context

        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        explore = AsyncMock(return_value=SimpleNamespace(entities=[SimpleNamespace(id="task_2")]))

        with (
            patch(
                "sibyl.api.routes.context.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch(
                "sibyl_core.tools.core.reflect_memory",
                AsyncMock(return_value=_reflection_pack()),
            ) as reflect_memory,
            patch("sibyl_core.tools.core.explore", explore),
        ):
            await reflect_context(
                request=ReflectionRequest(
                    content="We decided to add reflect.",
                    source_title="Planning",
                    intent=ContextIntent.BUILD,
                    project="proj_1",
                    related_to=["plan_1"],
                    task_ids=["task_1", "plan_1"],
                    persist=True,
                ),
                org=org,
                ctx=SimpleNamespace(),
            )

        assert reflect_memory.await_args.kwargs["related_to"] == ["plan_1", "task_1", "task_2"]
        explore.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reflect_skips_active_task_lookup_when_not_persisting(self) -> None:
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
            patch("sibyl_core.tools.core.explore", AsyncMock()) as explore,
        ):
            await reflect_context(
                request=ReflectionRequest(
                    content="We decided to add reflect.",
                    source_title="Planning",
                    intent=ContextIntent.BUILD,
                    project="proj_1",
                    task_ids=["task_1"],
                    persist=False,
                ),
                org=org,
                ctx=SimpleNamespace(),
            )

        assert reflect_memory.await_args.kwargs["related_to"] == ["task_1"]
        explore.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reflect_skips_active_task_lookup_without_project(self) -> None:
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
            patch("sibyl_core.tools.core.explore", AsyncMock()) as explore,
        ):
            await reflect_context(
                request=ReflectionRequest(
                    content="We decided to add reflect.",
                    source_title="Planning",
                    intent=ContextIntent.BUILD,
                    task_ids=["task_1"],
                    persist=True,
                ),
                org=org,
                ctx=SimpleNamespace(),
            )

        assert reflect_memory.await_args.kwargs["related_to"] == ["task_1"]
        explore.assert_not_awaited()

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
