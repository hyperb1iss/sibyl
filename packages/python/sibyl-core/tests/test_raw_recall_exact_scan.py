"""A small recall scope is scored exactly through its index; a large one walks the HNSW index.

A scope filter inside the HNSW bracket makes the walk visit captures until
enough of them pass. On SurrealDB 4.0.1 with 9,800 captures that took 1.8 s
for a range of 112 captures and 2.1 s to return nothing from an empty scope;
the exact read of the same ranges took 27 ms and 7 ms and returned the same
rows in the same order.
"""

from __future__ import annotations

import pytest

from sibyl_core.services import content_models, content_raw_recall
from sibyl_core.services.content_raw_recall import (
    RAW_CAPTURE_EXACT_SCAN_MAX_ROWS,
    _raw_scope_index_ranges,
    _raw_vector_exact_index,
    _recall_raw_memory_vector,
)

PRIVATE = {"organization_id": "org", "memory_scope": "private", "principal_id": "owner"}
PROJECT = {"organization_id": "org", "memory_scope": "project", "scope_key": "p1"}


class _CountingClient:
    """Answers scope counts by the range they name and records every statement."""

    def __init__(self, *, scope: object, project: object = None) -> None:
        self.totals = {"scope": scope, "project": project}
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> object:
        self.calls.append((query, params))
        total = self.totals["project" if "project_id" in params else "scope"]
        if isinstance(total, BaseException):
            raise total
        return [{"status": "OK", "result": [{"total": total}]}]

    async def execute_query_raw(self, query: str, **params: object) -> object:
        self.calls.append((query, params))
        return {"id": "fake", "result": [{"status": "OK", "result": []}]}

    def counts(self) -> list[str]:
        return [query for query, _ in self.calls if "count() AS total" in query]


def _query_embedding() -> content_raw_recall.RawQueryEmbedding:
    space = content_models.RawEmbeddingSpace(
        provider="deterministic", model="unit-test", dimensions=2
    )
    return content_raw_recall.RawQueryEmbedding([1.0, 0.0], space)


def test_scope_ranges_follow_the_scope_parameters() -> None:
    assert [(r.index, r.fields) for r in _raw_scope_index_ranges(PRIVATE)] == [
        ("idx_raw_captures_private_recent", ("organization_id", "memory_scope", "principal_id"))
    ]
    assert [(r.index, r.fields) for r in _raw_scope_index_ranges(PROJECT)] == [
        ("idx_raw_captures_scope", ("organization_id", "memory_scope", "scope_key"))
    ]
    unkeyed = {"organization_id": "org", "memory_scope": "organization"}
    assert [(r.index, r.fields) for r in _raw_scope_index_ranges(unkeyed)] == [
        ("idx_raw_captures_scope", ("organization_id", "memory_scope"))
    ]
    narrowed = _raw_scope_index_ranges({**PRIVATE, "project_id": "p1"})
    assert [(r.index, r.fields) for r in narrowed] == [
        ("idx_raw_captures_private_recent", ("organization_id", "memory_scope", "principal_id")),
        ("idx_raw_captures_project", ("organization_id", "project_id")),
    ]


@pytest.mark.asyncio
async def test_a_scope_within_the_cap_is_read_through_its_own_index() -> None:
    client = _CountingClient(scope=RAW_CAPTURE_EXACT_SCAN_MAX_ROWS)

    assert await _raw_vector_exact_index(client, PROJECT) == "idx_raw_captures_scope"
    [count] = client.counts()
    assert "organization_id = $organization_id AND memory_scope = $memory_scope" in count
    assert "scope_key = $scope_key" in count
    assert client.calls[0][1] == PROJECT


@pytest.mark.asyncio
async def test_a_large_scope_narrowed_to_a_small_project_reads_the_project_index() -> None:
    client = _CountingClient(scope=RAW_CAPTURE_EXACT_SCAN_MAX_ROWS + 1, project=40)

    params = {**PRIVATE, "project_id": "p1"}
    assert await _raw_vector_exact_index(client, params) == "idx_raw_captures_project"
    assert len(client.counts()) == 2
    assert client.calls[1][1] == {"organization_id": "org", "project_id": "p1"}


@pytest.mark.asyncio
async def test_a_scope_past_the_cap_walks_the_vector_index() -> None:
    client = _CountingClient(
        scope=RAW_CAPTURE_EXACT_SCAN_MAX_ROWS + 1, project=RAW_CAPTURE_EXACT_SCAN_MAX_ROWS + 1
    )

    assert await _raw_vector_exact_index(client, {**PRIVATE, "project_id": "p1"}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("unreadable", [None, "many", True, RuntimeError("count unavailable")])
async def test_an_unreadable_count_leaves_the_lane_on_the_walk(unreadable: object) -> None:
    client = _CountingClient(scope=unreadable)

    assert await _raw_vector_exact_index(client, PROJECT) is None


@pytest.mark.asyncio
async def test_the_scope_count_is_remembered_between_recalls() -> None:
    client = _CountingClient(scope=3)

    assert await _raw_vector_exact_index(client, PROJECT) == "idx_raw_captures_scope"
    assert await _raw_vector_exact_index(client, PROJECT) == "idx_raw_captures_scope"
    assert len(client.counts()) == 1
    # Another scope is another range, counted on its own.
    assert await _raw_vector_exact_index(client, {**PROJECT, "scope_key": "p2"}) is not None
    assert len(client.counts()) == 2


@pytest.mark.asyncio
async def test_a_small_scope_is_scored_exactly_with_the_walks_filters_and_order() -> None:
    client = _CountingClient(scope=3)

    rows = await _recall_raw_memory_vector(
        client,
        where_clause="organization_id = $organization_id AND memory_scope = $memory_scope "
        "AND scope_key = $scope_key",
        params=PROJECT,
        query_embedding=_query_embedding(),
        as_of=None,
        limit=2,
    )

    assert rows == []
    statement, params = client.calls[-1]
    assert "vector::similarity::cosine(embedding, $query_embedding) AS score" in statement
    assert "embedding <|" not in statement
    assert "idx_raw_captures_embedding" not in statement
    # Scope, then model, then the guards that keep an unusable vector out.
    assert (
        "scope_key = $scope_key AND metadata.embedding_metadata.provider = "
        "$query_embedding_provider" in statement
    )
    assert (
        "metadata.embedding_metadata.dimensions = $query_embedding_dimensions "
        "AND embedding != NONE AND array::len(embedding) = $query_embedding_length) "
        "ORDER BY score DESC, captured_at DESC LIMIT $candidate_limit;" in statement
    )
    assert params["query_embedding_length"] == 2
    assert params["candidate_limit"] == 8
    assert params["query_embedding"] == [1.0, 0.0]


@pytest.mark.asyncio
async def test_a_large_scope_keeps_the_filtered_hnsw_read() -> None:
    client = _CountingClient(scope=RAW_CAPTURE_EXACT_SCAN_MAX_ROWS + 1)

    await _recall_raw_memory_vector(
        client,
        where_clause="organization_id = $organization_id AND memory_scope = $memory_scope "
        "AND scope_key = $scope_key",
        params=PROJECT,
        query_embedding=_query_embedding(),
        as_of=None,
        limit=2,
    )

    statement, params = client.calls[-1]
    assert "FROM raw_captures WITH INDEX idx_raw_captures_embedding" in statement
    assert "embedding <|8, 40|> $query_embedding" in statement
    assert "vector::similarity::cosine" not in statement
    assert "query_embedding_length" not in params
