"""Tests for graph-to-Postgres sync helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from sibyl.db.sync import get_graph_projects
from sibyl_core.models.entities import EntityType


@pytest.mark.asyncio
async def test_get_graph_projects_pages_past_1000() -> None:
    page_one = [
        SimpleNamespace(id=f"project-{i}", title=f"Project {i}", description=f"Description {i}")
        for i in range(1000)
    ]
    page_two = [
        SimpleNamespace(
            id=f"project-{i}",
            title=f"Project {i}",
            description=f"Description {i}",
        )
        for i in range(1000, 1250)
    ]

    manager = MagicMock()
    manager.list_by_type = AsyncMock(side_effect=[page_one, page_two, []])

    with (
        patch("sibyl_core.graph.client.get_graph_client", AsyncMock(return_value=object())),
        patch("sibyl_core.graph.entities.EntityManager", return_value=manager),
    ):
        projects = await get_graph_projects("org-123")

    assert len(projects) == 1250
    assert projects[0] == {
        "id": "project-0",
        "name": "Project 0",
        "description": "Description 0",
    }
    assert projects[-1] == {
        "id": "project-1249",
        "name": "Project 1249",
        "description": "Description 1249",
    }
    assert manager.list_by_type.await_args_list == [
        call(entity_type=EntityType.PROJECT, limit=1000, offset=0),
        call(entity_type=EntityType.PROJECT, limit=1000, offset=1000),
        call(entity_type=EntityType.PROJECT, limit=1000, offset=2000),
    ]
