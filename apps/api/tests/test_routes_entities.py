"""Tests for entity route filtering."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import UUID

import pytest

from sibyl.api.routes import entities as entities_routes
from sibyl.api.routes.entities import SortField, SortOrder, list_entities
from sibyl.auth.errors import ProjectAccessDeniedError
from sibyl_core.auth import ProjectRole
from sibyl_core.models.entities import EntityType


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(user=SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000222")))


def _entity(
    entity_id: str,
    *,
    project_id: str | None,
    name: str,
    entity_type: EntityType = EntityType.TASK,
    archived: bool = False,
    status: str | None = None,
    content: str = "",
) -> SimpleNamespace:
    metadata = {"project_id": project_id} if project_id else {}
    if archived:
        metadata["archived"] = True
    if status is not None:
        metadata["status"] = status
    return SimpleNamespace(
        id=entity_id,
        entity_type=entity_type,
        name=name,
        description="",
        content=content,
        metadata=metadata,
        languages=[],
        tags=[],
    )


class TestListEntitiesRoute:
    @pytest.mark.asyncio
    async def test_single_project_entities_push_project_filter_into_graph_query(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        manager = MagicMock()
        manager._surreal_entity_node_ops.return_value = object()
        manager.list_by_type = AsyncMock(
            side_effect=[
                [
                    _entity("ent-match", project_id="proj-1", name="Match"),
                    _entity("ent-other", project_id="proj-2", name="Other"),
                ],
                [],
            ]
        )
        manager.list_all = AsyncMock()
        runtime = SimpleNamespace(entity_manager=manager)

        ctx = _ctx()

        with (
            patch(
                "sibyl.api.routes.entities.get_entity_graph_runtime",
                AsyncMock(return_value=runtime),
            ),
            patch(
                "sibyl.api.routes.entities.verify_entity_project_access",
                AsyncMock(),
            ) as verify_project,
        ):
            response = await list_entities(
                org=org,
                ctx=ctx,
                entity_type=EntityType.TASK,
                language=None,
                category=None,
                search=None,
                project_ids=["proj-1"],
                page=1,
                page_size=50,
                sort_by=SortField.UPDATED_AT,
                sort_order=SortOrder.DESC,
            )

        verify_project.assert_awaited_once_with(
            None,
            ctx,
            "proj-1",
            required_role=ProjectRole.VIEWER,
        )
        assert manager.list_by_type.await_args_list == [
            call(
                EntityType.TASK,
                limit=1000,
                offset=0,
                include_archived=True,
                project_id="proj-1",
            ),
        ]
        manager.list_all.assert_not_awaited()
        assert [entity.id for entity in response.entities] == ["ent-match"]
        assert response.total == 1
        assert response.has_more is False

    @pytest.mark.asyncio
    async def test_mixed_project_and_unassigned_entities_keep_python_filtering(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        manager = MagicMock()
        manager.list_by_type = AsyncMock(
            side_effect=[
                [
                    _entity("ent-match", project_id="proj-1", name="Match"),
                    _entity("ent-other", project_id="proj-2", name="Other"),
                    _entity("ent-unassigned", project_id=None, name="Unassigned"),
                ],
                [],
            ]
        )
        manager.list_all = AsyncMock()
        runtime = SimpleNamespace(entity_manager=manager)

        with (
            patch(
                "sibyl.api.routes.entities.get_entity_graph_runtime",
                AsyncMock(return_value=runtime),
            ),
            patch(
                "sibyl.api.routes.entities.verify_entity_project_access",
                AsyncMock(),
            ),
        ):
            response = await list_entities(
                org=org,
                ctx=_ctx(),
                entity_type=EntityType.TASK,
                language=None,
                category=None,
                search=None,
                project_ids=["proj-1", "__unassigned__"],
                page=1,
                page_size=50,
                sort_by=SortField.UPDATED_AT,
                sort_order=SortOrder.DESC,
            )

        assert manager.list_by_type.await_args_list == [
            call(
                EntityType.TASK,
                limit=1000,
                offset=0,
                include_archived=True,
            ),
        ]
        manager.list_all.assert_not_awaited()
        assert [entity.id for entity in response.entities] == [
            "ent-match",
            "ent-unassigned",
        ]
        assert response.total == 2
        assert response.has_more is False

    @pytest.mark.asyncio
    async def test_category_filter_uses_metadata_for_native_base_entities(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        manager = MagicMock()
        match = _entity(
            "procedure-match",
            project_id=None,
            name="Match",
            entity_type=EntityType.PROCEDURE,
        )
        other = _entity(
            "procedure-other",
            project_id=None,
            name="Other",
            entity_type=EntityType.PROCEDURE,
        )
        match.metadata["category"] = "workflow"
        other.metadata["category"] = "debugging"
        manager.list_by_type = AsyncMock(
            side_effect=[
                [match, other],
                [],
            ]
        )
        manager.list_all = AsyncMock()
        runtime = SimpleNamespace(entity_manager=manager)

        with (
            patch(
                "sibyl.api.routes.entities.get_entity_graph_runtime",
                AsyncMock(return_value=runtime),
            ),
            patch(
                "sibyl.api.routes.entities.list_accessible_project_graph_ids",
                AsyncMock(return_value=set()),
            ),
        ):
            response = await list_entities(
                org=org,
                ctx=_ctx(),
                entity_type=EntityType.PROCEDURE,
                language=None,
                category="work",
                search=None,
                project_ids=None,
                page=1,
                page_size=50,
                sort_by=SortField.UPDATED_AT,
                sort_order=SortOrder.DESC,
            )

        assert [entity.id for entity in response.entities] == ["procedure-match"]
        assert response.entities[0].category == "workflow"

    @pytest.mark.asyncio
    async def test_typed_entity_queries_page_past_first_batch(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        manager = MagicMock()
        manager.list_by_type = AsyncMock(
            side_effect=[
                [
                    _entity(
                        "ent-archived-1",
                        project_id="proj-1",
                        name="Archived 1",
                        archived=True,
                    ),
                    _entity(
                        "ent-archived-2",
                        project_id="proj-2",
                        name="Archived 2",
                        status="archived",
                    ),
                ],
                [
                    _entity("ent-1", project_id="proj-1", name="One"),
                    _entity("ent-2", project_id="proj-2", name="Two"),
                ],
                [
                    _entity("ent-3", project_id="proj-3", name="Three"),
                ],
                [],
            ]
        )
        manager.list_all = AsyncMock()
        runtime = SimpleNamespace(entity_manager=manager)

        with (
            patch.object(entities_routes, "LIST_BY_TYPE_PAGE_SIZE", 2),
            patch(
                "sibyl.api.routes.entities.get_entity_graph_runtime",
                AsyncMock(return_value=runtime),
            ),
            patch(
                "sibyl.api.routes.entities.list_accessible_project_graph_ids",
                AsyncMock(return_value={"proj-1", "proj-2", "proj-3"}),
            ),
        ):
            response = await list_entities(
                org=org,
                ctx=_ctx(),
                entity_type=EntityType.TASK,
                language=None,
                category=None,
                search=None,
                project_ids=None,
                page=1,
                page_size=50,
                sort_by=SortField.UPDATED_AT,
                sort_order=SortOrder.DESC,
            )

        assert manager.list_by_type.await_args_list == [
            call(EntityType.TASK, limit=2, offset=0, include_archived=True),
            call(EntityType.TASK, limit=2, offset=2, include_archived=True),
            call(EntityType.TASK, limit=2, offset=4, include_archived=True),
        ]
        manager.list_all.assert_not_awaited()
        assert [entity.id for entity in response.entities] == ["ent-1", "ent-2", "ent-3"]
        assert response.total == 3
        assert response.has_more is False

    @pytest.mark.asyncio
    async def test_default_surreal_entity_query_stops_after_page_has_more_probe(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        manager = MagicMock()
        manager._surreal_entity_node_ops.return_value = object()
        manager.list_by_type = AsyncMock(
            return_value=[
                _entity("ent-1", project_id="proj-1", name="One"),
                _entity("ent-2", project_id="proj-1", name="Two"),
            ]
        )
        manager.list_all = AsyncMock()
        runtime = SimpleNamespace(entity_manager=manager)

        with (
            patch.object(entities_routes, "LIST_BY_TYPE_PAGE_SIZE", 2),
            patch(
                "sibyl.api.routes.entities.get_entity_graph_runtime",
                AsyncMock(return_value=runtime),
            ),
            patch(
                "sibyl.api.routes.entities.verify_entity_project_access",
                AsyncMock(),
            ),
        ):
            response = await list_entities(
                org=org,
                ctx=_ctx(),
                entity_type=EntityType.TASK,
                language=None,
                category=None,
                search=None,
                project_ids=["proj-1"],
                page=1,
                page_size=1,
                sort_by=SortField.UPDATED_AT,
                sort_order=SortOrder.DESC,
            )

        manager.list_by_type.assert_awaited_once_with(
            EntityType.TASK,
            limit=2,
            offset=0,
            include_archived=True,
            project_id="proj-1",
        )
        manager.list_all.assert_not_awaited()
        assert [entity.id for entity in response.entities] == ["ent-1"]
        assert response.total == 2
        assert response.has_more is True

    @pytest.mark.asyncio
    async def test_native_entity_list_uses_lightweight_payloads(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        manager = MagicMock()
        manager.supports_lightweight_entity_list = True
        manager._surreal_entity_node_ops.return_value = object()
        manager.list_by_type = AsyncMock(
            return_value=[
                _entity(
                    "ent-1",
                    project_id="proj-1",
                    name="One",
                    content="x" * 10000,
                )
            ]
        )
        manager.list_all = AsyncMock()
        runtime = SimpleNamespace(entity_manager=manager)

        with (
            patch.object(entities_routes, "LIST_BY_TYPE_PAGE_SIZE", 2),
            patch(
                "sibyl.api.routes.entities.get_entity_graph_runtime",
                AsyncMock(return_value=runtime),
            ),
            patch(
                "sibyl.api.routes.entities.verify_entity_project_access",
                AsyncMock(),
            ),
        ):
            response = await list_entities(
                org=org,
                ctx=_ctx(),
                entity_type=EntityType.TASK,
                language=None,
                category=None,
                search=None,
                project_ids=["proj-1"],
                page=1,
                page_size=1,
                sort_by=SortField.UPDATED_AT,
                sort_order=SortOrder.DESC,
            )

        manager.list_by_type.assert_awaited_once_with(
            EntityType.TASK,
            limit=2,
            offset=0,
            include_archived=True,
            include_content=False,
            project_id="proj-1",
        )
        assert response.entities[0].content == ""

    @pytest.mark.asyncio
    async def test_default_legacy_entity_query_keeps_exhaustive_sorting(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        manager = MagicMock()
        manager._surreal_entity_node_ops.return_value = None
        older = _entity("older-returned-first", project_id=None, name="Older")
        newer = _entity("newer-returned-second", project_id=None, name="Newer")
        older.updated_at = datetime(2024, 1, 1, tzinfo=UTC)
        newer.updated_at = datetime(2025, 1, 1, tzinfo=UTC)
        manager.list_by_type = AsyncMock(
            side_effect=[
                [older],
                [newer],
                [],
            ]
        )
        manager.list_all = AsyncMock()
        runtime = SimpleNamespace(entity_manager=manager)

        with (
            patch.object(entities_routes, "LIST_BY_TYPE_PAGE_SIZE", 1),
            patch(
                "sibyl.api.routes.entities.get_entity_graph_runtime",
                AsyncMock(return_value=runtime),
            ),
            patch(
                "sibyl.api.routes.entities.list_accessible_project_graph_ids",
                AsyncMock(return_value=set()),
            ),
        ):
            response = await list_entities(
                org=org,
                ctx=_ctx(),
                entity_type=EntityType.TASK,
                language=None,
                category=None,
                search=None,
                project_ids=None,
                page=1,
                page_size=1,
                sort_by=SortField.UPDATED_AT,
                sort_order=SortOrder.DESC,
            )

        assert [entity.id for entity in response.entities] == ["newer-returned-second"]
        assert response.total == 2
        assert response.has_more is True

    @pytest.mark.asyncio
    async def test_untyped_project_filters_apply_to_bounded_batch(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        manager = MagicMock()
        manager._surreal_entity_node_ops.return_value = None
        archived_page = [
            _entity("ent-archived-1", project_id="proj-1", name="Archived 1", archived=True),
            _entity("ent-archived-2", project_id="proj-1", name="Archived 2", archived=True),
        ]
        live_page = [
            _entity("ent-match", project_id="proj-1", name="Match"),
            _entity("ent-unassigned", project_id=None, name="Unassigned"),
        ]
        manager.list_by_type = AsyncMock()
        manager.list_all = AsyncMock(return_value=archived_page + live_page)
        runtime = SimpleNamespace(entity_manager=manager)

        with (
            patch.object(entities_routes, "LIST_ALL_PAGE_SIZE", 2),
            patch(
                "sibyl.api.routes.entities.get_entity_graph_runtime",
                AsyncMock(return_value=runtime),
            ),
            patch(
                "sibyl.api.routes.entities.verify_entity_project_access",
                AsyncMock(),
            ),
        ):
            response = await list_entities(
                org=org,
                ctx=_ctx(),
                entity_type=None,
                language=None,
                category=None,
                search=None,
                project_ids=["proj-1", "__unassigned__"],
                page=1,
                page_size=50,
                sort_by=SortField.UPDATED_AT,
                sort_order=SortOrder.DESC,
            )

        manager.list_by_type.assert_not_awaited()
        manager.list_all.assert_awaited_once_with(limit=2, offset=0, include_archived=True)
        assert [entity.id for entity in response.entities] == ["ent-match", "ent-unassigned"]
        assert response.total == 2
        assert response.has_more is False

    @pytest.mark.asyncio
    async def test_default_entity_list_filters_to_accessible_projects_and_unassigned(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        manager = MagicMock()
        manager._surreal_entity_node_ops.return_value = object()
        manager.list_by_type = AsyncMock(
            return_value=[
                _entity("ent-match", project_id="proj-1", name="Match"),
                _entity("ent-hidden", project_id="proj-2", name="Hidden"),
                _entity("ent-unassigned", project_id=None, name="Unassigned"),
            ]
        )
        manager.list_all = AsyncMock()
        runtime = SimpleNamespace(entity_manager=manager)

        with (
            patch(
                "sibyl.api.routes.entities.get_entity_graph_runtime",
                AsyncMock(return_value=runtime),
            ),
            patch(
                "sibyl.api.routes.entities.list_accessible_project_graph_ids",
                AsyncMock(return_value={"proj-1"}),
            ) as list_projects,
        ):
            response = await list_entities(
                org=org,
                ctx=_ctx(),
                entity_type=EntityType.TASK,
                language=None,
                category=None,
                search=None,
                project_ids=None,
                page=1,
                page_size=50,
                sort_by=SortField.UPDATED_AT,
                sort_order=SortOrder.DESC,
            )

        list_projects.assert_awaited_once()
        assert [entity.id for entity in response.entities] == [
            "ent-match",
            "ent-unassigned",
        ]

    @pytest.mark.asyncio
    async def test_project_entity_list_filters_projects_by_entity_id(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        manager = MagicMock()
        manager._surreal_entity_node_ops.return_value = object()
        manager.list_by_type = AsyncMock(
            return_value=[
                _entity(
                    "project-visible",
                    project_id=None,
                    name="Visible",
                    entity_type=EntityType.PROJECT,
                ),
                _entity(
                    "project-hidden",
                    project_id=None,
                    name="Hidden",
                    entity_type=EntityType.PROJECT,
                ),
            ]
        )
        manager.list_all = AsyncMock()
        runtime = SimpleNamespace(entity_manager=manager)

        with (
            patch(
                "sibyl.api.routes.entities.get_entity_graph_runtime",
                AsyncMock(return_value=runtime),
            ),
            patch(
                "sibyl.api.routes.entities.list_accessible_project_graph_ids",
                AsyncMock(return_value={"project-visible"}),
            ),
        ):
            response = await list_entities(
                org=org,
                ctx=_ctx(),
                entity_type=EntityType.PROJECT,
                language=None,
                category=None,
                search=None,
                project_ids=None,
                page=1,
                page_size=50,
                sort_by=SortField.UPDATED_AT,
                sort_order=SortOrder.DESC,
            )

        manager.list_by_type.assert_awaited_once_with(
            EntityType.PROJECT,
            limit=1000,
            offset=0,
            include_archived=True,
        )
        assert [entity.id for entity in response.entities] == ["project-visible"]

    @pytest.mark.asyncio
    async def test_untyped_entity_list_filters_private_project_fixture_shapes(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        manager = MagicMock()
        manager._surreal_entity_node_ops.return_value = object()
        manager.list_by_type = AsyncMock()
        manager.list_all = AsyncMock(
            return_value=[
                _entity("task-visible", project_id="project-visible", name="Visible"),
                _entity("task-hidden", project_id="project-hidden", name="Hidden"),
                _entity("pattern-unassigned", project_id=None, name="Unassigned"),
                _entity(
                    "project-visible",
                    project_id=None,
                    name="Visible Project",
                    entity_type=EntityType.PROJECT,
                ),
                _entity(
                    "project-hidden",
                    project_id=None,
                    name="Hidden Project",
                    entity_type=EntityType.PROJECT,
                ),
            ]
        )
        runtime = SimpleNamespace(entity_manager=manager)

        with (
            patch(
                "sibyl.api.routes.entities.get_entity_graph_runtime",
                AsyncMock(return_value=runtime),
            ),
            patch(
                "sibyl.api.routes.entities.list_accessible_project_graph_ids",
                AsyncMock(return_value={"project-visible"}),
            ),
        ):
            response = await list_entities(
                org=org,
                ctx=_ctx(),
                entity_type=None,
                language=None,
                category=None,
                search=None,
                project_ids=None,
                page=1,
                page_size=50,
                sort_by=SortField.UPDATED_AT,
                sort_order=SortOrder.DESC,
            )

        manager.list_by_type.assert_not_awaited()
        manager.list_all.assert_awaited_once_with(
            limit=2000,
            offset=0,
            include_archived=True,
        )
        assert [entity.id for entity in response.entities] == [
            "task-visible",
            "pattern-unassigned",
            "project-visible",
        ]
        assert response.total == 3

    @pytest.mark.asyncio
    async def test_entity_list_rejects_inaccessible_project_filter(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))

        with (
            patch(
                "sibyl.api.routes.entities.verify_entity_project_access",
                AsyncMock(
                    side_effect=ProjectAccessDeniedError(
                        project_id="proj-2",
                        required_role=ProjectRole.VIEWER.value,
                    )
                ),
            ),
            pytest.raises(ProjectAccessDeniedError) as exc,
        ):
            await list_entities(
                org=org,
                ctx=_ctx(),
                entity_type=EntityType.TASK,
                language=None,
                category=None,
                search=None,
                project_ids=["proj-2"],
                page=1,
                page_size=50,
                sort_by=SortField.UPDATED_AT,
                sort_order=SortOrder.DESC,
            )

        assert exc.value.status_code == 403
        assert exc.value.detail["error"] == "project_access_denied"
