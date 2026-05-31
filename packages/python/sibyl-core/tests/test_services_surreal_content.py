"""Tests for Surreal-backed core content helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import asynccontextmanager

import pytest

from sibyl_core.embeddings.providers import EmbeddingMetadata
from sibyl_core.models.reflection import ReflectionCandidate
from sibyl_core.services.surreal_content import (
    MemoryScope,
    _replace_record,
    get_or_create_source,
    get_raw_memory_by_dedupe_key,
    get_raw_memory_by_source_id,
    list_raw_memories_for_promotion,
    list_unlinked_document_chunks,
    load_search_scope,
    raw_memory_embedding_text,
    recall_raw_memory,
    remember_raw_memory,
    remember_reflection_candidate_review,
    search_document_chunks,
)


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


def _raw_error_result(message: str) -> dict[str, object]:
    return {
        "id": "fake",
        "result": [
            {"status": "OK", "result": None},
            {"status": "ERR", "result": message},
        ],
    }


class FakeClient:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.closed = 0

    async def execute_query(
        self, query: str, params: dict[str, object] | None = None, **kwargs: object
    ) -> object:
        merged = dict(params or {})
        merged.update(kwargs)
        self.calls.append((query, merged))
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def execute_query_raw(
        self, query: str, params: dict[str, object] | None = None, **kwargs: object
    ) -> object:
        merged = dict(params or {})
        merged.update(kwargs)
        self.calls.append((query, merged))
        return self._responses.pop(0)

    async def close(self) -> None:
        self.closed += 1


class FakeEmbeddingProvider:
    def __init__(self, embedding: list[float]) -> None:
        self._metadata = EmbeddingMetadata(
            provider="deterministic",
            model="raw-test-v1",
            dimensions=len(embedding),
            cache_namespace="raw-test",
            tokenizer_estimate_method="unit-test",
        )
        self._embedding = embedding
        self.texts: list[str] = []
        self.input_kinds: list[str] = []

    @property
    def metadata(self) -> EmbeddingMetadata:
        return self._metadata

    async def embed_texts(
        self,
        texts: Sequence[str],
        *,
        input_kind: str = "document",
    ) -> list[list[float]]:
        self.texts.extend(str(text) for text in texts)
        self.input_kinds.append(str(input_kind))
        return [list(self._embedding) for _text in texts]


class TestSurrealContentHelpers:
    @pytest.mark.asyncio
    async def test_surreal_content_client_creates_per_context_client(self) -> None:
        first_client = FakeClient([])
        second_client = FakeClient([])

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                content_service,
                "build_surreal_content_client",
                lambda: first_client if first_client.closed == 0 else second_client,
            )
            async with content_service.surreal_content_client() as first:
                assert first is first_client
            async with content_service.surreal_content_client() as second:
                assert second is second_client

        assert first_client.closed == 1
        assert second_client.closed == 1

    @pytest.mark.asyncio
    async def test_replace_record_uses_single_upsert_statement(self) -> None:
        record = {
            "uuid": "src-1",
            "organization_id": "org-1",
            "name": "Docs",
        }
        fake_client = FakeClient([_query_result([record])])

        saved = await _replace_record(fake_client, "crawl_sources", uuid="src-1", record=record)

        assert saved["uuid"] == "src-1"
        assert len(fake_client.calls) == 1
        query, params = fake_client.calls[0]
        assert (
            "UPSERT crawl_sources CONTENT $record "
            "WHERE uuid = $uuid AND organization_id = $organization_id"
        ) in query
        assert "DELETE FROM crawl_sources" not in query
        assert params == {
            "uuid": "src-1",
            "organization_id": "org-1",
            "record": record,
        }

    @pytest.mark.asyncio
    async def test_replace_record_creates_when_upsert_matches_no_rows(self) -> None:
        record = {
            "uuid": "src-1",
            "organization_id": "org-1",
            "name": "Docs",
        }
        fake_client = FakeClient([_query_result([]), _query_result([record])])

        saved = await _replace_record(fake_client, "crawl_sources", uuid="src-1", record=record)

        assert saved["uuid"] == "src-1"
        assert len(fake_client.calls) == 2
        assert (
            "UPSERT crawl_sources CONTENT $record "
            "WHERE uuid = $uuid AND organization_id = $organization_id"
        ) in fake_client.calls[0][0]
        assert "CREATE crawl_sources CONTENT $record" in fake_client.calls[1][0]
        assert fake_client.calls[1][1] == {"record": record}

    @pytest.mark.asyncio
    async def test_replace_record_retries_scoped_upsert_when_create_conflicts(self) -> None:
        record = {
            "uuid": "src-1",
            "organization_id": "org-1",
            "name": "Docs",
        }
        fake_client = FakeClient(
            [_query_result([]), RuntimeError("unique conflict"), _query_result([record])]
        )

        saved = await _replace_record(fake_client, "crawl_sources", uuid="src-1", record=record)

        assert saved["uuid"] == "src-1"
        assert len(fake_client.calls) == 3
        assert "CREATE crawl_sources CONTENT $record" in fake_client.calls[1][0]
        assert (
            "UPSERT crawl_sources CONTENT $record "
            "WHERE uuid = $uuid AND organization_id = $organization_id"
        ) in fake_client.calls[2][0]
        assert fake_client.calls[2][1] == {
            "uuid": "src-1",
            "organization_id": "org-1",
            "record": record,
        }

    @pytest.mark.asyncio
    async def test_replace_record_fails_closed_when_create_returns_no_rows(self) -> None:
        record = {
            "uuid": "src-1",
            "organization_id": "org-1",
            "name": "Docs",
        }
        fake_client = FakeClient([_query_result([]), _query_result([])])

        with pytest.raises(RuntimeError, match="failed to persist crawl_sources record src-1"):
            await _replace_record(fake_client, "crawl_sources", uuid="src-1", record=record)

        assert len(fake_client.calls) == 2
        assert (
            "UPSERT crawl_sources CONTENT $record "
            "WHERE uuid = $uuid AND organization_id = $organization_id"
        ) in fake_client.calls[0][0]
        assert "CREATE crawl_sources CONTENT $record" in fake_client.calls[1][0]

    @pytest.mark.asyncio
    async def test_replace_record_requires_org_scope(self) -> None:
        fake_client = FakeClient([])

        with pytest.raises(RuntimeError, match="requires organization_id"):
            await _replace_record(
                fake_client,
                "raw_captures",
                uuid="capture-1",
                record={"uuid": "capture-1", "title": "missing"},
            )

        assert fake_client.calls == []

    @pytest.mark.asyncio
    async def test_get_or_create_source_returns_existing_record(self) -> None:
        fake_client = FakeClient(
            [
                _query_result(
                    [
                        {
                            "uuid": "src-1",
                            "organization_id": "org-1",
                            "name": "Docs",
                            "url": "https://docs.example.com",
                            "source_type": "website",
                            "crawl_status": "completed",
                        }
                    ]
                )
            ]
        )

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            source, created = await get_or_create_source(
                "https://docs.example.com",
                2,
                {},
                organization_id="org-1",
            )

        assert created is False
        assert source.id == "src-1"
        assert source.organization_id == "org-1"
        assert fake_client.calls[0][1] == {
            "organization_id": "org-1",
            "url": "https://docs.example.com",
        }

    @pytest.mark.asyncio
    async def test_list_unlinked_document_chunks_filters_linked_rows(self) -> None:
        fake_client = FakeClient(
            [
                _query_result(
                    [
                        {
                            "uuid": "chunk-1",
                            "organization_id": "org-1",
                            "source_id": "src-1",
                            "document_id": "doc-1",
                            "chunk_index": 0,
                            "chunk_type": "text",
                            "content": "unlinked chunk",
                            "has_entities": False,
                        },
                    ]
                ),
            ]
        )

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            chunks = await list_unlinked_document_chunks(
                organization_id="org-1",
                source_id="src-1",
                limit=10,
            )

        assert [chunk.id for chunk in chunks] == ["chunk-1"]
        query, params = fake_client.calls[0]
        assert "organization_id = $organization_id" in query
        assert "source_id = $source_id" in query
        assert "has_entities = false" in query
        assert params["organization_id"] == "org-1"
        assert params["source_id"] == "src-1"

    @pytest.mark.asyncio
    async def test_search_document_chunks_uses_direct_surreal_queries(self) -> None:
        fake_client = FakeClient(
            [
                _query_result(
                    [
                        {
                            "uuid": "src-1",
                            "organization_id": "org-1",
                            "name": "Docs",
                            "url": "https://docs.example.com",
                        }
                    ]
                ),
                _raw_query_result(
                    [
                        {
                            "uuid": "chunk-vector",
                            "document_id": "doc-1",
                            "chunk_index": 0,
                            "chunk_type": "code",
                            "content": "alpha vector match",
                            "language": "python",
                            "score": 0.91,
                        }
                    ]
                ),
                _raw_query_result(
                    [
                        {
                            "uuid": "chunk-lexical",
                            "document_id": "doc-1",
                            "chunk_index": 1,
                            "chunk_type": "code",
                            "content": "alpha lexical match",
                            "language": "python",
                            "score": 0.42,
                        }
                    ]
                ),
                _query_result(
                    [
                        {
                            "uuid": "doc-1",
                            "source_id": "src-1",
                            "url": "https://docs.example.com/guide",
                            "title": "Guide",
                            "has_code": True,
                        }
                    ]
                ),
            ]
        )

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            vector_rows, lexical_rows = await search_document_chunks(
                organization_id="org-1",
                query_text="alpha",
                query_embedding=[1.0, 0.0],
                source_id="src-1",
                language="Python",
                limit=5,
            )

        assert [row[0].id for row in vector_rows] == ["chunk-vector"]
        assert [row[0].id for row in lexical_rows] == ["chunk-lexical"]
        assert vector_rows[0][1].id == "doc-1"
        assert vector_rows[0][2] == "Docs"

        source_query, source_params = fake_client.calls[0]
        vector_query, vector_params = fake_client.calls[1]
        lexical_query, lexical_params = fake_client.calls[2]
        document_query, document_params = fake_client.calls[3]
        assert "FROM crawl_sources WHERE organization_id = $organization_id" in source_query
        assert "uuid = $source_id" in source_query
        assert source_params["organization_id"] == "org-1"
        assert source_params["source_id"] == "src-1"
        assert "FROM document_chunks WHERE organization_id = $organization_id" in vector_query
        assert "source_id INSIDE $source_ids" in vector_query
        assert "FROM document_chunks WHERE organization_id = $organization_id" in lexical_query
        assert "source_id INSIDE $source_ids" in lexical_query
        assert "embedding <|25, 40|> $query_embedding" in vector_query
        assert "content @0@ $search_query" in lexical_query
        assert "SELECT uuid, organization_id, source_id, url, title, has_code" in document_query
        assert vector_params["organization_id"] == "org-1"
        assert lexical_params["organization_id"] == "org-1"
        assert vector_params["language"] == "python"
        assert lexical_params["language"] == "python"
        assert vector_params["source_ids"] == ["src-1"]
        assert lexical_params["source_ids"] == ["src-1"]
        assert document_params["document_ids"] == ["doc-1"]

    @pytest.mark.asyncio
    async def test_search_document_chunks_filters_source_name_in_surreal(self) -> None:
        fake_client = FakeClient(
            [
                _query_result(
                    [
                        {
                            "uuid": "src-1",
                            "organization_id": "org-1",
                            "name": "Docs",
                            "url": "https://docs.example.com",
                        }
                    ]
                ),
                _raw_query_result(
                    [
                        {
                            "uuid": "chunk-lexical",
                            "document_id": "doc-1",
                            "chunk_index": 1,
                            "chunk_type": "text",
                            "content": "alpha lexical match",
                            "score": 0.42,
                        }
                    ]
                ),
                _query_result(
                    [
                        {
                            "uuid": "doc-1",
                            "source_id": "src-1",
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

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            vector_rows, lexical_rows = await search_document_chunks(
                organization_id="org-1",
                query_text="alpha",
                query_embedding=None,
                source_name="DOC",
                limit=5,
            )

        assert vector_rows == []
        assert [row[0].id for row in lexical_rows] == ["chunk-lexical"]

        source_query, source_params = fake_client.calls[0]
        assert "name @0@ $source_name" in source_query
        assert "string::contains" not in source_query
        assert source_params["source_name"] == "doc"

    @pytest.mark.asyncio
    async def test_search_document_chunks_sanitizes_query_fulltext_filter(self) -> None:
        fake_client = FakeClient(
            [
                _query_result(
                    [
                        {
                            "uuid": "src-1",
                            "organization_id": "org-1",
                            "name": "Docs",
                            "url": "https://docs.example.com",
                        }
                    ]
                ),
                _raw_query_result(
                    [
                        {
                            "uuid": "chunk-lexical",
                            "document_id": "doc-1",
                            "chunk_index": 1,
                            "chunk_type": "text",
                            "content": "alpha beta lexical match",
                            "score": 0.42,
                        }
                    ]
                ),
                _query_result(
                    [
                        {
                            "uuid": "doc-1",
                            "source_id": "src-1",
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

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            vector_rows, lexical_rows = await search_document_chunks(
                organization_id="org-1",
                query_text='alpha "beta"\x00',
                query_embedding=None,
                source_id="src-1",
                limit=5,
            )

        assert vector_rows == []
        assert [row[0].id for row in lexical_rows] == ["chunk-lexical"]
        lexical_query, lexical_params = fake_client.calls[1]
        assert "content @0@ $search_query" in lexical_query
        assert lexical_params["search_query"] == "alpha beta"

    @pytest.mark.asyncio
    async def test_search_document_chunks_empty_query_skips_lexical_search(self) -> None:
        fake_client = FakeClient(
            [
                _query_result(
                    [
                        {
                            "uuid": "src-1",
                            "organization_id": "org-1",
                            "name": "Docs",
                            "url": "https://docs.example.com",
                        }
                    ]
                ),
            ]
        )

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            vector_rows, lexical_rows = await search_document_chunks(
                organization_id="org-1",
                query_text='"\x00',
                query_embedding=None,
                source_id="src-1",
                limit=5,
            )

        assert vector_rows == []
        assert lexical_rows == []
        assert len(fake_client.calls) == 1

    @pytest.mark.asyncio
    async def test_search_document_chunks_sanitizes_source_name_fulltext_filter(self) -> None:
        fake_client = FakeClient(
            [
                _query_result([]),
            ]
        )

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            vector_rows, lexical_rows = await search_document_chunks(
                organization_id="org-1",
                query_text="alpha",
                query_embedding=None,
                source_name='DOCS "Portal"\x00',
                limit=5,
            )

        assert vector_rows == []
        assert lexical_rows == []
        source_query, source_params = fake_client.calls[0]
        assert "name @0@ $source_name" in source_query
        assert source_params["source_name"] == "docs portal"
        assert len(fake_client.calls) == 1

    @pytest.mark.asyncio
    async def test_search_document_chunks_empty_source_name_does_not_broaden_scope(self) -> None:
        fake_client = FakeClient(
            [
                _query_result([]),
            ]
        )

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            vector_rows, lexical_rows = await search_document_chunks(
                organization_id="org-1",
                query_text="alpha",
                query_embedding=None,
                source_name="",
                limit=5,
            )

        assert vector_rows == []
        assert lexical_rows == []
        source_query, source_params = fake_client.calls[0]
        assert "uuid = $source_name_empty_sentinel" in source_query
        assert source_params["source_name_empty_sentinel"] == "__sibyl_empty_source_name__"
        assert len(fake_client.calls) == 1

    @pytest.mark.asyncio
    async def test_load_search_scope_uses_source_name_fulltext_filter(self) -> None:
        fake_client = FakeClient(
            [
                _query_result([]),
            ]
        )

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            sources, sources_by_id, documents_by_id, chunks = await load_search_scope(
                organization_id="org-1",
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
    async def test_load_search_scope_empty_source_name_does_not_broaden_scope(self) -> None:
        fake_client = FakeClient(
            [
                _query_result([]),
            ]
        )

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            sources, sources_by_id, documents_by_id, chunks = await load_search_scope(
                organization_id="org-1",
                source_id=None,
                source_name="",
            )

        assert sources == []
        assert sources_by_id == {}
        assert documents_by_id == {}
        assert chunks == []
        source_query, source_params = fake_client.calls[0]
        assert "uuid = $source_name_empty_sentinel" in source_query
        assert source_params["source_name_empty_sentinel"] == "__sibyl_empty_source_name__"
        assert len(fake_client.calls) == 1

    @pytest.mark.asyncio
    async def test_load_search_scope_source_id_takes_precedence_over_source_name(self) -> None:
        fake_client = FakeClient(
            [
                _query_result([]),
            ]
        )

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            await load_search_scope(
                organization_id="org-1",
                source_id="src-1",
                source_name="docs",
            )

        source_query, source_params = fake_client.calls[0]
        assert "uuid = $source_id" in source_query
        assert "name @0@ $source_name" not in source_query
        assert source_params["source_id"] == "src-1"
        assert "source_name" not in source_params

    @pytest.mark.asyncio
    async def test_search_document_chunks_reports_raw_statement_errors(self) -> None:
        fake_client = FakeClient(
            [
                _query_result(
                    [
                        {
                            "uuid": "src-1",
                            "organization_id": "org-1",
                            "name": "Docs",
                            "url": "https://docs.example.com",
                        }
                    ]
                ),
                _raw_error_result("vector index unavailable"),
                _raw_error_result("fulltext index unavailable"),
            ]
        )

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            with pytest.raises(RuntimeError, match="vector index unavailable"):
                await search_document_chunks(
                    organization_id="org-1",
                    query_text="alpha",
                    query_embedding=[1.0, 0.0],
                    source_id="src-1",
                    limit=5,
                )

        assert len(fake_client.calls) == 3

    @pytest.mark.asyncio
    async def test_search_document_chunks_keeps_lexical_results_after_vector_timeout(
        self,
    ) -> None:
        fake_client = FakeClient(
            [
                _query_result(
                    [
                        {
                            "uuid": "src-1",
                            "organization_id": "org-1",
                            "name": "Docs",
                            "url": "https://docs.example.com",
                        }
                    ]
                ),
                _query_result(
                    [
                        {
                            "uuid": "doc-1",
                            "source_id": "src-1",
                            "url": "https://docs.example.com/auth",
                            "title": "Auth",
                            "has_code": False,
                        }
                    ]
                ),
            ]
        )

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        raw_call_count = 0

        async def slow_then_lexical(*_: object, **__: object) -> list[dict[str, object]]:
            nonlocal raw_call_count
            raw_call_count += 1
            if raw_call_count == 1:
                await asyncio.sleep(0.05)
                return []
            return [
                {
                    "uuid": "chunk-1",
                    "document_id": "doc-1",
                    "chunk_index": 0,
                    "chunk_type": "text",
                    "content": "literal auth",
                    "score": 0.44,
                }
            ]

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            monkeypatch.setattr(content_service, "_select_many_raw", slow_then_lexical)
            monkeypatch.setattr(content_service, "_DIRECT_SEARCH_QUERY_TIMEOUT_SECONDS", 0.01)
            vector_rows, lexical_rows = await search_document_chunks(
                organization_id="org-1",
                query_text="auth",
                query_embedding=[1.0, 0.0],
                source_id="src-1",
                limit=5,
            )

        assert vector_rows == []
        assert len(lexical_rows) == 1
        assert lexical_rows[0][0].id == "chunk-1"
        assert lexical_rows[0][4] == 0.44

    @pytest.mark.asyncio
    async def test_remember_raw_memory_persists_source_scope_and_provenance(self) -> None:
        persisted_memory = {
            "uuid": "memory-1",
            "organization_id": "org-1",
            "source_id": "source-email-1",
            "principal_id": "user-bliss",
            "memory_scope": "private",
            "title": "Architecture note",
            "raw_content": "Surreal stores raw memory before extraction.",
            "provenance": {"message_id": "msg-1"},
            "capture_surface": "email",
        }
        fake_client = FakeClient([_query_result([persisted_memory])])

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            memory = await remember_raw_memory(
                organization_id="org-1",
                principal_id="user-bliss",
                source_id="source-email-1",
                title="Architecture note",
                raw_content="Surreal stores raw memory before extraction.",
                provenance={"message_id": "msg-1"},
                capture_surface="email",
            )

        assert memory.source_id == "source-email-1"
        assert memory.principal_id == "user-bliss"
        assert memory.memory_scope is MemoryScope.PRIVATE
        assert memory.provenance == {"message_id": "msg-1"}
        saved_record = fake_client.calls[0][1]["record"]
        assert saved_record["source_id"] == "source-email-1"
        assert saved_record["principal_id"] == "user-bliss"
        assert saved_record["memory_scope"] == "private"
        assert saved_record["created_by_user_id"] == "user-bliss"
        assert saved_record["agent_id"] is None
        assert saved_record["project_id"] is None
        assert saved_record["review_state"] == "pending"

    @pytest.mark.asyncio
    async def test_list_raw_memories_for_promotion_filters_and_maps_entity_id(self) -> None:
        rows = [
            {
                "uuid": "memory-1",
                "organization_id": "org-1",
                "source_id": "source-email-1",
                "principal_id": "user-bliss",
                "memory_scope": "private",
                "review_state": "pending",
                "entity_id": "document-1",
                "title": "Architecture note",
                "raw_content": "promote this",
                "metadata": {},
            },
            {
                "uuid": "memory-2",
                "organization_id": "org-1",
                "source_id": "source-email-2",
                "principal_id": "user-bliss",
                "memory_scope": "private",
                "review_state": "superseded",
                "title": "Old note",
                "raw_content": "skip this",
                "metadata": {"superseded_by_raw_memory_id": "memory-1"},
            },
        ]
        fake_client = FakeClient([_query_result(rows)])

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            memories = await list_raw_memories_for_promotion(organization_id="org-1", limit=10)

        assert [memory.id for memory in memories] == ["memory-1"]
        assert memories[0].entity_id == "document-1"
        query, params = fake_client.calls[0]
        assert "metadata.raw_promotion_state" in query
        assert params["organization_id"] == "org-1"

    @pytest.mark.asyncio
    async def test_remember_raw_memory_writes_embedding_when_provider_supplied(self) -> None:
        embedding = [0.1, 0.2, 0.3]
        embedding_metadata = {
            "provider": "deterministic",
            "model": "raw-test-v1",
            "dimensions": 3,
            "cache_namespace": "raw-test",
            "tokenizer_estimate_method": "unit-test",
            "text_version": "raw-capture-v1",
            "normalize": True,
            "input_kind_sensitive": True,
        }
        persisted_memory = {
            "uuid": "memory-1",
            "organization_id": "org-1",
            "source_id": "source-email-1",
            "principal_id": "user-bliss",
            "memory_scope": "private",
            "title": "Architecture note",
            "raw_content": "Surreal stores raw memory before extraction.",
            "embedding": embedding,
            "metadata": {"embedding_metadata": embedding_metadata},
        }
        fake_client = FakeClient([_query_result([persisted_memory])])
        provider = FakeEmbeddingProvider(embedding)

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            memory = await remember_raw_memory(
                organization_id="org-1",
                principal_id="user-bliss",
                source_id="source-email-1",
                title="Architecture note",
                raw_content="Surreal stores raw memory before extraction.",
                embedding_provider=provider,
            )

        saved_record = fake_client.calls[0][1]["record"]
        assert provider.input_kinds == ["document"]
        assert provider.texts == [
            "Title: Architecture note\n\nSurreal stores raw memory before extraction."
        ]
        assert saved_record["embedding"] == embedding
        assert saved_record["metadata"]["embedding_metadata"] == embedding_metadata
        assert memory.embedding == embedding

    @pytest.mark.asyncio
    async def test_remember_raw_memory_auto_uses_configured_embedding_provider(self) -> None:
        embedding = [0.4, 0.5, 0.6]
        persisted_memory = {
            "uuid": "memory-1",
            "organization_id": "org-1",
            "source_id": "source-email-1",
            "principal_id": "user-bliss",
            "memory_scope": "private",
            "title": "Architecture note",
            "raw_content": "Surreal stores raw memory before extraction.",
            "embedding": embedding,
            "metadata": {
                "embedding_metadata": {
                    "provider": "deterministic",
                    "model": "raw-test-v1",
                    "dimensions": 3,
                    "cache_namespace": "raw-test",
                    "tokenizer_estimate_method": "unit-test",
                    "text_version": "raw-capture-v1",
                    "normalize": True,
                    "input_kind_sensitive": True,
                }
            },
        }
        fake_client = FakeClient([_query_result([persisted_memory])])
        provider = FakeEmbeddingProvider(embedding)

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            monkeypatch.setattr(
                content_service,
                "_configured_raw_memory_embedding_provider",
                lambda: provider,
            )
            await remember_raw_memory(
                organization_id="org-1",
                principal_id="user-bliss",
                source_id="source-email-1",
                title="Architecture note",
                raw_content="Surreal stores raw memory before extraction.",
            )

        saved_record = fake_client.calls[0][1]["record"]
        assert provider.input_kinds == ["document"]
        assert saved_record["embedding"] == embedding

    def test_raw_memory_embedding_text_bounds_title_and_content_surface(self) -> None:
        text = raw_memory_embedding_text(
            title="Architecture note",
            raw_content="x" * 13_000,
        )

        assert text.startswith("Title: Architecture note\n\n")
        assert len(text) == 12_000
        assert text.endswith("...[truncated for raw memory embedding]...")

    @pytest.mark.asyncio
    async def test_remember_reflection_candidate_review_stores_review_metadata(self) -> None:
        persisted_memory = {
            "uuid": "candidate-1",
            "organization_id": "org-1",
            "source_id": "source-session-1",
            "principal_id": "user-bliss",
            "memory_scope": "project",
            "scope_key": "project_123",
            "project_id": "project_123",
            "review_state": "pending",
            "entity_type": "decision",
            "title": "Decision: Native queue",
            "raw_content": "We decided reflection candidates need review.",
            "metadata": {
                "raw_source_ids": ["source-session-1"],
                "suggested_memory_scope": "project",
                "suggested_scope_key": "project_123",
                "review_state": "pending",
            },
            "capture_surface": "reflection_candidate",
        }
        fake_client = FakeClient([_query_result([persisted_memory])])

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        from sibyl_core.services import surreal_content as content_service

        candidate = ReflectionCandidate(
            kind="decision",
            title="Decision: Native queue",
            content="We decided reflection candidates need review.",
            reason="captures a durable choice",
            confidence=0.88,
            tags=["reflection", "decision"],
            metadata={"project_id": "project_123"},
        )

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            memory = await remember_reflection_candidate_review(
                organization_id="org-1",
                principal_id="user-bliss",
                candidate=candidate,
                raw_source_ids=["source-session-1"],
                memory_scope=MemoryScope.PROJECT,
                scope_key="project_123",
                suggested_memory_scope=MemoryScope.PROJECT,
                suggested_scope_key="project_123",
                extraction_prompt_metadata={"extractor": "test"},
            )

        saved_record = fake_client.calls[0][1]["record"]
        assert memory.entity_type == "decision"
        assert memory.capture_surface == "reflection_candidate"
        assert memory.review_state == "pending"
        assert saved_record["entity_type"] == "decision"
        assert saved_record["project_id"] == "project_123"
        assert saved_record["review_state"] == "pending"
        assert saved_record["metadata"]["raw_source_ids"] == ["source-session-1"]
        assert saved_record["metadata"]["suggested_memory_scope"] == "project"
        assert saved_record["metadata"]["extraction_prompt_metadata"] == {"extractor": "test"}

    @pytest.mark.asyncio
    async def test_recall_raw_memory_scopes_private_memories_to_principal(self) -> None:
        fake_client = FakeClient(
            [
                _query_result(
                    [
                        {
                            "uuid": "memory-1",
                            "organization_id": "org-1",
                            "source_id": "source-chat-1",
                            "principal_id": "user-a",
                            "memory_scope": "private",
                            "title": "Project habit",
                            "raw_content": "Bliss likes context packs with source ids.",
                            "score": 0.93,
                        }
                    ]
                )
            ]
        )

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            memories = await recall_raw_memory(
                organization_id="org-1",
                principal_id="user-a",
                query="context packs",
            )

        query, params = fake_client.calls[0]
        assert "principal_id = $principal_id" in query
        assert params["organization_id"] == "org-1"
        assert params["principal_id"] == "user-a"
        assert params["memory_scope"] == "private"
        assert params["agent_diary_surface"] == "agent_diary"
        assert "capture_surface != $agent_diary_surface" in query
        assert [memory.principal_id for memory in memories] == ["user-a"]

    @pytest.mark.asyncio
    async def test_recall_raw_memory_filters_import_metadata_in_surreal(self) -> None:
        fake_client = FakeClient(
            [
                _query_result(
                    [
                        {
                            "uuid": "memory-1",
                            "organization_id": "org-1",
                            "source_id": "source-mail-1",
                            "principal_id": "user-a",
                            "memory_scope": "private",
                            "title": "Mailbox thread",
                            "raw_content": "Nova and Bliss discussed SurrealDB.",
                            "metadata": {
                                "participants": ["nova@example.com", "bliss@example.com"],
                                "labels": ["mailbox", "email"],
                                "occurred_at": "2014-06-01T00:00:00+00:00",
                                "source_record_metadata": {"thread_id": "thread-1"},
                            },
                            "score": 0.87,
                        }
                    ]
                )
            ]
        )

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        async def no_embedding(_query: str):
            return None

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            monkeypatch.setattr(content_service, "_raw_memory_query_embedding", no_embedding)
            memories = await recall_raw_memory(
                organization_id="org-1",
                principal_id="user-a",
                query="surrealdb",
                participants=["nova@example.com"],
                labels=["email"],
                thread_id="thread-1",
                occurred_after="2014-01-01T00:00:00+00:00",
                occurred_before="2014-12-31T23:59:59+00:00",
            )

        query, params = fake_client.calls[0]
        assert "metadata.participants CONTAINSANY $participants" in query
        assert "metadata.labels CONTAINSANY $labels" in query
        assert "metadata.source_record_metadata.thread_id = $thread_id" in query
        assert "metadata.occurred_at >= $occurred_after" in query
        assert "metadata.occurred_at <= $occurred_before" in query
        assert params["participants"] == ["nova@example.com"]
        assert params["labels"] == ["email"]
        assert params["thread_id"] == "thread-1"
        assert [memory.id for memory in memories] == ["memory-1"]

    @pytest.mark.asyncio
    async def test_recall_raw_memory_fuses_fulltext_and_vector_results(self) -> None:
        fake_client = FakeClient(
            [
                _query_result(
                    [
                        {
                            "uuid": "memory-lexical",
                            "organization_id": "org-1",
                            "source_id": "source-mail-1",
                            "principal_id": "user-a",
                            "memory_scope": "private",
                            "title": "Mailbox thread",
                            "raw_content": "SurrealDB appears in the exact text.",
                            "score": 0.91,
                        }
                    ]
                ),
                _raw_query_result(
                    [
                        {
                            "uuid": "memory-vector",
                            "organization_id": "org-1",
                            "source_id": "source-mail-2",
                            "principal_id": "user-a",
                            "memory_scope": "private",
                            "title": "Semantic thread",
                            "raw_content": "The graph database powered recall.",
                            "score": 0.82,
                        }
                    ]
                ),
                _query_result(
                    [
                        {"id": "memory-vector", "rrf_score": 0.04},
                        {"id": "memory-lexical", "rrf_score": 0.03},
                    ]
                ),
            ]
        )

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        async def query_embedding(_query: str):
            return [1.0, 0.0]

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            monkeypatch.setattr(content_service, "_raw_memory_query_embedding", query_embedding)
            memories = await recall_raw_memory(
                organization_id="org-1",
                principal_id="user-a",
                query="surrealdb graph",
                limit=2,
            )

        assert [memory.id for memory in memories] == ["memory-vector", "memory-lexical"]
        assert "search::rrf($lists, $limit, $k)" in fake_client.calls[2][0]
        vector_query, vector_params = fake_client.calls[1]
        assert "embedding <|8, 40|> $query_embedding" in vector_query
        assert vector_params["query_embedding"] == [1.0, 0.0]
        assert all(memory.score > 0 for memory in memories)

    @pytest.mark.asyncio
    async def test_recall_raw_memory_filters_agent_diaries_explicitly(self) -> None:
        fake_client = FakeClient(
            [
                _query_result(
                    [
                        {
                            "uuid": "memory-1",
                            "organization_id": "org-1",
                            "source_id": "source-diary-1",
                            "principal_id": "user-a",
                            "memory_scope": "private",
                            "title": "Nova diary",
                            "raw_content": "Agent diary remembers implementation stance.",
                            "metadata": {"agent_id": "nova", "project_id": "project_123"},
                            "capture_surface": "agent_diary",
                            "score": 0.97,
                        }
                    ]
                )
            ]
        )

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            memories = await recall_raw_memory(
                organization_id="org-1",
                principal_id="user-a",
                query="implementation stance",
                agent_id="nova",
                project_id="project_123",
            )

        query, params = fake_client.calls[0]
        assert "agent_id = $agent_id" in query
        assert "project_id = $project_id" in query
        assert "capture_surface != $agent_diary_surface" not in query
        assert params["agent_id"] == "nova"
        assert params["project_id"] == "project_123"
        assert memories[0].agent_id == "nova"
        assert memories[0].project_id == "project_123"
        assert [memory.metadata["agent_id"] for memory in memories] == ["nova"]

    @pytest.mark.asyncio
    async def test_recall_raw_memory_falls_back_to_scoped_lexical_search(self) -> None:
        fake_client = FakeClient(
            [
                [{"status": "ERR", "result": "fulltext index unavailable"}],
                _query_result(
                    [
                        {
                            "uuid": "memory-1",
                            "organization_id": "org-1",
                            "source_id": "source-1",
                            "principal_id": "user-a",
                            "memory_scope": "private",
                            "title": "Planner",
                            "raw_content": "Context packs should expose rationale and sources.",
                        },
                        {
                            "uuid": "memory-2",
                            "organization_id": "org-1",
                            "source_id": "source-2",
                            "principal_id": "user-a",
                            "memory_scope": "private",
                            "title": "Groceries",
                            "raw_content": "Milk, eggs, tea.",
                        },
                    ]
                ),
            ]
        )

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            memories = await recall_raw_memory(
                organization_id="org-1",
                principal_id="user-a",
                query="context sources",
            )

        assert [memory.id for memory in memories] == ["memory-1"]
        assert memories[0].score == 1.0
        fallback_query, fallback_params = fake_client.calls[1]
        assert "principal_id = $principal_id" in fallback_query
        assert "capture_surface != $agent_diary_surface" in fallback_query
        assert fallback_params["principal_id"] == "user-a"
        assert fallback_params["agent_diary_surface"] == "agent_diary"

    @pytest.mark.asyncio
    async def test_recall_raw_memory_uses_lexical_when_fulltext_has_no_matches(self) -> None:
        fake_client = FakeClient(
            [
                _query_result([]),
                _query_result(
                    [
                        {
                            "uuid": "diary-1",
                            "organization_id": "org-1",
                            "source_id": "baseline:agent-diary",
                            "principal_id": "user-a",
                            "memory_scope": "private",
                            "agent_id": "nova",
                            "title": "Nova Baseline Diary",
                            "raw_content": (
                                "Nova diary says checkpoint Neon Thread for delegated handoff."
                            ),
                            "metadata": {"agent_id": "nova", "memory_kind": "agent_diary"},
                            "capture_surface": "agent_diary",
                        },
                        {
                            "uuid": "memory-2",
                            "organization_id": "org-1",
                            "source_id": "baseline:other",
                            "principal_id": "user-a",
                            "memory_scope": "private",
                            "agent_id": "nova",
                            "title": "Other diary",
                            "raw_content": "Unrelated scratch notes.",
                            "capture_surface": "agent_diary",
                        },
                    ]
                ),
            ]
        )

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            memories = await recall_raw_memory(
                organization_id="org-1",
                principal_id="user-a",
                query="What should Nova recall from the diary for delegated handoff? sibyl",
                agent_id="nova",
                limit=2,
            )

        assert [memory.id for memory in memories] == ["diary-1", "memory-2"]
        assert memories[0].score > memories[1].score
        fulltext_query, fulltext_params = fake_client.calls[0]
        fallback_query, fallback_params = fake_client.calls[1]
        assert "raw_content @1@ $search_query" in fulltext_query
        assert "raw_content @1@ $search_query" not in fallback_query
        assert fulltext_params["agent_id"] == "nova"
        assert fallback_params["agent_id"] == "nova"

    @pytest.mark.asyncio
    async def test_recall_raw_memory_requires_scope_key_for_project_scope(self) -> None:
        with pytest.raises(ValueError, match="project raw memory requires a scope_key"):
            await recall_raw_memory(
                organization_id="org-1",
                principal_id="user-a",
                query="context",
                memory_scope=MemoryScope.PROJECT,
            )

    @pytest.mark.parametrize(
        "memory_scope",
        [MemoryScope.DELEGATED, MemoryScope.PROJECT, MemoryScope.TEAM, MemoryScope.SHARED],
    )
    @pytest.mark.asyncio
    async def test_remember_raw_memory_requires_scope_key_for_keyed_scopes(
        self, memory_scope: MemoryScope
    ) -> None:
        with pytest.raises(
            ValueError, match=f"{memory_scope.value} raw memory requires a scope_key"
        ):
            await remember_raw_memory(
                organization_id="org-1",
                principal_id="user-a",
                source_id="source-1",
                raw_content="context",
                memory_scope=memory_scope,
            )


class TestGetRawMemoryBySourceId:
    @pytest.mark.asyncio
    async def test_unscoped_lookup_does_not_filter_by_scope(self) -> None:
        fake_client = FakeClient([_query_result([])])

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                content_service,
                "surreal_content_client",
                lambda: _yield_client(fake_client),
            )
            await get_raw_memory_by_source_id(
                organization_id="org-1",
                source_id="source-1",
            )

        query, params = fake_client.calls[0]
        assert "scope_key" not in query
        assert "principal_id" not in query
        assert "memory_scope" not in query
        assert params == {"organization_id": "org-1", "source_id": "source-1"}

    @pytest.mark.asyncio
    async def test_private_scope_lookup_filters_principal_and_null_scope_key(self) -> None:
        fake_client = FakeClient([_query_result([])])

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                content_service,
                "surreal_content_client",
                lambda: _yield_client(fake_client),
            )
            await get_raw_memory_by_source_id(
                organization_id="org-1",
                source_id="source-1",
                principal_id="user-a",
                memory_scope=MemoryScope.PRIVATE,
                scope_key=None,
            )

        query, params = fake_client.calls[0]
        assert "principal_id = $principal_id" in query
        assert "memory_scope = $memory_scope" in query
        assert "scope_key IS NONE" in query
        assert params == {
            "organization_id": "org-1",
            "source_id": "source-1",
            "principal_id": "user-a",
            "memory_scope": MemoryScope.PRIVATE.value,
        }

    @pytest.mark.asyncio
    async def test_keyed_scope_lookup_filters_scope_key_value(self) -> None:
        fake_client = FakeClient([_query_result([])])

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                content_service,
                "surreal_content_client",
                lambda: _yield_client(fake_client),
            )
            await get_raw_memory_by_source_id(
                organization_id="org-1",
                source_id="source-1",
                principal_id="user-a",
                memory_scope=MemoryScope.PROJECT,
                scope_key="project-7",
            )

        query, params = fake_client.calls[0]
        assert "scope_key = $scope_key" in query
        assert "scope_key IS NONE" not in query
        assert params["scope_key"] == "project-7"


class TestGetRawMemoryByDedupeKey:
    @pytest.mark.asyncio
    async def test_lookup_filters_by_org_and_metadata_key(self) -> None:
        fake_client = FakeClient([_query_result([])])

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                content_service,
                "surreal_content_client",
                lambda: _yield_client(fake_client),
            )
            await get_raw_memory_by_dedupe_key(
                organization_id="org-1",
                dedupe_key="source:abc",
            )

        query, params = fake_client.calls[0]
        assert "organization_id = $organization_id" in query
        assert "metadata.dedupe_key = $dedupe_key" in query
        assert params == {"organization_id": "org-1", "dedupe_key": "source:abc"}


@asynccontextmanager
async def _yield_client(fake_client: FakeClient):
    yield fake_client
