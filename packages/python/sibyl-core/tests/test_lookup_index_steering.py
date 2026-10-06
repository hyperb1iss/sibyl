"""Selective lookups stay on the index that holds only their rows.

The SurrealDB 3.x planner picks one index per statement without statistics,
and among several usable equalities it prefers the broadest partition: the
type index for notes (every note of the namespace) and the constant
BELONGS_TO name index for community membership (every such edge). Hints
to indexes that are part of the base graph schema keep those reads at one
point lookup, and epic progress binds the compound parent index with one
equality per epic instead of an IN list the planner cannot union.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import AsyncMock

from sibyl_core.backends.surreal.schema import GRAPH_SCHEMA_MIGRATIONS
from sibyl_core.backends.surreal.schema_version import GRAPH_SCHEMA_CURRENT_VERSION
from sibyl_core.models.entities import EntityType
from sibyl_core.retrieval import _search_expansion
from sibyl_core.services.graph_entities import EntityManager


class _RecordingClient:
    _url = "memory://"
    pool_size = 1

    def __init__(self, batch_results: list[list[dict[str, Any]]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.batches: list[tuple[str, dict[str, Any]]] = []
        self.batch_results = batch_results or []

    async def execute_query(self, query: str, **params: object) -> list[object]:
        self.calls.append((query, dict(params)))
        return []

    async def execute_query_batch(self, query: str, **params: object) -> list[object]:
        self.batches.append((query, dict(params)))
        return list(self.batch_results)


async def test_notes_for_task_hint_the_task_index() -> None:
    client = _RecordingClient()
    manager = EntityManager(client, group_id="org")  # type: ignore[arg-type]

    await manager.get_notes_for_task("task_a", limit=5)

    query, params = client.calls[0]
    assert "FROM entity WITH INDEX idx_entity_task" in query
    assert "AND task_id = $task_id" in query
    assert params == {"group_id": "org", "task_id": "task_a", "limit": 5}


async def test_community_ids_hint_the_source_index() -> None:
    client = _RecordingClient()

    await _search_expansion._community_ids_for_entities(
        client=client, source_uuids=["seed_a", "seed_b"], group_id="org", limit=10
    )

    query, params = client.calls[0]
    assert "FROM relates_to WITH INDEX idx_relates_source" in query
    assert "WHERE source_id IN $source_uuids" in query
    assert params["source_uuids"] == ["seed_a", "seed_b"]


async def test_epic_progress_binds_one_equality_per_epic() -> None:
    client = _RecordingClient(
        batch_results=[
            [{"status": "done", "task_count": 2}, {"status": "doing", "task_count": 1}],
            [],
        ]
    )
    manager = EntityManager(client, group_id="org")  # type: ignore[arg-type]

    progress = await manager._epic_progress_map({"epic_b", "epic_a"}, project_id="project_a")

    assert set(progress) == {"epic_a", "epic_b"}
    assert progress["epic_a"]["total_tasks"] == 3
    assert progress["epic_a"]["completed_tasks"] == 2
    assert progress["epic_b"]["total_tasks"] == 0
    query, params = client.batches[0]
    statements = query.split("\n")
    assert len(statements) == 2
    for index, statement in enumerate(statements):
        assert statement == (
            "SELECT status, count() AS task_count FROM entity "
            "WHERE group_id = $group_id AND entity_type = 'task' "
            f"AND parent_task_id = $epic_{index} AND project_id = $project_id GROUP BY status;"
        )
    assert not re.search(r"parent_task_id IN|attributes\.|WITH INDEX", query)
    assert (params["epic_0"], params["epic_1"], params["project_id"]) == (
        "epic_a",
        "epic_b",
        "project_a",
    )
    assert client.calls == []


async def test_epic_progress_without_epics_issues_no_query() -> None:
    client = _RecordingClient()
    manager = EntityManager(client, group_id="org")  # type: ignore[arg-type]
    manager._client.execute_query_batch = AsyncMock()  # type: ignore[method-assign]

    assert await manager._epic_progress_map(set()) == {}
    manager._client.execute_query_batch.assert_not_awaited()  # type: ignore[union-attr]


def test_migration_37_defines_the_entity_name_index() -> None:
    assert GRAPH_SCHEMA_CURRENT_VERSION >= 37
    migration = next(item for item in GRAPH_SCHEMA_MIGRATIONS if item.version == 37)
    assert migration.name == "entity_name_index"
    assert migration.statements == (
        "DEFINE INDEX IF NOT EXISTS idx_entity_name ON entity FIELDS name;",
    )


async def test_exact_name_search_hints_the_name_index() -> None:
    client = _RecordingClient()
    manager = EntityManager(client, group_id="org")  # type: ignore[arg-type]

    await manager.search_exact_name("Sibyl", entity_types=[EntityType.PROJECT], limit=3)

    query, params = client.calls[0]
    assert "FROM entity WITH INDEX idx_entity_name" in query
    assert "AND name = $name_query" in query
    assert "AND entity_type IN $entity_types" in query
    assert params["name_query"] == "Sibyl"
    assert params["entity_types"] == ["project"]
