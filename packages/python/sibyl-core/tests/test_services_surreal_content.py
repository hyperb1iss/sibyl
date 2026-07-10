"""Tests for Surreal-backed core content helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from surrealdb import AsyncSurreal

from sibyl_core.backends.surreal.content_client import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import (
    CONTENT_ENTITY_ANCHOR_MIGRATION_DEFINITIONS,
    CONTENT_LINEAGE_RELATION_MIGRATION_DEFINITIONS,
    bootstrap_content_schema,
)
from sibyl_core.embeddings.providers import EmbeddingMetadata
from sibyl_core.errors import RevisionConflictError
from sibyl_core.models.reflection import ReflectionCandidate
from sibyl_core.services.surreal_content import (
    MemoryScope,
    RawMemory,
    RawMemoryWrite,
    _raw_memory_from_record,
    _replace_record,
    get_or_create_source,
    get_raw_memory_by_dedupe_key,
    get_raw_memory_by_source_id,
    get_raw_memory_lineage,
    list_raw_memories_for_promotion,
    list_raw_memories_for_scope,
    list_reflection_dream_source_memories,
    list_unlinked_document_chunks,
    load_search_scope,
    materialize_content_lineage,
    raw_memory_embedding_text,
    recall_raw_memory,
    recall_raw_memory_with_sources,
    remember_raw_memories,
    remember_raw_memory,
    remember_reflection_candidate_review,
    save_raw_memory,
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
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def close(self) -> None:
        self.closed += 1


class FakeLog:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, object]]] = []

    def warning(self, event: str, **kwargs: object) -> None:
        self.warnings.append((event, kwargs))


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


class EmbeddedContentClient:
    def __init__(self, db: AsyncSurreal) -> None:
        self.db = db

    async def execute_query(
        self, query: str, params: dict[str, object] | None = None, **kwargs: object
    ) -> object:
        merged = dict(params or {})
        merged.update(kwargs)
        return await self.db.query(query, merged)

    async def execute_query_raw(
        self, query: str, params: dict[str, object] | None = None, **kwargs: object
    ) -> object:
        merged = dict(params or {})
        merged.update(kwargs)
        return await self.db.query_raw(query, merged)


class TestSurrealContentHelpers:
    @pytest.mark.asyncio
    async def test_raw_memory_lineage_reads_import_and_supersession_edges(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        derived = {
            "uuid": "derived-1",
            "raw_memory_id": "raw-1",
            "source_import_id": "import-1",
        }
        supersession = {
            "uuid": "supersedes-1",
            "raw_memory_id": "raw-2",
            "superseded_raw_memory_id": "raw-1",
        }
        client = FakeClient([_query_result([derived]), _query_result([supersession])])

        @asynccontextmanager
        async def fake_session():
            yield client

        from sibyl_core.services import surreal_content as content_service

        monkeypatch.setattr(content_service, "surreal_content_client", fake_session)

        result = await get_raw_memory_lineage(
            organization_id="org-1",
            memory_id="raw-1",
        )

        assert result == {
            "derived_from": [derived],
            "supersessions": [supersession],
        }
        assert len(client.calls) == 2
        assert client.calls[0][1] == {
            "organization_id": "org-1",
            "memory_id": "raw-1",
        }
        assert client.calls[1][1] == client.calls[0][1]

    @pytest.mark.asyncio
    async def test_raw_memory_save_enforces_expected_revision(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = SurrealContentClient(url="memory://")
        try:
            await bootstrap_content_schema(client, reset=True)

            @asynccontextmanager
            async def fake_session():
                yield client

            from sibyl_core.services import surreal_content as content_service

            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            created = await remember_raw_memory(
                organization_id="org-revision",
                principal_id="bliss",
                source_id="cli:manual",
                raw_content="Revision one",
                embedding_provider=None,
            )
            assert created.revision == 1

            updated = await save_raw_memory(
                replace(created, raw_content="Revision two"),
                expected_revision=1,
                embedding_provider=None,
            )
            assert updated.revision == 2
            assert updated.raw_content == "Revision two"

            with pytest.raises(RevisionConflictError) as stale_update:
                await save_raw_memory(
                    replace(created, raw_content="Stale overwrite"),
                    expected_revision=1,
                    embedding_provider=None,
                )
            assert stale_update.value.details == {
                "identifier": created.id,
                "expected_revision": 1,
                "actual_revision": 2,
            }
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_raw_memory_save_returns_saved_record_after_commit(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        existing = {
            "uuid": "raw-1",
            "organization_id": "org-1",
            "source_id": "cli:manual",
            "principal_id": "bliss",
            "memory_scope": "private",
            "raw_content": "Original",
            "revision": 1,
        }
        saved_record = {**existing, "raw_content": "Updated", "revision": 2}
        client = FakeClient(
            [
                _query_result([existing]),
                _raw_query_result([saved_record]),
            ]
        )

        @asynccontextmanager
        async def fake_session():
            yield client

        from sibyl_core.services import surreal_content as content_service

        monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
        saved = await save_raw_memory(
            RawMemory(
                id="raw-1",
                organization_id="org-1",
                source_id="cli:manual",
                principal_id="bliss",
                raw_content="Updated",
            ),
            expected_revision=1,
            embedding_provider=None,
        )

        transaction = client.calls[1][0]
        assert transaction.index("COMMIT TRANSACTION;") < transaction.index("RETURN $saved;")
        assert saved.raw_content == "Updated"
        assert saved.revision == 2

    @pytest.mark.asyncio
    async def test_raw_memory_save_atomically_records_supersession(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = SurrealContentClient(url="memory://")
        try:
            await bootstrap_content_schema(client, reset=True)

            @asynccontextmanager
            async def fake_session():
                yield client

            from sibyl_core.services import surreal_content as content_service

            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            superseded = await remember_raw_memory(
                organization_id="org-supersession",
                principal_id="bliss",
                source_id="cli:old",
                raw_content="Old decision",
                embedding_provider=None,
            )
            replacement = await remember_raw_memory(
                organization_id="org-supersession",
                principal_id="bliss",
                source_id="cli:new",
                raw_content="New decision",
                embedding_provider=None,
            )

            saved = await save_raw_memory(
                replace(
                    superseded,
                    metadata={"superseded_by_source_id": replacement.id},
                ),
                expected_revision=1,
                superseded_by_memory_id=replacement.id,
                embedding_provider=None,
            )
            lineage = await get_raw_memory_lineage(
                organization_id="org-supersession",
                memory_id=superseded.id,
            )

            assert saved.revision == 2
            assert lineage["supersessions"] == [
                {
                    "uuid": lineage["supersessions"][0]["uuid"],
                    "raw_memory_id": replacement.id,
                    "superseded_raw_memory_id": superseded.id,
                    "source_id": replacement.source_id,
                    "created_at": lineage["supersessions"][0]["created_at"],
                }
            ]
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_materialize_content_lineage_backfills_idempotent_edges(self) -> None:
        db = AsyncSurreal("memory://")
        try:
            await db.use("content_lineage", "content")
            await db.query(
                """
                DEFINE TABLE raw_captures SCHEMAFULL;
                DEFINE FIELD uuid ON raw_captures TYPE string;
                DEFINE FIELD organization_id ON raw_captures TYPE string;
                DEFINE FIELD source_id ON raw_captures TYPE string DEFAULT '';
                DEFINE FIELD metadata ON raw_captures TYPE object FLEXIBLE DEFAULT {};
                DEFINE FIELD created_at ON raw_captures TYPE datetime DEFAULT time::now();

                DEFINE TABLE source_imports SCHEMAFULL;
                DEFINE FIELD uuid ON source_imports TYPE string;
                DEFINE FIELD organization_id ON source_imports TYPE string;
                DEFINE FIELD raw_memory_ids ON source_imports TYPE array<string> DEFAULT [];
                DEFINE FIELD created_at ON source_imports TYPE datetime DEFAULT time::now();

                DEFINE TABLE crawled_documents SCHEMAFULL;
                DEFINE FIELD uuid ON crawled_documents TYPE string;
                DEFINE FIELD organization_id ON crawled_documents TYPE string;
                DEFINE FIELD created_at ON crawled_documents TYPE datetime DEFAULT time::now();

                DEFINE TABLE document_chunks SCHEMAFULL;
                DEFINE FIELD uuid ON document_chunks TYPE string;
                DEFINE FIELD organization_id ON document_chunks TYPE string;
                DEFINE FIELD source_id ON document_chunks TYPE string DEFAULT '';
                DEFINE FIELD document_id ON document_chunks TYPE string;
                DEFINE FIELD entity_ids ON document_chunks TYPE array<string> DEFAULT [];
                DEFINE FIELD created_at ON document_chunks TYPE datetime DEFAULT time::now();
                """
            )
            await db.query(CONTENT_ENTITY_ANCHOR_MIGRATION_DEFINITIONS)
            await db.query(CONTENT_LINEAGE_RELATION_MIGRATION_DEFINITIONS)
            await db.query(
                """
                CREATE raw_captures CONTENT {
                    uuid: 'raw-old-1',
                    organization_id: 'org-1',
                    source_id: 'source-old',
                    metadata: {},
                    created_at: time::now()
                };
                CREATE raw_captures CONTENT {
                    uuid: 'raw-new-1',
                    organization_id: 'org-1',
                    source_id: 'source-new',
                    metadata: { supersedes_raw_memory_id: 'raw-old-1' },
                    created_at: time::now()
                };
                CREATE raw_captures CONTENT {
                    uuid: 'raw-old-2',
                    organization_id: 'org-1',
                    source_id: 'source-old',
                    metadata: {},
                    created_at: time::now()
                };
                CREATE raw_captures CONTENT {
                    uuid: 'raw-new-2',
                    organization_id: 'org-1',
                    source_id: 'source-new',
                    metadata: { supersedes_raw_memory_id: 'raw-old-2' },
                    created_at: time::now()
                };
                CREATE raw_captures CONTENT {
                    uuid: 'raw-old-3',
                    organization_id: 'org-1',
                    source_id: 'source-old',
                    metadata: {},
                    created_at: time::now()
                };
                CREATE raw_captures CONTENT {
                    uuid: 'raw-new-3',
                    organization_id: 'org-1',
                    source_id: 'source-new',
                    metadata: { supersedes_raw_memory_id: 'raw-old-3' },
                    created_at: time::now()
                };
                CREATE source_imports CONTENT {
                    uuid: 'import-1',
                    organization_id: 'org-1',
                    raw_memory_ids: ['raw-new-1', 'raw-new-2', 'raw-new-3'],
                    created_at: time::now()
                };
                CREATE crawled_documents CONTENT {
                    uuid: 'doc-1',
                    organization_id: 'org-1',
                    created_at: time::now()
                };
                CREATE document_chunks CONTENT {
                    uuid: 'chunk-1',
                    organization_id: 'org-1',
                    source_id: 'source-new',
                    document_id: 'doc-1',
                    entity_ids: ['entity-a', 'entity-b'],
                    created_at: time::now()
                };
                CREATE document_chunks CONTENT {
                    uuid: 'chunk-2',
                    organization_id: 'org-1',
                    source_id: 'source-new',
                    document_id: 'doc-1',
                    entity_ids: [],
                    created_at: time::now()
                };
                CREATE document_chunks CONTENT {
                    uuid: 'chunk-3',
                    organization_id: 'org-1',
                    source_id: 'source-new',
                    document_id: 'doc-1',
                    entity_ids: ['entity-c'],
                    created_at: time::now()
                };
                """
            )

            client = EmbeddedContentClient(db)
            first = await materialize_content_lineage(
                client,  # type: ignore[arg-type]
                organization_id="org-1",
                limit=2,
            )
            second = await materialize_content_lineage(
                client,  # type: ignore[arg-type]
                organization_id="org-1",
                limit=2,
            )
            derived_from = await db.query(
                "SELECT raw_memory_id, source_import_id FROM derived_from ORDER BY raw_memory_id;"
            )
            chunk_of = await db.query(
                "SELECT chunk_id, document_id FROM chunk_of ORDER BY chunk_id;"
            )
            supersedes = await db.query(
                "SELECT raw_memory_id, superseded_raw_memory_id FROM supersedes "
                "ORDER BY raw_memory_id;"
            )
            extracted_into = await db.query(
                "SELECT entity_id, chunk_id FROM extracted_into ORDER BY entity_id, chunk_id;"
            )
            entity_anchors = await db.query(
                "SELECT uuid, organization_id FROM entity ORDER BY uuid;"
            )
        finally:
            await db.close()

        assert first.derived_from == 2
        assert first.chunk_of == 2
        assert first.supersedes == 2
        assert first.extracted_into == 2
        assert second.derived_from == 3
        assert second.chunk_of == 3
        assert second.supersedes == 3
        assert second.extracted_into == 3
        assert derived_from == [
            {"raw_memory_id": "raw-new-1", "source_import_id": "import-1"},
            {"raw_memory_id": "raw-new-2", "source_import_id": "import-1"},
            {"raw_memory_id": "raw-new-3", "source_import_id": "import-1"},
        ]
        assert chunk_of == [
            {"chunk_id": "chunk-1", "document_id": "doc-1"},
            {"chunk_id": "chunk-2", "document_id": "doc-1"},
            {"chunk_id": "chunk-3", "document_id": "doc-1"},
        ]
        assert supersedes == [
            {"raw_memory_id": "raw-new-1", "superseded_raw_memory_id": "raw-old-1"},
            {"raw_memory_id": "raw-new-2", "superseded_raw_memory_id": "raw-old-2"},
            {"raw_memory_id": "raw-new-3", "superseded_raw_memory_id": "raw-old-3"},
        ]
        assert extracted_into == [
            {"entity_id": "entity-a", "chunk_id": "chunk-1"},
            {"entity_id": "entity-b", "chunk_id": "chunk-1"},
            {"entity_id": "entity-c", "chunk_id": "chunk-3"},
        ]
        assert entity_anchors == [
            {"organization_id": "org-1", "uuid": "entity-a"},
            {"organization_id": "org-1", "uuid": "entity-b"},
            {"organization_id": "org-1", "uuid": "entity-c"},
        ]

    @pytest.mark.asyncio
    async def test_materialize_content_lineage_skips_missing_endpoints_without_consuming_limit(
        self,
    ) -> None:
        db = AsyncSurreal("memory://")
        try:
            await db.use("content_lineage_missing_endpoints", "content")
            await db.query(
                """
                DEFINE TABLE raw_captures SCHEMAFULL;
                DEFINE FIELD uuid ON raw_captures TYPE string;
                DEFINE FIELD organization_id ON raw_captures TYPE string;
                DEFINE FIELD source_id ON raw_captures TYPE string DEFAULT '';
                DEFINE FIELD metadata ON raw_captures TYPE object FLEXIBLE DEFAULT {};
                DEFINE FIELD created_at ON raw_captures TYPE datetime DEFAULT time::now();

                DEFINE TABLE source_imports SCHEMAFULL;
                DEFINE FIELD uuid ON source_imports TYPE string;
                DEFINE FIELD organization_id ON source_imports TYPE string;
                DEFINE FIELD raw_memory_ids ON source_imports TYPE array<string> DEFAULT [];
                DEFINE FIELD created_at ON source_imports TYPE datetime DEFAULT time::now();

                DEFINE TABLE crawled_documents SCHEMAFULL;
                DEFINE FIELD uuid ON crawled_documents TYPE string;
                DEFINE FIELD organization_id ON crawled_documents TYPE string;
                DEFINE FIELD created_at ON crawled_documents TYPE datetime DEFAULT time::now();

                DEFINE TABLE document_chunks SCHEMAFULL;
                DEFINE FIELD uuid ON document_chunks TYPE string;
                DEFINE FIELD organization_id ON document_chunks TYPE string;
                DEFINE FIELD source_id ON document_chunks TYPE string DEFAULT '';
                DEFINE FIELD document_id ON document_chunks TYPE string;
                DEFINE FIELD entity_ids ON document_chunks TYPE array<string> DEFAULT [];
                DEFINE FIELD created_at ON document_chunks TYPE datetime DEFAULT time::now();
                """
            )
            await db.query(CONTENT_ENTITY_ANCHOR_MIGRATION_DEFINITIONS)
            await db.query(CONTENT_LINEAGE_RELATION_MIGRATION_DEFINITIONS)
            await db.query(
                """
                CREATE raw_captures CONTENT {
                    uuid: 'raw-valid',
                    organization_id: 'org-1',
                    source_id: 'source-valid',
                    metadata: {},
                    created_at: time::now()
                };
                CREATE raw_captures CONTENT {
                    uuid: 'raw-missing-superseded',
                    organization_id: 'org-1',
                    source_id: 'source-valid',
                    metadata: { supersedes_raw_memory_id: 'raw-missing' },
                    created_at: time::now()
                };
                CREATE raw_captures CONTENT {
                    uuid: 'raw-old-valid',
                    organization_id: 'org-1',
                    source_id: 'source-valid',
                    metadata: {},
                    created_at: time::now()
                };
                CREATE raw_captures CONTENT {
                    uuid: 'raw-new-valid',
                    organization_id: 'org-1',
                    source_id: 'source-valid',
                    metadata: { supersedes_raw_memory_id: 'raw-old-valid' },
                    created_at: time::now()
                };
                CREATE source_imports CONTENT {
                    uuid: 'import-1',
                    organization_id: 'org-1',
                    raw_memory_ids: ['raw-missing', 'raw-valid'],
                    created_at: time::now()
                };
                CREATE crawled_documents CONTENT {
                    uuid: 'doc-valid',
                    organization_id: 'org-1',
                    created_at: time::now()
                };
                CREATE document_chunks CONTENT {
                    uuid: 'chunk-missing-document',
                    organization_id: 'org-1',
                    source_id: 'source-valid',
                    document_id: 'doc-missing',
                    entity_ids: [],
                    created_at: time::now()
                };
                CREATE document_chunks CONTENT {
                    uuid: 'chunk-valid',
                    organization_id: 'org-1',
                    source_id: 'source-valid',
                    document_id: 'doc-valid',
                    entity_ids: ['entity-valid'],
                    created_at: time::now()
                };
                """
            )

            client = EmbeddedContentClient(db)
            result = await materialize_content_lineage(
                client,  # type: ignore[arg-type]
                organization_id="org-1",
                limit=1,
            )
            derived_from = await db.query(
                "SELECT raw_memory_id, source_import_id FROM derived_from;"
            )
            chunk_of = await db.query("SELECT chunk_id, document_id FROM chunk_of;")
            supersedes = await db.query(
                "SELECT raw_memory_id, superseded_raw_memory_id FROM supersedes;"
            )
            extracted_into = await db.query("SELECT entity_id, chunk_id FROM extracted_into;")
            entity_anchors = await db.query("SELECT uuid, organization_id FROM entity;")
        finally:
            await db.close()

        assert result.derived_from == 1
        assert result.chunk_of == 1
        assert result.supersedes == 1
        assert result.extracted_into == 1
        assert derived_from == [{"raw_memory_id": "raw-valid", "source_import_id": "import-1"}]
        assert chunk_of == [{"chunk_id": "chunk-valid", "document_id": "doc-valid"}]
        assert supersedes == [
            {"raw_memory_id": "raw-new-valid", "superseded_raw_memory_id": "raw-old-valid"}
        ]
        assert extracted_into == [{"entity_id": "entity-valid", "chunk_id": "chunk-valid"}]
        assert entity_anchors == [{"organization_id": "org-1", "uuid": "entity-valid"}]

    @pytest.mark.asyncio
    async def test_surreal_content_client_reuses_shared_client(self) -> None:
        first_client = FakeClient([])
        second_client = FakeClient([])

        from sibyl_core.services import surreal_content as content_service

        await content_service.close_shared_surreal_content_client()
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                content_service,
                "build_surreal_content_client",
                lambda: first_client if first_client.closed == 0 else second_client,
            )
            try:
                async with (
                    content_service.surreal_content_client() as first,
                    content_service.surreal_content_client() as second,
                ):
                    assert first is second
                assert first is first_client
                assert first_client.closed == 0
            finally:
                await content_service.close_shared_surreal_content_client()

        assert first_client.closed == 1
        assert second_client.closed == 0

    def test_build_surreal_content_client_uses_configured_pool_size(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, object] = {}

        class FakeSurrealContentClient:
            def __init__(self, **kwargs: object) -> None:
                captured.update(kwargs)

        from sibyl_core.services import surreal_content as content_service

        monkeypatch.setattr(content_service, "SurrealContentClient", FakeSurrealContentClient)
        monkeypatch.setattr(content_service.settings, "surreal_pool_size", 8)
        monkeypatch.setattr(content_service.settings, "surreal_content_pool_size", 21)

        content_service.build_surreal_content_client()

        assert captured["pool_size"] == 21

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
                            "snippet": "alpha <mark>lexical</mark> match",
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
        assert lexical_rows[0][0].snippet == "alpha <mark>lexical</mark> match"
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
        assert "search::highlight('<mark>', '</mark>', 0) AS snippet" in lexical_query
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
    async def test_load_search_scope_excludes_chunk_embeddings(self) -> None:
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
                            "organization_id": "org-1",
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
                            "organization_id": "org-1",
                            "source_id": "src-1",
                            "document_id": "doc-1",
                            "chunk_index": 0,
                            "content": "alpha",
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
            _sources, _sources_by_id, _documents_by_id, chunks = await load_search_scope(
                organization_id="org-1",
                source_id="src-1",
                source_name=None,
            )

        chunk_query, chunk_params = fake_client.calls[2]
        assert [chunk.id for chunk in chunks] == ["chunk-1"]
        assert "SELECT * FROM document_chunks" not in chunk_query
        assert "embedding" not in chunk_query
        assert "document_id INSIDE $document_ids" in chunk_query
        assert chunk_params["document_ids"] == ["doc-1"]

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
    async def test_search_document_chunks_reports_vector_timeout_with_lexical_results(
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
            with pytest.raises(RuntimeError, match="surreal_document_vector_search timed out"):
                await search_document_chunks(
                    organization_id="org-1",
                    query_text="auth",
                    query_embedding=[1.0, 0.0],
                    source_id="src-1",
                    limit=5,
                )

        assert raw_call_count == 2

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
                metadata={
                    "memory_importance": 0.8,
                    "reflection_confidence": 0.9,
                },
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
        assert saved_record["metadata"]["importance"] == 0.8
        assert saved_record["metadata"]["confidence"] == 0.9
        assert saved_record["metadata"]["memory_importance"] == 0.8
        assert saved_record["metadata"]["reflection_confidence"] == 0.9

    @pytest.mark.asyncio
    async def test_list_raw_memories_for_promotion_filters_and_maps_entity_id(self) -> None:
        rows = [
            {
                "uuid": "memory-1",
                "organization_id": "org-1",
                "source_id": "source-email-1",
                "principal_id": "user-bliss",
                "memory_scope": "organization",
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
                "memory_scope": "organization",
                "review_state": "superseded",
                "title": "Old note",
                "raw_content": "skip this",
                "metadata": {"superseded_by_raw_memory_id": "memory-1"},
            },
            {
                "uuid": "memory-3",
                "organization_id": "org-1",
                "source_id": "source-email-3",
                "principal_id": "user-bliss",
                "memory_scope": "private",
                "review_state": "pending",
                "title": "Invalidated note",
                "raw_content": "skip this too",
                "metadata": {"invalid_at": "2020-01-01T00:00:00+00:00"},
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
        assert "memory_scope INSIDE $raw_promotion_visible_scopes" in query
        assert "metadata.raw_promotion_state" in query
        assert "metadata.raw_promotion_lineage_missing_count > 0" in query
        assert "metadata.raw_promotion_lineage_missing_count = NONE" in query
        assert "metadata.source_record_metadata.parent_uuid != NONE" in query
        assert params["organization_id"] == "org-1"
        assert params["raw_promotion_visible_scopes"] == ["organization", "public"]

    @pytest.mark.asyncio
    async def test_list_raw_memories_for_promotion_excludes_private_raw_ids(self) -> None:
        rows = [
            {
                "uuid": "memory-private",
                "organization_id": "org-1",
                "source_id": "source-private",
                "principal_id": "user-bliss",
                "memory_scope": "private",
                "review_state": "pending",
                "title": "Private note",
                "raw_content": "do not promote",
                "metadata": {},
            },
            {
                "uuid": "memory-org",
                "organization_id": "org-1",
                "source_id": "source-org",
                "principal_id": "user-bliss",
                "memory_scope": "organization",
                "review_state": "pending",
                "title": "Org note",
                "raw_content": "safe to promote",
                "metadata": {},
            },
        ]
        fake_client = FakeClient([_query_result(rows)])

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            memories = await list_raw_memories_for_promotion(
                organization_id="org-1",
                raw_memory_ids=["memory-private", "memory-org"],
                limit=10,
            )

        assert [memory.id for memory in memories] == ["memory-org"]
        query, params = fake_client.calls[0]
        assert "memory_scope INSIDE $raw_promotion_visible_scopes" in query
        assert params["raw_memory_ids"] == ["memory-private", "memory-org"]
        assert params["raw_promotion_visible_scopes"] == ["organization", "public"]

    @pytest.mark.asyncio
    async def test_list_raw_memories_for_scope_filters_currently_invalidated(self) -> None:
        rows = [
            {
                "uuid": "memory-1",
                "organization_id": "org-1",
                "source_id": "source-email-1",
                "principal_id": "user-bliss",
                "memory_scope": "private",
                "review_state": "pending",
                "title": "Current note",
                "raw_content": "keep this",
                "metadata": {},
            },
            {
                "uuid": "memory-2",
                "organization_id": "org-1",
                "source_id": "source-email-2",
                "principal_id": "user-bliss",
                "memory_scope": "private",
                "review_state": "pending",
                "title": "Invalidated note",
                "raw_content": "skip this",
                "metadata": {"valid_to": "2020-01-01T00:00:00+00:00"},
            },
        ]
        fake_client = FakeClient([_query_result(rows)])

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            memories = await list_raw_memories_for_scope(
                organization_id="org-1",
                principal_id="user-bliss",
                memory_scope=MemoryScope.PRIVATE,
                limit=10,
            )

        assert [memory.id for memory in memories] == ["memory-1"]

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
    async def test_save_raw_memory_reembeds_changed_content(self) -> None:
        old_embedding = [0.1, 0.2, 0.3]
        new_embedding = [0.7, 0.8, 0.9]
        existing_memory = {
            "uuid": "memory-1",
            "organization_id": "org-1",
            "source_id": "source-email-1",
            "principal_id": "user-bliss",
            "memory_scope": "private",
            "review_state": "pending",
            "title": "Architecture note",
            "raw_content": "Old Surreal write path.",
            "embedding": old_embedding,
            "metadata": {"embedding_metadata": {"dimensions": 3}},
        }
        persisted_memory = {
            **existing_memory,
            "raw_content": "Updated Surreal write path.",
            "embedding": new_embedding,
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
        fake_client = FakeClient(
            [_query_result([existing_memory]), _query_result([persisted_memory])]
        )
        provider = FakeEmbeddingProvider(new_embedding)

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        from sibyl_core.services import surreal_content as content_service

        memory = _raw_memory_from_record(existing_memory)
        memory.raw_content = "Updated Surreal write path."
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            saved = await save_raw_memory(memory, embedding_provider=provider)

        saved_record = fake_client.calls[1][1]["record"]
        assert provider.texts == ["Title: Architecture note\n\nUpdated Surreal write path."]
        assert saved_record["embedding"] == new_embedding
        assert saved_record["metadata"]["embedding_metadata"]["model"] == "raw-test-v1"
        assert saved.embedding == new_embedding

    @pytest.mark.asyncio
    async def test_save_raw_memory_clears_hidden_lifecycle_embeddings(self) -> None:
        existing_memory = {
            "uuid": "memory-1",
            "organization_id": "org-1",
            "source_id": "source-email-1",
            "principal_id": "user-bliss",
            "memory_scope": "private",
            "review_state": "pending",
            "title": "Architecture note",
            "raw_content": "Surreal write path.",
            "embedding": [0.1, 0.2, 0.3],
            "metadata": {"embedding_metadata": {"dimensions": 3}},
        }
        persisted_memory = {
            **existing_memory,
            "review_state": "superseded",
            "embedding": None,
            "metadata": {},
        }
        fake_client = FakeClient(
            [_query_result([existing_memory]), _query_result([persisted_memory])]
        )
        provider = FakeEmbeddingProvider([0.7, 0.8, 0.9])

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        from sibyl_core.services import surreal_content as content_service

        memory = _raw_memory_from_record(existing_memory)
        memory.review_state = "superseded"
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            saved = await save_raw_memory(memory, embedding_provider=provider)

        saved_record = fake_client.calls[1][1]["record"]
        assert provider.texts == []
        assert saved_record["embedding"] is None
        assert "embedding_metadata" not in saved_record["metadata"]
        assert saved.embedding is None

    @pytest.mark.asyncio
    async def test_save_raw_memory_reembeds_restored_memory(self) -> None:
        embedding = [0.7, 0.8, 0.9]
        hidden_memory = {
            "uuid": "memory-1",
            "organization_id": "org-1",
            "source_id": "source-email-1",
            "principal_id": "user-bliss",
            "memory_scope": "private",
            "review_state": "superseded",
            "title": "Architecture note",
            "raw_content": "Surreal write path.",
            "embedding": None,
            "metadata": {},
        }
        persisted_memory = {**hidden_memory, "review_state": "pending", "embedding": embedding}
        fake_client = FakeClient(
            [_query_result([hidden_memory]), _query_result([persisted_memory])]
        )
        provider = FakeEmbeddingProvider(embedding)

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        from sibyl_core.services import surreal_content as content_service

        memory = _raw_memory_from_record(hidden_memory)
        memory.review_state = "pending"
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            saved = await save_raw_memory(memory, embedding_provider=provider)

        saved_record = fake_client.calls[1][1]["record"]
        assert provider.texts == ["Title: Architecture note\n\nSurreal write path."]
        assert saved_record["embedding"] == embedding
        assert saved_record["metadata"]["embedding_metadata"]["model"] == "raw-test-v1"
        assert saved.embedding == embedding

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

    @pytest.mark.asyncio
    async def test_remember_raw_memories_batches_embeddings_and_write(self) -> None:
        embedding = [0.7, 0.8, 0.9]
        persisted = [
            {
                "uuid": "memory-1",
                "organization_id": "org-1",
                "source_id": "source-email-1",
                "principal_id": "user-bliss",
                "memory_scope": "private",
                "title": "First",
                "raw_content": "first body",
                "embedding": embedding,
                "metadata": {"embedding_metadata": {"dimensions": 3}},
            },
            {
                "uuid": "memory-2",
                "organization_id": "org-1",
                "source_id": "source-email-2",
                "principal_id": "user-bliss",
                "memory_scope": "private",
                "title": "Second",
                "raw_content": "second body",
                "embedding": embedding,
                "metadata": {"embedding_metadata": {"dimensions": 3}},
            },
        ]
        fake_client = FakeClient([_query_result(persisted)])
        provider = FakeEmbeddingProvider(embedding)

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            ids = iter(["memory-1", "memory-2"])
            monkeypatch.setattr(content_service, "uuid4", lambda: next(ids))
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            memories = await remember_raw_memories(
                [
                    RawMemoryWrite(
                        organization_id="org-1",
                        principal_id="user-bliss",
                        source_id="source-email-1",
                        title="First",
                        raw_content="first body",
                    ),
                    RawMemoryWrite(
                        organization_id="org-1",
                        principal_id="user-bliss",
                        source_id="source-email-2",
                        title="Second",
                        raw_content="second body",
                    ),
                ],
                embedding_provider=provider,
            )

        assert [memory.id for memory in memories] == ["memory-1", "memory-2"]
        assert provider.input_kinds == ["document"]
        assert provider.texts == ["Title: First\n\nfirst body", "Title: Second\n\nsecond body"]
        query, params = fake_client.calls[0]
        assert "INSERT INTO raw_captures $rows" in query
        assert len(params["rows"]) == 2
        assert [row["source_id"] for row in params["rows"]] == [
            "source-email-1",
            "source-email-2",
        ]
        assert all(row["embedding"] == embedding for row in params["rows"])

    @pytest.mark.asyncio
    async def test_remember_raw_memories_orders_shuffled_bulk_returns(self) -> None:
        embedding = [0.7, 0.8, 0.9]
        persisted = [
            {
                "uuid": "memory-2",
                "organization_id": "org-1",
                "source_id": "source-email-2",
                "principal_id": "user-bliss",
                "memory_scope": "private",
                "title": "Second",
                "raw_content": "second body",
                "embedding": embedding,
                "metadata": {"embedding_metadata": {"dimensions": 3}},
            },
            {
                "uuid": "memory-1",
                "organization_id": "org-1",
                "source_id": "source-email-1",
                "principal_id": "user-bliss",
                "memory_scope": "private",
                "title": "First",
                "raw_content": "first body",
                "embedding": embedding,
                "metadata": {"embedding_metadata": {"dimensions": 3}},
            },
        ]
        fake_client = FakeClient([_query_result(persisted)])
        provider = FakeEmbeddingProvider(embedding)

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            ids = iter(["memory-1", "memory-2"])
            monkeypatch.setattr(content_service, "uuid4", lambda: next(ids))
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            memories = await remember_raw_memories(
                [
                    RawMemoryWrite(
                        organization_id="org-1",
                        principal_id="user-bliss",
                        source_id="source-email-1",
                        title="First",
                        raw_content="first body",
                    ),
                    RawMemoryWrite(
                        organization_id="org-1",
                        principal_id="user-bliss",
                        source_id="source-email-2",
                        title="Second",
                        raw_content="second body",
                    ),
                ],
                embedding_provider=provider,
            )

        assert [memory.id for memory in memories] == ["memory-1", "memory-2"]
        assert [memory.source_id for memory in memories] == [
            "source-email-1",
            "source-email-2",
        ]

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
    async def test_list_reflection_dream_source_memories_excludes_self_feeding_surfaces(
        self,
    ) -> None:
        captured_at = datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC)

        def row(
            memory_id: str,
            *,
            capture_surface: str | None,
            metadata: dict[str, object] | None = None,
        ) -> dict[str, object]:
            return {
                "uuid": memory_id,
                "organization_id": "org-1",
                "source_id": f"{memory_id}:source",
                "principal_id": "user-bliss",
                "memory_scope": "private",
                "review_state": "pending",
                "entity_type": "raw_memory",
                "title": memory_id,
                "raw_content": "A durable source memory with enough signal.",
                "metadata": metadata or {},
                "capture_surface": capture_surface,
                "captured_at": captured_at,
                "created_at": captured_at,
            }

        fake_client = FakeClient(
            [
                _query_result(
                    [
                        row("valid-cli", capture_surface="cli"),
                        row("candidate", capture_surface="reflection_candidate"),
                        row("source", capture_surface="reflection_source"),
                        row("reflection", capture_surface="reflection"),
                        row("synthesis", capture_surface="synthesis_artifact"),
                        row(
                            "metadata-reflection",
                            capture_surface=None,
                            metadata={"capture_surface": "reflection"},
                        ),
                        row(
                            "processed",
                            capture_surface="cli",
                            metadata={"reflection_dream_processed_at": captured_at.isoformat()},
                        ),
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
            memories = await list_reflection_dream_source_memories(
                organization_id="org-1",
                limit=10,
            )

        query, params = fake_client.calls[0]
        assert "capture_surface != $reflection_surface" in query
        assert "capture_surface != $synthesis_surface" in query
        assert params["reflection_surface"] == "reflection"
        assert params["synthesis_surface"] == "synthesis_artifact"
        assert [memory.id for memory in memories] == ["valid-cli"]

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
                                "valid_from": "2014-03-01T00:00:00+00:00",
                                "source_record_metadata": {"thread_id": "thread-1"},
                            },
                            "captured_at": datetime(2014, 6, 1, tzinfo=UTC),
                            "created_at": datetime(2014, 6, 1, tzinfo=UTC),
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
                source_ids=["source-mail-1"],
                participants=["nova@example.com"],
                labels=["email"],
                thread_id="thread-1",
                occurred_after="2014-01-01T00:00:00+00:00",
                occurred_before="2014-12-31T23:59:59+00:00",
                as_of="2014-07-01T00:00:00+00:00",
            )

        query, params = fake_client.calls[0]
        assert "source_id IN $source_ids" in query
        assert "metadata.participants CONTAINSANY $participants" in query
        assert "metadata.labels CONTAINSANY $labels" in query
        assert "metadata.source_record_metadata.thread_id = $thread_id" in query
        assert "metadata.occurred_at >= $occurred_after" in query
        assert "metadata.occurred_at <= $occurred_before" in query
        assert "datetime(created_at) AND created_at <= $as_of" in query
        assert "datetime(captured_at) AND captured_at <= $as_of" in query
        assert "datetime(metadata.valid_from)" in query
        assert "datetime(metadata.invalid_at)" in query
        assert "created_at <= $as_of_text" in query
        assert "captured_at <= $as_of_text" in query
        assert "metadata.valid_from <= $as_of_text" in query
        assert "metadata.invalid_at > $as_of_text" in query
        assert "type::is" in query
        assert params["source_ids"] == ["source-mail-1"]
        assert params["participants"] == ["nova@example.com"]
        assert params["labels"] == ["email"]
        assert params["thread_id"] == "thread-1"
        assert params["as_of"] == datetime(2014, 7, 1, tzinfo=UTC)
        assert params["as_of_text"] == "2014-07-01T00:00:00+00:00"
        assert [memory.id for memory in memories] == ["memory-1"]

    @pytest.mark.asyncio
    async def test_recall_raw_memory_as_of_post_filter_hides_future_records(self) -> None:
        fake_client = FakeClient(
            [
                _query_result(
                    [
                        {
                            "uuid": "future-memory",
                            "organization_id": "org-1",
                            "source_id": "source-mail-1",
                            "principal_id": "user-a",
                            "memory_scope": "private",
                            "title": "Future thread",
                            "raw_content": "SurrealDB future note.",
                            "captured_at": datetime(2025, 5, 1, tzinfo=UTC),
                            "created_at": datetime(2025, 5, 1, tzinfo=UTC),
                            "score": 0.95,
                        },
                        {
                            "uuid": "valid-memory",
                            "organization_id": "org-1",
                            "source_id": "source-mail-1",
                            "principal_id": "user-a",
                            "memory_scope": "private",
                            "title": "Valid thread",
                            "raw_content": "SurrealDB valid note.",
                            "metadata": {
                                "valid_from": "2025-01-01T00:00:00+00:00",
                                "valid_to": "2025-04-01T00:00:00+00:00",
                            },
                            "captured_at": datetime(2025, 1, 1, tzinfo=UTC),
                            "created_at": datetime(2025, 1, 1, tzinfo=UTC),
                            "score": 0.9,
                        },
                        {
                            "uuid": "invalid-memory",
                            "organization_id": "org-1",
                            "source_id": "source-mail-1",
                            "principal_id": "user-a",
                            "memory_scope": "private",
                            "title": "Invalid thread",
                            "raw_content": "SurrealDB invalid note.",
                            "metadata": {"invalid_at": "2025-02-01T00:00:00+00:00"},
                            "captured_at": datetime(2025, 1, 1, tzinfo=UTC),
                            "created_at": datetime(2025, 1, 1, tzinfo=UTC),
                            "score": 0.88,
                        },
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
                as_of=datetime(2025, 3, 1, tzinfo=UTC),
            )

        assert [memory.id for memory in memories] == ["valid-memory"]

    @pytest.mark.asyncio
    async def test_recall_raw_memory_default_hides_currently_invalidated_records(self) -> None:
        fake_client = FakeClient(
            [
                _query_result(
                    [
                        {
                            "uuid": "invalid-memory",
                            "organization_id": "org-1",
                            "source_id": "source-mail-1",
                            "principal_id": "user-a",
                            "memory_scope": "private",
                            "title": "Invalid thread",
                            "raw_content": "SurrealDB invalid note.",
                            "metadata": {"invalid_at": "2001-01-01T00:00:00+00:00"},
                            "captured_at": datetime(2000, 1, 1, tzinfo=UTC),
                            "created_at": datetime(2000, 1, 1, tzinfo=UTC),
                            "score": 0.95,
                        },
                        {
                            "uuid": "current-memory",
                            "organization_id": "org-1",
                            "source_id": "source-mail-1",
                            "principal_id": "user-a",
                            "memory_scope": "private",
                            "title": "Current thread",
                            "raw_content": "SurrealDB current note.",
                            "captured_at": datetime(2000, 1, 1, tzinfo=UTC),
                            "created_at": datetime(2000, 1, 1, tzinfo=UTC),
                            "score": 0.9,
                        },
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
            )

        assert [memory.id for memory in memories] == ["current-memory"]
        query, params = fake_client.calls[0]
        assert "metadata.invalid_at" in query
        assert "metadata.valid_to" in query
        assert isinstance(params["as_of"], datetime)
        assert isinstance(params["as_of_text"], str)

    def test_raw_memory_snippet_prefers_marked_title_over_plain_content(self) -> None:
        memory = _raw_memory_from_record(
            {
                "uuid": "memory-title-hit",
                "organization_id": "org-1",
                "source_id": "source-mail-1",
                "principal_id": "user-a",
                "title": "Mailbox thread",
                "raw_content": "The body did not match the lexical query.",
                "content_snippet": "The body did not match the lexical query.",
                "title_snippet": "<mark>Mailbox</mark> thread",
            }
        )

        assert memory.snippet == "<mark>Mailbox</mark> thread"

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
                            "content_snippet": "<mark>SurrealDB</mark> appears in the exact text.",
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
        assert memories[1].snippet == "<mark>SurrealDB</mark> appears in the exact text."
        assert "search::rrf($lists, $limit, $k)" in fake_client.calls[2][0]
        fulltext_query, _fulltext_params = fake_client.calls[0]
        vector_query, vector_params = fake_client.calls[1]
        assert "SELECT * FROM raw_captures" not in fulltext_query
        assert "SELECT *, math::max" not in fulltext_query
        assert "id AS record_id, uuid" in fulltext_query
        assert "embedding," not in fulltext_query
        assert "SELECT *, (1 - vector::distance::knn()) AS score" not in vector_query
        assert "id AS record_id, uuid" in vector_query
        assert "embedding," not in vector_query
        assert "search::highlight('<mark>', '</mark>', 0) AS title_snippet" in fulltext_query
        assert "search::highlight('<mark>', '</mark>', 1) AS content_snippet" in fulltext_query
        assert "embedding <|8, 40|> $query_embedding" in vector_query
        assert vector_params["query_embedding"] == [1.0, 0.0]
        assert all(memory.score > 0 for memory in memories)

    @pytest.mark.asyncio
    async def test_recall_raw_memory_falls_back_when_vector_recall_fails(self) -> None:
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
                _raw_error_result("HNSW vector index unavailable"),
            ]
        )

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        async def query_embedding(_query: str):
            return [1.0, 0.0]

        fake_log = FakeLog()

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "log", fake_log)
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            monkeypatch.setattr(content_service, "_raw_memory_query_embedding", query_embedding)
            memories = await recall_raw_memory(
                organization_id="org-1",
                principal_id="user-a",
                query="surrealdb graph",
                limit=2,
            )

        assert len(fake_client.calls) == 2
        assert [memory.id for memory in memories] == ["memory-lexical"]
        assert fake_log.warnings == [
            (
                "raw_memory_vector_recall_failed",
                {
                    "organization_id": "org-1",
                    "memory_scope": "private",
                    "has_scope_key": False,
                    "error_type": "RuntimeError",
                },
            )
        ]
        vector_query, _vector_params = fake_client.calls[1]
        assert "embedding <|8, 40|> $query_embedding" in vector_query

    @pytest.mark.asyncio
    async def test_recall_raw_memory_falls_back_when_vector_driver_raises(self) -> None:
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
                OSError("connection dropped"),
            ]
        )

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        async def query_embedding(_query: str):
            return [1.0, 0.0]

        fake_log = FakeLog()

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "log", fake_log)
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            monkeypatch.setattr(content_service, "_raw_memory_query_embedding", query_embedding)
            memories = await recall_raw_memory(
                organization_id="org-1",
                principal_id="user-a",
                query="surrealdb graph",
                limit=2,
            )

        assert [memory.id for memory in memories] == ["memory-lexical"]
        assert fake_log.warnings == [
            (
                "raw_memory_vector_recall_failed",
                {
                    "organization_id": "org-1",
                    "memory_scope": "private",
                    "has_scope_key": False,
                    "error_type": "OSError",
                },
            )
        ]

    @pytest.mark.asyncio
    async def test_recall_raw_memory_with_sources_reports_vector_failure(self) -> None:
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
                _raw_error_result("HNSW vector index unavailable"),
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
            result = await recall_raw_memory_with_sources(
                organization_id="org-1",
                principal_id="user-a",
                query="surrealdb graph",
                limit=2,
            )

        assert [memory.id for memory in result.memories] == ["memory-lexical"]
        assert result.degraded is True
        assert result.as_metadata() == {
            "raw_recall_degraded": True,
            "raw_recall_failure_count": 1,
            "raw_recall_failures": [{"source": "raw_vector", "error_type": "RuntimeError"}],
        }
        assert len(fake_client.calls) == 2

    @pytest.mark.asyncio
    async def test_recall_raw_memory_falls_back_when_query_embedding_fails(self) -> None:
        class FailingEmbeddingProvider:
            metadata = EmbeddingMetadata(
                provider="deterministic",
                model="failing-query-provider",
                dimensions=2,
                cache_namespace="test",
                tokenizer_estimate_method="unit-test",
            )

            async def embed_texts(self, texts, *, input_kind: str = "document"):
                raise RuntimeError("embedding provider unavailable")

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
            ]
        )

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        fake_log = FakeLog()

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "log", fake_log)
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            monkeypatch.setattr(
                content_service,
                "_configured_raw_memory_embedding_provider",
                FailingEmbeddingProvider,
            )
            memories = await recall_raw_memory(
                organization_id="org-1",
                principal_id="user-a",
                query="surrealdb graph",
                limit=2,
            )

        assert [memory.id for memory in memories] == ["memory-lexical"]
        assert len(fake_client.calls) == 1
        assert fake_log.warnings == [
            (
                "raw_memory_query_embedding_failed",
                {
                    "provider": "deterministic",
                    "model": "failing-query-provider",
                    "dimensions": 2,
                    "query_length": len("surrealdb graph"),
                    "error_type": "RuntimeError",
                },
            )
        ]
        fulltext_query, _fulltext_params = fake_client.calls[0]
        assert "title @0@ $search_query OR raw_content @1@ $search_query" in fulltext_query

    @pytest.mark.asyncio
    async def test_recall_raw_memory_falls_back_when_embedding_provider_setup_fails(
        self,
    ) -> None:
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
            ]
        )

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        def failing_provider():
            raise ValueError("unsupported raw memory embedding provider")

        fake_log = FakeLog()

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "log", fake_log)
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            monkeypatch.setattr(
                content_service,
                "_configured_raw_memory_embedding_provider",
                failing_provider,
            )
            memories = await recall_raw_memory(
                organization_id="org-1",
                principal_id="user-a",
                query="surrealdb graph",
                limit=2,
            )

        assert [memory.id for memory in memories] == ["memory-lexical"]
        assert len(fake_client.calls) == 1
        assert fake_log.warnings == [
            (
                "raw_memory_query_embedding_failed",
                {
                    "provider": None,
                    "model": None,
                    "dimensions": None,
                    "query_length": len("surrealdb graph"),
                    "error_type": "ValueError",
                },
            )
        ]

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

    @pytest.mark.asyncio
    async def test_lookup_can_filter_import_visibility_scope(self) -> None:
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
                principal_id="user-a",
                memory_scope=MemoryScope.PROJECT,
                scope_key="project-7",
            )

        query, params = fake_client.calls[0]
        assert "principal_id = $principal_id" in query
        assert "memory_scope = $memory_scope" in query
        assert "scope_key = $scope_key" in query
        assert params["principal_id"] == "user-a"
        assert params["memory_scope"] == "project"
        assert params["scope_key"] == "project-7"


@asynccontextmanager
async def _yield_client(fake_client: FakeClient):
    yield fake_client
