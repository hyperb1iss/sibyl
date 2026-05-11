"""Tests for sibyl_core.graph.search_interface."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
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

    def test_surreal_driver_installs_native_search_interface(self) -> None:
        assert isinstance(SurrealDriver("memory://").search_interface, SurrealSearchInterface)
