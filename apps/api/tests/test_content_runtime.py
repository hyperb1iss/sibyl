from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from sibyl.persistence import content_common, content_runtime
from sibyl.persistence.legacy.crawler import (
    get_crawl_stats_payload as legacy_get_crawl_stats_payload,
)
from sibyl.persistence.legacy.entities import (
    get_raw_capture as legacy_get_raw_capture,
)
from sibyl.persistence.surreal import content as surreal_content
from sibyl.persistence.surreal.content import (
    get_crawl_stats_payload as surreal_get_crawl_stats_payload,
    get_raw_capture as surreal_get_raw_capture,
    list_rag_source_documents_page as surreal_list_rag_source_documents_page,
)


def test_content_runtime_uses_legacy_exports_in_legacy_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(content_runtime.settings, "store", "legacy")

    assert (
        content_runtime._resolve_backend_export("get_crawl_stats_payload")
        is legacy_get_crawl_stats_payload
    )
    assert content_runtime._resolve_backend_export("get_raw_capture") is legacy_get_raw_capture


def test_content_runtime_maps_surreal_exports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(content_runtime.settings, "store", "surreal")

    assert (
        content_runtime._resolve_backend_export("get_crawl_stats_payload")
        is surreal_get_crawl_stats_payload
    )
    assert (
        content_runtime._resolve_backend_export("list_rag_source_documents_page")
        is surreal_list_rag_source_documents_page
    )
    assert content_runtime._resolve_backend_export("get_raw_capture") is surreal_get_raw_capture


def test_content_runtime_only_exports_neutral_runtime_surface() -> None:
    assert not hasattr(content_runtime, "get_legacy_raw_capture")
    assert not hasattr(content_runtime, "list_legacy_raw_captures")
    assert not hasattr(content_runtime, "resolve_legacy_document_entity")
    assert not hasattr(surreal_content, "get_legacy_raw_capture")
    assert not hasattr(surreal_content, "list_legacy_raw_captures")
    assert not hasattr(surreal_content, "resolve_legacy_document_entity")
    assert content_common.__all__ == ["CrawlStats", "DocumentEntityRecord"]
    assert "LegacyCrawlStats" not in content_common.__all__
    assert "LegacyDocumentEntityRecord" not in content_common.__all__
    assert content_common.CrawlStats is content_common.LegacyCrawlStats
    assert content_common.DocumentEntityRecord is content_common.LegacyDocumentEntityRecord


def _query_result(records: list[dict[str, object]]) -> list[dict[str, object]]:
    return [{"status": "OK", "result": records}]


def _raw_query_result(records: list[dict[str, object]]) -> dict[str, object]:
    return {
        "id": "fake",
        "result": [
            {"status": "OK", "result": None},
            {"status": "OK", "result": records},
        ],
    }


class FakeSurrealClient:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> object:
        self.calls.append((query, params))
        return self._responses.pop(0)

    async def execute_query_raw(self, query: str, **params: object) -> object:
        self.calls.append((query, params))
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_content_runtime_skips_relational_session_in_surreal_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(content_runtime.settings, "store", "surreal")

    async with content_runtime.get_content_read_session() as session:
        assert session is None


@pytest.mark.asyncio
async def test_content_runtime_delegates_to_postgres_session_in_legacy_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = object()

    @asynccontextmanager
    async def mock_get_session():
        yield session

    monkeypatch.setattr(content_runtime.settings, "store", "legacy")
    monkeypatch.setattr(content_runtime, "get_session", mock_get_session)

    async with content_runtime.get_content_read_session() as yielded:
        assert yielded is session


@pytest.mark.asyncio
async def test_content_runtime_dependency_uses_active_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(content_runtime.settings, "store", "surreal")

    dependency = content_runtime.get_content_read_session_dependency()
    yielded = await anext(dependency)
    assert yielded is None

    with pytest.raises(StopAsyncIteration):
        await anext(dependency)


@pytest.mark.asyncio
async def test_surreal_search_scope_uses_source_name_fulltext_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params: object) -> list[object]:
            self.calls.append((query, params))
            return []

    fake_client = FakeClient()

    @asynccontextmanager
    async def fake_session():
        yield fake_client

    monkeypatch.setattr(surreal_content, "surreal_content_client", fake_session)

    sources, sources_by_id, documents_by_id, chunks = await surreal_content._load_search_scope(
        organization_id=uuid4(),
        source_id=None,
        source_name='DOCS "Portal"\x00',
    )

    assert sources == []
    assert sources_by_id == {}
    assert documents_by_id == {}
    assert chunks == []
    source_query, source_params = fake_client.calls[0]
    assert "name @0@ $source_name" in source_query
    assert "string::contains" not in source_query
    assert source_params["source_name"] == "docs portal"
    assert len(fake_client.calls) == 1


@pytest.mark.asyncio
async def test_surreal_search_scope_empty_source_name_does_not_broaden_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params: object) -> list[object]:
            self.calls.append((query, params))
            return []

    fake_client = FakeClient()

    @asynccontextmanager
    async def fake_session():
        yield fake_client

    monkeypatch.setattr(surreal_content, "surreal_content_client", fake_session)

    await surreal_content._load_search_scope(
        organization_id=uuid4(),
        source_id=None,
        source_name="",
    )

    source_query, source_params = fake_client.calls[0]
    assert "uuid = $source_name_empty_sentinel" in source_query
    assert source_params["source_name_empty_sentinel"] == "__sibyl_empty_source_name__"
    assert len(fake_client.calls) == 1


@pytest.mark.asyncio
async def test_surreal_search_scope_source_id_takes_precedence_over_source_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params: object) -> list[object]:
            self.calls.append((query, params))
            return []

    fake_client = FakeClient()
    source_id = uuid4()

    @asynccontextmanager
    async def fake_session():
        yield fake_client

    monkeypatch.setattr(surreal_content, "surreal_content_client", fake_session)

    await surreal_content._load_search_scope(
        organization_id=uuid4(),
        source_id=source_id,
        source_name="docs",
    )

    source_query, source_params = fake_client.calls[0]
    assert "uuid = $source_id" in source_query
    assert "name @0@ $source_name" not in source_query
    assert source_params["source_id"] == str(source_id)
    assert "source_name" not in source_params


@pytest.mark.asyncio
async def test_surreal_rag_search_uses_direct_knn_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = uuid4()
    document_id = uuid4()
    chunk_id = uuid4()
    org_id = uuid4()
    fake_client = FakeSurrealClient(
        [
            _query_result(
                [
                    {
                        "uuid": str(source_id),
                        "organization_id": str(org_id),
                        "name": "Docs",
                        "url": "https://docs.example.com",
                    }
                ]
            ),
            _raw_query_result(
                [
                    {
                        "uuid": str(chunk_id),
                        "document_id": str(document_id),
                        "chunk_index": 0,
                        "chunk_type": "text",
                        "content": "alpha semantic match",
                        "score": 0.88,
                    }
                ]
            ),
            _query_result(
                [
                    {
                        "uuid": str(document_id),
                        "source_id": str(source_id),
                        "url": "https://docs.example.com/guide",
                        "title": "Guide",
                    }
                ]
            ),
        ]
    )

    @asynccontextmanager
    async def fake_session():
        yield fake_client

    monkeypatch.setattr(surreal_content, "surreal_content_client", fake_session)

    rows = await surreal_content.search_rag_chunks(
        None,
        query_embedding=[0.1] * 1536,
        organization_id=org_id,
        similarity_threshold=0.5,
        match_count=5,
        source_name="Docs",
    )

    assert len(rows) == 1
    chunk, document, source_name, returned_source_id, score = rows[0]
    assert chunk.id == chunk_id
    assert document.id == document_id
    assert source_name == "Docs"
    assert returned_source_id == source_id
    assert score == 0.88
    source_query, _ = fake_client.calls[0]
    vector_query, vector_params = fake_client.calls[1]
    assert "name @0@ $source_name" in source_query
    assert "embedding <|25, 40|> $query_embedding" in vector_query
    assert "vector::distance::knn()" in vector_query
    assert vector_params["source_ids"] == [str(source_id)]


@pytest.mark.asyncio
async def test_surreal_code_examples_filter_code_and_language_in_surreal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = uuid4()
    document_id = uuid4()
    chunk_id = uuid4()
    org_id = uuid4()
    fake_client = FakeSurrealClient(
        [
            _query_result(
                [
                    {
                        "uuid": str(source_id),
                        "organization_id": str(org_id),
                        "name": "Docs",
                        "url": "https://docs.example.com",
                    }
                ]
            ),
            _raw_query_result(
                [
                    {
                        "uuid": str(chunk_id),
                        "document_id": str(document_id),
                        "chunk_index": 1,
                        "chunk_type": "code",
                        "content": "def alpha(): pass",
                        "language": "python",
                        "score": 0.92,
                    }
                ]
            ),
            _query_result(
                [
                    {
                        "uuid": str(document_id),
                        "source_id": str(source_id),
                        "url": "https://docs.example.com/api",
                        "title": "API",
                    }
                ]
            ),
        ]
    )

    @asynccontextmanager
    async def fake_session():
        yield fake_client

    monkeypatch.setattr(surreal_content, "surreal_content_client", fake_session)

    rows = await surreal_content.search_code_example_chunks(
        None,
        query_embedding=[0.1] * 1536,
        organization_id=org_id,
        match_count=5,
        source_id=source_id,
        language="Python",
    )

    assert len(rows) == 1
    chunk, document, returned_source_id, source_name, score = rows[0]
    assert chunk.id == chunk_id
    assert document.id == document_id
    assert returned_source_id == source_id
    assert source_name == "Docs"
    assert score == 0.92
    vector_query, vector_params = fake_client.calls[1]
    assert "chunk_type = 'code'" in vector_query
    assert "string::lowercase(language ?? '') = $language" in vector_query
    assert "embedding <|25, 40|> $query_embedding" in vector_query
    assert vector_params["language"] == "python"


@pytest.mark.asyncio
async def test_surreal_code_examples_filter_code_without_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = uuid4()
    document_id = uuid4()
    chunk_id = uuid4()
    org_id = uuid4()
    fake_client = FakeSurrealClient(
        [
            _query_result(
                [
                    {
                        "uuid": str(source_id),
                        "organization_id": str(org_id),
                        "name": "Docs",
                        "url": "https://docs.example.com",
                    }
                ]
            ),
            _raw_query_result(
                [
                    {
                        "uuid": str(chunk_id),
                        "document_id": str(document_id),
                        "chunk_index": 1,
                        "chunk_type": "code",
                        "content": "def beta(): pass",
                        "score": 0.91,
                    }
                ]
            ),
            _query_result(
                [
                    {
                        "uuid": str(document_id),
                        "source_id": str(source_id),
                        "url": "https://docs.example.com/api",
                        "title": "API",
                    }
                ]
            ),
        ]
    )

    @asynccontextmanager
    async def fake_session():
        yield fake_client

    monkeypatch.setattr(surreal_content, "surreal_content_client", fake_session)

    await surreal_content.search_code_example_chunks(
        None,
        query_embedding=[0.1] * 1536,
        organization_id=org_id,
        match_count=5,
        source_id=source_id,
        language=None,
    )

    vector_query, vector_params = fake_client.calls[1]
    assert "chunk_type = 'code'" in vector_query
    assert "string::lowercase(language ?? '') = $language" not in vector_query
    assert "language" not in vector_params


@pytest.mark.asyncio
async def test_surreal_hybrid_search_uses_direct_vector_and_fulltext_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = uuid4()
    document_id = uuid4()
    vector_chunk_id = uuid4()
    lexical_chunk_id = uuid4()
    org_id = uuid4()
    fake_client = FakeSurrealClient(
        [
            _query_result(
                [
                    {
                        "uuid": str(source_id),
                        "organization_id": str(org_id),
                        "name": "Docs",
                        "url": "https://docs.example.com",
                    }
                ]
            ),
            _raw_query_result(
                [
                    {
                        "uuid": str(vector_chunk_id),
                        "document_id": str(document_id),
                        "chunk_index": 0,
                        "chunk_type": "text",
                        "content": "semantic auth",
                        "score": 0.86,
                    }
                ]
            ),
            _raw_query_result(
                [
                    {
                        "uuid": str(lexical_chunk_id),
                        "document_id": str(document_id),
                        "chunk_index": 1,
                        "chunk_type": "text",
                        "content": "literal auth",
                        "score": 0.44,
                    }
                ]
            ),
            _query_result(
                [
                    {
                        "uuid": str(document_id),
                        "source_id": str(source_id),
                        "url": "https://docs.example.com/auth",
                        "title": "Auth",
                    }
                ]
            ),
        ]
    )

    @asynccontextmanager
    async def fake_session():
        yield fake_client

    monkeypatch.setattr(surreal_content, "surreal_content_client", fake_session)

    rows = await surreal_content.hybrid_search_chunks(
        None,
        query_text="auth",
        query_embedding=[0.1] * 1536,
        organization_id=org_id,
        similarity_threshold=0.5,
        match_count=5,
        source_name="Docs",
    )

    assert [row[0].id for row in rows] == [vector_chunk_id, lexical_chunk_id]
    assert rows[0][4] == 0.86
    assert rows[1][5] == 0.44
    vector_query, vector_params = fake_client.calls[1]
    lexical_query, lexical_params = fake_client.calls[2]
    assert "embedding <|25, 40|> $query_embedding" in vector_query
    assert "content @0@ $search_query" in lexical_query
    assert vector_params["source_ids"] == [str(source_id)]
    assert lexical_params["search_query"] == "auth"
