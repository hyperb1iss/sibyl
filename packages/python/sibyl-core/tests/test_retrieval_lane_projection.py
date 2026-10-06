"""The hot-path lane statements ship no vectors and no bodies.

SELECT * put a 1024-float vector and the full content on the wire for every
lane row, decoded on the event loop, when fusion needs neither. The node
lanes now project the columns the candidate builder reads, the edge lanes
leave the fact vector in the database, and the body is fetched once for the
rows that survive the fused cut.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from sibyl_core.embeddings.providers import EmbeddingMetadata
from sibyl_core.models.context import ContextFacet
from sibyl_core.retrieval import _search_sources as source_module
from sibyl_core.retrieval._search_plan import RetrievalSignal, SearchFilter
from sibyl_core.retrieval.candidates import CandidateKind, RetrievalCandidate
from sibyl_core.retrieval.search import build_context_retrieval_plan


def _plan(query: str = "quartz evidence") -> Any:
    return build_context_retrieval_plan(
        query=query,
        organization_id="org-123",
        facets=[ContextFacet.DECISIONS],
        facet_types={ContextFacet.DECISIONS: ["decision"]},
        principal_id="user-123",
        project="project_123",
        accessible_projects={"project_123"},
        limit=12,
    )


class _RecordingClient:
    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        self.queries: list[str] = []
        self.params: list[dict[str, object]] = []
        self._rows = rows or []

    async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
        self.queries.append(query)
        self.params.append(params)
        return list(self._rows)


def _projection_of(query: str, *, table: str) -> str:
    """The text between the innermost SELECT and its FROM <table>."""
    head, separator, _tail = query.partition(f"FROM {table}")
    assert separator, query
    return head.rsplit("SELECT", maxsplit=1)[1]


@pytest.mark.asyncio
async def test_node_lanes_project_candidate_columns_without_vectors_or_bodies() -> None:
    client = _RecordingClient()
    plan = _plan()
    search_filter = SearchFilter(project_ids=("project_123",))
    provider_metadata = EmbeddingMetadata(
        provider="deterministic",
        model="unit-test",
        dimensions=4,
        cache_namespace="lane-projection-test",
        tokenizer_estimate_method="utf8-byte-length",
    )

    await source_module._node_fulltext_candidates(
        client=client, plan=plan, search_filter=search_filter, limit=5
    )
    await source_module._exact_key_candidates(
        client=client, plan=plan, search_filter=search_filter, limit=5, probe_tokens=("abc-1",)
    )
    await source_module._node_vector_candidates(
        client=client,
        plan=plan,
        search_filter=search_filter,
        query_embedding=[0.1, 0.2, 0.3, 0.4],
        embedding_metadata=provider_metadata,
        limit=5,
    )

    assert len(client.queries) == len(source_module.NODE_FULLTEXT_FIELDS) + 2
    for query in client.queries:
        projection = _projection_of(query, table="entity")
        fields, omitted = projection.split("OMIT", maxsplit=1)
        assert "*" not in fields
        assert "embedding" not in fields
        assert re.search(r"\bcontent\b", fields) is None
        assert omitted.strip() == "attributes.content"
        for column in source_module.NODE_CANDIDATE_FIELDS:
            assert re.search(rf"\b{column}\b", fields), column
    vector_query = client.queries[-1]
    assert vector_query.count("name_embedding") == 1
    assert "name_embedding <|5, 40|> $query_embedding" in vector_query


@pytest.mark.asyncio
async def test_edge_lanes_leave_the_fact_vector_in_the_database() -> None:
    client = _RecordingClient()
    plan = _plan()
    search_filter = SearchFilter(project_ids=("project_123",))
    provider_metadata = EmbeddingMetadata(
        provider="deterministic",
        model="unit-test",
        dimensions=4,
        cache_namespace="lane-projection-test",
        tokenizer_estimate_method="utf8-byte-length",
    )

    await source_module._edge_vector_candidates(
        client=client,
        plan=plan,
        search_filter=search_filter,
        query_embedding=[0.1, 0.2, 0.3, 0.4],
        embedding_metadata=provider_metadata,
        limit=5,
    )
    projection = _projection_of(client.queries[-1], table="relates_to")
    assert "fact_embedding" not in projection
    assert "fact_embedding <|5, 40|> $query_embedding" in client.queries[-1]
    assert "fact_embedding" not in source_module._edge_select()


@pytest.mark.asyncio
async def test_bodies_are_fetched_once_for_the_fused_node_rows() -> None:
    body_rows: list[dict[str, object]] = [
        {
            "uuid": "node-a",
            "content": "the full body of node a",
            "description": "a blurb",
            "summary": None,
            "attribute_content": None,
            "attribute_description": None,
        },
        {
            "uuid": "node-b",
            "content": None,
            "description": "b blurb",
            "summary": None,
            "attribute_content": "the attribute body of node b",
            "attribute_description": None,
        },
    ]
    client = _RecordingClient(body_rows)

    def node(identifier: str, content: str) -> RetrievalCandidate:
        return RetrievalCandidate(
            id=identifier,
            type="decision",
            name=identifier,
            content=content,
            score=1.0,
            source=None,
            metadata={"project_id": "project_123"},
            project_id="project_123",
            kind=CandidateKind.NODE,
        )

    fused = [
        (node("node-a", "a blurb"), 0.9, {"sources": [RetrievalSignal.NODE_FULLTEXT.value]}),
        (node("node-b", "b blurb"), 0.8, {"sources": [RetrievalSignal.NODE_VECTOR.value]}),
        (
            node("node-walked", "walked blurb"),
            0.7,
            {"sources": [RetrievalSignal.GRAPH_EXPANSION.value]},
        ),
        (
            RetrievalCandidate(
                id="edge-1",
                type="claim",
                name="SUPPORTS",
                content="a supports b",
                score=0.6,
                source=None,
                metadata={},
                kind=CandidateKind.EDGE,
            ),
            0.6,
            {"sources": [RetrievalSignal.EDGE_FULLTEXT.value]},
        ),
    ]

    hydrated = await source_module._hydrate_candidate_bodies(
        client=client, group_id="org-123", fused=fused
    )

    assert len(client.queries) == 1
    assert client.params[0] == {"group_id": "org-123", "ids": ["node-a", "node-b"]}
    assert "uuid IN $ids" in client.queries[0]
    assert [candidate.content for candidate, _score, _metadata in hydrated] == [
        "the full body of node a",
        "the attribute body of node b",
        "walked blurb",
        "a supports b",
    ]
    assert hydrated[1][0].metadata["content"] == "the attribute body of node b"
    assert [score for _candidate, score, _metadata in hydrated] == [0.9, 0.8, 0.7, 0.6]
