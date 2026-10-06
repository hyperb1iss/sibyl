"""Explore list pages a single type over one database window.

Before this, a page at offset k fetched ``limit + k + 50`` rows from
``START 0`` and sliced in Python, so the task board's pager read every row
of every earlier page again on each page (O(n^2) over the type).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from sibyl_core.services.graph_entities import EntityManager
from sibyl_core.tools.explore import explore

GROUP_ID = "org-window"
PROJECT_ID = "project_visible"


def _task_row(
    index: int,
    *,
    project_id: str = PROJECT_ID,
    archived_in_attributes: bool = False,
) -> dict[str, Any]:
    stamp = datetime(2026, 10, 1, tzinfo=UTC) - timedelta(minutes=index)
    attributes: dict[str, Any] = {
        "description": "",
        "entity_type": "task",
        "project_id": project_id,
    }
    if archived_in_attributes:
        # A legacy row keeps its status under attributes only, so the DB
        # predicate admits it and the Python recheck drops it.
        attributes["status"] = "archived"
    return {
        "uuid": f"task_{index:04d}",
        "name": f"Task {index}",
        "entity_type": "task",
        "group_id": GROUP_ID,
        "project_id": project_id,
        "status": None if archived_in_attributes else "todo",
        "attributes": attributes,
        "created_at": stamp,
        "updated_at": stamp,
    }


class _WindowClient:
    """Serves entity rows by the LIMIT/START the statement binds."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.queries: list[tuple[str, dict[str, Any]]] = []

    async def execute_query(self, query: str, **params: Any) -> list[dict[str, Any]]:
        self.queries.append((query, params))
        if "FROM entity" not in query:
            return []
        start = int(params.get("offset", 0))
        limit = int(params.get("limit", len(self.rows)))
        return [dict(row) for row in self.rows[start : start + limit]]

    def entity_statements(self) -> list[tuple[str, dict[str, Any]]]:
        return [(query, params) for query, params in self.queries if "FROM entity" in query]


def _runtime(rows: list[dict[str, Any]]) -> tuple[SimpleNamespace, _WindowClient]:
    client = _WindowClient(rows)
    manager = EntityManager(client, group_id=GROUP_ID)  # type: ignore[arg-type]
    runtime = SimpleNamespace(client=client, entity_manager=manager, relationship_manager=None)
    return runtime, client


async def _list_tasks(runtime: SimpleNamespace, *, limit: int, offset: int):
    with patch("sibyl_core.tools.explore.get_graph_runtime", AsyncMock(return_value=runtime)):
        return await explore(
            mode="list",
            types=["task"],
            limit=limit,
            offset=offset,
            organization_id=GROUP_ID,
            principal_id="reader",
            accessible_projects={PROJECT_ID},
        )


@pytest.mark.asyncio
async def test_page_three_reads_only_its_own_window() -> None:
    runtime, client = _runtime([_task_row(index) for index in range(45)])

    response = await _list_tasks(runtime, limit=10, offset=30)

    statements = client.entity_statements()
    assert len(statements) == 1, [params for _, params in statements]
    query, params = statements[0]
    assert "START $offset" in query
    assert params["offset"] == 30
    assert params["limit"] == 11
    assert [entity.id for entity in response.entities] == [
        f"task_{index:04d}" for index in range(30, 40)
    ]
    assert response.total == 10
    assert response.has_more is True
    assert response.next_offset == 40


@pytest.mark.asyncio
async def test_last_window_reports_no_more() -> None:
    runtime, client = _runtime([_task_row(index) for index in range(45)])

    response = await _list_tasks(runtime, limit=10, offset=40)

    assert [entity.id for entity in response.entities] == [
        f"task_{index:04d}" for index in range(40, 45)
    ]
    assert response.has_more is False
    assert response.next_offset is None
    assert len(client.entity_statements()) == 1


@pytest.mark.asyncio
async def test_dropped_rows_shorten_the_page_without_moving_the_next_window() -> None:
    rows = [_task_row(index) for index in range(12)]
    rows[3] = _task_row(3, project_id="project_hidden")
    rows[5] = _task_row(5, archived_in_attributes=True)
    runtime, client = _runtime(rows)

    response = await _list_tasks(runtime, limit=10, offset=0)

    ids = [entity.id for entity in response.entities]
    assert "task_0003" not in ids
    assert "task_0005" not in ids
    assert len(ids) == 8
    assert response.has_more is True
    assert response.next_offset == 10
    assert response.actual_total is None
    assert len(client.entity_statements()) == 1


@pytest.mark.asyncio
async def test_single_complete_window_reports_its_total() -> None:
    runtime, _client = _runtime([_task_row(index) for index in range(4)])

    response = await _list_tasks(runtime, limit=10, offset=0)

    assert response.has_more is False
    assert response.actual_total == 4
