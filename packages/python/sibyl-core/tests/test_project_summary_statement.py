"""A project summary is one grouped statement, not a walk over every task."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services.graph_entities import EntityManager

GROUP_ID = "org-summary"
PROJECT_ID = "project_hub"


def _task(uuid: str, status: str, priority: str = "medium") -> dict[str, Any]:
    return {"uuid": uuid, "name": f"Task {uuid}", "status": status, "priority": priority}


class _SummaryClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.queries: list[tuple[str, dict[str, Any]]] = []

    async def execute_query(self, query: str, **params: Any) -> list[dict[str, Any]]:
        self.queries.append((query, params))
        if "GROUP BY status" in query and "doing:" in query:
            return [self.payload]
        if "entity_type = $entity_type" in query:
            return []
        return []


@pytest.mark.asyncio
async def test_summary_reads_counts_and_buckets_from_one_statement() -> None:
    client = _SummaryClient(
        {
            "status_counts": [
                {"status": "todo", "n": 500},
                {"status": "done", "n": 393},
                {"status": "doing", "n": 2},
                {"status": None, "n": 8},
            ],
            "doing": [_task("d1", "doing"), _task("d2", "doing")],
            "blocked": [_task("b1", "blocked")],
            "review": [],
            "recent": [_task("r1", "todo"), _task("r2", "todo"), _task("r3", "todo")],
            "critical": [_task("c1", "todo", "critical")],
            "high": [_task("h1", "todo", "high"), _task("h2", "todo", "high")],
            "flagged": [_task("f1", "todo", "someday")],
        }
    )
    manager = EntityManager(client, group_id=GROUP_ID)  # type: ignore[arg-type]

    summary = await manager.get_project_summary(PROJECT_ID)

    task_statements = [(q, p) for q, p in client.queries if "FROM entity" in q]
    assert len(task_statements) == 2  # the summary RETURN and the epic list
    statement, params = task_statements[0]
    assert "SELECT *" not in statement
    assert "LIMIT $actionable_limit" in statement
    assert "LIMIT $critical_limit" in statement
    assert "START" not in statement
    assert params == {
        "group_id": GROUP_ID,
        "project_id": PROJECT_ID,
        "actionable_limit": 5,
        "critical_limit": 3,
    }
    assert summary["status_counts"] == {"todo": 508, "done": 393, "doing": 2}
    assert summary["total_tasks"] == 903
    assert summary["progress_pct"] == 43.5
    assert [task["id"] for task in summary["actionable_tasks"]] == ["d1", "d2", "b1", "r1", "r2"]
    assert [task["id"] for task in summary["critical_tasks"]] == ["c1", "h1", "h2"]
    assert summary["epics"] == []


@pytest.mark.asyncio
async def test_summary_only_asks_progress_for_the_epics_it_lists() -> None:
    client = _SummaryClient({"status_counts": []})
    manager = EntityManager(client, group_id=GROUP_ID)  # type: ignore[arg-type]
    epic = Entity(
        id="epic_one",
        entity_type=EntityType.EPIC,
        name="Epic one",
        description="",
        organization_id=GROUP_ID,
        metadata={"status": "active"},
    )
    manager.list_epics_for_project = AsyncMock(return_value=[epic])  # type: ignore[method-assign]
    manager._epic_progress_map = AsyncMock(  # type: ignore[method-assign]
        return_value={"epic_one": {"total_tasks": 4, "completed_tasks": 1}}
    )

    summary = await manager.get_project_summary(PROJECT_ID, epic_limit=2)

    manager.list_epics_for_project.assert_awaited_once_with(
        PROJECT_ID, limit=2, enrich_progress=False
    )
    manager._epic_progress_map.assert_awaited_once_with({"epic_one"}, project_id=PROJECT_ID)
    assert summary["epics"] == [
        {
            "id": "epic_one",
            "name": "Epic one",
            "status": "active",
            "progress_pct": 25.0,
            "total_tasks": 4,
        }
    ]
    assert summary["total_tasks"] == 0
