"""Tests for Surreal-backed core content helpers."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from sibyl_core.services.surreal_content import (
    MemoryScope,
    get_or_create_source,
    list_unlinked_document_chunks,
    load_search_scope,
    recall_raw_memory,
    remember_raw_memory,
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

    async def execute_query(
        self, query: str, params: dict[str, object] | None = None, **kwargs: object
    ) -> object:
        merged = dict(params or {})
        merged.update(kwargs)
        self.calls.append((query, merged))
        return self._responses.pop(0)

    async def execute_query_raw(
        self, query: str, params: dict[str, object] | None = None, **kwargs: object
    ) -> object:
        merged = dict(params or {})
        merged.update(kwargs)
        self.calls.append((query, merged))
        return self._responses.pop(0)

    async def close(self) -> None:
        return None


class TestSurrealContentHelpers:
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
                            "url": "https://docs.example.com/guide",
                            "title": "Guide",
                        }
                    ]
                ),
                _query_result(
                    [
                        {
                            "uuid": "chunk-1",
                            "document_id": "doc-1",
                            "chunk_index": 0,
                            "chunk_type": "text",
                            "content": "unlinked chunk",
                            "has_entities": False,
                        },
                        {
                            "uuid": "chunk-2",
                            "document_id": "doc-1",
                            "chunk_index": 1,
                            "chunk_type": "text",
                            "content": "linked chunk",
                            "has_entities": True,
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
        assert "LET $document_ids" in vector_query
        document_scope_query = (
            "SELECT VALUE uuid FROM crawled_documents WHERE source_id INSIDE $source_ids"
        )
        assert document_scope_query in vector_query
        assert document_scope_query in lexical_query
        assert "SELECT * FROM document_chunks" not in vector_query
        assert "embedding <|25, 40|> $query_embedding" in vector_query
        assert "content @0@ $search_query" in lexical_query
        assert "SELECT uuid, source_id, url, title, has_code" in document_query
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
    async def test_remember_raw_memory_persists_source_scope_and_provenance(self) -> None:
        fake_client = FakeClient(
            [
                _query_result([]),
                _query_result(
                    [
                        {
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
        saved_record = fake_client.calls[1][1]["record"]
        assert saved_record["source_id"] == "source-email-1"
        assert saved_record["principal_id"] == "user-bliss"
        assert saved_record["memory_scope"] == "private"
        assert saved_record["created_by_user_id"] == "user-bliss"

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
        assert "metadata.agent_id = $agent_id" in query
        assert "metadata.project_id = $project_id" in query
        assert "capture_surface != $agent_diary_surface" not in query
        assert params["agent_id"] == "nova"
        assert params["project_id"] == "project_123"
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
    async def test_recall_raw_memory_requires_scope_key_for_project_scope(self) -> None:
        with pytest.raises(ValueError, match="project raw memory requires a scope_key"):
            await recall_raw_memory(
                organization_id="org-1",
                principal_id="user-a",
                query="context",
                memory_scope=MemoryScope.PROJECT,
            )
