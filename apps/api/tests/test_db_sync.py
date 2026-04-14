"""Tests for graph-to-Postgres sync helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from sibyl.db import sync as sync_module
from sibyl.db.sync import get_graph_projects
from sibyl_core.models.entities import EntityType


@pytest.mark.asyncio
async def test_get_graph_projects_pages_past_1000() -> None:
    archived_page = [
        SimpleNamespace(
            id="project-archived-1",
            title="Archived 1",
            description="Archived",
            metadata={"status": "archived"},
        ),
        SimpleNamespace(
            id="project-archived-2",
            title="Archived 2",
            description="Archived",
            metadata={"status": "archived"},
        ),
    ]
    live_page = [
        SimpleNamespace(
            id="project-live-1",
            title="Project Live 1",
            description="Description 1",
            metadata={},
        )
    ]
    terminal_page = [
        SimpleNamespace(
            id="project-live-2",
            title="Project Live 2",
            description="Description 2",
            metadata={},
        )
    ]

    manager = MagicMock()
    manager.list_by_type = AsyncMock(side_effect=[archived_page, live_page, terminal_page, []])

    with (
        patch.object(sync_module, "GRAPH_PROJECT_PAGE_SIZE", 2),
        patch("sibyl_core.graph.client.get_graph_client", AsyncMock(return_value=object())),
        patch("sibyl_core.graph.entities.EntityManager", return_value=manager),
    ):
        projects = await get_graph_projects("org-123")

    assert len(projects) == 2
    assert projects[0] == {
        "id": "project-live-1",
        "name": "Project Live 1",
        "description": "Description 1",
    }
    assert projects[1] == {
        "id": "project-live-2",
        "name": "Project Live 2",
        "description": "Description 2",
    }
    assert manager.list_by_type.await_args_list == [
        call(entity_type=EntityType.PROJECT, limit=2, offset=0, include_archived=True),
        call(entity_type=EntityType.PROJECT, limit=2, offset=2, include_archived=True),
        call(entity_type=EntityType.PROJECT, limit=2, offset=4, include_archived=True),
        call(entity_type=EntityType.PROJECT, limit=2, offset=6, include_archived=True),
    ]
