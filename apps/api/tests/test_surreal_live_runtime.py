from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from structlog.testing import capture_logs

from sibyl_core.backends.surreal import (
    SurrealAuthClient,
    SurrealContentClient,
    bootstrap_auth_schema,
    bootstrap_content_schema,
)
from sibyl_core.backends.surreal.auth_schema import AUTH_SCHEMA_NAME
from sibyl_core.backends.surreal.content_schema import (
    CONTENT_SCHEMA_CURRENT_VERSION,
    CONTENT_SCHEMA_NAME,
    EMBEDDING_DIM,
)
from sibyl_core.backends.surreal.dedicated_client import DedicatedSurrealClient
from sibyl_core.backends.surreal.schema import (
    EMBEDDING_DIM as GRAPH_EMBEDDING_DIM,
    bootstrap_schema,
)
from sibyl_core.backends.surreal.schema_version import (
    GRAPH_SCHEMA_CURRENT_VERSION,
    GRAPH_SCHEMA_NAME,
    get_schema_version,
    record_schema_version,
)
from sibyl_core.backends.surreal.url_schemes import is_embedded_surreal_url
from sibyl_core.embeddings.providers import EmbeddingMetadata
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services.graph import (
    CLEAR_MEMORY_SCOPE,
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
from tests.embedding_upgrade import (
    previous_release_stamp,
    upgrade_content_to_sweep,
    upgrade_graph_to_sweep,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("SIBYL_LIVE_SURREAL_TESTS") != "1",
    reason="live SurrealDB runtime smoke tests are disabled",
)


def _live_surreal_url() -> str:
    url = os.environ.get("SIBYL_SURREAL_URL", "")
    if not url or is_embedded_surreal_url(url):
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

    async def embed_chunks_with_metadata(self, chunks):
        return await self.embed_chunks(chunks), {
            "provider": "deterministic",
            "model": "live-chunk-test",
            "dimensions": len(self._embedding),
            "text_version": "document-chunk-v1",
        }


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


@asynccontextmanager
async def _live_graph_manager() -> AsyncIterator[tuple[SurrealGraphClient, EntityManager]]:
    group_id = str(uuid4())
    client = SurrealGraphClient(
        group_id=group_id,
        url=_live_surreal_url(),
        username=_surreal_username(),
        password=_surreal_password(),
    )
    manager = EntityManager(client, group_id=group_id)
    try:
        await prepare_graph_schema(client)
        yield client, manager
    finally:
        await client.close()
        with suppress(Exception):
            await _drop_surreal_namespace(client.namespace)


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
        from sibyl_core.services import (
            content_client as content_client_service,
            content_raw_recall,
        )

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

        monkeypatch.setattr(
            content_client_service,
            "surreal_content_client",
            live_content_session,
        )
        monkeypatch.setattr(
            content_raw_recall,
            "raw_memory_query_embedding",
            raw_query_embedding,
        )
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
async def test_live_surreal_server_batches_fenced_entity_embedding_backfill() -> None:
    async with _live_graph_manager() as (client, _manager):
        manager = EntityManager(
            client,
            group_id=client.group_id,
            embedding_provider=_StaticEmbeddingProvider([0.25] * GRAPH_EMBEDDING_DIM),
        )
        entities = tuple(
            Entity(
                id=f"live-batched-{index}-{uuid4().hex}",
                entity_type=EntityType.SESSION,
                name=f"Live batched entity {index}",
                content=f"Live content {index}",
                organization_id=client.group_id,
            )
            for index in range(2)
        )
        await manager.create_direct_bulk(entities)

        ready_ids = await manager.backfill_embeddings_if_current(entities)
        stored = [await manager.get(entity.id) for entity in entities]

        changed = entities[1].model_copy(update={"content": "Changed live content"})
        await manager.create_direct_bulk((changed,))
        stale_ids = await manager.backfill_embeddings_if_current((entities[1],))

        assert ready_ids == [entity.id for entity in entities]
        assert all(entity.embedding for entity in stored)
        assert stale_ids == []


@pytest.mark.asyncio
async def test_live_surreal_server_merges_attributes_on_partial_rewrite() -> None:
    async with _live_graph_manager() as (client, manager):
        await manager.create_direct(
            Entity(
                id="decision_attribute_merge",
                entity_type=EntityType.DECISION,
                name="Merged decision",
                organization_id=client.group_id,
                metadata={"original_key": "preserved", "shared_key": "old"},
            )
        )
        await manager.create_direct(
            Entity(
                id="decision_attribute_merge",
                entity_type=EntityType.DECISION,
                name="Merged decision",
                organization_id=client.group_id,
                metadata={"shared_key": "new", "added_key": "added"},
            )
        )

        fetched = await manager.get("decision_attribute_merge")

        assert fetched.metadata["original_key"] == "preserved"
        assert fetched.metadata["shared_key"] == "new"
        assert fetched.metadata["added_key"] == "added"


@pytest.mark.asyncio
async def test_live_surreal_server_clears_promoted_scope_with_sentinel() -> None:
    async with _live_graph_manager() as (client, manager):
        await manager.create_direct(
            Entity(
                id="note_sentinel_clear",
                entity_type=EntityType.NOTE,
                name="Scoped note",
                organization_id=client.group_id,
                metadata={"memory_scope": "private", "principal_id": "owner-1"},
            )
        )
        await manager.create_direct(
            Entity(
                id="note_sentinel_clear",
                entity_type=EntityType.NOTE,
                name="Scoped note",
                organization_id=client.group_id,
                metadata={"memory_scope": CLEAR_MEMORY_SCOPE},
            )
        )

        fetched = await manager.get("note_sentinel_clear")
        rows = normalize_records(
            await client.execute_query(
                """
                SELECT memory_scope, attributes.memory_scope AS attribute_scope
                FROM entity
                WHERE group_id = $group_id AND uuid = 'note_sentinel_clear'
                LIMIT 1;
                """,
                group_id=client.group_id,
            )
        )

        assert fetched.metadata.get("memory_scope") is None
        assert fetched.metadata["principal_id"] == "owner-1"
        assert not rows[0].get("memory_scope")
        assert not rows[0].get("attribute_scope")


@pytest.mark.asyncio
async def test_live_surreal_server_folds_legacy_snapshot_before_clear() -> None:
    async with _live_graph_manager() as (client, manager):
        await manager.create_direct(
            Entity(
                id="note_snapshot_fold",
                entity_type=EntityType.NOTE,
                name="Legacy snapshot note",
                organization_id=client.group_id,
                metadata={"withdrawn": "old", "current": "new"},
            )
        )
        await client.execute_query(
            """
            UPDATE entity SET
                attributes.metadata = $snapshot,
                attributes.withdrawn = NONE,
                attributes.snapshot_only = NONE
            WHERE group_id = $group_id AND uuid = 'note_snapshot_fold';
            """,
            group_id=client.group_id,
            snapshot=json.dumps(
                {
                    "withdrawn": "old",
                    "snapshot_only": "preserved",
                    "current": "stale",
                }
            ),
        )
        await manager.create_direct(
            Entity(
                id="note_snapshot_fold",
                entity_type=EntityType.NOTE,
                name="Legacy snapshot note",
                organization_id=client.group_id,
                metadata={"withdrawn": None},
            )
        )

        fetched = await manager.get("note_snapshot_fold")
        rows = normalize_records(
            await client.execute_query(
                """
                SELECT attributes
                FROM entity
                WHERE group_id = $group_id AND uuid = 'note_snapshot_fold'
                LIMIT 1;
                """,
                group_id=client.group_id,
            )
        )

        assert "withdrawn" not in fetched.metadata
        assert fetched.metadata["snapshot_only"] == "preserved"
        assert fetched.metadata["current"] == "new"
        assert "metadata" not in rows[0]["attributes"]


@pytest.mark.asyncio
async def test_live_surreal_server_rewrites_same_uuid_in_place() -> None:
    async with _live_graph_manager() as (client, manager):

        def row(name: str, content: str) -> Entity:
            return Entity(
                id="procedure_same_uuid",
                entity_type=EntityType.PROCEDURE,
                name=name,
                content=content,
                organization_id=client.group_id,
            )

        first_ids = await manager.create_direct_bulk([row("First name", "first body")])
        second_ids = await manager.create_direct_bulk([row("Second name", "second body")])
        rows = normalize_records(
            await client.execute_query(
                """
                SELECT uuid, name, content, revision
                FROM entity
                WHERE group_id = $group_id AND uuid = 'procedure_same_uuid';
                """,
                group_id=client.group_id,
            )
        )

        assert first_ids == ["procedure_same_uuid"]
        assert second_ids == ["procedure_same_uuid"]
        assert rows == [
            {
                "uuid": "procedure_same_uuid",
                "name": "Second name",
                "content": "second body",
                "revision": 2,
            }
        ]


@pytest.mark.asyncio
async def test_live_surreal_server_repairs_partial_graph_required_fields() -> None:
    group_id = str(uuid4())
    client = SurrealGraphClient(
        group_id=group_id,
        url=_live_surreal_url(),
        username=_surreal_username(),
        password=_surreal_password(),
    )

    test_failed = False
    try:
        await bootstrap_schema(client)
        await client.execute_query(
            """
            CREATE entity:legacy_required_fields SET
                uuid = 'legacy_required_fields',
                name = 'Legacy Required Fields',
                entity_type = 'pattern',
                labels = [],
                attributes = {
                    retrieval_count: 7,
                    citation_count: 5,
                    misled_count: 3,
                },
                group_id = $group_id,
                created_at = time::now();
            DEFINE FIELD OVERWRITE revision ON entity TYPE option<int> DEFAULT 1;
            DEFINE FIELD OVERWRITE retrieval_count ON entity TYPE option<int> DEFAULT 0;
            DEFINE FIELD OVERWRITE citation_count ON entity TYPE option<int> DEFAULT 0;
            DEFINE FIELD OVERWRITE misled_count ON entity TYPE option<int> DEFAULT 0;
            UPDATE entity:legacy_required_fields SET
                revision = NONE,
                retrieval_count = NONE,
                citation_count = NONE,
                misled_count = NONE;
            DEFINE FIELD OVERWRITE updated_at ON entity TYPE option<string>;
            UPDATE entity:legacy_required_fields SET
                updated_at = '2026-07-10T17:37:01Z';
            DEFINE FIELD OVERWRITE revision ON entity TYPE int DEFAULT 1 ASSERT $value >= 1;
            DEFINE FIELD OVERWRITE retrieval_count ON entity TYPE int DEFAULT 0;
            DEFINE FIELD OVERWRITE citation_count ON entity TYPE int DEFAULT 0;
            DEFINE FIELD OVERWRITE misled_count ON entity TYPE int DEFAULT 0;
            """,
            group_id=client.group_id,
        )
        await record_schema_version(
            client.execute_query,
            version=14,
            migrations=(),
            name=GRAPH_SCHEMA_NAME,
        )
        staged_rows = normalize_records(
            await client.execute_query(
                """
                SELECT updated_at,
                    type::is_string(updated_at) AS updated_at_is_string
                FROM entity:legacy_required_fields;
                """
            )
        )
        assert staged_rows == [
            {
                "updated_at": "2026-07-10T17:37:01Z",
                "updated_at_is_string": True,
            }
        ]

        await bootstrap_schema(client)

        rows = normalize_records(
            await client.execute_query(
                """
                SELECT revision, retrieval_count, citation_count, misled_count, updated_at
                FROM entity:legacy_required_fields;
                """
            )
        )
        assert rows == [
            {
                "revision": 1,
                "retrieval_count": 7,
                "citation_count": 5,
                "misled_count": 3,
                "updated_at": datetime(2026, 7, 10, 17, 37, 1, tzinfo=UTC),
            }
        ]
    except Exception:
        test_failed = True
        raise
    finally:
        await client.close()
        if test_failed:
            with suppress(Exception):
                await _drop_surreal_namespace(client.namespace)
        else:
            await _drop_surreal_namespace(client.namespace)


@pytest.mark.asyncio
@pytest.mark.parametrize("starting_point", ["fresh", "stamped_v20", "reset"])
async def test_live_graph_bootstrap_logs_no_failed_queries(starting_point: str) -> None:
    # A 3.x server fails any statement against a missing table, and the
    # dedicated client logs every failure as a warning even when the caller
    # expects and handles it, so bootstrap must never touch absent tables.
    client = SurrealGraphClient(
        group_id=str(uuid4()),
        url=_live_surreal_url(),
        username=_surreal_username(),
        password=_surreal_password(),
    )

    test_failed = False
    try:
        if starting_point != "fresh":
            await bootstrap_schema(client)
        if starting_point == "stamped_v20":
            await record_schema_version(
                client.execute_query,
                version=20,
                migrations=(),
                name=GRAPH_SCHEMA_NAME,
            )

        with capture_logs() as entries:
            await bootstrap_schema(client, reset=starting_point == "reset")

        failed_queries = [
            (entry.get("statement"), entry.get("tables"), entry.get("query_origin"))
            for entry in entries
            if entry["event"] == "surreal_query_failed"
        ]
        assert failed_queries == []
        assert await get_schema_version(client.execute_query) == GRAPH_SCHEMA_CURRENT_VERSION
    except Exception:
        test_failed = True
        raise
    finally:
        await client.close()
        if test_failed:
            with suppress(Exception):
                await _drop_surreal_namespace(client.namespace)
        else:
            await _drop_surreal_namespace(client.namespace)


@pytest.mark.asyncio
async def test_live_graph_bootstrap_cleans_orphan_edges_after_losing_schema_version() -> None:
    # A namespace that lost its schema_version record still has relation
    # tables, so bootstrap must clean their orphans before enforcing endpoints.
    client = SurrealGraphClient(
        group_id=str(uuid4()),
        url=_live_surreal_url(),
        username=_surreal_username(),
        password=_surreal_password(),
    )

    test_failed = False
    try:
        await bootstrap_schema(client)
        await client.execute_query(
            """
            CREATE entity:kept SET uuid = 'kept', name = 'Kept', entity_type = 'pattern',
                labels = [], attributes = {}, group_id = $group_id;
            CREATE entity:other SET uuid = 'other', name = 'Other', entity_type = 'pattern',
                labels = [], attributes = {}, group_id = $group_id;
            RELATE entity:kept->relates_to:valid->entity:other SET
                uuid = 'valid', name = 'RELATED_TO', fact = 'valid edge', group_id = $group_id;
            DEFINE TABLE OVERWRITE relates_to SCHEMAFULL TYPE RELATION IN entity OUT entity;
            DEFINE TABLE OVERWRITE mentions SCHEMAFULL TYPE RELATION IN episode OUT entity;
            RELATE entity:kept->relates_to:orphan->entity:ghost SET
                uuid = 'orphan', name = 'RELATED_TO', fact = 'orphan edge', group_id = $group_id;
            RELATE episode:ghost->mentions:orphan->entity:kept SET
                uuid = 'orphan', group_id = $group_id;
            DELETE schema_version:graph;
            """,
            group_id=client.group_id,
        )
        assert sorted(await client.execute_query("SELECT VALUE uuid FROM relates_to;")) == [
            "orphan",
            "valid",
        ]
        assert await client.execute_query("SELECT VALUE uuid FROM mentions;") == ["orphan"]

        with capture_logs() as entries:
            await bootstrap_schema(client)

        failed_queries = [
            (entry.get("statement"), entry.get("tables"), entry.get("query_origin"))
            for entry in entries
            if entry["event"] == "surreal_query_failed"
        ]
        assert failed_queries == []
        assert await client.execute_query("SELECT VALUE uuid FROM relates_to;") == ["valid"]
        assert await client.execute_query("SELECT VALUE uuid FROM mentions;") == []
        assert await get_schema_version(client.execute_query) == GRAPH_SCHEMA_CURRENT_VERSION
    except Exception:
        test_failed = True
        raise
    finally:
        await client.close()
        if test_failed:
            with suppress(Exception):
                await _drop_surreal_namespace(client.namespace)
        else:
            await _drop_surreal_namespace(client.namespace)


@pytest.mark.asyncio
async def test_live_surreal_server_resumes_partial_content_migration() -> None:
    namespace = f"content_migration_live_{uuid4().hex}"
    client = SurrealContentClient(
        url=_live_surreal_url(),
        username=_surreal_username(),
        password=_surreal_password(),
        namespace=namespace,
        database="content",
    )

    test_failed = False
    try:
        await bootstrap_content_schema(client, reset=True)
        await client.execute_query(
            """
            DEFINE FIELD include_postgres ON backup_settings TYPE option<bool>;
            DEFINE FIELD include_postgres ON backups TYPE option<bool>;
            CREATE backup_settings:legacy SET
                uuid = 'legacy',
                organization_id = 'legacy-org',
                include_postgres = true;
            CREATE backups:legacy SET
                uuid = 'legacy',
                organization_id = 'legacy-org',
                backup_id = 'legacy',
                include_postgres = true;
            REMOVE FIELD include_postgres ON backup_settings;
            REMOVE FIELD include_postgres ON backups;
            REMOVE FIELD revision ON raw_captures;
            REMOVE FIELD retrieval_count ON raw_captures;
            REMOVE FIELD citation_count ON raw_captures;
            REMOVE FIELD misled_count ON raw_captures;
            CREATE raw_captures:legacy SET
                uuid = 'legacy',
                organization_id = 'legacy-org',
                metadata = {
                    retrieval_count: 7,
                    citation_count: 5,
                    misled_count: 3
                };
            """
        )
        await record_schema_version(
            client.execute_query,
            version=13,
            migrations=(),
            name=CONTENT_SCHEMA_NAME,
        )

        await bootstrap_content_schema(client)

        assert (
            await get_schema_version(client.execute_query, name=CONTENT_SCHEMA_NAME)
            == CONTENT_SCHEMA_CURRENT_VERSION
        )
        raw_rows = normalize_records(
            await client.execute_query(
                """
                SELECT revision, retrieval_count, citation_count, misled_count
                FROM raw_captures:legacy;
                """
            )
        )
        assert raw_rows == [
            {
                "revision": 1,
                "retrieval_count": 7,
                "citation_count": 5,
                "misled_count": 3,
            }
        ]
        backup_rows = normalize_records(await client.execute_query("SELECT * FROM backups:legacy;"))
        backup_setting_rows = normalize_records(
            await client.execute_query("SELECT * FROM backup_settings:legacy;")
        )
        assert "include_postgres" not in backup_rows[0]
        assert "include_postgres" not in backup_setting_rows[0]
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


@pytest.mark.asyncio
async def test_live_auth_replay_identity_migration_preserves_data_lineage() -> None:
    namespace = f"replay_identity_{uuid4().hex}"
    client = SurrealAuthClient(
        url=_live_surreal_url(),
        username=_surreal_username(),
        password=_surreal_password(),
        namespace=namespace,
        database="auth",
    )
    replacement = SurrealAuthClient(
        url=_live_surreal_url(),
        username=_surreal_username(),
        password=_surreal_password(),
        namespace=namespace,
        database="replacement",
    )
    try:
        await bootstrap_auth_schema(client)
        assert await get_schema_version(client.execute_query, name=AUTH_SCHEMA_NAME) >= 7
        first = normalize_records(
            await client.execute_query("SELECT instance_id FROM server_identity:singleton;")
        )
        assert len(first) == 1
        assert UUID(str(first[0]["instance_id"]))
        await bootstrap_auth_schema(client)
        assert (
            normalize_records(
                await client.execute_query("SELECT instance_id FROM server_identity:singleton;")
            )
            == first
        )
        await bootstrap_auth_schema(replacement)
        other = normalize_records(
            await replacement.execute_query("SELECT instance_id FROM server_identity:singleton;")
        )
        assert len(other) == 1
        assert other != first
    finally:
        await client.close()
        await replacement.close()
        await _drop_surreal_namespace(namespace)


@pytest.mark.asyncio
async def test_live_eval_admission_returns_memory_and_rolls_back_failed_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from sibyl_core.services import content_client, eval_admission
    from sibyl_core.tasks.eval_receipts import TaskAssignment, sign_outcome

    namespace = f"eval_admission_live_{uuid4().hex}"
    client = SurrealContentClient(
        url=_live_surreal_url(),
        username=_surreal_username(),
        password=_surreal_password(),
        namespace=namespace,
        database="content",
    )

    @asynccontextmanager
    async def live_content_session():
        yield client

    monkeypatch.setattr(content_client, "surreal_content_client", live_content_session)
    key = Ed25519PrivateKey.generate()
    assignment = TaskAssignment(
        organization_id=str(uuid4()),
        owner_principal_id="live-owner",
        experiment_id="live-experiment",
        experiment_revision="1",
        task_id="live-task",
        task_revision="1",
        task_sha256="a" * 64,
        family_id="live-family",
        split="learning",
        arm_id="raw",
        checkpoint=0,
        seed=1,
        memory_pack_sha256="b" * 64,
        controller_policy_sha256="c" * 64,
        checker_sha256="d" * 64,
        oracle_sha256="e" * 64,
        evaluator_sha256="f" * 64,
        runtime_sha256="0" * 64,
        image="sha256:" + "1" * 64,
        attempt_id=uuid4().hex,
    )

    async def admit(task: TaskAssignment) -> eval_admission.EvalAdmissionResult:
        outcome = {
            "schema_version": "sibyl-json-cli-outcome-v1",
            "attempt_id": task.attempt_id,
            "snapshot_sha256": "2" * 64,
            "status": "passed",
            "passed": True,
            **{
                field: getattr(task, field)
                for field in (
                    "checker_sha256",
                    "oracle_sha256",
                    "evaluator_sha256",
                    "runtime_sha256",
                    "image",
                )
            },
        }
        evidence = {
            "outcome_bytes": json.dumps(outcome).encode(),
            "transcript_bytes": b'{"action":"check"}\n',
            "episode_bytes": b"Live admission preserves these exact episode bytes.",
        }
        receipt = sign_outcome(
            assignment=task, issuer_id="live-issuer", private_key=key, **evidence
        )
        return await eval_admission.admit_eval_outcome(
            organization_id=task.organization_id,
            experiment_id=task.experiment_id,
            attempt_id=task.attempt_id,
            principal_id=task.owner_principal_id,
            issuer_id="live-issuer",
            trusted_public_key=key.public_key(),
            expected_controller_policy_sha256=task.controller_policy_sha256,
            receipt_bytes=receipt,
            **evidence,
        )

    async def stored_state():
        return (
            await client.execute_query("SELECT * FROM eval_attempts ORDER BY uuid;"),
            await client.execute_query("SELECT * FROM raw_captures ORDER BY uuid;"),
        )

    try:
        await bootstrap_content_schema(client, reset=True)
        assert (
            await eval_admission.register_eval_assignment(
                organization_id=assignment.organization_id, assignment=assignment
            )
            == assignment
        )
        first = await admit(assignment)
        assert first.memory.raw_content == "Live admission preserves these exact episode bytes."
        assert first.memory.revision == 1
        before_retry = await stored_state()
        replay = await admit(assignment)
        assert replay.memory.id == first.memory.id
        assert replay.receipt_sha256 == first.receipt_sha256
        assert await stored_state() == before_retry
        assert len(before_retry[0]) == len(before_retry[1]) == 1

        second = assignment.model_copy(update={"attempt_id": uuid4().hex})
        await eval_admission.register_eval_assignment(
            organization_id=second.organization_id, assignment=second
        )
        before_failure = await stored_state()
        with monkeypatch.context() as patch:
            marker = "LET $memory ="
            assert eval_admission._ADMIT.count(marker) == 1
            patch.setattr(
                eval_admission,
                "_ADMIT",
                eval_admission._ADMIT.replace(
                    marker, "THROW 'eval admission conflict: injected after writes';\n" + marker
                ),
            )
            with pytest.raises(eval_admission.EvalAdmissionConflict, match="injected after writes"):
                await admit(second)
        assert await stored_state() == before_failure

        third = assignment.model_copy(update={"attempt_id": uuid4().hex})
        with monkeypatch.context() as patch:
            marker = "RETURN (SELECT * FROM eval_attempts"
            assert eval_admission._REGISTER.count(marker) == 1
            patch.setattr(
                eval_admission,
                "_REGISTER",
                eval_admission._REGISTER.replace(
                    marker,
                    "THROW 'eval admission conflict: injected after registration';\n" + marker,
                ),
            )
            with pytest.raises(
                eval_admission.EvalAdmissionConflict, match="injected after registration"
            ):
                await eval_admission.register_eval_assignment(
                    organization_id=third.organization_id, assignment=third
                )
        assert await stored_state() == before_failure
    finally:
        await client.close()
        await _drop_surreal_namespace(namespace)


@pytest.mark.parametrize("operation", ["read", "mutation", "takeover"])
async def test_live_surreal_schema_renewal_and_takeover(monkeypatch, operation):
    import asyncio

    from sibyl_core.backends.surreal import schema
    from sibyl_core.backends.surreal.schema_ownership import (
        SchemaOwnershipLost,
        try_acquire_schema_ownership,
    )

    group_id = str(uuid4())
    clients = [
        SurrealGraphClient(
            group_id=group_id,
            url=_live_surreal_url(),
            username=_surreal_username(),
            password=_surreal_password(),
            pool_size=1,
        )
        for _ in range(2)
    ]
    client, observer = clients
    successor = None

    async def short_claim(execute, **kwargs):
        return await try_acquire_schema_ownership(execute, lease_seconds=1, **kwargs)

    async def stopped_renewal(ownership):
        await asyncio.Event().wait()

    try:
        await prepare_graph_schema(client)
        await client.execute_query("DEFINE TABLE schema_renewal_probe SCHEMALESS;")
        monkeypatch.setattr(schema, "try_acquire_schema_ownership", short_claim)
        if operation == "takeover":
            monkeypatch.setattr(schema, "_renew_schema_ownership", stopped_renewal)
        async with schema._graph_schema_ownership(client) as ownership:
            work = asyncio.create_task(
                ownership.read("SLEEP 3s; RETURN 1;")
                if operation == "read"
                else ownership.mutate("SLEEP 3s; CREATE schema_renewal_probe:body SET value = 1;")
            )
            try:
                await asyncio.sleep(1.3)
                assert not work.done()
                successor = await try_acquire_schema_ownership(
                    observer.execute_query, initialize=False
                )
                assert not work.done(), "the contender must claim while the body is in flight"
                if operation == "takeover":
                    assert successor is not None
                    with pytest.raises(SchemaOwnershipLost):
                        await work
                    assert await observer.execute_query("SELECT * FROM schema_renewal_probe;") == []
                else:
                    assert successor is None
                    await work
                    assert await observer.execute_query(
                        "SELECT VALUE deadline > time::now() FROM schema_lease:graph;"
                    ) == [True]
                    if operation == "mutation":
                        assert await observer.execute_query(
                            "SELECT VALUE value FROM schema_renewal_probe:body;"
                        ) == [1]
            finally:
                if not work.done():
                    work.cancel()
                await asyncio.gather(work, return_exceptions=True)
        if successor is None:
            successor = await try_acquire_schema_ownership(observer.execute_query, initialize=False)
        assert successor is not None
        await successor.mutate("CREATE schema_renewal_probe:successor SET value = 2;")
        assert await observer.execute_query(
            "SELECT VALUE value FROM schema_renewal_probe:successor;"
        ) == [2]
    finally:
        if successor is not None:
            await successor.release()
        await asyncio.gather(*(connection.close() for connection in clients))
        with suppress(Exception):
            await _drop_surreal_namespace(client.namespace)


async def test_live_raw_capture_membership_precedes_vector_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sibyl_core.services import content_client, content_raw_recall
    from sibyl_core.services.surreal_content import recall_raw_memory_with_sources

    namespace = f"capture_membership_live_{uuid4().hex}"
    organization_id = str(uuid4())
    client = SurrealContentClient(
        url=_live_surreal_url(),
        username=_surreal_username(),
        password=_surreal_password(),
        namespace=namespace,
        database="content",
    )
    near = [1.0, *([0.0] * (EMBEDDING_DIM - 1))]
    far = [0.0, 1.0, *([0.0] * (EMBEDDING_DIM - 2))]

    @asynccontextmanager
    async def session():
        yield client

    async def query_embedding(_query: str) -> list[float]:
        return list(near)

    monkeypatch.setattr(content_client, "surreal_content_client", session)
    monkeypatch.setattr(content_raw_recall, "raw_memory_query_embedding", query_embedding)
    try:
        await bootstrap_content_schema(client, reset=True)
        retained = await remember_raw_memories(
            [
                RawMemoryWrite(
                    organization_id=organization_id,
                    principal_id="owner",
                    source_id="retained",
                    raw_content="An observation",
                )
            ],
            embedding_provider=_StaticEmbeddingProvider(far),
        )
        await remember_raw_memories(
            [
                RawMemoryWrite(
                    organization_id=organization_id,
                    principal_id="owner",
                    source_id=f"nearer-nonmember-{index}",
                    raw_content="An observation",
                )
                for index in range(64)
            ],
            embedding_provider=_StaticEmbeddingProvider(near),
        )
        for capture_ids in (None, [retained[0].id]):
            result = await recall_raw_memory_with_sources(
                organization_id=organization_id,
                principal_id="owner",
                query="unmatched-query-marker",
                capture_ids=capture_ids,
                limit=1,
            )
            lanes = {source.source: source for source in result.sources}
            assert lanes["raw_fulltext"].failure is None
            assert lanes["raw_fulltext"].candidates == ()
            assert lanes["raw_vector"].failure is None
            assert len(result.memories) == 1
            assert (result.memories[0].id == retained[0].id) is (capture_ids is not None)
    finally:
        await client.close()
        await _drop_surreal_namespace(namespace)


# --- Embedding model sweep -------------------------------------------------
#
# A deployment that switches embedding providers keeps its old vectors until
# the lifecycle sweep replaces them. These run against a real 3.x server
# because the sweep's walk, fenced write, lease and legacy-stamping
# statements are exactly the shapes the embedded 2.x engine is lenient with.


def _previous_graph_stamp() -> dict[str, object]:
    return previous_release_stamp(
        EmbeddingMetadata(
            provider="openai",
            model="text-embedding-3-small",
            dimensions=GRAPH_EMBEDDING_DIM,
            cache_namespace="graph",
            tokenizer_estimate_method="provider-default",
            input_kind_sensitive=False,
        ).to_dict()
    )


class _SweepTargetProvider:
    """A deterministic provider standing in for the newly configured model."""

    def __init__(self, dimensions: int, *, model: str = "live-sweep-target") -> None:
        from sibyl_core.embeddings.providers import DeterministicEmbeddingProvider

        self._inner = DeterministicEmbeddingProvider(
            EmbeddingMetadata(
                provider="deterministic",
                model=model,
                dimensions=dimensions,
                cache_namespace="graph",
                tokenizer_estimate_method="unit-test",
            )
        )
        self.metadata = self._inner.metadata
        self.texts: list[str] = []
        self.crash_on_call: int | None = None
        self.calls = 0

    async def embed_texts(self, texts, *, input_kind: str = "document"):
        if input_kind == "document":
            self.calls += 1
            if self.crash_on_call == self.calls:
                raise asyncio.CancelledError
            self.texts.extend(texts)
        return await self._inner.embed_texts(texts, input_kind=input_kind)


async def _seed_live_graph(manager: EntityManager, client: SurrealGraphClient, *, stamp):
    from sibyl_core.models.entities import Relationship, RelationshipType
    from sibyl_core.services.graph import RelationshipManager

    vector = [1.0, *([0.0] * (GRAPH_EMBEDDING_DIM - 1))]
    entities = [
        Entity(
            id=f"stamped-{index:02d}",
            entity_type=EntityType.TOPIC,
            name=f"Live sweep entity {index}",
            description=f"Entity number {index}",
            organization_id=client.group_id,
            metadata={"embedding_metadata": stamp} if stamp is not None else {},
            embedding=list(vector),
        )
        for index in range(10)
    ] + [
        Entity(
            id=f"legacy-{index}",
            entity_type=EntityType.TOPIC,
            name=f"Live legacy entity {index}",
            organization_id=client.group_id,
            embedding=list(vector),
        )
        for index in range(3)
    ]
    await manager.create_direct_bulk(entities)
    relationships = RelationshipManager(client, group_id=client.group_id)
    await relationships.create_direct_bulk(
        [
            Relationship(
                id=f"edge-{index}",
                relationship_type=RelationshipType.RELATED_TO,
                source_id=f"stamped-{index:02d}",
                target_id=f"stamped-{index + 1:02d}",
                metadata={
                    "fact": f"entity {index} relates to entity {index + 1}",
                    "fact_embedding": list(vector),
                    **({"embedding_metadata": stamp} if stamp is not None else {}),
                },
            )
            for index in range(4)
        ]
    )


async def _live_vector_stamps(client: SurrealGraphClient, table: str) -> dict[str, dict]:
    vector = "name_embedding" if table == "entity" else "fact_embedding"
    rows = normalize_records(
        await client.execute_query(
            f"SELECT uuid, {vector} AS vector, attributes.embedding_metadata AS stamp, "  # noqa: S608
            f"revision FROM {table} WHERE group_id = $group_id;",
            group_id=client.group_id,
        )
    )
    return {str(row["uuid"]): row for row in rows}


@pytest.mark.asyncio
async def test_live_embedding_sweep_replaces_another_models_vectors_across_a_crash() -> None:
    from sibyl_core.services.embedding_sweep import (
        SWEEP_BUSY,
        SWEEP_COMPLETED,
        read_embedding_sweep_state,
    )
    from sibyl_core.services.graph import RelationshipManager
    from sibyl_core.services.graph_embedding_sweep import (
        GRAPH_EMBEDDING_PLANE,
        sweep_graph_embeddings,
    )
    from sibyl_core.services.graph_runtime import GraphRuntime

    async with _live_graph_manager() as (client, manager):
        await _seed_live_graph(manager, client, stamp=_previous_graph_stamp())
        await upgrade_graph_to_sweep(client)
        target = _SweepTargetProvider(GRAPH_EMBEDDING_DIM)
        searcher = EntityManager(client, group_id=client.group_id, embedding_provider=target)
        runtime = GraphRuntime(
            client=client,
            entity_manager=searcher,
            relationship_manager=RelationshipManager(client, group_id=client.group_id),
        )
        before = await _live_vector_stamps(client, "entity")

        # Nothing from the old model is ever scored against the new one.
        assert (
            await searcher._vector_search(query="Live sweep entity", entity_types=None, limit=5)
            == []
        )

        target.crash_on_call = 2
        with pytest.raises(asyncio.CancelledError):
            await sweep_graph_embeddings(
                runtime, embedding_provider=target, page_size=4, batch_size=4, concurrency=1
            )
        # Make the interrupted pass look like a killed worker: its lease is
        # still recorded, owned by nobody alive, and has run out.
        key_state = await read_embedding_sweep_state(
            GRAPH_EMBEDDING_PLANE, client.group_id, client.execute_query
        )
        assert key_state["legacy_decision"] == "reembed"
        assert key_state["legacy_basis"] == "prior_stamps_differ"
        from sibyl_core.services.embedding_sweep import embedding_state_key

        await client.execute_query(
            "UPDATE type::record($key) SET lease_owner = 'dead-worker', "
            "lease_until = time::now() + 5m;",
            key=embedding_state_key(client.group_id, GRAPH_EMBEDDING_PLANE),
        )
        target.crash_on_call = None
        held = await sweep_graph_embeddings(runtime, embedding_provider=target)
        assert held.status == SWEEP_BUSY
        await client.execute_query(
            "UPDATE type::record($key) SET lease_until = time::now() - 1s;",
            key=embedding_state_key(client.group_id, GRAPH_EMBEDDING_PLANE),
        )

        resumed = await sweep_graph_embeddings(
            runtime, embedding_provider=target, page_size=4, batch_size=4, concurrency=1
        )

        assert resumed.status == SWEEP_COMPLETED
        assert resumed.pending == 0
        stamp = target.metadata.to_dict()
        entities = await _live_vector_stamps(client, "entity")
        edges = await _live_vector_stamps(client, "relates_to")
        assert len(entities) == 13
        assert len(edges) == 4
        for row_id, row in entities.items():
            assert row["stamp"] == stamp, row_id
            assert row["vector"] != before[row_id]["vector"]
            assert row["revision"] == before[row_id]["revision"]
        assert all(edge["stamp"] == stamp for edge in edges.values())
        # Every stale row reached the provider exactly once across both passes.
        assert len(target.texts) == len(entities) + len(edges)
        assert (
            len(
                await searcher._vector_search(query="Live sweep entity", entity_types=None, limit=5)
            )
            > 0
        )


@pytest.mark.asyncio
async def test_live_a_sweep_that_loses_its_lease_mid_batch_writes_nothing() -> None:
    from sibyl_core.services.embedding_sweep import (
        SWEEP_LEASE_LOST,
        embedding_state_key,
        read_embedding_sweep_state,
    )
    from sibyl_core.services.graph import RelationshipManager
    from sibyl_core.services.graph_embedding_sweep import (
        GRAPH_EMBEDDING_PLANE,
        sweep_graph_embeddings,
    )
    from sibyl_core.services.graph_runtime import GraphRuntime

    async with _live_graph_manager() as (client, manager):
        await _seed_live_graph(manager, client, stamp=_previous_graph_stamp())
        await upgrade_graph_to_sweep(client)
        target = _SweepTargetProvider(GRAPH_EMBEDDING_DIM)
        runtime = GraphRuntime(
            client=client,
            entity_manager=EntityManager(client, group_id=client.group_id),
            relationship_manager=RelationshipManager(client, group_id=client.group_id),
        )
        embed = target.embed_texts

        async def taken_over(texts, *, input_kind="document"):
            # While this call is in flight another process takes the plane.
            await client.execute_query(
                "UPDATE type::record($key) SET lease_owner = 'another-process', "
                "lease_until = time::now() + 5m;",
                key=embedding_state_key(client.group_id, GRAPH_EMBEDDING_PLANE),
            )
            return await embed(texts, input_kind=input_kind)

        target.embed_texts = taken_over  # type: ignore[method-assign]
        before = await _live_vector_stamps(client, "entity")

        result = await sweep_graph_embeddings(
            runtime, embedding_provider=target, page_size=2, batch_size=2, concurrency=1
        )

        assert result.status == SWEEP_LEASE_LOST
        assert result.recovered == 0
        after = await _live_vector_stamps(client, "entity")
        stamp = target.metadata.to_dict()
        for row_id, row in after.items():
            assert row["vector"] == before[row_id]["vector"], row_id
            assert row["stamp"] != stamp, row_id
        state = await read_embedding_sweep_state(
            GRAPH_EMBEDDING_PLANE, client.group_id, client.execute_query
        )
        assert state["lease_owner"] == "another-process"


@pytest.mark.asyncio
async def test_live_embedding_sweep_adopts_vectors_on_a_plain_upgrade() -> None:
    from sibyl_core.services.embedding_sweep import SWEEP_COMPLETED, SWEEP_CURRENT
    from sibyl_core.services.graph import RelationshipManager
    from sibyl_core.services.graph_embedding_sweep import sweep_graph_embeddings
    from sibyl_core.services.graph_runtime import GraphRuntime

    async with _live_graph_manager() as (client, manager):
        unchanged = _SweepTargetProvider(GRAPH_EMBEDDING_DIM, model="unchanged")
        await _seed_live_graph(manager, client, stamp=unchanged.metadata.to_dict())
        # Unstamped relationship vectors from before stamping.
        await client.execute_query(
            "UPDATE relates_to SET attributes.embedding_metadata = NONE WHERE uuid = 'edge-0';"
        )
        await upgrade_graph_to_sweep(client)
        runtime = GraphRuntime(
            client=client,
            entity_manager=manager,
            relationship_manager=RelationshipManager(client, group_id=client.group_id),
        )
        before = await _live_vector_stamps(client, "entity")

        result = await sweep_graph_embeddings(runtime, embedding_provider=unchanged)

        assert result.status == SWEEP_COMPLETED
        assert (result.adopted, result.recovered) == (4, 0)
        assert unchanged.texts == []
        stamp = unchanged.metadata.to_dict()
        entities = await _live_vector_stamps(client, "entity")
        assert all(row["stamp"] == stamp for row in entities.values())
        assert all(entities[key]["vector"] == before[key]["vector"] for key in entities)
        assert (await _live_vector_stamps(client, "relates_to"))["edge-0"]["stamp"] == stamp
        repeated = await sweep_graph_embeddings(runtime, embedding_provider=unchanged)
        assert repeated.status == SWEEP_CURRENT


@pytest.mark.asyncio
async def test_live_graph_restore_sends_unstamped_vectors_through_the_sweep(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from sibyl_core.embeddings.provenance import UNVERIFIED_EMBEDDING_PROVIDER
    from sibyl_core.services.embedding_sweep import SWEEP_COMPLETED
    from sibyl_core.services.graph import RelationshipManager
    from sibyl_core.services.graph_embedding_sweep import sweep_graph_embeddings
    from sibyl_core.services.graph_runtime import GraphRuntime
    from sibyl_core.tools.admin import create_backup, restore_backup

    async with _live_graph_manager() as (client, manager):
        previous = _previous_graph_stamp()
        await _seed_live_graph(manager, client, stamp=previous)
        source = GraphRuntime(
            client=client,
            entity_manager=manager,
            relationship_manager=RelationshipManager(client, group_id=client.group_id),
        )
        destination_client = SurrealGraphClient(
            group_id=client.group_id,
            url=_live_surreal_url(),
            username=_surreal_username(),
            password=_surreal_password(),
            database=f"restore_{uuid4().hex}",
        )
        try:
            # A second database under the same organization namespace; the
            # per-organization schema cache already marked the namespace ready.
            await bootstrap_schema(destination_client)
            destination = GraphRuntime(
                client=destination_client,
                entity_manager=EntityManager(destination_client, group_id=client.group_id),
                relationship_manager=RelationshipManager(
                    destination_client, group_id=client.group_id
                ),
            )
            monkeypatch.setattr(
                "sibyl_core.tools.admin.get_graph_runtime", AsyncMock(return_value=source)
            )
            backup = await create_backup(organization_id=client.group_id)
            assert backup.success, backup.message
            monkeypatch.setattr(
                "sibyl_core.tools.admin.get_graph_runtime", AsyncMock(return_value=destination)
            )
            restored = await restore_backup(
                backup.backup_data, organization_id=client.group_id, clean=True
            )
            assert restored.success, restored.errors

            imported = await _live_vector_stamps(destination_client, "entity")
            assert imported["stamped-00"]["stamp"] == previous
            assert imported["legacy-0"]["stamp"]["provider"] == UNVERIFIED_EMBEDDING_PROVIDER

            target = _SweepTargetProvider(GRAPH_EMBEDDING_DIM)
            swept = await sweep_graph_embeddings(destination, embedding_provider=target)

            assert swept.status == SWEEP_COMPLETED
            assert swept.recovered == 17
            stamp = target.metadata.to_dict()
            for table in ("entity", "relates_to"):
                rows = await _live_vector_stamps(destination_client, table)
                assert all(row["stamp"] == stamp for row in rows.values()), table
        finally:
            await destination_client.close()


@pytest.mark.asyncio
async def test_live_chunk_sweep_reads_raw_evidence_and_filters_the_chunk_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sibyl_core.embeddings.provenance import document_chunk_embedding_metadata
    from sibyl_core.services import content_client
    from sibyl_core.services.content_documents import search_document_chunks
    from sibyl_core.services.document_embedding_sweep import (
        decide_document_chunk_legacy_vectors,
        sweep_document_chunk_embeddings,
    )
    from sibyl_core.services.embedding_sweep import SWEEP_COMPLETED

    namespace = f"chunk_sweep_live_{uuid4().hex}"
    organization_id = str(uuid4())
    source_id = str(uuid4())
    client = SurrealContentClient(
        url=_live_surreal_url(),
        username=_surreal_username(),
        password=_surreal_password(),
        namespace=namespace,
        database="content",
    )

    @asynccontextmanager
    async def session():
        yield client

    monkeypatch.setattr(content_client, "surreal_content_client", session)
    # Rows the previous release wrote, so their stamps carry no stamp version.
    previous = previous_release_stamp(
        document_chunk_embedding_metadata(
            provider="openai", model="text-embedding-3-small", dimensions=EMBEDDING_DIM
        )
    )
    target = document_chunk_embedding_metadata(
        provider="deterministic", model="live-chunk-target", dimensions=EMBEDDING_DIM
    )
    old_vector = [1.0, *([0.0] * (EMBEDDING_DIM - 1))]
    new_vector = [0.0, 1.0, *([0.0] * (EMBEDDING_DIM - 2))]
    embedded: list[dict[str, object]] = []

    async def embed_chunks(rows):
        embedded.extend(rows)
        return [list(new_vector) for _ in rows], dict(target)

    try:
        await bootstrap_content_schema(client, reset=True)
        await content_client.select_many(
            client,
            "CREATE crawl_sources CONTENT $record RETURN NONE;",
            record={
                "uuid": source_id,
                "organization_id": organization_id,
                "name": "Live guide",
                "url": "https://docs.example.test",
            },
        )
        await content_client.select_many(
            client,
            "CREATE raw_captures CONTENT $record RETURN NONE;",
            record={
                "uuid": str(uuid4()),
                "organization_id": organization_id,
                "principal_id": "owner",
                "source_id": "evidence",
                "raw_content": "captured under the previous model",
                "embedding": list(old_vector),
                "metadata": {"embedding_metadata": {**previous, "cache_namespace": "raw-memory"}},
            },
        )
        for index in range(7):
            await content_client.select_many(
                client,
                "CREATE crawled_documents CONTENT $record RETURN NONE;",
                record={
                    "uuid": f"doc-{index}",
                    "organization_id": organization_id,
                    "source_id": source_id,
                    "url": f"https://docs.example.test/{index}",
                    "title": f"Page {index}",
                    "content": "body",
                },
            )
            chunk = {
                "uuid": f"chunk-{index}",
                "organization_id": organization_id,
                "source_id": source_id,
                "document_id": f"doc-{index}",
                "content": f"Live chunk body {index}",
                "embedding": list(old_vector),
            }
            if index < 5:
                chunk["embedding_metadata"] = previous
            await content_client.select_many(
                client, "CREATE document_chunks CONTENT $record RETURN NONE;", record=chunk
            )

        await upgrade_content_to_sweep(client)
        stale, _ = await search_document_chunks(
            organization_id=organization_id,
            query_text="",
            query_embedding=list(old_vector),
            limit=10,
            embedding_metadata=target,
        )
        unfiltered, _ = await search_document_chunks(
            organization_id=organization_id,
            query_text="",
            query_embedding=list(old_vector),
            limit=10,
        )
        assert stale == []
        assert len(unfiltered) == 7

        await decide_document_chunk_legacy_vectors(
            organization_id, stamp=target, embed_chunks=embed_chunks
        )
        result = await sweep_document_chunk_embeddings(
            organization_id,
            stamp=target,
            embed_chunks=embed_chunks,
            page_size=3,
            batch_size=3,
            concurrency=2,
        )

        assert result.status == SWEEP_COMPLETED
        assert result.legacy_decision == "reembed"
        assert (result.recovered, result.adopted, result.pending) == (7, 0, 0)
        assert len(embedded) == 7
        rows = await content_client.select_many(
            client,
            "SELECT uuid, embedding, embedding_metadata FROM document_chunks "
            "WHERE organization_id = $organization_id;",
            organization_id=organization_id,
        )
        assert all(row["embedding_metadata"] == target for row in rows)
        assert all(row["embedding"][:2] == [0.0, 1.0] for row in rows)
        current, _ = await search_document_chunks(
            organization_id=organization_id,
            query_text="",
            query_embedding=list(new_vector),
            limit=10,
            embedding_metadata=target,
        )
        assert len(current) == 7
    finally:
        await client.close()
        await _drop_surreal_namespace(namespace)


@pytest.mark.asyncio
async def test_live_chunk_sweep_refuses_a_size_the_stored_field_cannot_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sibyl_core.embeddings.provenance import document_chunk_embedding_metadata
    from sibyl_core.services import content_client, document_embedding_sweep as chunk_module
    from sibyl_core.services.embedding_sweep import SWEEP_SKIPPED_DIMENSION_MISMATCH

    namespace = f"chunk_dims_live_{uuid4().hex}"
    organization_id = str(uuid4())
    client = SurrealContentClient(
        url=_live_surreal_url(),
        username=_surreal_username(),
        password=_surreal_password(),
        namespace=namespace,
        database="content",
    )
    wider = document_chunk_embedding_metadata(
        provider="deterministic", model="wider", dimensions=EMBEDDING_DIM * 2
    )
    embedded: list[object] = []

    async def embed_chunks(rows):
        embedded.extend(rows)
        return [[0.0] * (EMBEDDING_DIM * 2) for _ in rows], dict(wider)

    try:
        await bootstrap_content_schema(client, reset=True)
        await content_client.select_many(
            client,
            "CREATE document_chunks CONTENT $record RETURN NONE;",
            record={
                "uuid": "chunk",
                "organization_id": organization_id,
                "document_id": "doc",
                "content": "body",
                "embedding": [1.0, *([0.0] * (EMBEDDING_DIM - 1))],
                "embedding_metadata": {**wider, "dimensions": EMBEDDING_DIM, "model": "old"},
            },
        )
        # The operator raised SIBYL_EMBEDDING_DIMENSIONS and restarted; the
        # stored field kept its original size.
        monkeypatch.setattr(chunk_module, "EMBEDDING_DIM", EMBEDDING_DIM * 2)

        result = await chunk_module.sweep_document_chunk_embeddings(
            organization_id, stamp=wider, embed_chunks=embed_chunks, client=client
        )

        assert result.status == SWEEP_SKIPPED_DIMENSION_MISMATCH
        assert result.schema_dimensions == EMBEDDING_DIM
        assert embedded == []
    finally:
        await client.close()
        await _drop_surreal_namespace(namespace)


# The upgrade and the provider switch land in one restart. Each organization is
# written the way the previous release left it, both schemas are rewound to
# the release before the sweep, and the restart replays the real migrations;
# lifecycle repair then runs exactly as the scheduler would.

_PREVIOUS_CHUNK_STAMP = {
    "provider": "openai",
    "model": "text-embedding-3-small",
    "dimensions": EMBEDDING_DIM,
    "text_version": "document-chunk-v1",
}
_TARGET_CHUNK_STAMP = {**_PREVIOUS_CHUNK_STAMP, "provider": "bedrock", "model": "cohere.embed-v4:0"}
_OLD_GRAPH_VECTOR = [1.0, *([0.0] * (GRAPH_EMBEDDING_DIM - 1))]
_OLD_CHUNK_VECTOR = [1.0, *([0.0] * (EMBEDDING_DIM - 1))]
_NEW_CHUNK_VECTOR = [0.0, 1.0, *([0.0] * (EMBEDDING_DIM - 2))]


def _live_content_client(namespace: str) -> SurrealContentClient:
    return SurrealContentClient(
        url=_live_surreal_url(),
        username=_surreal_username(),
        password=_surreal_password(),
        namespace=namespace,
        database="content",
    )


def _live_graph_client(group_id: str) -> SurrealGraphClient:
    return SurrealGraphClient(
        group_id=group_id,
        url=_live_surreal_url(),
        username=_surreal_username(),
        password=_surreal_password(),
    )


async def _seed_previous_release_org(
    content: SurrealContentClient,
    client: SurrealGraphClient,
    *,
    graph_stamped: bool,
    raw_captures: int,
    chunks: int = 4,
) -> None:
    """Rows as the previous release wrote them: graph stamps only on some, chunks bare."""
    from sibyl_core.services import content_client

    manager = EntityManager(client, group_id=client.group_id)
    await _seed_live_graph(
        manager, client, stamp=_previous_graph_stamp() if graph_stamped else None
    )
    for index in range(raw_captures):
        await content_client.select_many(
            content,
            "CREATE raw_captures CONTENT $record RETURN NONE;",
            record={
                "uuid": str(uuid4()),
                "organization_id": client.group_id,
                "principal_id": "owner",
                "source_id": str(uuid4()),
                "raw_content": f"captured under the previous model {index}",
                "embedding": list(_OLD_CHUNK_VECTOR),
                "metadata": {
                    "embedding_metadata": {
                        **_PREVIOUS_CHUNK_STAMP,
                        "cache_namespace": "raw-memory",
                        "text_version": "raw-capture-v1",
                    }
                },
            },
        )
    for index in range(chunks):
        await content_client.select_many(
            content,
            "CREATE document_chunks CONTENT $record RETURN NONE;",
            record={
                "uuid": f"{client.group_id}-chunk-{index}",
                "organization_id": client.group_id,
                "source_id": "source",
                "document_id": f"{client.group_id}-doc-{index}",
                "content": f"Chunk body {index}",
                "embedding": list(_OLD_CHUNK_VECTOR),
            },
        )


def _run_lifecycle_against(
    monkeypatch: pytest.MonkeyPatch,
    *,
    content: SurrealContentClient,
    clients: dict[str, SurrealGraphClient],
    graph_target: _SweepTargetProvider,
    chunk_stamp: dict[str, object],
    embedded_chunks: list[object],
    broken: frozenset[str] = frozenset(),
    raw_provider: Any = None,
    record_models: bool = False,
) -> None:
    """Point lifecycle repair at these stores and providers; stub the unrelated repairs.

    ``broken`` organizations fail to open their graph runtime. With
    ``raw_provider`` the real raw capture repair runs with that provider, and
    with ``record_models`` the real deployment model recorder runs.
    """
    from unittest.mock import AsyncMock

    from sibyl.jobs import lifecycle_repair
    from sibyl_core.projection.repair import LifecycleRepairResult
    from sibyl_core.services import content_client, graph_embedding_sweep
    from sibyl_core.services.graph import RelationshipManager
    from sibyl_core.services.graph_runtime import GraphRuntime

    @asynccontextmanager
    async def session():
        yield content

    @asynccontextmanager
    async def background(group_id):
        if group_id in broken:
            raise ConnectionError(f"graph namespace for {group_id} is unreachable")
        client = clients[group_id]
        await prepare_graph_schema(client)
        yield GraphRuntime(
            client=client,
            entity_manager=EntityManager(client, group_id=group_id),
            relationship_manager=RelationshipManager(client, group_id=group_id),
        )

    async def embed_chunks(rows):
        embedded_chunks.extend(rows)
        return [list(_NEW_CHUNK_VECTOR) for _row in rows], dict(chunk_stamp)

    monkeypatch.setattr(content_client, "surreal_content_client", session)
    monkeypatch.setattr(lifecycle_repair, "background_graph_runtime", background)
    monkeypatch.setattr(lifecycle_repair, "list_org_ids", AsyncMock(return_value=list(clients)))
    monkeypatch.setattr(lifecycle_repair, "configured_embedding_provider", lambda: graph_target)
    monkeypatch.setattr(
        graph_embedding_sweep, "configured_embedding_provider", lambda: graph_target
    )
    monkeypatch.setattr(
        lifecycle_repair,
        "document_chunk_sweep_inputs",
        AsyncMock(return_value=(dict(chunk_stamp), True, embed_chunks)),
    )
    if record_models:
        from sibyl.jobs import embedding_sweep as sweep_jobs

        class _Service:
            async def chunk_embedding_metadata(self):
                return dict(chunk_stamp), True

        monkeypatch.setattr(sweep_jobs, "configured_embedding_provider", lambda: graph_target)
        monkeypatch.setattr(sweep_jobs, "EmbeddingService", _Service)
    else:
        monkeypatch.setattr(lifecycle_repair, "record_configured_embedding_models", AsyncMock())
    stubbed = [
        "repair_graph_lifecycle",
        "repair_promoted_embeddings",
        "repair_raw_source_lifecycle",
    ]
    if raw_provider is None:
        stubbed.append("repair_raw_capture_embeddings")
    else:
        monkeypatch.setattr(
            "sibyl_core.services.content_models.configured_raw_memory_embedding_provider",
            lambda: raw_provider,
        )
    for name in stubbed:
        monkeypatch.setattr(lifecycle_repair, name, AsyncMock(return_value=LifecycleRepairResult()))


async def _restart_on_new_models(
    content: SurrealContentClient,
    clients: dict[str, SurrealGraphClient],
    *,
    graph_stamp: dict[str, object],
    chunk_stamp: dict[str, object],
) -> None:
    from sibyl_core.services import content_client
    from sibyl_core.services.embedding_evidence import record_deployment_models

    await upgrade_content_to_sweep(content)

    async def execute(query, **params):
        return await content_client.select_many(content, query, **params)

    # The first pass of this release already runs the new models, so the
    # deployment record has no older model to compare against.
    await record_deployment_models(execute, graph=graph_stamp, content=chunk_stamp)
    for client in clients.values():
        await upgrade_graph_to_sweep(client)


async def _rewind_content_schema(content: SurrealContentClient) -> None:
    """Leave the content schema where the previous release left it."""
    from sibyl_core.backends.surreal.schema_version import schema_version_record_id

    await content.execute_query("REMOVE TABLE IF EXISTS embedding_states;")
    await content.execute_query("REMOVE TABLE IF EXISTS embedding_deployment;")
    await content.execute_query(
        "UPDATE type::record($record) SET version = 46;",
        record=schema_version_record_id(CONTENT_SCHEMA_NAME),
    )


async def _rewind_graph_schema(client: SurrealGraphClient) -> None:
    from sibyl_core.backends.surreal.schema_version import schema_version_record_id
    from sibyl_core.services.graph_client import mark_graph_schema_dirty

    await client.execute_query("REMOVE TABLE IF EXISTS embedding_states;")
    await client.execute_query(
        "UPDATE type::record($record) SET version = 30;",
        record=schema_version_record_id(GRAPH_SCHEMA_NAME),
    )
    mark_graph_schema_dirty(client.group_id)


async def _plane_states(
    content: SurrealContentClient, client: SurrealGraphClient
) -> tuple[dict, dict]:
    from sibyl_core.services import content_client
    from sibyl_core.services.embedding_sweep import read_embedding_sweep_state

    graph = await read_embedding_sweep_state("graph", client.group_id, client.execute_query)
    chunks = await read_embedding_sweep_state(
        "document_chunks",
        client.group_id,
        lambda query, **params: content_client.select_many(content, query, **params),
    )
    return graph, chunks


async def _assert_org_fully_reembedded(
    content: SurrealContentClient,
    client: SurrealGraphClient,
    *,
    graph_stamp: dict[str, object],
    chunk_stamp: dict[str, object],
) -> None:
    from sibyl_core.services import content_client

    for table in ("entity", "relates_to"):
        rows = await _live_vector_stamps(client, table)
        assert rows, table
        for row_id, row in rows.items():
            assert row["stamp"] == graph_stamp, (client.group_id, table, row_id)
            assert row["vector"] != _OLD_GRAPH_VECTOR, (client.group_id, table, row_id)
    chunks = await content_client.select_many(
        content,
        "SELECT uuid, embedding, embedding_metadata FROM document_chunks "
        "WHERE organization_id = $organization_id;",
        organization_id=client.group_id,
    )
    assert chunks
    for chunk in chunks:
        assert chunk["embedding_metadata"] == chunk_stamp, chunk["uuid"]
        assert chunk["embedding"][:2] == [0.0, 1.0], chunk["uuid"]


@pytest.mark.asyncio
async def test_live_same_restart_switch_repairs_every_organization_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sibyl.jobs import lifecycle_repair
    from sibyl_core.services import content_client

    namespace = f"sweep_restart_live_{uuid4().hex}"
    content = _live_content_client(namespace)
    # A: stamped graph, raw captures. B: unstamped graph, raw captures.
    # C: unstamped graph, none. D: stamped graph, none. J: C plus writes made
    # after the restart and before the first pass.
    shapes = {
        "A": {"graph_stamped": True, "raw_captures": 2},
        "B": {"graph_stamped": False, "raw_captures": 2},
        "C": {"graph_stamped": False, "raw_captures": 0},
        "D": {"graph_stamped": True, "raw_captures": 0},
        "J": {"graph_stamped": False, "raw_captures": 0},
    }
    orgs = {shape: str(uuid4()) for shape in shapes}
    clients = {orgs[shape]: _live_graph_client(orgs[shape]) for shape in shapes}
    graph_target = _SweepTargetProvider(GRAPH_EMBEDDING_DIM, model="live-restart-target")
    graph_stamp = graph_target.metadata.to_dict()
    chunk_stamp = _TARGET_CHUNK_STAMP
    embedded_chunks: list[object] = []
    try:
        await bootstrap_content_schema(content, reset=True)
        for shape, spec in shapes.items():
            client = clients[orgs[shape]]
            await prepare_graph_schema(client)
            await _seed_previous_release_org(content, client, **spec)

        await _restart_on_new_models(
            content, clients, graph_stamp=graph_stamp, chunk_stamp=chunk_stamp
        )
        late = clients[orgs["J"]]
        await EntityManager(late, group_id=late.group_id).create_direct_bulk(
            [
                Entity(
                    id="written-after-restart",
                    entity_type=EntityType.TOPIC,
                    name="Written after restart",
                    organization_id=late.group_id,
                    metadata={"embedding_metadata": graph_stamp},
                    embedding=(await graph_target.embed_texts(["late"]))[0],
                )
            ]
        )
        await content_client.select_many(
            content,
            "CREATE raw_captures CONTENT $record RETURN NONE;",
            record={
                "uuid": str(uuid4()),
                "organization_id": late.group_id,
                "principal_id": "owner",
                "source_id": str(uuid4()),
                "raw_content": "captured after the restart",
                "metadata": {"embedding_metadata": {**chunk_stamp, "cache_namespace": "raw"}},
            },
        )
        graph_target.texts.clear()
        graph_target.calls = 0
        # Before any pass, a query in the new space scores nothing old.
        searcher = EntityManager(
            clients[orgs["C"]], group_id=orgs["C"], embedding_provider=graph_target
        )
        assert (
            await searcher._vector_search(query="Live sweep entity", entity_types=None, limit=5)
            == []
        )

        _run_lifecycle_against(
            monkeypatch,
            content=content,
            clients=clients,
            graph_target=graph_target,
            chunk_stamp=chunk_stamp,
            embedded_chunks=embedded_chunks,
        )
        for _tick in range(3):
            summary = await lifecycle_repair.repair_lifecycle_all_orgs({})
            assert summary["failed_organizations"] == 0, summary
        assert summary["embedding_unverified"] == 0

        # A graph with no stamps of its own learns of the switch from another
        # organization's graph or from the raw captures; which arrives first
        # names the basis, never the decision.
        unstamped = {"deployment_stamps_differ", "other_plane_switched"}
        expected = {
            "A": ({"prior_stamps_differ"}, "prior_stamps_differ"),
            "B": (unstamped, "prior_stamps_differ"),
            "C": (unstamped, "prior_stamps_differ"),
            "D": ({"prior_stamps_differ"}, "prior_stamps_differ"),
            "J": (unstamped, "prior_stamps_differ"),
        }
        for shape, (graph_bases, chunk_basis) in expected.items():
            client = clients[orgs[shape]]
            graph_state, chunk_state = await _plane_states(content, client)
            assert graph_state["legacy_decision"] == "reembed", shape
            assert graph_state["legacy_basis"] in graph_bases, shape
            assert chunk_state["legacy_decision"] == "reembed", shape
            assert chunk_state["legacy_basis"] == chunk_basis, shape
            assert not graph_state.get("legacy_warning"), shape
            assert not chunk_state.get("legacy_warning"), shape
            await _assert_org_fully_reembedded(
                content, client, graph_stamp=graph_stamp, chunk_stamp=chunk_stamp
            )
        # Every stale graph row (13 entities and 4 relationships per
        # organization) and chunk (4 per organization) was embedded once.
        assert len(graph_target.texts) == 17 * len(shapes)
        assert len(embedded_chunks) == 4 * len(shapes)
        found = await searcher._vector_search(query="Live sweep entity", entity_types=None, limit=5)
        assert found
        stamps = await _live_vector_stamps(clients[orgs["C"]], "entity")
        assert all(stamps[entity.id]["stamp"] == graph_stamp for entity, _score in found)
    finally:
        await content.close()
        await _drop_surreal_namespace(namespace)
        for client in clients.values():
            await client.close()
            with suppress(Exception):
                await _drop_surreal_namespace(client.namespace)


async def _plant_stamps_without_vectors(
    content: SurrealContentClient, client: SurrealGraphClient, *, graph_stamp, chunk_stamp
) -> None:
    """Stamps a client could supply on rows that never had a vector."""
    from sibyl_core.services import content_client

    await content_client.select_many(
        content,
        "CREATE raw_captures CONTENT $record RETURN NONE;",
        record={
            "uuid": str(uuid4()),
            "organization_id": client.group_id,
            "principal_id": "tenant",
            "source_id": "planted",
            "raw_content": "planted",
            "metadata": {"embedding_metadata": {**chunk_stamp, "cache_namespace": "raw"}},
        },
    )
    await EntityManager(client, group_id=client.group_id).create_direct_bulk(
        [
            Entity(
                id="planted",
                entity_type=EntityType.TOPIC,
                name="Planted",
                organization_id=client.group_id,
                metadata={"embedding_metadata": graph_stamp},
            )
        ]
    )


@asynccontextmanager
async def _previous_release_deployment(
    shapes: dict[str, bool],
    *,
    graph_stamp: dict[str, object],
    chunk_stamp: dict[str, object],
    planted: bool = False,
    upgrade_content: bool = True,
) -> AsyncIterator[tuple[SurrealContentClient, dict[str, SurrealGraphClient]]]:
    """A deployment with no real raw captures, restarted onto new models.

    Shapes map to whether the graph is stamped. ``planted`` adds stamps with no
    vector, naming the new models; ``upgrade_content=False`` leaves the
    content schema at the release before the sweep, as a worker would find it.
    """
    namespace = f"sweep_noraw_live_{uuid4().hex}"
    content = _live_content_client(namespace)
    orgs = {shape: str(uuid4()) for shape in shapes}
    by_shape = {shape: _live_graph_client(orgs[shape]) for shape in shapes}
    try:
        await bootstrap_content_schema(content, reset=True)
        for shape, stamped in shapes.items():
            await prepare_graph_schema(by_shape[shape])
            await _seed_previous_release_org(
                content, by_shape[shape], graph_stamped=stamped, raw_captures=0
            )
            if planted:
                await _plant_stamps_without_vectors(
                    content, by_shape[shape], graph_stamp=graph_stamp, chunk_stamp=chunk_stamp
                )
        if upgrade_content:
            await _restart_on_new_models(
                content,
                {client.group_id: client for client in by_shape.values()},
                graph_stamp=graph_stamp,
                chunk_stamp=chunk_stamp,
            )
        else:
            await _rewind_content_schema(content)
            for client in by_shape.values():
                await _rewind_graph_schema(client)
        yield content, by_shape
    finally:
        await content.close()
        with suppress(Exception):
            await _drop_surreal_namespace(namespace)
        for client in by_shape.values():
            await client.close()
            with suppress(Exception):
                await _drop_surreal_namespace(client.namespace)


@pytest.mark.asyncio
async def test_live_one_organizations_graph_speaks_for_the_rest_without_raw_captures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sibyl.jobs import lifecycle_repair

    graph_target = _SweepTargetProvider(GRAPH_EMBEDDING_DIM, model="live-restart-target")
    graph_stamp = graph_target.metadata.to_dict()
    # C2 is listed before D2, so its verdict must wait for D2's graph.
    async with _previous_release_deployment(
        {"C2": False, "D2": True}, graph_stamp=graph_stamp, chunk_stamp=_TARGET_CHUNK_STAMP
    ) as (content, by_shape):
        _run_lifecycle_against(
            monkeypatch,
            content=content,
            clients={client.group_id: client for client in by_shape.values()},
            graph_target=graph_target,
            chunk_stamp=_TARGET_CHUNK_STAMP,
            embedded_chunks=[],
        )
        first = await lifecycle_repair.repair_lifecycle_all_orgs({})
        assert first["embedding_deferred"] == 2
        for _tick in range(2):
            await lifecycle_repair.repair_lifecycle_all_orgs({})

        c2_graph, c2_chunks = await _plane_states(content, by_shape["C2"])
        d2_graph, d2_chunks = await _plane_states(content, by_shape["D2"])
        assert c2_graph["legacy_basis"] == "deployment_stamps_differ"
        assert c2_chunks["legacy_basis"] == "other_plane_switched"
        assert d2_graph["legacy_basis"] == "prior_stamps_differ"
        assert d2_chunks["legacy_basis"] == "other_plane_switched"
        for client in by_shape.values():
            await _assert_org_fully_reembedded(
                content, client, graph_stamp=graph_stamp, chunk_stamp=_TARGET_CHUNK_STAMP
            )


@pytest.mark.asyncio
async def test_live_no_evidence_anywhere_warns_until_the_operator_reembeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sibyl.api.routes.admin import _embedding_plane_state
    from sibyl.jobs import lifecycle_repair
    from sibyl_core.services.document_embedding_sweep import (
        count_document_chunk_embeddings_for_reembed,
        mark_document_chunk_embeddings_for_reembed,
    )
    from sibyl_core.services.graph_embedding_sweep import (
        count_graph_embeddings_for_reembed,
        mark_graph_embeddings_for_reembed,
    )

    graph_target = _SweepTargetProvider(GRAPH_EMBEDDING_DIM, model="live-restart-target")
    graph_stamp = graph_target.metadata.to_dict()
    # Stamps naming the new models on rows without a vector must not pass
    # for evidence that the old vectors are current.
    async with _previous_release_deployment(
        {"C3": False}, graph_stamp=graph_stamp, chunk_stamp=_TARGET_CHUNK_STAMP, planted=True
    ) as (content, by_shape):
        client = by_shape["C3"]
        _run_lifecycle_against(
            monkeypatch,
            content=content,
            clients={client.group_id: client},
            graph_target=graph_target,
            chunk_stamp=_TARGET_CHUNK_STAMP,
            embedded_chunks=[],
        )
        await lifecycle_repair.repair_lifecycle_all_orgs({})
        adopted = await lifecycle_repair.repair_lifecycle_all_orgs({})

        assert adopted["embedding_unverified"] == 2
        for state in await _plane_states(content, client):
            assert state["legacy_basis"] == "no_prior_evidence"
            assert _embedding_plane_state(state) == "adopted_without_evidence"
        # 17 legacy rows plus the planted entity, which the sweep gave a
        # vector because its stamp had none beside it.
        assert await count_graph_embeddings_for_reembed(client) == 18
        assert (
            await count_document_chunk_embeddings_for_reembed(client.group_id, client=content) == 4
        )
        await mark_graph_embeddings_for_reembed(client)
        await mark_document_chunk_embeddings_for_reembed(client.group_id, client=content)
        repaired = await lifecycle_repair.repair_lifecycle_all_orgs({})

        assert repaired["embedding_unverified"] == 0
        await _assert_org_fully_reembedded(
            content, client, graph_stamp=graph_stamp, chunk_stamp=_TARGET_CHUNK_STAMP
        )


def _content_raw_provider(chunk_stamp: dict[str, object]) -> Any:
    """A raw capture embedder writing the same model the chunk plane is configured for."""
    from sibyl_core.embeddings.providers import DeterministicEmbeddingProvider

    return DeterministicEmbeddingProvider(
        EmbeddingMetadata(
            provider=str(chunk_stamp["provider"]),
            model=str(chunk_stamp["model"]),
            dimensions=EMBEDDING_DIM,
            cache_namespace="raw-memory",
            tokenizer_estimate_method="provider-default",
        )
    )


@pytest.mark.asyncio
async def test_live_a_worker_that_ticks_before_the_content_upgrade_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sibyl.jobs import lifecycle_repair
    from sibyl_core.embeddings.provenance import EMBEDDING_STAMP_VERSION, STAMP_VERSION_FIELD
    from sibyl_core.services import content_client
    from sibyl_core.services.embedding_evidence import read_content_snapshot, read_graph_snapshot

    graph_target = _SweepTargetProvider(GRAPH_EMBEDDING_DIM, model="live-restart-target")
    graph_stamp = graph_target.metadata.to_dict()
    namespace = f"sweep_worker_first_live_{uuid4().hex}"
    content = _live_content_client(namespace)
    native, bare = str(uuid4()), str(uuid4())
    clients = {org: _live_graph_client(org) for org in (native, bare)}
    try:
        await bootstrap_content_schema(content, reset=True)
        await prepare_graph_schema(clients[native])
        await _seed_previous_release_org(
            content, clients[native], graph_stamped=True, raw_captures=10
        )
        await prepare_graph_schema(clients[bare])
        await _seed_previous_release_org(
            content, clients[bare], graph_stamped=False, raw_captures=0
        )
        await _rewind_content_schema(content)
        for client in clients.values():
            await _rewind_graph_schema(client)
        _run_lifecycle_against(
            monkeypatch,
            content=content,
            clients=clients,
            graph_target=graph_target,
            chunk_stamp=_TARGET_CHUNK_STAMP,
            embedded_chunks=[],
            raw_provider=_content_raw_provider(_TARGET_CHUNK_STAMP),
        )

        # The worker ticks before the API has migrated the content schema.
        waiting = await lifecycle_repair.repair_lifecycle_all_orgs({})

        assert waiting["embedding_schema_pending"] == 4
        assert waiting["failed_organizations"] == 0
        raw = await content_client.select_many(
            content, "SELECT metadata.embedding_metadata.model AS model FROM raw_captures;"
        )
        assert {row["model"] for row in raw} == {_PREVIOUS_CHUNK_STAMP["model"]}

        # New code writes in the new model before either migration runs. Its
        # stamps carry a stamp version, so neither snapshot counts them as
        # evidence of what the previous release wrote.
        await content_client.select_many(
            content,
            "CREATE raw_captures CONTENT $record RETURN NONE;",
            record={
                "uuid": str(uuid4()),
                "organization_id": native,
                "principal_id": "owner",
                "source_id": str(uuid4()),
                "raw_content": "captured by the new release before its migration",
                "embedding": list(_NEW_CHUNK_VECTOR),
                "metadata": {
                    "embedding_metadata": {
                        **_TARGET_CHUNK_STAMP,
                        "cache_namespace": "raw-memory",
                        "text_version": "raw-capture-v1",
                        STAMP_VERSION_FIELD: EMBEDDING_STAMP_VERSION,
                    }
                },
            },
        )
        await clients[native].execute_query(
            "INSERT INTO entity $rows;",
            rows=[
                {
                    "uuid": "written-by-new-release",
                    "group_id": native,
                    "name": "Written by the new release",
                    "entity_type": "topic",
                    "name_embedding": [0.0, 1.0, *([0.0] * (GRAPH_EMBEDDING_DIM - 2))],
                    "attributes": {"embedding_metadata": graph_stamp},
                    "created_at": datetime.now(UTC),
                }
            ],
        )

        # The API starts and migrates; the snapshot sees the old raw stamps.
        await bootstrap_content_schema(content)

        async def execute(query, **params):
            return await content_client.select_many(content, query, **params)

        assert {stamp["model"] for stamp in await read_content_snapshot(execute)} == {
            _PREVIOUS_CHUNK_STAMP["model"]
        }
        for _tick in range(3):
            summary = await lifecycle_repair.repair_lifecycle_all_orgs({})
            assert summary["failed_organizations"] == 0, summary
        graph_snapshot = await read_graph_snapshot(clients[native].execute_query, native)
        assert {stamp["model"] for stamp in graph_snapshot} == {"text-embedding-3-small"}
        for org in (native, bare):
            _graph, chunks = await _plane_states(content, clients[org])
            assert chunks["legacy_decision"] == "reembed", org
            assert chunks["legacy_basis"] == "prior_stamps_differ", org
            await _assert_org_fully_reembedded(
                content, clients[org], graph_stamp=graph_stamp, chunk_stamp=_TARGET_CHUNK_STAMP
            )
        raw = await content_client.select_many(
            content, "SELECT metadata.embedding_metadata.model AS model FROM raw_captures;"
        )
        assert {row["model"] for row in raw} == {_TARGET_CHUNK_STAMP["model"]}
    finally:
        await content.close()
        with suppress(Exception):
            await _drop_surreal_namespace(namespace)
        for client in clients.values():
            await client.close()
            with suppress(Exception):
                await _drop_surreal_namespace(client.namespace)


@pytest.mark.asyncio
async def test_live_a_broken_organization_holds_unproven_verdicts_only_for_the_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sibyl.api.routes.admin import get_embedding_sweep_status
    from sibyl.jobs import lifecycle_repair
    from sibyl_core.config import settings as core_settings

    graph_target = _SweepTargetProvider(GRAPH_EMBEDDING_DIM, model="live-restart-target")
    graph_stamp = graph_target.metadata.to_dict()
    monkeypatch.setattr(core_settings, "embedding_sweep_evidence_wait_seconds", 2.0)
    async with _previous_release_deployment(
        {"C4": False}, graph_stamp=graph_stamp, chunk_stamp=_TARGET_CHUNK_STAMP
    ) as (content, by_shape):
        waiting = by_shape["C4"]
        broken = str(uuid4())
        _run_lifecycle_against(
            monkeypatch,
            content=content,
            clients={waiting.group_id: waiting, broken: waiting},
            graph_target=graph_target,
            chunk_stamp=_TARGET_CHUNK_STAMP,
            embedded_chunks=[],
            broken=frozenset({broken}),
        )

        @asynccontextmanager
        async def status_session():
            yield content

        monkeypatch.setattr(
            "sibyl.persistence.surreal.content.surreal_content_client", status_session
        )

        first = await lifecycle_repair.repair_lifecycle_all_orgs({})
        assert first["embedding_deferred"] == 2
        status = await get_embedding_sweep_status(waiting.group_id)
        assert status["graph"]["state"] == "awaiting_evidence"
        assert status["graph"]["waiting_on_organizations"] == [broken]

        await asyncio.sleep(2.2)
        bounded = await lifecycle_repair.repair_lifecycle_all_orgs({})
        settled = await lifecycle_repair.repair_lifecycle_all_orgs({})

        assert bounded["embedding_deferred"] == 0
        assert settled["embedding_unverified"] == 2
        for state in await _plane_states(content, waiting):
            assert state["legacy_basis"] == "no_prior_evidence"


@pytest.mark.asyncio
async def test_live_a_configuration_that_never_swept_cleanly_never_pins_the_model_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sibyl.jobs import lifecycle_repair
    from sibyl_core.services import content_client
    from sibyl_core.services.embedding_evidence import read_deployment_models

    class _RefusingProvider(_SweepTargetProvider):
        async def embed_texts(self, texts, *, input_kind: str = "document"):
            raise ValueError("model not found for this account")

    correct = _SweepTargetProvider(GRAPH_EMBEDDING_DIM, model="text-embedding-3-small")
    correct.metadata = EmbeddingMetadata(**_previous_graph_stamp())
    wrong = _RefusingProvider(GRAPH_EMBEDDING_DIM, model="cohere.embed-v4:0")
    async with _previous_release_deployment(
        {"N": True},
        graph_stamp=correct.metadata.to_dict(),
        chunk_stamp=_PREVIOUS_CHUNK_STAMP,
        upgrade_content=True,
    ) as (content, by_shape):
        client = by_shape["N"]
        # Only stamped rows, so no verdict can be taken on the wrong config.
        await client.execute_query("DELETE entity WHERE attributes.embedding_metadata = NONE;")
        await content_client.select_many(content, "DELETE document_chunks;")

        async def execute(query, **params):
            return await content_client.select_many(content, query, **params)

        await content_client.select_many(content, "DELETE embedding_deployment:models;")
        _run_lifecycle_against(
            monkeypatch,
            content=content,
            clients={client.group_id: client},
            graph_target=wrong,
            chunk_stamp=_PREVIOUS_CHUNK_STAMP,
            embedded_chunks=[],
            record_models=True,
        )
        refused = await lifecycle_repair.repair_lifecycle_all_orgs({})
        assert refused["embedding_failed"] > 0
        assert await read_deployment_models(execute) == {}

        _run_lifecycle_against(
            monkeypatch,
            content=content,
            clients={client.group_id: client},
            graph_target=correct,
            chunk_stamp=_PREVIOUS_CHUNK_STAMP,
            embedded_chunks=[],
            record_models=True,
        )
        fixed = await lifecycle_repair.repair_lifecycle_all_orgs({})

        assert fixed["embedding_reembedded"] == 0
        assert correct.texts == []
        record = await read_deployment_models(execute)
        assert record["first_graph"]["model"] == "text-embedding-3-small"


@pytest.mark.asyncio
async def test_live_two_step_upgrade_adopts_without_warnings_then_switches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sibyl.jobs import lifecycle_repair

    previous = _SweepTargetProvider(GRAPH_EMBEDDING_DIM, model="text-embedding-3-small")
    previous.metadata = EmbeddingMetadata(**_previous_graph_stamp())
    target = _SweepTargetProvider(GRAPH_EMBEDDING_DIM, model="live-restart-target")
    namespace = f"sweep_two_step_live_{uuid4().hex}"
    content = _live_content_client(namespace)
    shapes = {
        "A2": {"graph_stamped": True, "raw_captures": 2},
        "B2": {"graph_stamped": False, "raw_captures": 2},
        "C2": {"graph_stamped": False, "raw_captures": 0},
    }
    orgs = {shape: str(uuid4()) for shape in shapes}
    clients = {orgs[shape]: _live_graph_client(orgs[shape]) for shape in shapes}
    try:
        await bootstrap_content_schema(content, reset=True)
        for shape, spec in shapes.items():
            client = clients[orgs[shape]]
            await prepare_graph_schema(client)
            await _seed_previous_release_org(content, client, **spec)

        # Step one: upgrade on the unchanged models.
        await _restart_on_new_models(
            content,
            clients,
            graph_stamp=previous.metadata.to_dict(),
            chunk_stamp=_PREVIOUS_CHUNK_STAMP,
        )
        _run_lifecycle_against(
            monkeypatch,
            content=content,
            clients=clients,
            graph_target=previous,
            chunk_stamp=_PREVIOUS_CHUNK_STAMP,
            embedded_chunks=[],
        )
        for _tick in range(3):
            upgraded = await lifecycle_repair.repair_lifecycle_all_orgs({})
        assert upgraded["embedding_unverified"] == 0
        assert previous.texts == []
        for shape in shapes:
            graph_state, chunk_state = await _plane_states(content, clients[orgs[shape]])
            for state in (graph_state, chunk_state):
                assert state.get("legacy_decision") in {"adopt", "none"}, shape
                assert not state.get("legacy_warning"), shape
            # Unstamped graphs adopt on the stamped one's evidence, reported
            # as such rather than as complete.
            expected_notice = None if shape == "A2" else "adopted_on_deployment_evidence"
            assert graph_state.get("legacy_notice") == expected_notice, shape

        # Step two: switch providers; every vector now names its model.
        embedded: list[object] = []
        _run_lifecycle_against(
            monkeypatch,
            content=content,
            clients=clients,
            graph_target=target,
            chunk_stamp=_TARGET_CHUNK_STAMP,
            embedded_chunks=embedded,
        )
        for _tick in range(3):
            await lifecycle_repair.repair_lifecycle_all_orgs({})
        for shape in shapes:
            await _assert_org_fully_reembedded(
                content,
                clients[orgs[shape]],
                graph_stamp=target.metadata.to_dict(),
                chunk_stamp=_TARGET_CHUNK_STAMP,
            )
        assert len(target.texts) == 17 * len(shapes)
        assert len(embedded) == 4 * len(shapes)
    finally:
        await content.close()
        with suppress(Exception):
            await _drop_surreal_namespace(namespace)
        for client in clients.values():
            await client.close()
            with suppress(Exception):
                await _drop_surreal_namespace(client.namespace)


# Mid-sweep, old-model vectors can sit nearer a query than every new-model
# vector. A stamp filter applied after the HNSW read would then receive only
# old rows and return nothing; the filter must shape the read itself.


def _crowding_vectors(query: list[float], dimensions: int) -> tuple[list[float], list[float]]:
    """The query itself (nearest possible) and a clearly farther new-model vector."""
    import math

    # A nudge along one axis: about 0.96 cosine to the query, so a genuine
    # near-duplicate, yet farther than rows sitting exactly on it.
    farther = [value + (0.3 if index == 0 else 0.0) for index, value in enumerate(query)]
    norm = math.sqrt(sum(value * value for value in farther)) or 1.0
    return list(query), [value / norm for value in farther[:dimensions]]


@pytest.mark.asyncio
async def test_live_graph_vector_lanes_reach_new_rows_behind_nearer_old_ones() -> None:
    from sibyl_core.models.entities import RelationshipType
    from sibyl_core.retrieval._search_plan import RetrievalPlan, SearchFilter
    from sibyl_core.retrieval._search_sources import (
        _edge_vector_candidates,
        _node_vector_candidates,
    )

    async with _live_graph_manager() as (client, _manager):
        new = _SweepTargetProvider(GRAPH_EMBEDDING_DIM, model="live-crowding-new")
        old_stamp = _previous_graph_stamp()
        new_stamp = new.metadata.to_dict()
        query = (await new.embed_texts(["crowded query"], input_kind="query"))[0]
        nearest, farther = _crowding_vectors(query, GRAPH_EMBEDDING_DIM)
        rows = [
            {
                "uuid": f"old-{index:02d}",
                "group_id": client.group_id,
                "name": f"Old {index}",
                "entity_type": "topic",
                "name_embedding": list(nearest),
                "attributes": {"embedding_metadata": old_stamp},
                "created_at": datetime.now(UTC),
            }
            for index in range(64)
        ] + [
            {
                "uuid": f"new-{index}",
                "group_id": client.group_id,
                "name": f"New {index}",
                "entity_type": "topic",
                "name_embedding": list(farther),
                "attributes": {"embedding_metadata": new_stamp},
                "created_at": datetime.now(UTC),
            }
            for index in range(3)
        ]
        # A new-model entity exactly where the old ones sit: its duplicates in
        # its own space lie farther away than 64 old-model rows.
        rows.append(
            {
                "uuid": "new-seed",
                "group_id": client.group_id,
                "name": "New seed",
                "entity_type": "topic",
                "name_embedding": list(nearest),
                "attributes": {"embedding_metadata": new_stamp},
                "created_at": datetime.now(UTC),
            }
        )
        await client.execute_query("INSERT INTO entity $rows;", rows=rows)
        from sibyl_core.models.entities import Relationship
        from sibyl_core.services.graph import RelationshipManager

        await RelationshipManager(client, group_id=client.group_id).create_direct_bulk(
            [
                Relationship(
                    id=f"{kind}-edge-{index:02d}",
                    relationship_type=RelationshipType.RELATED_TO,
                    source_id="new-0",
                    target_id="new-1",
                    metadata={
                        "fact": f"{kind} fact {index}",
                        "fact_embedding": list(nearest if kind == "old" else farther),
                        "embedding_metadata": old_stamp if kind == "old" else new_stamp,
                    },
                )
                for kind, count in (("old", 64), ("new", 3))
                for index in range(count)
            ]
        )
        plan = RetrievalPlan(
            query="crowded query",
            organization_id=client.group_id,
            facets=(),
            facet_types={},
            scopes=(),
            denied_scopes=(),
        )
        searcher = EntityManager(client, group_id=client.group_id, embedding_provider=new)

        entity_hits = await searcher._vector_search(
            query="crowded query", entity_types=None, limit=1
        )
        typed_hits = await searcher._vector_search(
            query="crowded query",
            entity_types=[EntityType.TOPIC],
            limit=1,
            knn_type_overfetch=4,
        )
        node_hits = await _node_vector_candidates(
            client=client,
            plan=plan,
            search_filter=SearchFilter(),
            query_embedding=query,
            embedding_metadata=new.metadata,
            limit=1,
        )
        typed_node_hits = await _node_vector_candidates(
            client=client,
            plan=plan,
            search_filter=SearchFilter(node_types=("topic",), knn_type_overfetch=4),
            query_embedding=query,
            embedding_metadata=new.metadata,
            limit=1,
        )
        edge_hits = await _edge_vector_candidates(
            client=client,
            plan=plan,
            search_filter=SearchFilter(),
            query_embedding=query,
            embedding_metadata=new.metadata,
            limit=1,
        )
        from sibyl_core.retrieval.dedup import DedupConfig, EntityDeduplicator

        pairs = await EntityDeduplicator(
            client=client,
            entity_manager=EntityManager(client, group_id=client.group_id),
            config=DedupConfig(similarity_threshold=0.9, batch_size=4, min_name_overlap=0.0),
        ).find_duplicates(threshold=0.9)

    seed_pairs = [
        {pair.entity1_id, pair.entity2_id}
        for pair in pairs
        if "new-seed" in (pair.entity1_id, pair.entity2_id)
    ]
    lanes = {
        "entity": [entity.id for entity, _score in entity_hits],
        "typed entity": [entity.id for entity, _score in typed_hits],
        "node": [hit.id for hit in node_hits],
        "typed node": [hit.id for hit in typed_node_hits],
        "edge": [hit.id for hit in edge_hits],
        "dedup": sorted(uuid for pair in seed_pairs for uuid in pair),
    }
    starved = {
        name: ids
        for name, ids in lanes.items()
        if not ids or not all(uuid.startswith("new-") for uuid in ids)
    }
    assert not starved, starved


@pytest.mark.asyncio
async def test_live_chunk_vector_lanes_reach_new_rows_behind_nearer_old_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sibyl.persistence.surreal import content as app_content
    from sibyl_core.services import content_client
    from sibyl_core.services.content_documents import search_document_chunks

    namespace = f"chunk_crowding_live_{uuid4().hex}"
    organization_id = str(uuid4())
    source_id = str(uuid4())
    client = _live_content_client(namespace)

    @asynccontextmanager
    async def session():
        yield client

    monkeypatch.setattr(content_client, "surreal_content_client", session)
    monkeypatch.setattr(app_content, "surreal_content_client", session)
    query = [1.0 / (EMBEDDING_DIM**0.5)] * EMBEDDING_DIM
    nearest, farther = _crowding_vectors(query, EMBEDDING_DIM)
    try:
        await bootstrap_content_schema(client, reset=True)
        await content_client.select_many(
            client,
            "CREATE crawl_sources CONTENT $record RETURN NONE;",
            record={
                "uuid": source_id,
                "organization_id": organization_id,
                "name": "Crowded guide",
                "url": "https://docs.example.test",
            },
        )
        new_chunks: set[str] = set()
        for kind, count in (("old", 64), ("new", 3)):
            for index in range(count):
                document_id, chunk_id = str(uuid4()), str(uuid4())
                if kind == "new":
                    new_chunks.add(chunk_id)
                await content_client.select_many(
                    client,
                    "CREATE crawled_documents CONTENT $record RETURN NONE;",
                    record={
                        "uuid": document_id,
                        "organization_id": organization_id,
                        "source_id": source_id,
                        "url": f"https://docs.example.test/{document_id}",
                        "title": f"{kind} page {index}",
                        "content": "body",
                    },
                )
                await content_client.select_many(
                    client,
                    "CREATE document_chunks CONTENT $record RETURN NONE;",
                    record={
                        "uuid": chunk_id,
                        "organization_id": organization_id,
                        "source_id": source_id,
                        "document_id": document_id,
                        "content": f"{kind} chunk body {index}",
                        "chunk_type": "code",
                        "embedding": list(nearest if kind == "old" else farther),
                        "embedding_metadata": (
                            _PREVIOUS_CHUNK_STAMP if kind == "old" else _TARGET_CHUNK_STAMP
                        ),
                    },
                )

        core_hits, _ = await search_document_chunks(
            organization_id=organization_id,
            query_text="",
            query_embedding=list(query),
            limit=1,
            embedding_metadata=_TARGET_CHUNK_STAMP,
        )
        rag_hits = await app_content.search_rag_chunks(
            None,
            query_embedding=list(query),
            organization_id=organization_id,
            similarity_threshold=0.0,
            match_count=1,
            embedding_metadata=_TARGET_CHUNK_STAMP,
        )
        code_hits = await app_content.search_code_example_chunks(
            None,
            query_embedding=list(query),
            organization_id=organization_id,
            match_count=1,
            embedding_metadata=_TARGET_CHUNK_STAMP,
        )
        hybrid_hits = await app_content.hybrid_search_chunks(
            None,
            query_text="nothing lexical matches",
            query_embedding=list(query),
            organization_id=organization_id,
            similarity_threshold=0.0,
            match_count=1,
            embedding_metadata=_TARGET_CHUNK_STAMP,
        )
    finally:
        await client.close()
        await _drop_surreal_namespace(namespace)

    starved = {
        name: sorted(str(hit[0].id) for hit in hits)
        for name, hits in (
            ("core", core_hits),
            ("rag", rag_hits),
            ("code", code_hits),
            ("hybrid", hybrid_hits),
        )
        if not hits or not {str(hit[0].id) for hit in hits} <= new_chunks
    }
    assert not starved, starved
