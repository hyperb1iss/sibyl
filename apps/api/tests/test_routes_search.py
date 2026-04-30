from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from sibyl.api.routes.search import explore, search
from sibyl.api.schemas import ExploreRequest, SearchRequest
from sibyl.auth.errors import ProjectAccessDeniedError
from sibyl.db.models import ProjectRole


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
    async def test_explore_uses_project_scope_helper_for_project_id_lists(self) -> None:
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
                "sibyl.api.routes.search.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1", "proj_2"]),
            ) as list_projects,
            patch("sibyl_core.tools.core.explore", AsyncMock(return_value=result)) as core_explore,
        ):
            response = await explore(
                request=ExploreRequest(mode="list", project_ids=["proj_1"]),
                org=org,
                ctx=ctx,
            )

        list_projects.assert_awaited_once_with(ctx)
        assert response.total == 1
        assert response.entities[0]["id"] == "task_1"
        assert core_explore.await_args.kwargs["project_ids"] == ["proj_1"]
        assert core_explore.await_args.kwargs["accessible_projects"] is None

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
                "sibyl.api.routes.search.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
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
