from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from sibyl.api.routes.search import explore, search
from sibyl.api.schemas import ExploreRequest, SearchRequest
from sibyl.auth.errors import ProjectAccessDeniedError
from sibyl_core.auth import ProjectRole


@dataclass
class _SearchResult:
    results: list[dict]
    total: int
    query: str
    filters: dict[str, object] = field(default_factory=dict)
    graph_count: int = 0
    document_count: int = 0
    limit: int = 10
    offset: int = 0
    has_more: bool = False


class TestSearchRoute:
    @pytest.mark.asyncio
    async def test_search_without_project_passes_default_accessible_scope(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        ctx = SimpleNamespace()
        result = _SearchResult(
            results=[
                {
                    "id": "pattern_unassigned",
                    "type": "pattern",
                    "name": "Unassigned pattern",
                    "content": "content",
                    "score": 0.9,
                    "metadata": {},
                    "source": None,
                    "source_id": None,
                    "result_origin": "graph",
                    "usage_hint": None,
                    "created_at": None,
                    "updated_at": None,
                }
            ],
            total=1,
            query="seam",
        )

        with (
            patch(
                "sibyl.api.routes.search.list_accessible_project_graph_ids",
                AsyncMock(return_value={"proj_1"}),
            ) as list_projects,
            patch("sibyl_core.tools.core.search", AsyncMock(return_value=result)) as core_search,
        ):
            response = await search(
                request=SearchRequest(query="seam"),
                org=org,
                ctx=ctx,
            )

        list_projects.assert_awaited_once_with(ctx)
        assert response.total == 1
        assert response.results[0].id == "pattern_unassigned"
        assert core_search.await_args.kwargs["project"] is None
        assert core_search.await_args.kwargs["accessible_projects"] == {"proj_1"}

    @pytest.mark.asyncio
    async def test_search_verifies_project_filter_directly(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        ctx = SimpleNamespace()
        result = _SearchResult(
            results=[
                {
                    "id": "pattern_1",
                    "type": "pattern",
                    "name": "Use the seam",
                    "content": "content",
                    "score": 0.9,
                    "metadata": {},
                    "source": None,
                    "source_id": None,
                    "result_origin": "graph",
                    "usage_hint": None,
                    "created_at": None,
                    "updated_at": None,
                }
            ],
            total=1,
            query="seam",
        )

        with (
            patch(
                "sibyl.api.routes.search.verify_entity_project_access",
                AsyncMock(return_value=ProjectRole.VIEWER),
            ) as verify_project,
            patch("sibyl_core.tools.core.search", AsyncMock(return_value=result)) as core_search,
        ):
            response = await search(
                request=SearchRequest(query="seam", project="proj_1"),
                org=org,
                ctx=ctx,
            )

        verify_project.assert_awaited_once_with(
            None,
            ctx,
            "proj_1",
            required_role=ProjectRole.VIEWER,
            require_existing_project=True,
        )
        assert response.total == 1
        assert response.results[0].id == "pattern_1"
        assert core_search.await_args.kwargs["project"] == "proj_1"
        assert core_search.await_args.kwargs["accessible_projects"] is None

    @pytest.mark.asyncio
    async def test_search_rejects_inaccessible_project(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))

        with (
            patch(
                "sibyl.api.routes.search.verify_entity_project_access",
                AsyncMock(
                    side_effect=ProjectAccessDeniedError(
                        project_id="proj_2",
                        required_role="viewer",
                    )
                ),
            ),
            pytest.raises(ProjectAccessDeniedError) as exc,
        ):
            await search(
                request=SearchRequest(query="seam", project="proj_2"),
                org=org,
                ctx=SimpleNamespace(),
            )

        assert exc.value.status_code == 403
        assert exc.value.detail["error"] == "project_access_denied"


class TestExploreRoute:
    @pytest.mark.asyncio
    async def test_explore_verifies_project_id_lists_directly(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        ctx = SimpleNamespace()
        result = SimpleNamespace(
            mode="list",
            entities=[{"id": "task_1", "name": "Ship it"}],
            total=1,
            filters={"project_ids": ["proj_1"]},
            limit=10,
            offset=0,
            has_more=False,
            actual_total=1,
        )

        with (
            patch(
                "sibyl.api.routes.search.verify_entity_project_access",
                AsyncMock(return_value=ProjectRole.VIEWER),
            ) as verify_project,
            patch("sibyl_core.tools.core.explore", AsyncMock(return_value=result)) as core_explore,
        ):
            response = await explore(
                request=ExploreRequest(mode="list", project_ids=["proj_1"]),
                org=org,
                ctx=ctx,
            )

        verify_project.assert_awaited_once_with(
            None,
            ctx,
            "proj_1",
            required_role=ProjectRole.VIEWER,
            require_existing_project=True,
        )
        assert response.total == 1
        assert response.entities[0]["id"] == "task_1"
        assert core_explore.await_args.kwargs["project_ids"] == ["proj_1"]
        assert core_explore.await_args.kwargs["accessible_projects"] is None


    @pytest.mark.asyncio
    async def test_explore_related_with_project_ids_preserves_accessible_filter(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        ctx = SimpleNamespace()
        result = SimpleNamespace(
            mode="related",
            entities=[{"id": "task_1", "name": "Ship it"}],
            total=1,
            filters={"project_ids": ["proj_1"]},
            limit=10,
            offset=0,
            has_more=False,
            actual_total=1,
        )

        with (
            patch(
                "sibyl.api.routes.search.verify_entity_project_access",
                AsyncMock(return_value=ProjectRole.VIEWER),
            ),
            patch("sibyl_core.tools.core.explore", AsyncMock(return_value=result)) as core_explore,
        ):
            await explore(
                request=ExploreRequest(mode="related", entity_id="entity_1", project_ids=["proj_1"]),
                org=org,
                ctx=ctx,
            )

        assert core_explore.await_args.kwargs["project_ids"] == ["proj_1"]
        assert core_explore.await_args.kwargs["accessible_projects"] == {"proj_1"}

    @pytest.mark.asyncio
    async def test_explore_without_project_passes_default_accessible_scope(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        ctx = SimpleNamespace()
        result = SimpleNamespace(
            mode="list",
            entities=[
                {"id": "task_1", "name": "Visible task"},
                {"id": "pattern_1", "name": "Unassigned pattern"},
            ],
            total=2,
            filters={},
            limit=10,
            offset=0,
            has_more=False,
            actual_total=2,
        )

        with (
            patch(
                "sibyl.api.routes.search.list_accessible_project_graph_ids",
                AsyncMock(return_value={"proj_1"}),
            ) as list_projects,
            patch("sibyl_core.tools.core.explore", AsyncMock(return_value=result)) as core_explore,
        ):
            response = await explore(
                request=ExploreRequest(mode="list"),
                org=org,
                ctx=ctx,
            )

        list_projects.assert_awaited_once_with(ctx)
        assert [entity["id"] for entity in response.entities] == ["task_1", "pattern_1"]
        assert core_explore.await_args.kwargs["project"] is None
        assert core_explore.await_args.kwargs["project_ids"] is None
        assert core_explore.await_args.kwargs["accessible_projects"] == {"proj_1"}

    @pytest.mark.asyncio
    async def test_explore_verifies_single_project_filter_directly(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        ctx = SimpleNamespace()
        result = SimpleNamespace(
            mode="dependencies",
            entities=[{"id": "task_1", "name": "Ship it"}],
            total=1,
            filters={"project": "proj_1"},
            limit=10,
            offset=0,
            has_more=False,
            actual_total=1,
        )

        with (
            patch(
                "sibyl.api.routes.search.verify_entity_project_access",
                AsyncMock(return_value=ProjectRole.VIEWER),
            ) as verify_project,
            patch("sibyl_core.tools.core.explore", AsyncMock(return_value=result)) as core_explore,
        ):
            response = await explore(
                request=ExploreRequest(mode="dependencies", project="proj_1"),
                org=org,
                ctx=ctx,
            )

        verify_project.assert_awaited_once_with(
            None,
            ctx,
            "proj_1",
            required_role=ProjectRole.VIEWER,
            require_existing_project=True,
        )
        assert response.total == 1
        assert core_explore.await_args.kwargs["project"] == "proj_1"
        assert core_explore.await_args.kwargs["project_ids"] is None
        assert core_explore.await_args.kwargs["accessible_projects"] is None

    @pytest.mark.asyncio
    async def test_explore_rejects_inaccessible_projects(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))

        with (
            patch(
                "sibyl.api.routes.search.verify_entity_project_access",
                AsyncMock(
                    side_effect=ProjectAccessDeniedError(
                        project_id="proj_2",
                        required_role="viewer",
                    )
                ),
            ),
            pytest.raises(ProjectAccessDeniedError) as exc,
        ):
            await explore(
                request=ExploreRequest(mode="list", project_ids=["proj_2"]),
                org=org,
                ctx=SimpleNamespace(),
            )

        assert exc.value.status_code == 403
        assert exc.value.detail["error"] == "project_access_denied"
