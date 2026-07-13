from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException

from sibyl.api.routes.search import explore, search
from sibyl.api.schemas import ExploreRequest, SearchRequest
from sibyl.auth.errors import ProjectAccessDeniedError
from sibyl_core.auth import OrganizationRole, ProjectRole


@dataclass
class _SearchResult:
    results: list[dict]
    total: int
    query: str
    filters: dict[str, object] = field(default_factory=dict)
    graph_count: int = 0
    document_count: int = 0
    raw_memory_count: int = 0
    limit: int = 10
    offset: int = 0
    has_more: bool = False


def _ctx(
    *,
    user_id: str = "user-123",
    api_key_memory_scope_keys: set[str] | list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=user_id,
        organization_id="org-1",
        org_role=OrganizationRole.MEMBER,
        api_key_memory_scope_keys=api_key_memory_scope_keys,
    )


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
            patch(
                "sibyl.api.routes.search.configured_embedding_provider",
                return_value=object(),
            ),
            patch(
                "sibyl.api.routes.search.capture_embedding_usage",
                return_value=nullcontext({"requests": 1, "prompt_tokens": 4}),
            ),
        ):
            response = await search(
                request=SearchRequest(
                    query="seam",
                    content_max_chars=18_000,
                    include_retrieval_diagnostics=True,
                ),
                org=org,
                ctx=ctx,
            )

        list_projects.assert_awaited_once_with(ctx)
        assert response.total == 1
        assert response.results[0].id == "pattern_unassigned"
        assert core_search.await_args.kwargs["project"] is None
        assert core_search.await_args.kwargs["accessible_projects"] == {"proj_1"}
        assert core_search.await_args.kwargs["include_raw_memory"] is False
        assert core_search.await_args.kwargs["content_max_chars"] == 18_000
        assert core_search.await_args.kwargs["include_retrieval_diagnostics"] is True
        assert response.filters["embedding_usage"] == {
            "requests": 1,
            "prompt_tokens": 4,
        }

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

    @pytest.mark.asyncio
    async def test_search_passes_reference_time_to_core(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        result = _SearchResult(results=[], total=0, query="seam")

        with (
            patch(
                "sibyl.api.routes.search.list_accessible_project_graph_ids",
                AsyncMock(return_value=set()),
            ),
            patch("sibyl_core.tools.core.search", AsyncMock(return_value=result)) as core_search,
        ):
            await search(
                request=SearchRequest(
                    query="What happened 10 days ago?",
                    reference_time="2026/01/20 00:00",
                ),
                org=org,
                ctx=SimpleNamespace(),
            )

        assert core_search.await_args.kwargs["reference_time"] == "2026/01/20 00:00"

    @pytest.mark.asyncio
    async def test_search_passes_as_of_to_core(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        result = _SearchResult(results=[], total=0, query="seam")

        with (
            patch(
                "sibyl.api.routes.search.list_accessible_project_graph_ids",
                AsyncMock(return_value=set()),
            ),
            patch("sibyl_core.tools.core.search", AsyncMock(return_value=result)) as core_search,
        ):
            await search(
                request=SearchRequest(
                    query="What did we know?",
                    as_of="2025-03-15T00:00:00+00:00",
                ),
                org=org,
                ctx=SimpleNamespace(),
            )

        assert core_search.await_args.kwargs["as_of"] == "2025-03-15T00:00:00+00:00"

    @pytest.mark.asyncio
    async def test_search_forwards_raw_memory_facets_after_policy_check(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        occurred_after = datetime(2014, 1, 1, tzinfo=UTC)
        occurred_before = datetime(2014, 12, 31, 23, 59, 59, tzinfo=UTC)
        result = _SearchResult(
            results=[
                {
                    "id": "raw_memory:memory-1",
                    "type": "raw_memory",
                    "name": "Mailbox thread",
                    "content": "Nova and Bliss discussed SurrealDB.",
                    "score": 0.87,
                    "metadata": {},
                    "source": "source-mail-1",
                    "source_id": None,
                    "result_origin": "raw_memory",
                    "usage_hint": None,
                    "created_at": None,
                    "updated_at": None,
                }
            ],
            total=1,
            query="surrealdb",
            raw_memory_count=1,
        )

        with (
            patch(
                "sibyl.api.routes.search.list_accessible_project_graph_ids",
                AsyncMock(return_value={"project_123"}),
            ) as list_projects,
            patch("sibyl_core.tools.core.search", AsyncMock(return_value=result)) as core_search,
        ):
            response = await search(
                request=SearchRequest(
                    query="surrealdb",
                    include_graph=False,
                    include_documents=False,
                    memory_scope="project",
                    scope_key="project_123",
                    source_id="source-mail-1",
                    participants=["nova@example.com"],
                    labels=["email"],
                    thread_id="thread-1",
                    occurred_after=occurred_after,
                    occurred_before=occurred_before,
                ),
                org=org,
                ctx=_ctx(),
            )

        list_projects.assert_awaited_once()
        assert response.raw_memory_count == 1
        kwargs = core_search.await_args.kwargs
        assert kwargs["include_raw_memory"] is True
        assert kwargs["memory_scope"] == "project"
        assert kwargs["scope_key"] == "project_123"
        assert kwargs["source_id"] == "source-mail-1"
        assert kwargs["participants"] == ["nova@example.com"]
        assert kwargs["labels"] == ["email"]
        assert kwargs["thread_id"] == "thread-1"
        assert kwargs["occurred_after"] == occurred_after
        assert kwargs["occurred_before"] == occurred_before
        assert kwargs["principal_id"] == "user-123"
        assert kwargs["accessible_projects"] == {"project_123"}

    @pytest.mark.asyncio
    async def test_search_rejects_api_key_without_raw_memory_grant(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))

        with (
            patch(
                "sibyl.api.routes.search.list_accessible_project_graph_ids",
                AsyncMock(return_value=set()),
            ),
            patch("sibyl_core.tools.core.search", AsyncMock()) as core_search,
            pytest.raises(HTTPException) as exc,
        ):
            await search(
                request=SearchRequest(
                    query="raw memory",
                    include_graph=False,
                    include_documents=False,
                ),
                org=org,
                ctx=_ctx(api_key_memory_scope_keys=[]),
            )

        assert exc.value.status_code == 403
        assert exc.value.detail == "api_key_memory_space_denied"
        core_search.assert_not_awaited()


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
                request=ExploreRequest(
                    mode="related", entity_id="entity_1", project_ids=["proj_1"]
                ),
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
