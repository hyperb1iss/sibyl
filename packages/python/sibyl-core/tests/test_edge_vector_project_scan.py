"""The edge vector lane scans a small project's own edges and keeps the HNSW bracket for the rest.

A project filter beside the bracket makes SurrealDB 4.0 walk the vector graph
until enough edges pass it, which for a small project is the whole graph. The
lane counts the projects' entities first and, under the limit, reads their
edges directly. What the two reads return for the same graph is checked on a
server in test_edge_vector_project_membership; this pins which read runs.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sibyl_core.backends.surreal.in_clumps import IN_CLUMP
from sibyl_core.embeddings.providers import EmbeddingMetadata
from sibyl_core.retrieval import _search_sources as source_module
from sibyl_core.retrieval._search_plan import SearchFilter

METADATA = EmbeddingMetadata(
    provider="deterministic",
    model="unit-test",
    dimensions=4,
    cache_namespace="edge-scan-test",
    tokenizer_estimate_method="utf8-byte-length",
)
PLAN = SimpleNamespace(organization_id="org-123", vector_min_score=0.0)


class _CountingClient:
    """Answers the entity count with a fixed reply and records every statement."""

    def __init__(self, entities: object) -> None:
        self.entities = entities
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
        self.calls.append((query, dict(params)))
        if "count()" in query:
            if isinstance(self.entities, BaseException):
                raise self.entities
            return [] if self.entities is None else [{"count": self.entities}]
        return []


async def _lane(client: _CountingClient, search_filter: SearchFilter) -> list[str]:
    await source_module._edge_vector_candidates(
        client=client,
        plan=PLAN,
        search_filter=search_filter,
        query_embedding=[0.1, 0.2, 0.3, 0.4],
        embedding_metadata=METADATA,
        limit=5,
    )
    return [query for query, _params in client.calls]


@pytest.mark.parametrize(
    "entities", [None, 0, 1, source_module.EDGE_PROJECT_SCAN_ENTITY_LIMIT], ids=str
)
async def test_a_small_project_is_scanned(entities: int | None) -> None:
    client = _CountingClient(entities)

    count, scan = await _lane(client, SearchFilter(project_ids=("project_123",)))

    assert "FROM entity WHERE project_id IN $project_ids GROUP ALL" in count
    assert client.calls[0][1] == {"project_ids": ["project_123"]}
    assert "<|" not in scan
    assert "vector::similarity::cosine(fact_embedding, $query_embedding) AS score" in scan
    # Every way `_edge_filter_clause` admits an edge to a project is read.
    assert "->relates_to, <-relates_to" in scan
    assert "WHERE project_id IN $project_ids" in scan
    assert "OR attributes.project_id IN $project_ids" in scan
    assert "FROM relates_to WHERE attributes.project_id IN $project_ids" in scan
    params = client.calls[1][1]
    assert params["project_ids"] == ["project_123"]
    assert params["limit"] == 5
    assert params["group_id"] == "org-123"


async def test_a_large_project_keeps_the_bracket() -> None:
    client = _CountingClient(source_module.EDGE_PROJECT_SCAN_ENTITY_LIMIT + 1)

    _count, bracket = await _lane(client, SearchFilter(project_ids=("project_123",)))

    assert "fact_embedding <|5, 40|> $query_embedding" in bracket
    assert "in.project_id IN $project_ids" in bracket


@pytest.mark.parametrize(
    "reply", [RuntimeError("count unavailable"), "many", True, -1, 3.0], ids=repr
)
async def test_a_count_that_cannot_be_read_keeps_the_bracket(reply: object) -> None:
    """Routing is an optimization; a failed count must not cost the lane its rows."""
    client = _CountingClient(reply)

    _count, bracket = await _lane(client, SearchFilter(project_ids=("project_123",)))

    assert "fact_embedding <|5, 40|> $query_embedding" in bracket


@pytest.mark.parametrize(
    "search_filter",
    [
        pytest.param(SearchFilter(), id="no-project"),
        pytest.param(
            SearchFilter(project_ids=tuple(f"project_{index}" for index in range(IN_CLUMP + 1))),
            id="more-projects-than-one-clump",
        ),
    ],
)
async def test_a_search_no_count_can_route_keeps_the_bracket(search_filter: SearchFilter) -> None:
    client = _CountingClient(0)

    (bracket,) = await _lane(client, search_filter)

    assert "fact_embedding <|5, 40|> $query_embedding" in bracket


async def test_the_scan_keeps_the_other_edge_filters_and_drops_the_vector() -> None:
    client = _CountingClient(3)

    _count, scan = await _lane(
        client,
        SearchFilter(project_ids=("project_123",), edge_types=("SUPERSEDES",), edge_uuids=("e1",)),
    )

    assert "name IN $edge_types" in scan
    assert "uuid IN $edge_uuids" in scan
    assert "group_id = $group_id" in scan
    assert "fact_embedding != NONE" in scan
    # The project is the set scanned, not a filter on it.
    assert "in.project_id IN $project_ids" not in scan
    projected = scan.replace("vector::similarity::cosine(fact_embedding, $query_embedding)", "")
    assert "fact_embedding" not in projected.partition("WHERE")[0]
    assert client.calls[1][1]["edge_types"] == ["SUPERSEDES"]
