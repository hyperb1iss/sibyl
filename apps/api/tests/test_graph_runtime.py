from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sibyl.persistence import graph_runtime


class _ProjectDeleteDriver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query_raw(self, query: str, **params: object) -> object:
        self.calls.append((query, params))
        return {
            "result": [
                {"status": "OK", "result": []},
                {"status": "OK", "result": []},
                {"status": "OK", "result": []},
            ]
        }


class _StatsDriver:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def execute_query(self, query: str, **params: object) -> object:
        self.queries.append(query)
        if "FROM episode" in query or "FROM mentions" in query:
            raise AssertionError("archive-only graph tables must not feed live stats")
        if "FROM entity" in query and "GROUP BY entity_type" in query:
            return [{"entity_type": "task", "cnt": 2}]
        if "FROM relates_to" in query and "GROUP BY name" in query:
            return [{"relationship_type": "depends_on", "cnt": 1}]
        if "FROM community" in query:
            return [{"cnt": 3}]
        return []


class _StatsPayloadClient:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def execute_query(self, query: str, **params: object) -> object:
        self.queries.append(query)
        return [
            {
                "entity_types": [{"entity_type": "task", "cnt": 2}],
                "community_count": [{"cnt": 3}],
                "saga_count": [{"cnt": 0}],
            }
        ]


@pytest.mark.asyncio
async def test_delete_project_graph_data_sweeps_project_scoped_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _ProjectDeleteDriver()

    monkeypatch.setattr(
        graph_runtime,
        "_get_graph_runtime",
        AsyncMock(return_value=SimpleNamespace(client=driver)),
    )
    monkeypatch.setattr(graph_runtime, "_surreal_driver_for", lambda candidate: candidate)

    await graph_runtime.delete_project_graph_data("org-123", "project-alpha")

    assert len(driver.calls) == 1
    query, params = driver.calls[0]
    assert "BEGIN TRANSACTION;" in query
    assert "LET $project_entity_ids" in query
    assert "LET $project_episode_ids" in query
    assert "DELETE FROM relates_to" in query
    assert "DELETE FROM mentions" in query
    assert "DELETE FROM has_episode" in query
    assert "DELETE FROM next_episode" in query
    assert "DELETE FROM has_member" in query
    assert "DELETE FROM entity" in query
    assert "DELETE FROM episode" in query
    assert "COMMIT TRANSACTION;" in query
    assert params == {"group_id": "org-123", "project_id": "project-alpha"}


@pytest.mark.asyncio
async def test_graph_search_index_stats_skips_archive_only_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _StatsDriver()
    index = graph_runtime.GraphSearchIndex(
        driver,
        "org-123",
        SimpleNamespace(),
    )

    monkeypatch.setattr(graph_runtime, "_driver_for_client", lambda client, group_id: driver)
    monkeypatch.setattr(graph_runtime, "_surreal_driver_for", lambda candidate: candidate)

    stats = await index.stats()

    assert stats.entities_by_type == {"task": 2, "community": 3}
    assert stats.relationships_by_type == {"depends_on": 1}
    assert all("FROM episode" not in query for query in driver.queries)
    assert all("FROM mentions" not in query for query in driver.queries)


@pytest.mark.asyncio
async def test_graph_stats_payload_skips_archive_only_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _StatsPayloadClient()
    monkeypatch.setattr(
        graph_runtime,
        "_get_graph_runtime",
        AsyncMock(return_value=SimpleNamespace(client=client)),
    )

    payload = await graph_runtime._graph_stats_payload("org-123")

    assert payload["entity_counts"]["task"] == 2
    assert payload["entity_counts"]["community"] == 3
    assert payload["entity_counts"]["episode"] == 0
    assert all("episode_count" not in query for query in client.queries)
