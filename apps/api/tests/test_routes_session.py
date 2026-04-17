from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from sibyl.api.routes.session import get_session_bundle
from sibyl.auth.errors import ProjectAccessDeniedError


class TestSessionBundleRoute:
    @pytest.mark.asyncio
    async def test_scoped_bundle_packages_tasks_and_memory(self) -> None:
        org = SimpleNamespace(
            id=UUID("00000000-0000-0000-0000-000000000111"),
            slug="hyper",
        )
        ctx = SimpleNamespace()

        explore_result = SimpleNamespace(
            entities=[
                {
                    "id": "task_1",
                    "name": "Ship session snapshot",
                    "metadata": {"status": "doing", "priority": "high"},
                },
                {
                    "id": "task_2",
                    "name": "Archive triage",
                    "metadata": {"status": "blocked", "priority": "critical"},
                },
            ]
        )
        search_result = SimpleNamespace(
            results=[
                {
                    "id": "task_1",
                    "type": "task",
                    "name": "Ship session snapshot",
                    "content": "duplicate task result",
                    "metadata": {},
                },
                {
                    "id": "procedure_1",
                    "type": "procedure",
                    "name": "Review raw captures before consolidation",
                    "content": "[Procedure] Check the archive queue before you run maintenance.",
                    "metadata": {},
                },
            ]
        )

        with (
            patch(
                "sibyl.api.routes.session.list_legacy_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1", "proj_2"]),
            ),
            patch(
                "sibyl_core.tools.core.explore", AsyncMock(return_value=explore_result)
            ) as explore,
            patch("sibyl_core.tools.core.search", AsyncMock(return_value=search_result)) as search,
        ):
            response = await get_session_bundle(
                query=None,
                task_limit=5,
                memory_limit=3,
                project_ids=["proj_1"],
                org=org,
                ctx=ctx,
            )

        assert response.context.scope == "project_selection"
        assert response.context.project_ids == ["proj_1"]
        assert response.query == "Ship session snapshot | Archive triage"
        assert response.remember_next == "Unblock Archive triage before you pick up new work."
        assert [task.id for task in response.tasks] == ["task_1", "task_2"]
        assert [memory.id for memory in response.relevant_entities] == ["procedure_1"]
        assert response.relevant_entities[0].preview == (
            "Check the archive queue before you run maintenance."
        )

        assert explore.await_count == 1
        assert search.await_count == 1
        assert explore.await_args.kwargs["project_ids"] == ["proj_1"]
        assert search.await_args.kwargs["project"] == "proj_1"
        assert search.await_args.kwargs["accessible_projects"] is None

    @pytest.mark.asyncio
    async def test_rejects_inaccessible_project_scope(self) -> None:
        org = SimpleNamespace(
            id=UUID("00000000-0000-0000-0000-000000000111"),
            slug="hyper",
        )

        with (
            patch(
                "sibyl.api.routes.session.list_legacy_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            pytest.raises(ProjectAccessDeniedError) as exc,
        ):
            await get_session_bundle(
                query=None,
                task_limit=5,
                memory_limit=3,
                project_ids=["proj_2"],
                org=org,
                ctx=SimpleNamespace(),
            )

        assert exc.value.status_code == 403
        assert exc.value.detail["error"] == "project_access_denied"

    @pytest.mark.asyncio
    async def test_all_projects_bundle_uses_generic_no_task_guidance(self) -> None:
        org = SimpleNamespace(
            id=UUID("00000000-0000-0000-0000-000000000111"),
            slug="hyper",
        )

        with (
            patch(
                "sibyl.api.routes.session.list_legacy_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch(
                "sibyl_core.tools.core.explore",
                AsyncMock(return_value=SimpleNamespace(entities=[])),
            ) as explore,
            patch("sibyl_core.tools.core.search", AsyncMock()) as search,
        ):
            response = await get_session_bundle(
                query=None,
                task_limit=5,
                memory_limit=3,
                project_ids=None,
                org=org,
                ctx=SimpleNamespace(),
            )

        assert response.context.scope == "all_projects"
        assert response.query is None
        assert response.tasks == []
        assert response.relevant_entities == []
        assert (
            response.remember_next
            == "No active tasks yet. Start one or capture the next useful learning."
        )
        assert explore.await_args.kwargs["accessible_projects"] == ["proj_1"]
        search.assert_not_awaited()
