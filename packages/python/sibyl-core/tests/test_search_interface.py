"""Tests for sibyl_core.graph.search_interface."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from graphiti_core.search.search_filters import ComparisonOperator, DateFilter, SearchFilters

from sibyl_core.backends.surreal import SurrealDriver
from sibyl_core.graph.search_interface import SurrealSearchInterface


class FakeSurrealSearchDriver:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records = records
        self.queries: list[tuple[str, dict[str, Any]]] = []

    def build_fulltext_query(self, query: str) -> str:
        return query.strip()

    async def execute_query(self, cypher_query_: str, **params: Any) -> list[dict[str, Any]]:
        self.queries.append((cypher_query_, params))
        return deepcopy(self.records)


class FakeEdgeFulltextSearchDriver:
    def __init__(self) -> None:
        self.queries: list[tuple[str, dict[str, Any]]] = []

    def build_fulltext_query(self, query: str) -> str:
        return query.strip()

    async def execute_query(self, cypher_query_: str, **params: Any) -> list[dict[str, Any]]:
        self.queries.append((cypher_query_, params))
        if "fact @0@" in cypher_query_:
            return [
                {"uuid": "edge-1", "score": 0.9},
                {"uuid": "edge-drop", "score": 0.8},
                {"uuid": "edge-2", "score": 0.7},
                {"uuid": "edge-3", "score": 0.6},
                {"uuid": "edge-4", "score": 0.5},
            ]
        now = datetime.now(UTC)
        return [
            {
                "uuid": "edge-4",
                "name": "RELATES_TO",
                "fact": "Later match",
                "fact_embedding": None,
                "group_id": "org-1",
                "episodes": [],
                "attributes": {"project_id": "project-1"},
                "created_at": now,
                "expired_at": None,
                "valid_at": now,
                "invalid_at": None,
                "source_node_uuid": "src-4",
                "target_node_uuid": "tgt-4",
            },
            {
                "uuid": "edge-1",
                "name": "RELATES_TO",
                "fact": "Surreal planner warning",
                "fact_embedding": None,
                "group_id": "org-1",
                "episodes": [],
                "attributes": {"project_id": "project-1"},
                "created_at": now,
                "expired_at": None,
                "valid_at": now,
                "invalid_at": None,
                "source_node_uuid": "src-1",
                "target_node_uuid": "tgt-1",
            },
            {
                "uuid": "edge-3",
                "name": "RELATES_TO",
                "fact": "Third match",
                "fact_embedding": None,
                "group_id": "org-1",
                "episodes": [],
                "attributes": {"project_id": "project-1"},
                "created_at": now,
                "expired_at": None,
                "valid_at": now,
                "invalid_at": None,
                "source_node_uuid": "src-3",
                "target_node_uuid": "tgt-3",
            },
            {
                "uuid": "edge-2",
                "name": "RELATES_TO",
                "fact": "Second match",
                "fact_embedding": None,
                "group_id": "org-1",
                "episodes": [],
                "attributes": {"project_id": "project-1"},
                "created_at": now,
                "expired_at": None,
                "valid_at": now,
                "invalid_at": None,
                "source_node_uuid": "src-2",
                "target_node_uuid": "tgt-2",
            },
        ]


class TestSurrealSearchInterface:
    @pytest.mark.asyncio
    async def test_node_fulltext_search_uses_surrealql(self) -> None:
        now = datetime.now(UTC)
        driver = FakeSurrealSearchDriver(
            [
                {
                    "uuid": "node-1",
                    "name": "Graph Memory",
                    "name_embedding": None,
                    "group_id": "org-1",
                    "labels": ["Entity", "Pattern"],
                    "created_at": now,
                    "summary": "Surreal graph search",
                    "attributes": {},
                }
            ]
        )

        result = await SurrealSearchInterface().node_fulltext_search(
            driver,
            "graph memory",
            SearchFilters(node_labels=["Pattern"]),
            ["org-1"],
            5,
        )

        query, params = driver.queries[0]
        assert "CALL " not in query
        assert "MATCH " not in query
        assert "FROM entity" in query
        assert "name @0@ $query" in query
        assert params["group_ids"] == ["org-1"]
        assert params["node_label"] == "Pattern"
        assert result[0].uuid == "node-1"

    @pytest.mark.asyncio
    async def test_edge_similarity_search_uses_surreal_vector_query(self) -> None:
        now = datetime.now(UTC)
        driver = FakeSurrealSearchDriver(
            [
                {
                    "uuid": "edge-1",
                    "name": "RELATES_TO",
                    "fact": "Sibyl uses SurrealDB",
                    "fact_embedding": [0.1, 0.2],
                    "group_id": "org-1",
                    "episodes": [],
                    "attributes": {},
                    "created_at": now,
                    "expired_at": None,
                    "valid_at": now,
                    "invalid_at": None,
                    "source_node_uuid": "src-1",
                    "target_node_uuid": "tgt-1",
                }
            ]
        )

        result = await SurrealSearchInterface().edge_similarity_search(
            driver,
            [0.1, 0.2],
            "src-1",
            "tgt-1",
            SearchFilters(
                edge_uuids=["edge-1"],
                valid_at=[
                    [
                        DateFilter(
                            date=now,
                            comparison_operator=ComparisonOperator.greater_than_equal,
                        )
                    ]
                ],
            ),
            ["org-1"],
            5,
            0.6,
        )

        query, params = driver.queries[0]
        assert "CALL " not in query
        assert "MATCH " not in query
        assert "FROM relates_to" in query
        assert "fact_embedding <|20, 40|> $search_vector" in query
        assert params["source_node_uuid"] == "src-1"
        assert params["target_node_uuid"] == "tgt-1"
        assert params["edge_uuids"] == ["edge-1"]
        assert params["valid_at_0_0"] == now
        assert "valid_at >= $valid_at_0_0" in query
        assert result[0].uuid == "edge-1"

    @pytest.mark.asyncio
    async def test_edge_fulltext_splits_match_from_relation_hydration(self) -> None:
        driver = FakeEdgeFulltextSearchDriver()

        result = await SurrealSearchInterface().edge_fulltext_search(
            driver,
            "surreal planner",
            SimpleNamespace(
                project_ids=("project-1",),
                edge_uuids=("edge-1", "edge-2", "edge-3", "edge-4"),
                edge_types=("RELATES_TO",),
            ),
            ["org-1"],
            3,
        )

        assert [edge.uuid for edge in result] == ["edge-1", "edge-2", "edge-3"]
        match_query = driver.queries[0][0]
        hydrate_query = driver.queries[1][0]
        assert "fact @0@ $query" in match_query
        assert "in." not in match_query
        assert "out." not in match_query
        assert "attributes." not in match_query
        assert "uuid IN $edge_uuids" in match_query
        assert "name IN $edge_types" in match_query
        assert driver.queries[0][1]["match_limit"] == 32
        assert "fact @0@ $query" not in hydrate_query
        assert "uuid IN $match_uuids" in hydrate_query

    @pytest.mark.asyncio
    async def test_episode_fulltext_skips_project_filtered_search(self) -> None:
        driver = FakeSurrealSearchDriver([])

        result = await SurrealSearchInterface().episode_fulltext_search(
            driver,
            "raw memory",
            SimpleNamespace(project_ids=("project-1",)),
            ["org-1"],
            5,
        )

        assert result == []
        assert driver.queries == []

    def test_surreal_driver_installs_native_search_interface(self) -> None:
        assert isinstance(SurrealDriver("memory://").search_interface, SurrealSearchInterface)
