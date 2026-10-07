"""Document search learns what a scope holds before walking the shared index.

The chunk HNSW index spans every organization in the content namespace, so a
filtered walk for an organization that owns none of it visits all of it to
find nothing, and one that owns a small share visits most of it. A cheap
count through the org/source index now says whether there is anything to
score and how to score it, and the write paths invalidate that count.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

import pytest

from sibyl_core.services import content_client
from sibyl_core.services.content_documents import (
    DOCUMENT_CHUNK_EXACT_SCAN_MAX_ROWS,
    invalidate_document_chunk_counts,
    reset_document_chunk_count_cache,
    search_document_chunks,
)

SOURCE = {
    "uuid": "src-1",
    "organization_id": "org-1",
    "name": "Docs",
    "url": "https://docs.example.com",
}
CHUNK = {
    "uuid": "chunk-1",
    "document_id": "doc-1",
    "chunk_index": 0,
    "chunk_type": "text",
    "content": "alpha vector match",
    "score": 0.91,
}
DOCUMENT = {
    "uuid": "doc-1",
    "source_id": "src-1",
    "url": "https://docs.example.com/guide",
    "title": "Guide",
}


def _ok(records: Sequence[Mapping[str, Any]]) -> list[dict[str, object]]:
    return [{"status": "OK", "result": [dict(record) for record in records]}]


def _raw(records: Sequence[Mapping[str, Any]]) -> dict[str, object]:
    return {
        "id": "fake",
        "result": [
            {"status": "OK", "result": None},
            {"status": "OK", "result": [dict(record) for record in records]},
        ],
    }


class _Client:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute_query(
        self, query: str, params: dict[str, Any] | None = None, **kwargs: Any
    ) -> object:
        merged = dict(params or {})
        merged.update(kwargs)
        self.calls.append((query, merged))
        return self._responses.pop(0)

    def queries(self) -> list[str]:
        return [query for query, _params in self.calls]

    def count_queries(self) -> list[str]:
        return [query for query in self.queries() if "SELECT count() AS total" in query]


def _install(monkeypatch: pytest.MonkeyPatch, client: _Client) -> None:
    @asynccontextmanager
    async def session():
        yield client

    monkeypatch.setattr(content_client, "surreal_content_client", session)


@pytest.fixture(autouse=True)
def _fresh_cache():
    reset_document_chunk_count_cache()
    yield
    reset_document_chunk_count_cache()


@pytest.mark.asyncio
async def test_a_scope_without_chunks_issues_no_vector_or_lexical_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client([_ok([SOURCE]), _ok([{"total": 0}])])
    _install(monkeypatch, client)

    rows = await search_document_chunks(
        organization_id="org-1", query_text="alpha", query_embedding=[1.0, 0.0], limit=5
    )

    assert rows == ([], [])
    queries = client.queries()
    assert len(queries) == 2
    assert "SELECT count() AS total FROM (SELECT uuid FROM document_chunks" in queries[1]
    assert "organization_id = $organization_id AND source_id IN $source_ids" in queries[1]
    assert client.calls[1][1]["source_ids"] == ["src-1"]
    assert not any("<|" in query or "vector::" in query or "@0@" in query for query in queries)


@pytest.mark.asyncio
async def test_the_chunk_count_is_cached_per_scope_until_invalidated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client(
        [
            _ok([SOURCE]),
            _ok([{"total": 0}]),
            _ok([SOURCE]),
            _ok([SOURCE]),
            _ok([{"total": 0}]),
        ]
    )
    _install(monkeypatch, client)

    for _ in range(2):
        await search_document_chunks(
            organization_id="org-1", query_text="alpha", query_embedding=[1.0, 0.0], limit=5
        )
    assert len(client.count_queries()) == 1

    invalidate_document_chunk_counts("org-1")
    await search_document_chunks(
        organization_id="org-1", query_text="alpha", query_embedding=[1.0, 0.0], limit=5
    )
    assert len(client.count_queries()) == 2


@pytest.mark.asyncio
async def test_a_small_scope_is_scored_exactly_through_the_org_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client([_ok([SOURCE]), _ok([{"total": 3}]), _raw([CHUNK]), _raw([]), _ok([DOCUMENT])])
    _install(monkeypatch, client)

    vector_rows, lexical_rows = await search_document_chunks(
        organization_id="org-1", query_text="alpha", query_embedding=[1.0, 0.0], limit=5
    )

    assert [row[0].id for row in vector_rows] == ["chunk-1"]
    assert lexical_rows == []
    vector_query, vector_params = client.calls[2]
    assert "vector::similarity::cosine(embedding, $query_embedding) AS score" in vector_query
    assert "<|" not in vector_query
    assert "organization_id = $organization_id AND source_id IN $source_ids" in vector_query
    assert "embedding != NONE AND array::len(embedding) = $dimensions" in vector_query
    assert "WHERE score >= $similarity_threshold" in vector_query
    assert vector_params["dimensions"] == 2
    assert vector_params["source_ids"] == ["src-1"]


@pytest.mark.asyncio
async def test_a_large_scope_walks_the_vector_index(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _Client(
        [
            _ok([SOURCE]),
            _ok([{"total": DOCUMENT_CHUNK_EXACT_SCAN_MAX_ROWS + 1}]),
            _raw([]),
            _raw([]),
        ]
    )
    _install(monkeypatch, client)

    await search_document_chunks(
        organization_id="org-1", query_text="alpha", query_embedding=[1.0, 0.0], limit=5
    )

    vector_query, _params = client.calls[2]
    assert "embedding <|25, 40|> $query_embedding" in vector_query
    assert "vector::similarity::cosine" not in vector_query
