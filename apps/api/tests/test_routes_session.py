from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from sibyl.api.routes.session import get_session_bundle
from sibyl.auth.context import AuthContext
from sibyl.auth.errors import ProjectAccessDeniedError
from tests.harness.auth import stub_auth_context


def _ctx() -> AuthContext:
    return stub_auth_context(user_id=UUID("00000000-0000-0000-0000-000000000123"))


class TestSessionBundleRoute:
    @pytest.mark.asyncio
    async def test_scoped_bundle_packages_tasks_and_memory(self) -> None:
        org = SimpleNamespace(
            id=UUID("00000000-0000-0000-0000-000000000111"),
            slug="hyper",
        )
        ctx = _ctx()

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
                    "id": "decision_1",
                    "type": "decision",
                    "name": "Use session bundles for wake-up",
                    "content": "[Decision] Check the archive queue before you run maintenance.",
                    "metadata": {},
                },
            ]
        )

        with (
            patch(
                "sibyl.api.routes.session.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1", "proj_2"]),
            ),
            patch(
                "sibyl_core.tools.core.explore", AsyncMock(return_value=explore_result)
            ) as explore,
            patch("sibyl_core.tools.core.search", AsyncMock(return_value=search_result)) as search,
            patch("sibyl.api.routes.session.recall_raw_memory", AsyncMock(return_value=[])),
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
        assert [memory.id for memory in response.relevant_entities] == ["decision_1"]
        assert response.relevant_entities[0].preview == (
            "Check the archive queue before you run maintenance."
        )
        assert response.relevant_entities[0].memory_scope is None

        assert explore.await_count == 1
        assert search.await_count == 1
        assert explore.await_args.kwargs["project_ids"] == ["proj_1"]
        assert search.await_args.kwargs["project"] == "proj_1"
        assert search.await_args.kwargs["accessible_projects"] is None
        assert "decision" in search.await_args.kwargs["types"]
        assert "plan" in search.await_args.kwargs["types"]
        assert "idea" in search.await_args.kwargs["types"]

    @pytest.mark.asyncio
    async def test_rejects_inaccessible_project_scope(self) -> None:
        org = SimpleNamespace(
            id=UUID("00000000-0000-0000-0000-000000000111"),
            slug="hyper",
        )

        with (
            patch(
                "sibyl.api.routes.session.list_accessible_project_graph_ids",
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
                ctx=_ctx(),
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
                "sibyl.api.routes.session.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch(
                "sibyl_core.tools.core.explore",
                AsyncMock(return_value=SimpleNamespace(entities=[])),
            ) as explore,
            patch("sibyl_core.tools.core.search", AsyncMock()) as search,
            patch("sibyl.api.routes.session.recall_raw_memory", AsyncMock()) as recall_raw,
        ):
            response = await get_session_bundle(
                query=None,
                task_limit=5,
                memory_limit=3,
                project_ids=None,
                org=org,
                ctx=_ctx(),
            )

        assert response.context.scope == "all_projects"
        assert response.query is None
        assert response.tasks == []
        assert response.relevant_entities == []
        assert (
            response.remember_next
            == "No active tasks yet. Start one or remember the next useful learning."
        )
        assert explore.await_args.kwargs["accessible_projects"] == ["proj_1"]
        search.assert_not_awaited()
        recall_raw.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_session_bundle_blends_private_and_project_raw_memory(self) -> None:
        org = SimpleNamespace(
            id=UUID("00000000-0000-0000-0000-000000000111"),
            slug="hyper",
        )
        explore_result = SimpleNamespace(
            entities=[
                {
                    "id": "task_1",
                    "name": "Wake from raw memory",
                    "metadata": {"status": "doing", "priority": "high"},
                },
            ]
        )
        search_result = SimpleNamespace(results=[])

        async def fake_raw_recall(**kwargs: object) -> list[SimpleNamespace]:
            if kwargs["memory_scope"] == "private":
                return [
                    SimpleNamespace(
                        id="raw_private",
                        title="Private wake note",
                        raw_content="Remember the private handoff.",
                        source_id="cli:manual",
                        capture_surface="cli",
                        memory_scope="private",
                        scope_key=None,
                    )
                ]
            return [
                SimpleNamespace(
                    id="raw_project",
                    title="Project wake note",
                    raw_content="Remember the project handoff.",
                    source_id="api:manual",
                    capture_surface="api",
                    memory_scope="project",
                    scope_key="proj_1",
                )
            ]

        with (
            patch(
                "sibyl.api.routes.session.list_accessible_project_graph_ids",
                AsyncMock(return_value=["proj_1"]),
            ),
            patch("sibyl_core.tools.core.explore", AsyncMock(return_value=explore_result)),
            patch("sibyl_core.tools.core.search", AsyncMock(return_value=search_result)),
            patch(
                "sibyl.api.routes.session.recall_raw_memory",
                AsyncMock(side_effect=fake_raw_recall),
            ) as recall_raw,
        ):
            response = await get_session_bundle(
                query=None,
                task_limit=5,
                memory_limit=3,
                project_ids=["proj_1"],
                org=org,
                ctx=_ctx(),
            )

        assert [memory.id for memory in response.relevant_entities] == [
            "raw_memory:raw_private",
            "raw_memory:raw_project",
        ]
        assert response.relevant_entities[0].entity_type == "raw_memory"
        assert response.relevant_entities[0].memory_scope == "private"
        assert response.relevant_entities[1].scope_key == "proj_1"
        assert [call.kwargs["memory_scope"] for call in recall_raw.await_args_list] == [
            "private",
            "project",
        ]
