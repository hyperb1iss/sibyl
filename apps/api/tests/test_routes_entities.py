"""Tests for entity route filtering."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import UUID

import pytest

from sibyl.api.routes import entities as entities_routes
from sibyl.api.routes.entities import SortField, SortOrder, list_entities
from sibyl_core.models.entities import EntityType


def _entity(
    entity_id: str,
    *,
    project_id: str | None,
    name: str,
    archived: bool = False,
) -> SimpleNamespace:
    metadata = {"project_id": project_id} if project_id else {}
    if archived:
        metadata["archived"] = True
    return SimpleNamespace(
        id=entity_id,
        entity_type=EntityType.TASK,
        name=name,
        description="",
        content="",
        metadata=metadata,
        languages=[],
        tags=[],
    )


class TestListEntitiesRoute:
    @pytest.mark.asyncio
    async def test_single_project_entities_push_project_filter_into_graph_query(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        client = object()
        manager = MagicMock()
        manager.list_by_type = AsyncMock(
            return_value=[
                _entity("ent-match", project_id="proj-1", name="Match"),
                _entity("ent-other", project_id="proj-2", name="Other"),
            ]
        )
        manager.list_all = AsyncMock()

        with (
            patch("sibyl.api.routes.entities.get_graph_client", AsyncMock(return_value=client)),
            patch("sibyl.api.routes.entities.EntityManager", return_value=manager),
        ):
            response = await list_entities(
                org=org,
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

        manager.list_by_type.assert_awaited_once_with(
            EntityType.TASK,
            limit=1000,
            project_id="proj-1",
        )
        manager.list_all.assert_not_awaited()
        assert [entity.id for entity in response.entities] == ["ent-match"]
        assert response.total == 1
        assert response.has_more is False

    @pytest.mark.asyncio
    async def test_mixed_project_and_unassigned_entities_keep_python_filtering(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        client = object()
        manager = MagicMock()
        manager.list_by_type = AsyncMock(
            return_value=[
                _entity("ent-match", project_id="proj-1", name="Match"),
                _entity("ent-unassigned", project_id=None, name="Unassigned"),
                _entity("ent-other", project_id="proj-2", name="Other"),
            ]
        )
        manager.list_all = AsyncMock()

        with (
            patch("sibyl.api.routes.entities.get_graph_client", AsyncMock(return_value=client)),
            patch("sibyl.api.routes.entities.EntityManager", return_value=manager),
        ):
            response = await list_entities(
                org=org,
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

        manager.list_by_type.assert_awaited_once_with(EntityType.TASK, limit=1000)
        manager.list_all.assert_not_awaited()
        assert [entity.id for entity in response.entities] == [
            "ent-match",
            "ent-unassigned",
        ]
        assert response.total == 2
        assert response.has_more is False

    @pytest.mark.asyncio
    async def test_untyped_project_filters_skip_archived_only_pages(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        client = object()
        manager = MagicMock()
        archived_page = [
            _entity("ent-archived-1", project_id="proj-1", name="Archived 1", archived=True),
            _entity("ent-archived-2", project_id="proj-1", name="Archived 2", archived=True),
        ]
        live_page = [
            _entity("ent-match", project_id="proj-1", name="Match"),
            _entity("ent-unassigned", project_id=None, name="Unassigned"),
        ]
        manager.list_by_type = AsyncMock()
        manager.list_all = AsyncMock(side_effect=[archived_page, live_page, []])

        with (
            patch.object(entities_routes, "LIST_ALL_PAGE_SIZE", 2),
            patch("sibyl.api.routes.entities.get_graph_client", AsyncMock(return_value=client)),
            patch("sibyl.api.routes.entities.EntityManager", return_value=manager),
        ):
            response = await list_entities(
                org=org,
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
        assert manager.list_all.await_args_list == [
            call(limit=2, offset=0, include_archived=True),
            call(limit=2, offset=2, include_archived=True),
            call(limit=2, offset=4, include_archived=True),
        ]
        assert [entity.id for entity in response.entities] == ["ent-match", "ent-unassigned"]
        assert response.total == 2
        assert response.has_more is False
