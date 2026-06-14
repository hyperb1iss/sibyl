from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any
from uuid import uuid4

import pytest

from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema
from sibyl_core.backends.surreal.content_schema import EMBEDDING_DIM
from sibyl_core.backends.surreal.dedicated_client import DedicatedSurrealClient
from sibyl_core.embeddings.providers import EmbeddingMetadata
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services.graph import (
    EntityManager,
    SurrealGraphClient,
    close_graph_clients,
    get_surreal_graph_runtime,
    normalize_records,
    prepare_graph_schema,
)
from sibyl_core.services.surreal_content import (
    MemoryScope,
    RawMemoryWrite,
    materialize_content_lineage,
    recall_raw_memory,
    remember_raw_memories,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("SIBYL_LIVE_SURREAL_TESTS") != "1",
    reason="live SurrealDB runtime smoke tests are disabled",
)

_EMBEDDED_SURREAL_SCHEMES = ("memory://", "surrealkv://", "rocksdb://", "file://")


def _live_surreal_url() -> str:
    url = os.environ.get("SIBYL_SURREAL_URL", "")
    if not url or url.startswith(_EMBEDDED_SURREAL_SCHEMES):
        pytest.skip("live SurrealDB tests require SIBYL_SURREAL_URL to point at a server")
    return url


def _surreal_username() -> str:
    return os.environ.get("SIBYL_SURREAL_USERNAME", "root")


def _surreal_password() -> str:
    return os.environ.get("SIBYL_SURREAL_PASSWORD", "root")


class _StaticEmbeddingProvider:
    def __init__(self, embedding: list[float]) -> None:
        self._embedding = embedding
        self.metadata = EmbeddingMetadata(
            provider="deterministic",
            model="live-raw-memory-test",
            dimensions=len(embedding),
            cache_namespace="live-raw-memory-test",
            tokenizer_estimate_method="unit-test",
        )

    async def embed_texts(self, texts, *, input_kind: str = "document"):
        return [list(self._embedding) for _text in texts]


class _StaticChunkEmbedder:
    def __init__(self, embedding: list[float]) -> None:
        self._embedding = embedding

    async def embed_chunks(self, chunks):
        return [list(self._embedding) for _chunk in chunks]


async def _drop_surreal_namespace(namespace: str) -> None:
    from surrealdb import AsyncSurreal

    client = AsyncSurreal(_live_surreal_url())
    try:
        username = _surreal_username()
        password = _surreal_password()
        if username and password:
            await client.signin({"username": username, "password": password})
        await client.query(f"REMOVE NAMESPACE IF EXISTS {namespace};")
    finally:
        await client.close()


def _graph_namespace_for_group(group_id: str) -> str:
    return f"org_{group_id.replace('-', '').lower()}"


async def _assert_live_extracted_into_endpoints(
    content_client: SurrealContentClient,
    *,
    organization_id: str,
    live_graph_runtime: Callable[..., Awaitable[Any]],
) -> None:
    derived_from = normalize_records(
        await content_client.execute_query("SELECT * FROM derived_from;")
    )
    chunk_of = normalize_records(await content_client.execute_query("SELECT * FROM chunk_of;"))
    extracted_into = normalize_records(
        await content_client.execute_query("SELECT * FROM extracted_into;")
    )
    entity_anchors = normalize_records(
        await content_client.execute_query(
            """
            SELECT id AS record_id, uuid, organization_id
            FROM entity
            WHERE organization_id = $organization_id;
            """,
            organization_id=organization_id,
        )
    )
    chunks = normalize_records(
        await content_client.execute_query(
            """
            SELECT id AS record_id, uuid
            FROM document_chunks
            WHERE organization_id = $organization_id;
            """,
            organization_id=organization_id,
        )
    )
    assert len(derived_from) == 1
    assert len(chunk_of) == 1
    assert len(extracted_into) == 1
    assert len(entity_anchors) == 1
    assert len(chunks) == 1
    projected_entity_id = str(extracted_into[0]["entity_id"])
    graph_runtime = await live_graph_runtime(organization_id)
    projected_entity = await graph_runtime.entity_manager.get(projected_entity_id)
    assert projected_entity.id == projected_entity_id
    assert projected_entity.organization_id == organization_id
    assert entity_anchors[0]["uuid"] == projected_entity_id
    assert str(extracted_into[0]["in"]) == str(entity_anchors[0]["record_id"])
    assert str(extracted_into[0]["out"]) == str(chunks[0]["record_id"])


async def _assert_live_raw_ingestion_path(
    monkeypatch: pytest.MonkeyPatch,
    *,
    namespace: str,
) -> None:
    organization_id = str(uuid4())
    principal_id = "user-live"
    source_id = "source-live"
    content_client = SurrealContentClient(
        url=_live_surreal_url(),
        username=_surreal_username(),
        password=_surreal_password(),
        namespace=namespace,
        database="content",
    )
    try:
        await bootstrap_content_schema(content_client, reset=True)

        from sibyl.jobs import memory_extraction, raw_promotion
        from sibyl.persistence.surreal import content as app_content_service
        from sibyl_core.models.memory_extraction import (
            ExtractedMemoryEntity,
            MemoryBatchEntityExtractionResult,
            SourceMemoryExtraction,
        )
        from sibyl_core.services import surreal_content as content_service

        @asynccontextmanager
        async def live_content_session():
            session = SurrealContentClient(
                url=_live_surreal_url(),
                username=_surreal_username(),
                password=_surreal_password(),
                namespace=namespace,
                database="content",
            )
            try:
                yield session
            finally:
                await session.close()

        embedding = [1.0, *([0.0] * (EMBEDDING_DIM - 1))]

        async def raw_query_embedding(_query: str) -> list[float]:
            return list(embedding)

        class FakeExtractionProvider:
            async def extract_many(self, _prompts, *, max_concurrent: int):
                assert max_concurrent >= 1
                return [
                    MemoryBatchEntityExtractionResult(
                        sources=[
                            SourceMemoryExtraction(
                                source_id=memories[0].id,
                                entities=[
                                    ExtractedMemoryEntity(
                                        name="Sapphire Memory",
                                        entity_type="topic",
                                        summary="A live raw ingestion probe entity.",
                                        confidence=0.9,
                                        evidence="alpha imported capture",
                                    )
                                ],
                            )
                        ]
                    )
                ]

        async def live_graph_runtime(group_id: str, **_kwargs):
            return await get_surreal_graph_runtime(group_id, embedding_provider=None)

        monkeypatch.setattr(content_service, "surreal_content_client", live_content_session)
        monkeypatch.setattr(content_service, "_raw_memory_query_embedding", raw_query_embedding)
        monkeypatch.setattr(app_content_service, "surreal_content_client", live_content_session)
        monkeypatch.setattr(
            raw_promotion,
            "EmbeddingService",
            lambda: _StaticChunkEmbedder(embedding),
        )
        monkeypatch.setattr(raw_promotion, "get_entity_graph_runtime", live_graph_runtime)
        monkeypatch.setattr(raw_promotion.settings, "auto_extract_entities", False)
        monkeypatch.setattr(memory_extraction, "get_surreal_graph_runtime", live_graph_runtime)
        monkeypatch.setattr(
            memory_extraction,
            "memory_batch_entity_extractor",
            lambda **_kwargs: FakeExtractionProvider(),
        )
        memories = await remember_raw_memories(
            [
                RawMemoryWrite(
                    organization_id=organization_id,
                    principal_id=principal_id,
                    source_id=source_id,
                    raw_content="alpha imported capture without the query marker",
                    title="Live raw memory vector probe",
                    memory_scope=MemoryScope.ORGANIZATION,
                    metadata={"dedupe_key": "live-dedupe"},
                    capture_surface="source_import",
                )
            ],
            embedding_provider=_StaticEmbeddingProvider(embedding),
        )
        recalled = await recall_raw_memory(
            organization_id=organization_id,
            principal_id=principal_id,
            query="semantic-only",
            memory_scope=MemoryScope.ORGANIZATION,
            limit=1,
        )
        assert [memory.id for memory in recalled] == [memories[0].id]

        await content_client.execute_query(
            """
            CREATE source_imports CONTENT {
                uuid: 'import-live',
                organization_id: $organization_id,
                principal_id: $principal_id,
                adapter_name: 'live',
                raw_memory_ids: [$raw_memory_id],
                status: 'completed',
                created_at: time::now(),
                updated_at: time::now()
            };
            """,
            organization_id=organization_id,
            principal_id=principal_id,
            raw_memory_id=memories[0].id,
        )

        promotion = await raw_promotion.promote_raw_captures(
            {},
            organization_id,
            raw_memory_ids=[memories[0].id],
            limit=1,
        )
        assert promotion["promoted_count"] == 1
        assert promotion["content_lineage"]["derived_from"] == 1
        assert promotion["content_lineage"]["chunk_of"] == 1

        extraction = await memory_extraction.extract_memory_entities(
            {},
            [
                {
                    "id": memories[0].id,
                    "entity_type": "document",
                    "name": "Live raw memory vector probe",
                    "content": memories[0].raw_content,
                    "organization_id": organization_id,
                    "principal_id": principal_id,
                    "metadata": {
                        "document_id": memories[0].id,
                        "memory_scope": MemoryScope.ORGANIZATION.value,
                        "principal_id": principal_id,
                    },
                }
            ],
            organization_id,
            created_source_ids=[memories[0].id],
            max_entities_per_source=4,
            max_source_chars=2_000,
            max_concurrent=1,
            max_tokens=512,
        )
        assert extraction["linked_chunks"] == 1

        followup_lineage = await materialize_content_lineage(
            content_client,
            organization_id=organization_id,
            limit=10,
        )
        assert followup_lineage.extracted_into == 1

        await _assert_live_extracted_into_endpoints(
            content_client,
            organization_id=organization_id,
            live_graph_runtime=live_graph_runtime,
        )
    finally:
        with suppress(Exception):
            await close_graph_clients()
        with suppress(Exception):
            await _drop_surreal_namespace(_graph_namespace_for_group(organization_id))
        await content_client.close()


async def _assert_live_event_and_changefeed_primitives(
    client: DedicatedSurrealClient,
) -> None:
    await client.execute_query(
        """
        DEFINE TABLE live_event_source SCHEMALESS;
        DEFINE TABLE live_event_log SCHEMALESS;
        DEFINE EVENT OVERWRITE capture_event ON TABLE live_event_source
            WHEN $input.log_event = true
            THEN (
                CREATE live_event_log SET
                    source = $after.id,
                    event = $event,
                    value = $after.value
            );
        CREATE live_event_source:visible SET log_event = true, value = 'captured';
        CREATE live_event_source:hidden SET log_event = false, value = 'ignored';
        """
    )
    event_rows = normalize_records(
        await client.execute_query("SELECT event, value FROM live_event_log;")
    )
    assert event_rows == [{"event": "CREATE", "value": "captured"}]

    changefeed_result = await client.execute_query_raw(
        """
        DEFINE TABLE live_changefeed_source CHANGEFEED 1d;
        CREATE live_changefeed_source:first SET
            uuid = 'raw-live',
            organization_id = 'org-live',
            value = 'alpha';
        SHOW CHANGES FOR TABLE live_changefeed_source SINCE 0 LIMIT 10;
        """
    )
    changefeed_rows = [row for row in normalize_records(changefeed_result) if "changes" in row]
    assert any("live_changefeed_source" in str(row["changes"]) for row in changefeed_rows)

    from sibyl.jobs.raw_changefeed import RawCaptureChangeRef, _raw_capture_refs_for_org

    assert _raw_capture_refs_for_org(changefeed_rows, organization_id="org-live") == [
        RawCaptureChangeRef(raw_memory_id="raw-live", organization_id="org-live")
    ]


async def _assert_live_search_and_vector_primitives(
    client: DedicatedSurrealClient,
) -> None:
    await client.execute_query(
        """
        DEFINE ANALYZER live_text_analyzer
            TOKENIZERS blank, class
            FILTERS lowercase, ascii, snowball(english);
        DEFINE TABLE live_text_probe SCHEMALESS;
        DEFINE FIELD body ON live_text_probe TYPE string;
        DEFINE INDEX live_text_probe_body_ft ON live_text_probe FIELDS body
            FULLTEXT ANALYZER live_text_analyzer BM25 HIGHLIGHTS;
        CREATE live_text_probe:one SET body = 'sapphire memory glows';
        CREATE live_text_probe:two SET body = 'plain quartz';
        """
    )
    fulltext_rows = normalize_records(
        await client.execute_query(
            """
            SELECT body,
                   search::score(0) AS score,
                   search::highlight('<mark>', '</mark>', 0) AS highlight
            FROM live_text_probe
            WHERE body @0@ $search_query;
            """,
            search_query="sapphire",
        )
    )
    assert len(fulltext_rows) == 1
    assert fulltext_rows[0]["body"] == "sapphire memory glows"
    assert isinstance(fulltext_rows[0]["score"], (int, float))
    assert "<mark>sapphire</mark>" in fulltext_rows[0]["highlight"]

    await client.execute_query(
        """
        DEFINE TABLE live_vector_probe SCHEMALESS;
        DEFINE FIELD embedding ON live_vector_probe TYPE array<float, 4>;
        DEFINE INDEX live_vector_probe_embedding ON live_vector_probe FIELDS embedding
            HNSW DIMENSION 4 DIST COSINE TYPE F32 EFC 40 M 8;
        CREATE live_vector_probe:one SET label = 'one', embedding = [1.0, 0.0, 0.0, 0.0];
        CREATE live_vector_probe:two SET label = 'two', embedding = [0.0, 1.0, 0.0, 0.0];
        """
    )
    knn_query = """
        SELECT label, vector::distance::knn() AS dist
        FROM live_vector_probe
        WHERE embedding <|1, 40|> $query_embedding;
    """
    knn_rows = normalize_records(
        await client.execute_query(knn_query, query_embedding=[1.0, 0.0, 0.0, 0.0])
    )
    assert knn_rows[0]["label"] == "one"

    explain_rows = normalize_records(
        await client.execute_query(
            f"{knn_query.strip().removesuffix(';')} EXPLAIN FULL;",
            query_embedding=[1.0, 0.0, 0.0, 0.0],
        )
    )
    assert "live_vector_probe_embedding" in str(explain_rows)

    rrf_rows = normalize_records(
        await client.execute_query(
            "RETURN search::rrf($lists, $limit, $k);",
            lists=[
                [{"id": "alpha", "score": 1.0}, {"id": "beta", "score": 0.5}],
                [{"id": "beta", "score": 1.0}],
            ],
            limit=2,
            k=60.0,
        )
    )
    assert {str(row.get("uuid") or row.get("record_id")) for row in rrf_rows} == {
        "alpha",
        "beta",
    }


@pytest.mark.asyncio
async def test_live_surreal_server_round_trips_native_entity() -> None:
    group_id = str(uuid4())
    entity_id = f"nightly-{uuid4().hex}"
    client = SurrealGraphClient(
        group_id=group_id,
        url=_live_surreal_url(),
        username=_surreal_username(),
        password=_surreal_password(),
    )
    manager = EntityManager(client, group_id=group_id)

    test_failed = False
    try:
        await prepare_graph_schema(client)
        await manager.create_direct(
            Entity(
                id=entity_id,
                entity_type=EntityType.PATTERN,
                name="Nightly Surreal runtime",
                description="SurrealDB server smoke test",
                organization_id=group_id,
                metadata={"runtime": "surreal"},
            )
        )

        fetched = await manager.get(entity_id)

        assert fetched.id == entity_id
        assert fetched.organization_id == group_id
        assert fetched.metadata["runtime"] == "surreal"
    except Exception:
        test_failed = True
        raise
    finally:
        with suppress(Exception):
            await manager.delete(entity_id)
        await client.close()
        if test_failed:
            with suppress(Exception):
                await _drop_surreal_namespace(client.namespace)
        else:
            await _drop_surreal_namespace(client.namespace)


@pytest.mark.asyncio
async def test_live_surreal_server_executes_3x_ingestion_primitives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = f"ingestion_live_{uuid4().hex}"
    client = DedicatedSurrealClient(
        url=_live_surreal_url(),
        username=_surreal_username(),
        password=_surreal_password(),
        namespace=namespace,
        database="probe",
        client_kind="live_probe",
        pool_size=1,
    )

    test_failed = False
    try:
        await _assert_live_event_and_changefeed_primitives(client)
        await _assert_live_search_and_vector_primitives(client)
        await _assert_live_raw_ingestion_path(monkeypatch, namespace=namespace)
    except Exception:
        test_failed = True
        raise
    finally:
        await client.close()
        if test_failed:
            with suppress(Exception):
                await _drop_surreal_namespace(namespace)
        else:
            await _drop_surreal_namespace(namespace)
