from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

import sibyl_core.services.graph as graph_module
from sibyl_core.backends.surreal.connection import SurrealQueryError
from sibyl_core.backends.surreal.schema import (
    ANALYZER_DEFINITIONS,
    EMBEDDING_DIM,
    NODE_DEFINITIONS,
    bootstrap_schema,
    render_fulltext_compatible_sql,
)
from sibyl_core.backends.surreal.schema_version import (
    GRAPH_SCHEMA_CURRENT_VERSION,
    GRAPH_SCHEMA_NAME,
    ensure_schema_version_table,
    get_schema_version,
    record_schema_version,
)
from sibyl_core.embeddings.providers import (
    DeterministicEmbeddingProvider,
    EmbeddingMetadata,
)
from sibyl_core.errors import RevisionConflictError
from sibyl_core.models.entities import Entity, EntityType, Procedure, Relationship, RelationshipType
from sibyl_core.models.tasks import EpicStatus, TaskPriority
from sibyl_core.retrieval.dedup import EntityDeduplicator
from sibyl_core.services.graph import (
    EntityManager,
    RelationshipManager,
    SurrealGraphClient,
    _execute_graph_transaction,
    _validate_native_embedding_dimensions,
    entity_from_surreal_row,
    get_surreal_graph_runtime,
    normalize_records,
    prepare_graph_schema,
    relationship_from_surreal_row,
)


class _EmbeddingWriteClient:
    group_id = "org-native"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
        self.calls.append((query, params))
        if "SELECT id AS record_id FROM entity" in query:
            return [{"record_id": f"entity:{params['uuid']}"}]
        if "INSERT INTO entity $rows ON DUPLICATE KEY UPDATE" in query:
            rows = cast("list[dict[str, object]]", params["rows"])
            return [{"uuid": row["uuid"], "name_embedding": row["name_embedding"]} for row in rows]
        if "RELATE $src->$rel->$tgt" in query:
            return [{"uuid": params["uuid"], "fact_embedding": params["fact_embedding"]}]
        return []


class _BatchEntityReadClient:
    group_id = "org-native"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
        self.calls.append((query, params))
        return [
            {
                "uuid": entity_id,
                "name": entity_id,
                "entity_type": "session",
                "description": f"Description for {entity_id}",
                "content": f"Content for {entity_id}",
                "group_id": self.group_id,
                "attributes": {},
            }
            for entity_id in ("first", "second")
        ]


class _EntityUpdatePatchClient:
    group_id = "org-native"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
        self.calls.append((query, params))
        if "UPDATE entity MERGE $patch" not in query:
            raise AssertionError("update should happen in one server-side write")
        patch = cast("dict[str, object]", params["patch"])
        attributes_patch = cast("dict[str, object]", patch["attributes"])
        return [
            {
                "uuid": params["uuid"],
                "name": patch.get("name", "Original"),
                "entity_type": "task",
                "description": patch.get("description", "Original description"),
                "content": patch.get("content", "Original content"),
                "group_id": params["group_id"],
                "created_by": patch.get("created_by"),
                "modified_by": patch.get("modified_by"),
                "attributes": {
                    "existing": "preserved",
                    "metadata": json.dumps({"legacy": "preserved"}),
                    **attributes_patch,
                },
                "created_at": datetime(2026, 1, 1, tzinfo=UTC),
                "updated_at": patch["updated_at"],
                "status": patch.get("status"),
            }
        ]


class _TransactionDeleteClient:
    group_id = "org-native"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> object:
        msg = "transactional deletes should use execute_query_raw when available"
        raise AssertionError(msg)

    async def execute_query_raw(self, query: str, **params: object) -> object:
        self.calls.append((query, params))
        deleted = [{"uuid": params["uuid"]}]
        return {
            "result": [
                {"status": "OK", "result": None},
                {"status": "OK", "result": deleted},
                {"status": "OK", "result": []},
                {"status": "OK", "result": deleted},
                {"status": "OK", "result": None},
            ]
        }


class _RelatedBatchClient:
    group_id = "org-native"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
        self.calls.append((query, params))
        now = datetime.now(UTC)
        if "FROM relates_to" in query and "source_id = $entity_id" in query:
            if params["entity_id"] != "seed-a":
                return []
            return [
                _related_edge_row(
                    uuid="rel_seed_a_target_a",
                    source_id="seed-a",
                    target_id="target-a",
                    related_id="target-a",
                    group_id=self.group_id,
                    created_at=now,
                ),
                _related_edge_row(
                    uuid="rel_seed_a_seed_b",
                    source_id="seed-a",
                    target_id="seed-b",
                    related_id="seed-b",
                    group_id=self.group_id,
                    created_at=now,
                ),
            ]
        if "FROM relates_to" in query and "target_id = $entity_id" in query:
            if params["entity_id"] == "seed-a":
                return [
                    _related_edge_row(
                        uuid="rel_source_a_seed_a",
                        source_id="source-a",
                        target_id="seed-a",
                        related_id="source-a",
                        group_id=self.group_id,
                        created_at=now,
                    )
                ]
            if params["entity_id"] != "seed-b":
                return []
            return [
                _related_edge_row(
                    uuid="rel_seed_a_seed_b",
                    source_id="seed-a",
                    target_id="seed-b",
                    related_id="seed-a",
                    group_id=self.group_id,
                    created_at=now,
                ),
            ]
        if "FROM entity" in query:
            entity_ids = cast("list[str]", params["entity_ids"])
            return [
                {
                    "record_id": f"entity:{entity_id}",
                    "uuid": entity_id,
                    "name": entity_id.title(),
                    "entity_type": "topic",
                    "summary": "",
                    "group_id": self.group_id,
                    "attributes": {},
                    "created_at": now,
                    "updated_at": now,
                }
                for entity_id in entity_ids
            ]
        return []


def _related_edge_row(
    *,
    uuid: str,
    source_id: str,
    target_id: str,
    related_id: str | None = None,
    group_id: str,
    created_at: datetime,
) -> dict[str, object]:
    return {
        "record_id": f"relates_to:{uuid}",
        "uuid": uuid,
        "name": "RELATED_TO",
        "fact": f"{source_id} points at {target_id}",
        "group_id": group_id,
        "episodes": [],
        "attributes": {},
        "created_at": created_at,
        "expired_at": None,
        "valid_at": created_at,
        "invalid_at": None,
        "source_uuid": source_id,
        "target_uuid": target_id,
    } | _related_entity_row(related_id or target_id, group_id=group_id, created_at=created_at)


def _related_entity_row(
    entity_id: str,
    *,
    group_id: str,
    created_at: datetime,
) -> dict[str, object]:
    return {
        "related_record_id": f"entity:{entity_id}",
        "related_uuid": entity_id,
        "related_name": entity_id.title(),
        "related_entity_type": "topic",
        "related_summary": "",
        "related_description": "",
        "related_labels": [],
        "related_attributes": {},
        "related_group_id": group_id,
        "related_created_at": created_at,
        "related_updated_at": created_at,
    }


class _CappedRelatedBatchClient:
    group_id = "org-native"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
        self.calls.append((query, params))
        now = datetime.now(UTC)
        if "FROM relates_to" in query and "source_id = $entity_id" in query:
            if params["entity_id"] == "seed-b":
                return [
                    _related_edge_row(
                        uuid="rel_seed_b_target_b",
                        source_id="seed-b",
                        target_id="target-b",
                        group_id=self.group_id,
                        created_at=now,
                    )
                ]
            if params["entity_id"] != "seed-a":
                return []
            return [
                _related_edge_row(
                    uuid=f"rel_seed_a_target_{index}",
                    source_id="seed-a",
                    target_id=f"target-{index}",
                    group_id=self.group_id,
                    created_at=now,
                )
                for index in range(cast("int", params["limit"]))
            ]
        if "FROM relates_to" in query and "target_id = $entity_id" in query:
            return []
        if "FROM entity" in query:
            entity_ids = cast("list[str]", params["entity_ids"])
            return [
                {
                    "record_id": f"entity:{entity_id}",
                    "uuid": entity_id,
                    "name": entity_id.title(),
                    "entity_type": "topic",
                    "summary": "",
                    "group_id": self.group_id,
                    "attributes": {},
                    "created_at": now,
                    "updated_at": now,
                }
                for entity_id in entity_ids
            ]
        return []


def _deterministic_provider() -> DeterministicEmbeddingProvider:
    return DeterministicEmbeddingProvider(
        EmbeddingMetadata(
            provider="deterministic",
            model="unit-test",
            dimensions=4,
            cache_namespace="native-graph-test",
            tokenizer_estimate_method="utf8-byte-length",
        )
    )


class _FailingEmbeddingProvider:
    metadata = EmbeddingMetadata(
        provider="deterministic",
        model="failing-unit-test",
        dimensions=4,
        cache_namespace="native-graph-test",
        tokenizer_estimate_method="unit-test",
    )

    async def embed_texts(
        self,
        texts: list[str],
        *,
        input_kind: str = "document",
    ) -> list[list[float]]:
        del texts, input_kind
        raise TimeoutError("embedding provider stalled")


class _SlowEmbeddingProvider:
    metadata = EmbeddingMetadata(
        provider="deterministic",
        model="slow-unit-test",
        dimensions=4,
        cache_namespace="native-graph-test",
        tokenizer_estimate_method="unit-test",
    )

    async def embed_texts(
        self,
        texts: list[str],
        *,
        input_kind: str = "document",
    ) -> list[list[float]]:
        del texts, input_kind
        await asyncio.sleep(10)
        return [[0.1, 0.2, 0.3, 0.4]]


class _CoordinatedSearchEmbeddingProvider:
    metadata = EmbeddingMetadata(
        provider="deterministic",
        model="coordinated-unit-test",
        dimensions=4,
        cache_namespace="native-graph-test",
        tokenizer_estimate_method="unit-test",
    )

    def __init__(
        self, *, fulltext_started: asyncio.Event, embedding_started: asyncio.Event
    ) -> None:
        self._fulltext_started = fulltext_started
        self._embedding_started = embedding_started

    async def embed_texts(
        self,
        texts: list[str],
        *,
        input_kind: str = "document",
    ) -> list[list[float]]:
        del texts, input_kind
        self._embedding_started.set()
        await self._fulltext_started.wait()
        return [[0.1, 0.2, 0.3, 0.4]]


class _CoordinatedSearchClient:
    group_id = "org-native"

    def __init__(
        self, *, fulltext_started: asyncio.Event, embedding_started: asyncio.Event
    ) -> None:
        self._fulltext_started = fulltext_started
        self._embedding_started = embedding_started
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
        self.calls.append((query, params))
        if params.get("_query_label") == "entity.search.fulltext":
            self._fulltext_started.set()
            await self._embedding_started.wait()
            return [
                {
                    "record_id": "entity:parallel_search",
                    "uuid": "parallel_search",
                    "name": "Parallel Search",
                    "entity_type": "topic",
                    "summary": "parallel search",
                    "group_id": self.group_id,
                    "attributes": {},
                    "created_at": datetime.now(UTC),
                    "updated_at": datetime.now(UTC),
                    "score": 1.0,
                }
            ]
        if params.get("_query_label") == "entity.search.vector":
            return []
        return []


class _ExplicitAnchorSearchClient:
    group_id = "org-native"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
        self.calls.append((query, params))
        search_query = params.get("search_query")
        if search_query == "report related link":
            return [
                self._row(
                    "partial-anchor",
                    "The Report form is open, but the requested section is elsewhere.",
                ),
                self._row(
                    "distant-anchor",
                    "Report " + ("unrelated " * 100) + "Related Links.",
                ),
                self._row(
                    "exact-anchor",
                    "On the Report record, Related Links contains Create Order.",
                ),
            ]
        if params.get("_query_label") == "entity.search.fulltext":
            return [
                self._row("primary-one", "Primary result one."),
                self._row("primary-two", "Primary result two."),
                self._row("primary-three", "Primary result three."),
            ]
        return []

    def _row(self, entity_id: str, content: str) -> dict[str, object]:
        now = datetime.now(UTC)
        return {
            "record_id": f"entity:{entity_id}",
            "uuid": entity_id,
            "name": entity_id,
            "entity_type": "session",
            "summary": content,
            "description": content,
            "content": content,
            "group_id": self.group_id,
            "attributes": {},
            "created_at": now,
            "updated_at": now,
            "score": 1.0,
        }


@pytest.mark.asyncio
async def test_graph_client_cache_evicts_oldest_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await graph_module.close_graph_clients()
    closed: list[str] = []

    class FakeNativeGraphClient:
        def __init__(self, *, group_id: str, **_: object) -> None:
            self.group_id = group_id

        async def close(self) -> None:
            closed.append(self.group_id)

    monkeypatch.setattr(graph_module, "SurrealGraphClient", FakeNativeGraphClient)
    monkeypatch.setattr(graph_module.settings, "surreal_graph_client_cache_size", 2)

    await graph_module.get_surreal_graph_client("org-a")
    await graph_module.get_surreal_graph_client("org-b")
    graph_module._prepared_groups.update({"org-a", "org-b"})
    await graph_module.get_surreal_graph_client("org-c")

    assert closed == ["org-a"]
    assert list(graph_module._clients) == ["org-b", "org-c"]
    assert "org-a" not in graph_module._prepared_groups

    await graph_module.close_graph_clients()


@pytest.mark.asyncio
async def test_graph_client_uses_configured_pool_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await graph_module.close_graph_clients()
    captured: dict[str, object] = {}

    class FakeNativeGraphClient:
        def __init__(self, *, group_id: str, **kwargs: object) -> None:
            captured.update(kwargs)
            self.group_id = group_id

        async def close(self) -> None:
            return None

    monkeypatch.setattr(graph_module, "SurrealGraphClient", FakeNativeGraphClient)
    monkeypatch.setattr(graph_module.settings, "surreal_pool_size", 8)
    monkeypatch.setattr(graph_module.settings, "surreal_graph_pool_size", 34)

    try:
        await graph_module.get_surreal_graph_client("org-pool")
    finally:
        await graph_module.close_graph_clients()

    assert captured["pool_size"] == 34


@pytest.mark.asyncio
async def test_replace_entity_retries_transient_surreal_query_id_keyerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _TransientEntityWriteClient()
    bootstrap_schema = AsyncMock()
    monkeypatch.setattr(graph_module, "bootstrap_schema", bootstrap_schema)
    graph_module._prepared_groups.add(client.group_id)

    try:
        row = await graph_module._replace_entity(
            cast("Any", client),
            Entity(id="entity-retry", entity_type=EntityType.SESSION, name="Retry Session"),
            group_id=client.group_id,
        )
    finally:
        graph_module._prepared_groups.discard(client.group_id)

    assert row["uuid"] == "entity-retry"
    assert client.calls == 2
    bootstrap_schema.assert_awaited_once_with(client)


@pytest.mark.asyncio
async def test_replace_entities_retries_legacy_updated_at_string_schema() -> None:
    client = _LegacyUpdatedAtEntityWriteClient()
    explicit_updated_at = datetime(2026, 5, 31, 2, 52, tzinfo=UTC)

    rows = await graph_module._replace_entities_bulk(
        cast("Any", client),
        [
            Entity(id="entity-legacy-one", entity_type=EntityType.SESSION, name="Legacy One"),
            Entity(
                id="entity-legacy-two",
                entity_type=EntityType.SESSION,
                name="Legacy Two",
                updated_at=explicit_updated_at,
            ),
        ],
        group_id=client.group_id,
    )

    assert [row["uuid"] for row in rows] == ["entity-legacy-one", "entity-legacy-two"]
    assert len(client.calls) == 2
    first_rows = cast("list[dict[str, object]]", client.calls[0][1]["rows"])
    retry_rows = cast("list[dict[str, object]]", client.calls[1][1]["rows"])
    assert all(isinstance(row["updated_at"], datetime) for row in first_rows)
    assert all(isinstance(row["updated_at"], str) for row in retry_rows)
    assert retry_rows[1]["updated_at"] == explicit_updated_at.isoformat()
    retry_attributes = cast(dict[str, object], retry_rows[1]["attributes"])
    assert retry_attributes["updated_at"] == explicit_updated_at.isoformat()


@pytest.mark.asyncio
async def test_graph_runtime_can_skip_schema_preparation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _EmbeddingWriteClient()
    bootstrap_schema = AsyncMock()
    monkeypatch.setattr(graph_module, "bootstrap_schema", bootstrap_schema)
    monkeypatch.setattr(
        graph_module,
        "get_surreal_graph_client",
        AsyncMock(return_value=client),
    )

    runtime = await get_surreal_graph_runtime(client.group_id, ensure_schema=False)

    assert runtime.client is client
    bootstrap_schema.assert_not_awaited()


class _TransientEntityWriteClient:
    group_id = "org-retry"

    def __init__(self) -> None:
        self.calls = 0

    async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
        self.calls += 1
        if self.calls == 1:
            raise KeyError("c4ae8fd2-a34d-4f8f-8225-1fd0f7a91cf6")
        rows = cast("list[dict[str, object]]", params["rows"])
        return [{"uuid": rows[0]["uuid"], "name": rows[0]["name"]}]


class _LegacyUpdatedAtEntityWriteClient:
    group_id = "org-legacy-updated-at"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
        self.calls.append((query, params))
        rows = cast("list[dict[str, object]]", params["rows"])
        if len(self.calls) == 1:
            msg = (
                "Couldn't coerce value for field `updated_at` of `entity:abc`: "
                "Expected `none | string` but found `d'2026-05-31T02:52:00Z'`"
            )
            raise RuntimeError(msg)
        return [{"uuid": row["uuid"], "updated_at": row["updated_at"]} for row in rows]


def test_native_embedding_dimension_validation_requires_schema_match() -> None:
    _validate_native_embedding_dimensions(
        DeterministicEmbeddingProvider(
            EmbeddingMetadata(
                provider="deterministic",
                model="unit-test",
                dimensions=EMBEDDING_DIM,
                cache_namespace="native-graph-test",
                tokenizer_estimate_method="utf8-byte-length",
            )
        )
    )
    with pytest.raises(ValueError, match="must match Surreal graph schema"):
        _validate_native_embedding_dimensions(
            DeterministicEmbeddingProvider(
                EmbeddingMetadata(
                    provider="deterministic",
                    model="unit-test",
                    dimensions=EMBEDDING_DIM + 1,
                    cache_namespace="native-graph-test",
                    tokenizer_estimate_method="utf8-byte-length",
                )
            )
        )


def test_entity_from_surreal_row_preserves_native_policy_metadata() -> None:
    entity = entity_from_surreal_row(
        {
            "id": "entity:procedure_native",
            "uuid": "procedure_native",
            "name": "Native Procedure",
            "entity_type": "procedure",
            "description": "Top-level description",
            "content": "Top-level content",
            "group_id": "org-native",
            "created_by": "stef",
            "modified_by": "nova",
            "project_id": "project_native",
            "source_id": "raw_1",
            "source_ids": ["raw_1", "raw_2"],
            "confidence": 0.94,
            "valid_at": "2026-05-13T12:00:00+00:00",
            "valid_from": "2026-05-13T12:00:00+00:00",
            "invalid_at": "2026-05-14T12:00:00+00:00",
            "tags": ["native", "procedure"],
            "attributes": {
                "metadata": json.dumps(
                    {
                        "category": None,
                        "required_tools": ["moon"],
                        "steps": [
                            {
                                "order": 1,
                                "title": "Verify",
                                "success_criteria": "Targeted tests pass",
                            }
                        ],
                    }
                ),
                "source_file": "docs/native.md",
            },
            "created_at": "2026-05-13T12:00:00+00:00",
            "updated_at": "2026-05-13T12:30:00+00:00",
            "name_embedding": [0.5] * EMBEDDING_DIM,
        }
    )

    assert isinstance(entity, Procedure)
    assert entity.id == "procedure_native"
    assert entity.category == ""
    assert entity.required_tools == ["moon"]
    assert entity.steps[0].title == "Verify"
    assert entity.created_by == "stef"
    assert entity.modified_by == "nova"
    assert entity.source_file == "docs/native.md"
    assert entity.embedding == [0.5] * EMBEDDING_DIM
    assert entity.metadata["project_id"] == "project_native"
    assert entity.metadata["source_id"] == "raw_1"
    assert entity.metadata["source_ids"] == ["raw_1", "raw_2"]
    assert entity.metadata["confidence"] == 0.94
    assert entity.metadata["valid_at"] == "2026-05-13T12:00:00+00:00"
    assert entity.metadata["valid_from"] == "2026-05-13T12:00:00+00:00"
    assert entity.metadata["invalid_at"] == "2026-05-14T12:00:00+00:00"
    assert entity.metadata["record_id"] == "entity:procedure_native"
    assert "category" not in entity.metadata


@pytest.mark.asyncio
async def test_native_entity_manager_generates_embeddings_with_native_provider() -> None:
    client = _EmbeddingWriteClient()
    provider = _deterministic_provider()
    manager = EntityManager(
        cast(SurrealGraphClient, client),
        group_id=client.group_id,
        embedding_provider=provider,
    )

    created_id = await manager.create_direct(
        Entity(
            id="entity_embed",
            entity_type=EntityType.PATTERN,
            name="Native Embedding Pattern",
            description="Generated without Graphiti.",
            organization_id=client.group_id,
            created_by="stef",
            modified_by="nova",
        ),
        generate_embedding=True,
    )

    assert created_id == "entity_embed"
    write_query, write_params = client.calls[0]
    assert "INSERT INTO entity $rows ON DUPLICATE KEY UPDATE" in write_query
    assert "created_by = created_by ?? $input.created_by" in write_query
    assert "modified_by = $input.modified_by ?? modified_by" in write_query
    rows = cast("list[dict[str, object]]", write_params["rows"])
    assert len(cast(list[float], rows[0]["name_embedding"])) == 4
    assert isinstance(rows[0]["updated_at"], datetime)
    attributes = cast(dict[str, object], rows[0]["attributes"])
    assert isinstance(attributes["updated_at"], datetime)
    assert attributes["embedding_metadata"] == provider.metadata.to_dict()
    assert rows[0]["created_by"] == "stef"
    assert rows[0]["modified_by"] == "nova"


@pytest.mark.asyncio
async def test_native_entity_manager_get_many_preserves_requested_order_and_scope() -> None:
    client = _BatchEntityReadClient()
    manager = EntityManager(cast("SurrealGraphClient", client), group_id=client.group_id)

    entities = await manager.get_many(["second", "first", "second", "missing"])

    assert [entity.id for entity in entities] == ["second", "first"]
    query, params = client.calls[0]
    assert "uuid IN $uuids" in query
    assert params == {
        "group_id": "org-native",
        "uuids": ["second", "first", "missing"],
    }


@pytest.mark.asyncio
async def test_native_entity_manager_update_uses_server_side_merge() -> None:
    client = _EntityUpdatePatchClient()
    manager = EntityManager(cast("SurrealGraphClient", client), group_id=client.group_id)

    updated = await manager.update(
        "task-native",
        {
            "metadata": {"project_id": "project-native"},
            "modified_by": "nova",
            "status": "done",
            "title": "Updated task",
        },
    )

    assert updated is not None
    assert updated.name == "Updated task"
    assert updated.metadata["existing"] == "preserved"
    assert updated.metadata["legacy"] == "preserved"
    assert updated.metadata["project_id"] == "project-native"
    assert updated.metadata["status"] == "done"
    assert updated.modified_by == "nova"
    query, params = client.calls[0]
    assert "BEGIN TRANSACTION" in query
    assert "UPDATE entity MERGE $patch" in query
    assert "revision = $expected_revision" in query
    assert "revision += 1" in query
    assert len(client.calls) == 1
    assert params["expected_revision"] is None
    patch = cast("dict[str, object]", params["patch"])
    attributes = cast("dict[str, object]", patch["attributes"])
    assert patch["modified_by"] == "nova"
    assert patch["status"] == "done"
    assert attributes["status"] == "done"


@pytest.mark.asyncio
async def test_native_entity_upsert_preserves_creator_and_updates_modifier() -> None:
    client = SurrealGraphClient(group_id="org-native-actor-upsert", url="memory://")
    try:
        await prepare_graph_schema(client)
        manager = EntityManager(client, group_id=client.group_id)

        await manager.create_direct(
            Entity(
                id="actor_upsert",
                entity_type=EntityType.TASK,
                name="Original actor task",
                organization_id=client.group_id,
                created_by="creator-a",
                modified_by="modifier-a",
            )
        )
        await manager.create_direct(
            Entity(
                id="actor_upsert",
                entity_type=EntityType.TASK,
                name="Rewritten actor task",
                organization_id=client.group_id,
                created_by="creator-b",
                modified_by="modifier-b",
            )
        )

        rows = normalize_records(
            await client.execute_query(
                """
                SELECT name, created_by, modified_by
                FROM entity
                WHERE group_id = $group_id AND uuid = "actor_upsert"
                LIMIT 1;
                """,
                group_id=client.group_id,
            )
        )

        assert rows[0]["name"] == "Rewritten actor task"
        assert rows[0]["created_by"] == "creator-a"
        assert rows[0]["modified_by"] == "modifier-b"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_native_entity_manager_bulk_generates_embeddings_in_batches() -> None:
    client = _EmbeddingWriteClient()
    provider = _deterministic_provider()
    manager = EntityManager(
        cast(SurrealGraphClient, client),
        group_id=client.group_id,
        embedding_provider=provider,
    )

    created_ids = await manager.create_direct_bulk(
        [
            Entity(
                id="entity_embed_one",
                entity_type=EntityType.SESSION,
                name="First session",
                description="Generated in a batch.",
                organization_id=client.group_id,
            ),
            Entity(
                id="entity_embed_two",
                entity_type=EntityType.SESSION,
                name="Second session",
                description="Generated in a batch.",
                organization_id=client.group_id,
            ),
        ],
        generate_embeddings=True,
        embedding_batch_size=2,
    )

    assert created_ids == ["entity_embed_one", "entity_embed_two"]
    write_calls = [
        params
        for query, params in client.calls
        if "INSERT INTO entity $rows ON DUPLICATE KEY UPDATE" in query
    ]
    assert len(write_calls) == 1
    rows = cast("list[dict[str, object]]", write_calls[0]["rows"])
    assert [row["uuid"] for row in rows] == ["entity_embed_one", "entity_embed_two"]
    assert all(len(cast("list[float]", row["name_embedding"])) == 4 for row in rows)


@pytest.mark.asyncio
async def test_native_entity_manager_bulk_writes_without_embedding_on_provider_failure() -> None:
    client = _EmbeddingWriteClient()
    manager = EntityManager(
        cast(SurrealGraphClient, client),
        group_id=client.group_id,
        embedding_provider=_FailingEmbeddingProvider(),
    )

    created_ids = await manager.create_direct_bulk(
        [
            Entity(
                id="entity_embed_failure",
                entity_type=EntityType.SESSION,
                name="Bulk session without embedding",
                description="The durable write should still land.",
                organization_id=client.group_id,
            ),
        ],
        generate_embeddings=True,
    )

    assert created_ids == ["entity_embed_failure"]
    write_calls = [
        params
        for query, params in client.calls
        if "INSERT INTO entity $rows ON DUPLICATE KEY UPDATE" in query
    ]
    rows = cast("list[dict[str, object]]", write_calls[0]["rows"])
    assert rows[0]["uuid"] == "entity_embed_failure"
    assert rows[0]["name_embedding"] is None
    attributes = cast(dict[str, object], rows[0]["attributes"])
    assert "embedding_metadata" not in attributes


@pytest.mark.asyncio
async def test_native_entity_delete_runs_raw_transaction() -> None:
    client = _TransactionDeleteClient()
    manager = EntityManager(cast("SurrealGraphClient", client), group_id=client.group_id)

    deleted = await manager.delete("entity_delete")

    assert deleted is True
    assert len(client.calls) == 1
    query, params = client.calls[0]
    assert "BEGIN TRANSACTION;" in query
    assert "DELETE FROM relates_to" in query
    assert "DELETE FROM mentions" in query
    assert "DELETE FROM entity" in query
    assert "COMMIT TRANSACTION;" in query
    assert params == {"group_id": client.group_id, "uuid": "entity_delete"}


@pytest.mark.asyncio
async def test_native_entity_manager_search_uses_short_query_embedding_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _EmbeddingWriteClient()
    manager = EntityManager(
        cast(SurrealGraphClient, client),
        group_id=client.group_id,
        embedding_provider=_SlowEmbeddingProvider(),
    )
    monkeypatch.setattr(
        graph_module.settings,
        "graph_search_embedding_timeout_seconds",
        0.01,
    )

    started = time.perf_counter()
    results = await manager.search(query="slow vector query", limit=5)

    assert results == []
    assert (time.perf_counter() - started) < 1


@pytest.mark.asyncio
async def test_native_entity_manager_search_uses_configured_knn_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _EmbeddingWriteClient()
    provider = DeterministicEmbeddingProvider(
        EmbeddingMetadata(
            provider="deterministic",
            model="unit-test",
            dimensions=4,
            cache_namespace="native-graph-test",
            tokenizer_estimate_method="unit-test",
        )
    )
    manager = EntityManager(
        cast(SurrealGraphClient, client),
        group_id=client.group_id,
        embedding_provider=provider,
    )
    monkeypatch.setattr(graph_module.settings, "graph_knn_ef", 88)

    await manager.search(query="configured vector effort", limit=5)

    assert any("name_embedding <|32, 88|> $query_embedding" in query for query, _ in client.calls)


@pytest.mark.asyncio
async def test_native_entity_manager_search_projections_omit_embeddings() -> None:
    client = _EmbeddingWriteClient()
    manager = EntityManager(cast(SurrealGraphClient, client), group_id=client.group_id)

    await manager.search(query="projection check", limit=1)

    fulltext_query = next(
        query
        for query, params in client.calls
        if params.get("_query_label") == "entity.search.fulltext"
    )
    fallback_query = client.calls[-1][0]
    assert "SELECT *" not in fulltext_query
    assert "SELECT *" not in fallback_query
    assert "id AS record_id" in fulltext_query
    assert "id AS record_id" in fallback_query
    assert "name_embedding" not in fulltext_query
    assert "name_embedding" not in fallback_query


@pytest.mark.asyncio
async def test_native_entity_manager_search_overlaps_fulltext_and_vector_branches() -> None:
    fulltext_started = asyncio.Event()
    embedding_started = asyncio.Event()
    client = _CoordinatedSearchClient(
        fulltext_started=fulltext_started,
        embedding_started=embedding_started,
    )
    provider = _CoordinatedSearchEmbeddingProvider(
        fulltext_started=fulltext_started,
        embedding_started=embedding_started,
    )
    manager = EntityManager(
        cast(SurrealGraphClient, client),
        group_id=client.group_id,
        embedding_provider=provider,
    )

    results = await asyncio.wait_for(manager.search(query="parallel search", limit=5), timeout=1)

    assert [entity.id for entity, _score in results] == ["parallel_search"]
    assert fulltext_started.is_set()
    assert embedding_started.is_set()
    assert {params.get("_query_label") for _query, params in client.calls} == {
        "entity.search.fulltext",
        "entity.search.vector",
    }
    vector_query = next(
        query
        for query, params in client.calls
        if params.get("_query_label") == "entity.search.vector"
    )
    assert "SELECT *, (1 - vector::distance::knn()) AS score" not in vector_query
    assert "id AS record_id" in vector_query
    assert "name_embedding," not in vector_query


@pytest.mark.asyncio
async def test_native_entity_manager_rescues_full_explicit_anchor_candidate() -> None:
    client = _ExplicitAnchorSearchClient()
    manager = EntityManager(cast("SurrealGraphClient", client), group_id=client.group_id)
    query = 'On a `Report` record, what is the only entry under "Related Links"?'

    results = await manager.search(query=query, limit=3)

    assert [entity.id for entity, _score in results] == [
        "primary-one",
        "primary-two",
        "exact-anchor",
    ]
    anchor_query, anchor_call = next(
        (statement, params)
        for statement, params in client.calls
        if params.get("_query_label") == "entity.search.fulltext_explicit_anchors"
    )
    assert anchor_call["search_query"] == "report related link"
    assert "AND content @AND@ $search_query" in anchor_query


@pytest.mark.asyncio
async def test_native_entity_manager_skips_anchor_probe_for_single_anchor() -> None:
    client = _ExplicitAnchorSearchClient()
    manager = EntityManager(cast("SurrealGraphClient", client), group_id=client.group_id)

    results = await manager.search(query='What is under "Related Links"?', limit=3)

    assert [entity.id for entity, _score in results] == [
        "primary-one",
        "primary-two",
        "primary-three",
    ]
    assert all(
        params.get("_query_label") != "entity.search.fulltext_explicit_anchors"
        for _query, params in client.calls
    )


@pytest.mark.asyncio
async def test_native_entity_manager_bulk_writes_entities_in_one_surreal_batch() -> None:
    client = SurrealGraphClient(group_id="org-native-bulk-write", url="memory://")
    try:
        await prepare_graph_schema(client)
        manager = EntityManager(client, group_id=client.group_id)

        created_ids = await manager.create_direct_bulk(
            [
                Entity(
                    id="session_bulk_one",
                    entity_type=EntityType.SESSION,
                    name="Bulk Session One",
                    content="First live bulk record.",
                    organization_id=client.group_id,
                    metadata={"valid_at": "2026/01/01 10:00"},
                ),
                Entity(
                    id="session_bulk_two",
                    entity_type=EntityType.SESSION,
                    name="Bulk Session Two",
                    content="Second live bulk record.",
                    organization_id=client.group_id,
                    metadata={"valid_at": "2026/01/02 10:00"},
                ),
            ]
        )
        updated_ids = await manager.create_direct_bulk(
            [
                Entity(
                    id="session_bulk_two",
                    entity_type=EntityType.SESSION,
                    name="Bulk Session Two Updated",
                    content="Second live bulk record updated.",
                    organization_id=client.group_id,
                    metadata={"valid_at": "2026/01/03 10:00"},
                ),
            ]
        )

        rows = normalize_records(
            await client.execute_query(
                """
                SELECT uuid, name, entity_type, group_id, content, attributes
                FROM entity
                WHERE uuid IN ["session_bulk_one", "session_bulk_two"]
                ORDER BY uuid ASC;
                """
            )
        )
    finally:
        await client.close()

    assert created_ids == ["session_bulk_one", "session_bulk_two"]
    assert updated_ids == ["session_bulk_two"]
    assert [row["uuid"] for row in rows] == ["session_bulk_one", "session_bulk_two"]
    assert all(row["group_id"] == client.group_id for row in rows)
    assert rows[0]["attributes"]["valid_at"] == "2026/01/01 10:00"
    assert rows[1]["name"] == "Bulk Session Two Updated"
    assert rows[1]["content"] == "Second live bulk record updated."
    assert rows[1]["attributes"]["valid_at"] == "2026/01/03 10:00"


@pytest.mark.asyncio
async def test_native_embedding_backfill_cannot_overwrite_or_resurrect_stale_entity() -> None:
    client = SurrealGraphClient(group_id="org-native-fenced-embedding", url="memory://")
    provider = DeterministicEmbeddingProvider(
        EmbeddingMetadata(
            provider="deterministic",
            model="unit-test",
            dimensions=1024,
            cache_namespace="native-fenced-embedding-test",
            tokenizer_estimate_method="utf8-byte-length",
        )
    )
    try:
        await prepare_graph_schema(client)
        manager = EntityManager(
            client,
            group_id=client.group_id,
            embedding_provider=provider,
        )
        stale = Entity(
            id="session_fenced",
            entity_type=EntityType.SESSION,
            name="Captured state",
            content="  version one\n",
            organization_id=client.group_id,
        )
        current = stale.model_copy(update={"content": "  version two\n"})
        await manager.create_direct_bulk((stale,))
        await manager.create_direct_bulk((current,))

        stale_ids = await manager.backfill_embeddings_if_current((stale,))
        prepared = await manager.prepare_entities_for_write(
            (await manager.get(current.id),),
            generate_embeddings=True,
        )
        current_ids = await manager.backfill_embeddings_if_current((current,))
        stored = await manager.get(current.id)
        await manager.delete(current.id)
        deleted_ids = await manager.backfill_embeddings_if_current((stale,))
    finally:
        await client.close()

    assert stale_ids == []
    assert prepared[0].embedding
    assert current_ids == [current.id]
    assert stored.content == "version two"
    assert stored.embedding
    assert deleted_ids == []


@pytest.mark.asyncio
async def test_native_embedding_backfill_accepts_hydrated_storage_defaults() -> None:
    client = SurrealGraphClient(group_id="org-native-hydrated-embedding", url="memory://")
    provider = DeterministicEmbeddingProvider(
        EmbeddingMetadata(
            provider="deterministic",
            model="unit-test",
            dimensions=1024,
            cache_namespace="native-hydrated-embedding-test",
            tokenizer_estimate_method="utf8-byte-length",
        )
    )
    try:
        await prepare_graph_schema(client)
        manager = EntityManager(
            client,
            group_id=client.group_id,
            embedding_provider=provider,
        )
        embed_texts = AsyncMock(wraps=provider.embed_texts)
        provider.embed_texts = embed_texts  # type: ignore[method-assign]
        raw = Entity(
            id="session_hydrated",
            entity_type=EntityType.SESSION,
            name="  Captured state\n",
            content="  exact evidence bytes\n",
            organization_id=client.group_id,
        )
        await manager.create_direct(raw)
        hydrated = await manager.get(raw.id)

        hydrated_ids = await manager.backfill_embeddings_if_current((hydrated,))
        first_call_count = embed_texts.await_count
        stored = await manager.get(raw.id)
        repeated_ids = await manager.backfill_embeddings_if_current((hydrated,))
    finally:
        await client.close()

    assert hydrated.name == "Captured state"
    assert hydrated.description == "Captured state"
    assert hydrated.content == "exact evidence bytes"
    assert hydrated_ids == [raw.id]
    assert repeated_ids == [raw.id]
    assert stored.embedding
    assert first_call_count == 1
    assert embed_texts.await_count == first_call_count


@pytest.mark.asyncio
async def test_native_relationship_bulk_persists_edges_readable_after_write() -> None:
    # Regression guard for the bulk relates_to upsert path: it must execute and
    # persist edges that are then readable (the prior unit test used a fake client
    # that never ran the SurrealQL). Note: the original bug here was a `type::thing`
    # call that the 2.x embedded engine accepts but the 3.x server rejects, so this
    # memory:// test alone does not catch a 3.x-only parse error; the live E2E does.
    client = SurrealGraphClient(group_id="org-native-rel-bulk", url="memory://")
    try:
        await prepare_graph_schema(client)
        entity_manager = EntityManager(client, group_id=client.group_id)
        # Use the graph embedding dimension so fact_embedding matches the
        # relates_to.fact_embedding field; a mismatched dim would fail the write.
        provider = DeterministicEmbeddingProvider(
            EmbeddingMetadata(
                provider="deterministic",
                model="unit-test",
                dimensions=1024,
                cache_namespace="native-graph-test",
                tokenizer_estimate_method="utf8-byte-length",
            )
        )
        relationship_manager = RelationshipManager(
            client, group_id=client.group_id, embedding_provider=provider
        )

        await entity_manager.create_direct_bulk(
            [
                Entity(
                    id="task_src",
                    entity_type=EntityType.TASK,
                    name="Source task",
                    organization_id=client.group_id,
                ),
                Entity(
                    id="task_tgt",
                    entity_type=EntityType.TASK,
                    name="Target task",
                    organization_id=client.group_id,
                ),
            ]
        )

        # Mirror the add() / worker path: create_bulk(generate_embeddings=True),
        # which writes fact_embedding. create_bulk swallows write failures into a
        # (0, N) count, so a constraint violation silently drops every edge.
        created_count, failed_count = await relationship_manager.create_bulk(
            [
                Relationship(
                    id="rel_depends",
                    source_id="task_src",
                    target_id="task_tgt",
                    relationship_type=RelationshipType.DEPENDS_ON,
                )
            ]
        )
        created_ids = ["rel_depends"] if created_count else []

        outgoing = await relationship_manager.get_for_entity("task_src", direction="outgoing")
        incoming = await relationship_manager.get_for_entity("task_tgt", direction="incoming")
        fetched = await relationship_manager.get("rel_depends")
        # get_related_entities is the graph-traversal read the REST entity route
        # uses; it must surface the bulk-written edge (the entity GET `related`).
        related_src = await relationship_manager.get_related_entities("task_src")
        related_tgt = await relationship_manager.get_related_entities("task_tgt")
    finally:
        await client.close()

    assert (created_count, failed_count) == (1, 0)
    assert created_ids == ["rel_depends"]
    assert [rel.id for rel in outgoing] == ["rel_depends"]
    assert [rel.id for rel in incoming] == ["rel_depends"]
    assert fetched.source_id == "task_src"
    assert fetched.target_id == "task_tgt"
    assert fetched.relationship_type == RelationshipType.DEPENDS_ON
    assert {rel.id for _entity, rel in related_src} == {"rel_depends"}
    assert {rel.id for _entity, rel in related_tgt} == {"rel_depends"}


@pytest.mark.asyncio
async def test_graph_delete_transaction_rolls_back_after_mid_transaction_error() -> None:
    client = SurrealGraphClient(group_id="org-native-delete-rollback", url="memory://")
    try:
        await prepare_graph_schema(client)
        entity_manager = EntityManager(client, group_id=client.group_id)
        relationship_manager = RelationshipManager(client, group_id=client.group_id)

        await entity_manager.create_direct_bulk(
            [
                Entity(
                    id="rollback_src",
                    entity_type=EntityType.TOPIC,
                    name="Rollback Source",
                    organization_id=client.group_id,
                ),
                Entity(
                    id="rollback_tgt",
                    entity_type=EntityType.TOPIC,
                    name="Rollback Target",
                    organization_id=client.group_id,
                ),
            ]
        )
        await relationship_manager.create_direct_bulk(
            [
                Relationship(
                    id="rel_rollback",
                    source_id="rollback_src",
                    target_id="rollback_tgt",
                    relationship_type=RelationshipType.RELATED_TO,
                )
            ]
        )

        before = normalize_records(
            await client.execute_query(
                "SELECT uuid FROM relates_to WHERE uuid = $uuid;",
                uuid="rel_rollback",
            )
        )

        with pytest.raises(SurrealQueryError):
            await _execute_graph_transaction(
                client,
                """
                BEGIN TRANSACTION;
                DELETE FROM relates_to
                WHERE group_id = $group_id AND uuid = $relationship_id
                RETURN BEFORE;
                CREATE entity SET
                    uuid = $source_id,
                    name = 'Duplicate Source',
                    entity_type = 'topic',
                    group_id = $group_id;
                COMMIT TRANSACTION;
                """,
                group_id=client.group_id,
                relationship_id="rel_rollback",
                source_id="rollback_src",
            )

        after = normalize_records(
            await client.execute_query(
                "SELECT uuid FROM relates_to WHERE uuid = $uuid;",
                uuid="rel_rollback",
            )
        )
    finally:
        await client.close()

    assert [row["uuid"] for row in before] == ["rel_rollback"]
    assert [row["uuid"] for row in after] == ["rel_rollback"]


@pytest.mark.asyncio
async def test_native_project_summary_sorts_critical_tasks_by_priority() -> None:
    entity_manager = EntityManager(cast(Any, object()), group_id="org-native-graph")
    entity_manager.list_by_type = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            Entity(
                id="task-high",
                entity_type=EntityType.TASK,
                name="High task",
                description="",
                organization_id="org-native-graph",
                metadata={
                    "project_id": "project_native",
                    "status": "todo",
                    "priority": "high",
                },
            ),
            Entity(
                id="task-critical",
                entity_type=EntityType.TASK,
                name="Critical task",
                description="",
                organization_id="org-native-graph",
                metadata={
                    "project_id": "project_native",
                    "status": "todo",
                    "priority": "critical",
                },
            ),
        ]
    )
    entity_manager.list_epics_for_project = AsyncMock(return_value=[])  # type: ignore[method-assign]

    summary = await entity_manager.get_project_summary("project_native")

    assert [task["id"] for task in summary["critical_tasks"]] == [
        "task-critical",
        "task-high",
    ]


@pytest.mark.asyncio
async def test_native_relationship_manager_generates_fact_embeddings() -> None:
    client = _EmbeddingWriteClient()
    provider = _deterministic_provider()
    manager = RelationshipManager(
        cast(SurrealGraphClient, client),
        group_id=client.group_id,
        embedding_provider=provider,
    )

    created_id = await manager.create(
        Relationship(
            id="rel_embed",
            source_id="source_entity",
            target_id="target_entity",
            relationship_type=RelationshipType.RELATED_TO,
            metadata={
                "fact": "Source relates to target through native embeddings.",
                "fact_embedding": [],
            },
        )
    )

    assert created_id == "rel_embed"
    write_params = client.calls[2][1]
    assert len(cast(list[float], write_params["fact_embedding"])) == 4
    attributes = cast(dict[str, object], write_params["attributes"])
    assert attributes["embedding_metadata"] == provider.metadata.to_dict()
    assert "fact_embedding" not in attributes


@pytest.mark.asyncio
async def test_native_relationship_delete_runs_raw_transaction() -> None:
    client = _TransactionDeleteClient()
    manager = RelationshipManager(
        cast("SurrealGraphClient", client),
        group_id=client.group_id,
    )

    deleted = await manager.delete("rel_delete")

    assert deleted is True
    assert len(client.calls) == 1
    query, params = client.calls[0]
    assert "BEGIN TRANSACTION;" in query
    assert "DELETE FROM relates_to" in query
    assert "DELETE FROM mentions" in query
    assert "DELETE FROM entity" not in query
    assert "COMMIT TRANSACTION;" in query
    assert params == {"group_id": client.group_id, "uuid": "rel_delete"}


def test_entity_from_surreal_row_hydrates_legacy_shaped_rows() -> None:
    entity = entity_from_surreal_row(
        {
            "id": "legacy_procedure",
            "name": "Legacy Procedure",
            "labels": ["Entity", "Procedure"],
            "summary": "Legacy summary",
            "attributes": {
                "metadata": {
                    "category": None,
                    "project_id": "project_legacy",
                    "source_ids": ["graphiti:episode_1"],
                },
                "created_by": "legacy-agent",
            },
            "created_at": "2026-05-13T12:00:00+00:00",
        }
    )

    assert isinstance(entity, Procedure)
    assert entity.id == "legacy_procedure"
    assert entity.entity_type == EntityType.PROCEDURE
    assert entity.description == "Legacy summary"
    assert entity.content == "Legacy summary"
    assert entity.category == ""
    assert entity.created_by == "legacy-agent"
    assert entity.metadata["project_id"] == "project_legacy"
    assert entity.metadata["source_ids"] == ["graphiti:episode_1"]


def test_normalize_records_preserves_surreal_record_id_without_leaking_id() -> None:
    records = normalize_records(
        {
            "id": "entity:procedure_native",
            "uuid": "procedure_native",
            "name": "Native Procedure",
        }
    )

    assert records == [
        {
            "record_id": "entity:procedure_native",
            "uuid": "procedure_native",
            "name": "Native Procedure",
        }
    ]


def test_relationship_from_surreal_row_preserves_temporal_provenance() -> None:
    relationship = relationship_from_surreal_row(
        {
            "id": "relates_to:rel_native",
            "uuid": "rel_native",
            "name": "SUPPORTS",
            "fact": "Task supports the plan",
            "group_id": "org-native",
            "source_uuid": "task_native",
            "target_uuid": "plan_native",
            "fact_embedding": [0.25] * EMBEDDING_DIM,
            "project_id": "project_native",
            "source_ids": ["raw_1", "raw_2"],
            "confidence": 0.87,
            "valid_at": "2026-05-13T12:00:00+00:00",
            "invalid_at": "2026-05-14T12:00:00+00:00",
            "expired_at": "2026-05-15T12:00:00+00:00",
            "created_by": "stef",
            "modified_by": "nova",
            "direction": "outgoing",
            "episodes": ["episode_1"],
            "attributes": {
                "metadata": json.dumps({"weight": 0.42, "project_id": "nested_project"}),
            },
            "created_at": "2026-05-13T12:30:00+00:00",
        }
    )

    assert relationship.id == "rel_native"
    assert relationship.relationship_type is RelationshipType.SUPPORTS
    assert relationship.source_id == "task_native"
    assert relationship.target_id == "plan_native"
    assert relationship.weight == 0.42
    assert relationship.metadata["fact"] == "Task supports the plan"
    assert relationship.metadata["fact_embedding"] == [0.25] * EMBEDDING_DIM
    assert relationship.metadata["project_id"] == "nested_project"
    assert relationship.metadata["source_ids"] == ["raw_1", "raw_2"]
    assert relationship.metadata["confidence"] == 0.87
    assert relationship.metadata["valid_at"] == "2026-05-13T12:00:00+00:00"
    assert relationship.metadata["invalid_at"] == "2026-05-14T12:00:00+00:00"
    assert relationship.metadata["expired_at"] == "2026-05-15T12:00:00+00:00"
    assert relationship.metadata["created_by"] == "stef"
    assert relationship.metadata["modified_by"] == "nova"
    assert relationship.metadata["direction"] == "outgoing"
    assert relationship.metadata["episodes"] == ["episode_1"]
    assert relationship.metadata["record_id"] == "relates_to:rel_native"


@pytest.mark.asyncio
async def test_native_entity_lists_order_by_updated_at_before_created_at() -> None:
    client = SurrealGraphClient(group_id="org-native-ordering", url="memory://")
    try:
        await prepare_graph_schema(client)
        entity_manager = EntityManager(client, group_id=client.group_id)

        await entity_manager.create_direct(
            Entity(
                id="task_created_newer",
                entity_type=EntityType.TASK,
                name="Created newer",
                organization_id=client.group_id,
                created_at=datetime(2026, 5, 14, tzinfo=UTC),
                metadata={
                    "status": "todo",
                    "updated_at": "2026-05-13T12:00:00+00:00",
                },
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="task_updated_newer",
                entity_type=EntityType.TASK,
                name="Updated newer",
                organization_id=client.group_id,
                created_at=datetime(2026, 5, 13, tzinfo=UTC),
                metadata={
                    "status": "todo",
                    "updated_at": "2026-05-15T12:00:00+00:00",
                },
            )
        )

        typed = await entity_manager.list_by_type(
            EntityType.TASK,
            include_archived=True,
            limit=2,
        )
        all_entities = await entity_manager.list_all(include_archived=True, limit=2)

        assert [entity.id for entity in typed] == [
            "task_updated_newer",
            "task_created_newer",
        ]
        assert [entity.id for entity in all_entities] == [
            "task_updated_newer",
            "task_created_newer",
        ]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_graph_migration_normalizes_legacy_updated_at_values() -> None:
    client = SurrealGraphClient(group_id="org-native-updated-at-migration", url="memory://")
    try:
        legacy_definitions = NODE_DEFINITIONS.replace(
            "DEFINE FIELD IF NOT EXISTS updated_at ON entity TYPE option<datetime>;",
            "DEFINE FIELD IF NOT EXISTS updated_at ON entity TYPE option<string>;",
        ).replace(
            "DEFINE FIELD IF NOT EXISTS created_by ON entity TYPE option<string>;\n"
            "DEFINE FIELD IF NOT EXISTS modified_by ON entity TYPE option<string>;\n",
            "",
        )
        await client.execute_query(
            render_fulltext_compatible_sql(
                ANALYZER_DEFINITIONS + "\n" + legacy_definitions,
                url="memory://",
            )
        )
        await client.execute_query(
            """
            CREATE entity:legacy_string SET
                uuid = 'legacy_string',
                name = 'Legacy String',
                entity_type = 'task',
                labels = [],
                attributes = {
                    status: 'todo',
                    created_by: 'legacy-user',
                    modified_by: 'legacy-agent',
                },
                group_id = $group_id,
                created_at = d'2026-05-10T12:00:00Z',
                updated_at = '2026-05-15T12:00:00+00:00';
            CREATE entity:malformed_string SET
                uuid = 'malformed_string',
                name = 'Malformed String',
                entity_type = 'task',
                labels = [],
                attributes = {
                    status: 'todo',
                    created_by: 42,
                    modified_by: { agent: 'legacy-agent' },
                },
                group_id = $group_id,
                created_at = d'2026-05-14T12:00:00Z',
                updated_at = 'not-a-date';
            CREATE entity:missing_updated SET
                uuid = 'missing_updated',
                name = 'Missing Updated',
                entity_type = 'task',
                labels = [],
                attributes = { status: 'todo' },
                group_id = $group_id,
                created_at = d'2026-05-13T12:00:00Z',
                updated_at = NONE;
            """,
            group_id=client.group_id,
        )
        await ensure_schema_version_table(client.execute_query, group_id=client.group_id)
        await record_schema_version(
            client.execute_query,
            version=4,
            migrations=(),
            name=GRAPH_SCHEMA_NAME,
        )

        await bootstrap_schema(client)
        entity_manager = EntityManager(client, group_id=client.group_id)
        await entity_manager.create_direct(
            Entity(
                id="native_datetime",
                entity_type=EntityType.TASK,
                name="Native Datetime",
                organization_id=client.group_id,
                created_at=datetime(2026, 5, 11, tzinfo=UTC),
                updated_at=datetime(2026, 5, 16, 12, tzinfo=UTC),
                metadata={"status": "todo"},
            )
        )

        rows = normalize_records(
            await client.execute_query(
                """
                SELECT uuid, updated_at, created_by, modified_by
                FROM entity
                WHERE group_id = $group_id;
                """,
                group_id=client.group_id,
            )
        )
        by_uuid = {str(row["uuid"]): row.get("updated_at") for row in rows}
        assert by_uuid["legacy_string"] == datetime(2026, 5, 15, 12, tzinfo=UTC)
        assert by_uuid["malformed_string"] is None
        assert by_uuid["missing_updated"] is None
        legacy_row = next(row for row in rows if row.get("uuid") == "legacy_string")
        malformed_row = next(row for row in rows if row.get("uuid") == "malformed_string")
        assert legacy_row["created_by"] == "legacy-user"
        assert legacy_row["modified_by"] == "legacy-agent"
        assert malformed_row.get("created_by") is None
        assert malformed_row.get("modified_by") is None

        listed = await entity_manager.list_by_type(
            EntityType.TASK,
            include_archived=True,
            limit=4,
        )

        assert [entity.id for entity in listed] == [
            "native_datetime",
            "legacy_string",
            "malformed_string",
            "missing_updated",
        ]
        assert await get_schema_version(client.execute_query) == GRAPH_SCHEMA_CURRENT_VERSION
    finally:
        await client.close()


@pytest.mark.parametrize("schema_version", [9, 13])
@pytest.mark.asyncio
async def test_graph_migration_repairs_partial_required_fields(schema_version: int) -> None:
    client = SurrealGraphClient(
        group_id=f"org-required-field-repair-{schema_version}",
        url="memory://",
    )
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
            DEFINE FIELD OVERWRITE revision ON entity TYPE int DEFAULT 1 ASSERT $value >= 1;
            DEFINE FIELD OVERWRITE retrieval_count ON entity TYPE int DEFAULT 0;
            DEFINE FIELD OVERWRITE citation_count ON entity TYPE int DEFAULT 0;
            DEFINE FIELD OVERWRITE misled_count ON entity TYPE int DEFAULT 0;
            """,
            group_id=client.group_id,
        )
        await record_schema_version(
            client.execute_query,
            version=schema_version,
            migrations=(),
            name=GRAPH_SCHEMA_NAME,
        )

        await bootstrap_schema(client)

        rows = normalize_records(
            await client.execute_query(
                """
                SELECT revision, retrieval_count, citation_count, misled_count
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
            }
        ]
        assert await get_schema_version(client.execute_query) == GRAPH_SCHEMA_CURRENT_VERSION
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_graph_bootstrap_backfills_unversioned_legacy_required_fields() -> None:
    client = SurrealGraphClient(group_id="org-unversioned-required-fields", url="memory://")
    try:
        await client.execute_query(
            """
            DEFINE TABLE entity SCHEMALESS;
            CREATE entity:unversioned_required_fields SET
                uuid = 'unversioned_required_fields',
                name = 'Unversioned Required Fields',
                entity_type = 'pattern',
                labels = [],
                attributes = {
                    retrieval_count: 11,
                    citation_count: 7,
                    misled_count: 2,
                },
                group_id = $group_id,
                created_at = time::now();
            """,
            group_id=client.group_id,
        )

        await bootstrap_schema(client)

        rows = normalize_records(
            await client.execute_query(
                """
                SELECT revision, retrieval_count, citation_count, misled_count
                FROM entity:unversioned_required_fields;
                """
            )
        )
        assert rows == [
            {
                "revision": 1,
                "retrieval_count": 11,
                "citation_count": 7,
                "misled_count": 2,
            }
        ]
        assert await get_schema_version(client.execute_query) == GRAPH_SCHEMA_CURRENT_VERSION
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_graph_migration_rejects_invalid_entity_type_values() -> None:
    client = SurrealGraphClient(group_id="org-native-entity-type-migration", url="memory://")
    try:
        await client.execute_query(
            render_fulltext_compatible_sql(
                ANALYZER_DEFINITIONS + "\n" + NODE_DEFINITIONS,
                url="memory://",
            )
        )
        await client.execute_query(
            """
            CREATE entity:bad_type SET
                uuid = 'bad_type',
                name = 'Bad Type',
                entity_type = 'spell',
                labels = [],
                attributes = {},
                group_id = $group_id,
                created_at = time::now();
            """,
            group_id=client.group_id,
        )
        await ensure_schema_version_table(client.execute_query, group_id=client.group_id)
        await record_schema_version(
            client.execute_query,
            version=7,
            migrations=(),
            name=GRAPH_SCHEMA_NAME,
        )

        with pytest.raises(RuntimeError, match=r"entity\.entity_type enum assertion"):
            await bootstrap_schema(client)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_graph_migration_normalizes_memory_quality_metadata() -> None:
    client = SurrealGraphClient(group_id="org-quality-migration", url="memory://")
    try:
        await bootstrap_schema(client)
        entity_manager = EntityManager(client, group_id=client.group_id)
        relationship_manager = RelationshipManager(client, group_id=client.group_id)
        await entity_manager.create_direct(
            Entity(
                id="quality_source",
                entity_type=EntityType.EPISODE,
                name="Quality source",
                organization_id=client.group_id,
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="quality_target",
                entity_type=EntityType.TOPIC,
                name="Quality target",
                organization_id=client.group_id,
            )
        )
        await relationship_manager.create(
            Relationship(
                id="quality_relation",
                source_id="quality_source",
                target_id="quality_target",
                relationship_type=RelationshipType.RELATED_TO,
            )
        )
        await relationship_manager.create(
            Relationship(
                id="quality_string_relation",
                source_id="quality_target",
                target_id="quality_source",
                relationship_type=RelationshipType.RELATED_TO,
            )
        )
        await client.execute_query(
            """
            UPDATE entity SET
                attributes.retention_importance = 'bogus',
                attributes.importance = 0.2,
                attributes.promotion_confidence = false,
                attributes.reflection_confidence = 0.7,
                attributes.confidence = 0.1
            WHERE uuid = 'quality_source';
            UPDATE relates_to SET
                attributes.projection_confidence = 0.6,
                attributes.confidence = 0.1
            WHERE uuid = 'quality_relation';
            UPDATE entity SET
                attributes.importance = 'bogus',
                attributes.memory_importance = '0.8',
                attributes.confidence = 'bogus',
                attributes.share_confidence = '0.45'
            WHERE uuid = 'quality_target';
            UPDATE relates_to SET
                attributes.importance = 'bogus',
                attributes.memory_importance = '0.65',
                attributes.confidence = 'bogus',
                attributes.share_confidence = '0.35'
            WHERE uuid = 'quality_string_relation';
            """
        )
        await record_schema_version(
            client.execute_query,
            version=10,
            migrations=(),
            name=GRAPH_SCHEMA_NAME,
        )

        await bootstrap_schema(client)

        entity_rows = normalize_records(
            await client.execute_query(
                "SELECT attributes FROM entity WHERE uuid = 'quality_source' LIMIT 1;"
            )
        )
        relation_rows = normalize_records(
            await client.execute_query(
                "SELECT attributes FROM relates_to WHERE uuid = 'quality_relation' LIMIT 1;"
            )
        )
        string_entity_rows = normalize_records(
            await client.execute_query(
                "SELECT attributes FROM entity WHERE uuid = 'quality_target' LIMIT 1;"
            )
        )
        string_relation_rows = normalize_records(
            await client.execute_query(
                "SELECT attributes FROM relates_to WHERE uuid = 'quality_string_relation' LIMIT 1;"
            )
        )
        entity_metadata = entity_rows[0]["attributes"]
        relation_metadata = relation_rows[0]["attributes"]
        string_entity_metadata = string_entity_rows[0]["attributes"]
        string_relation_metadata = string_relation_rows[0]["attributes"]
        assert entity_metadata["importance"] == 0.2
        assert entity_metadata["confidence"] == 0.7
        assert relation_metadata["confidence"] == 0.6
        assert entity_metadata["retention_importance"] == 0.2
        assert entity_metadata["reflection_confidence"] == 0.7
        assert relation_metadata["projection_confidence"] == 0.6
        assert string_entity_metadata["importance"] == "bogus"
        assert string_entity_metadata["memory_importance"] == "0.8"
        assert string_entity_metadata["confidence"] == "bogus"
        assert string_entity_metadata["share_confidence"] == "0.45"
        assert string_relation_metadata["importance"] == "bogus"
        assert string_relation_metadata["memory_importance"] == "0.65"
        assert string_relation_metadata["confidence"] == "bogus"
        assert string_relation_metadata["share_confidence"] == "0.35"

        loaded_entity = await entity_manager.get("quality_source")
        loaded_string_entity = await entity_manager.get("quality_target")
        loaded_relationships = await relationship_manager.get_for_entity(
            "quality_source",
            direction="outgoing",
        )
        loaded_string_relationships = await relationship_manager.get_for_entity(
            "quality_target",
            direction="outgoing",
        )
        assert loaded_entity is not None
        assert loaded_string_entity is not None
        assert loaded_entity.metadata["importance"] == 0.2
        assert loaded_entity.metadata["confidence"] == 0.7
        assert loaded_relationships[0].metadata["confidence"] == 0.6
        assert loaded_string_entity.metadata["importance"] == 0.8
        assert loaded_string_entity.metadata["confidence"] == 0.45
        assert loaded_string_relationships[0].metadata["importance"] == 0.65
        assert loaded_string_relationships[0].metadata["confidence"] == 0.35
        for legacy_key in (
            "retention_importance",
            "memory_importance",
            "promotion_confidence",
            "reflection_confidence",
            "projection_confidence",
            "share_confidence",
        ):
            assert legacy_key not in loaded_entity.metadata
            assert legacy_key not in loaded_relationships[0].metadata
            assert legacy_key not in loaded_string_entity.metadata
            assert legacy_key not in loaded_string_relationships[0].metadata
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_native_entity_lists_can_omit_heavy_content_fields() -> None:
    client = SurrealGraphClient(group_id="org-native-light-list", url="memory://")
    try:
        await prepare_graph_schema(client)
        entity_manager = EntityManager(client, group_id=client.group_id)

        await entity_manager.create_direct(
            Entity(
                id="task_heavy",
                entity_type=EntityType.TASK,
                name="Heavy task",
                description="Small preview",
                content="x" * 10000,
                organization_id=client.group_id,
                embedding=[0.2] * EMBEDDING_DIM,
                created_at=datetime(2026, 5, 14, tzinfo=UTC),
            )
        )

        entities = await entity_manager.list_by_type(
            EntityType.TASK,
            include_archived=True,
            include_content=False,
            limit=1,
        )

        assert entities[0].content == "Small preview"
        assert entities[0].embedding is None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_native_entity_manager_counts_by_type_without_listing_entities() -> None:
    client = SurrealGraphClient(group_id="org-native-counts", url="memory://")
    try:
        await prepare_graph_schema(client)
        entity_manager = EntityManager(client, group_id=client.group_id)

        await entity_manager.create_direct(
            Entity(
                id="pattern_count",
                entity_type=EntityType.PATTERN,
                name="Pattern count",
                organization_id=client.group_id,
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="task_count",
                entity_type=EntityType.TASK,
                name="Task count",
                organization_id=client.group_id,
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="task_count_archived",
                entity_type=EntityType.TASK,
                name="Archived task count",
                organization_id=client.group_id,
                metadata={"status": "archived"},
            )
        )

        active_counts = await entity_manager.count_by_type()
        all_counts = await entity_manager.count_by_type(include_archived=True)

        assert active_counts["pattern"] == 1
        assert active_counts["task"] == 1
        assert all_counts["task"] == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_hierarchical_graph_uses_managers() -> None:
    import sibyl_core.services.graph_communities as communities

    client = SurrealGraphClient(group_id="org-native-hierarchy", url="memory://")
    try:
        await prepare_graph_schema(client)
        entity_manager = EntityManager(client, group_id=client.group_id)
        relationship_manager = RelationshipManager(client, group_id=client.group_id)

        await entity_manager.create_direct(
            Entity(
                id="task_hierarchy",
                entity_type=EntityType.TASK,
                name="Hierarchy task",
                description="Task node",
                content="x" * 10000,
                organization_id=client.group_id,
                created_at=datetime(2026, 5, 14, tzinfo=UTC),
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="topic_hierarchy",
                entity_type=EntityType.TOPIC,
                name="Hierarchy topic",
                description="Topic node",
                organization_id=client.group_id,
                created_at=datetime(2026, 5, 14, tzinfo=UTC),
            )
        )
        await relationship_manager.create(
            Relationship(
                id="rel_hierarchy",
                source_id="task_hierarchy",
                target_id="topic_hierarchy",
                relationship_type=RelationshipType.RELATED_TO,
            )
        )

        communities.GRAPH_SNAPSHOT_CACHE.clear()
        communities.HIERARCHICAL_CACHE.clear()
        communities.GRAPH_LOD_CACHE.clear()

        data = await communities.get_hierarchical_graph(
            client,
            client.group_id,
            max_nodes=10,
            max_edges=10,
        )

        assert {node["id"] for node in data.nodes} == {
            "task_hierarchy",
            "topic_hierarchy",
        }
        assert data.displayed_edges == 1
        assert data.total_nodes == 2
        assert data.total_edges == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_graph_filters_recheck_metadata_only_denormalized_fields() -> None:
    client = SurrealGraphClient(group_id="org-native-legacy-filters", url="memory://")
    try:
        await prepare_graph_schema(client)
        entity_manager = EntityManager(client, group_id=client.group_id)

        await entity_manager.create_direct(
            Entity(
                id="task_legacy_metadata_only",
                entity_type=EntityType.TASK,
                name="Legacy metadata-only task",
                organization_id=client.group_id,
                metadata={
                    "project_id": "project_legacy",
                    "status": "doing",
                    "priority": "high",
                    "complexity": "simple",
                    "feature": "legacy",
                },
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="task_legacy_archived_metadata_only",
                entity_type=EntityType.TASK,
                name="Legacy archived metadata-only task",
                organization_id=client.group_id,
                metadata={
                    "project_id": "project_legacy",
                    "status": "archived",
                    "priority": "high",
                    "complexity": "simple",
                    "feature": "legacy",
                },
            )
        )

        for entity_id in (
            "task_legacy_metadata_only",
            "task_legacy_archived_metadata_only",
        ):
            await client.execute_query(
                """
                UPDATE entity SET
                    project_id = NONE,
                    status = NONE,
                    priority = NONE,
                    complexity = NONE,
                    feature = NONE,
                    attributes.project_id = NONE,
                    attributes.status = NONE,
                    attributes.priority = NONE,
                    attributes.complexity = NONE,
                    attributes.feature = NONE
                WHERE uuid = $uuid;
                """,
                uuid=entity_id,
            )

        filtered = await entity_manager.list_by_type(
            EntityType.TASK,
            project_id="project_legacy",
            status="doing",
            priority="high",
            complexity="simple",
            feature="legacy",
        )
        visible_ids = {
            entity.id for entity in await entity_manager.list_all(include_archived=False)
        }

        assert [entity.id for entity in filtered] == ["task_legacy_metadata_only"]
        assert "task_legacy_archived_metadata_only" not in visible_ids
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_native_relationship_manager_batches_related_entity_lookup() -> None:
    client = SurrealGraphClient(group_id="org-native-batch-related", url="memory://")
    try:
        await prepare_graph_schema(client)
        entity_manager = EntityManager(client, group_id=client.group_id)
        relationship_manager = RelationshipManager(client, group_id=client.group_id)

        for entity_id, entity_type in (
            ("task_seed_a", EntityType.TASK),
            ("task_seed_b", EntityType.TASK),
            ("topic_target", EntityType.TOPIC),
            ("pattern_target", EntityType.PATTERN),
        ):
            await entity_manager.create_direct(
                Entity(
                    id=entity_id,
                    entity_type=entity_type,
                    name=entity_id.replace("_", " ").title(),
                    organization_id=client.group_id,
                    metadata={"status": "todo"},
                )
            )

        created, failed = await relationship_manager.create_bulk(
            [
                Relationship(
                    id="rel_seed_a_topic",
                    source_id="task_seed_a",
                    target_id="topic_target",
                    relationship_type=RelationshipType.RELATED_TO,
                ),
                Relationship(
                    id="rel_pattern_seed_a",
                    source_id="pattern_target",
                    target_id="task_seed_a",
                    relationship_type=RelationshipType.RELATED_TO,
                ),
                Relationship(
                    id="rel_seed_b_topic",
                    source_id="task_seed_b",
                    target_id="topic_target",
                    relationship_type=RelationshipType.RELATED_TO,
                ),
            ]
        )

        assert (created, failed) == (3, 0)

        related = await relationship_manager.get_related_entities_batch(
            ["task_seed_a", "task_seed_b"],
            limit_per_entity=10,
        )

        seed_a = related["task_seed_a"]
        seed_b = related["task_seed_b"]
        assert {entity.id for entity, _ in seed_a} == {"topic_target", "pattern_target"}
        assert {entity.id for entity, _ in seed_b} == {"topic_target"}
        directions_by_id = {
            relationship.id: relationship.metadata["direction"] for _, relationship in seed_a
        }
        assert directions_by_id == {
            "rel_seed_a_topic": "outgoing",
            "rel_pattern_seed_a": "incoming",
        }
    finally:
        await client.close()


class _RelationshipBulkWriteClient:
    group_id = "org-native"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
        self.calls.append((query, params))
        if "AS record_id" in query and "FROM entity" in query:
            uuids = cast("list[str]", params["uuids"])
            return [{"uuid": uuid, "record_id": f"entity:{uuid}"} for uuid in uuids]
        return []


@pytest.mark.asyncio
async def test_native_relationship_bulk_writes_in_one_surreal_query() -> None:
    client = _RelationshipBulkWriteClient()
    provider = _deterministic_provider()
    manager = RelationshipManager(
        cast("SurrealGraphClient", client),
        group_id=client.group_id,
        embedding_provider=provider,
    )

    relationships = [
        Relationship(
            id=f"rel_bulk_{index}",
            source_id=f"source_{index}",
            target_id=f"target_{index}",
            relationship_type=RelationshipType.RELATED_TO,
        )
        for index in range(5)
    ]

    created_ids = await manager.create_direct_bulk(relationships, generate_embeddings=True)

    assert created_ids == [relationship.id for relationship in relationships]

    write_calls = [
        call for call in client.calls if "INSERT RELATION INTO relates_to $rows" in call[0]
    ]
    endpoint_lookups = [
        call for call in client.calls if "AS record_id" in call[0] and "FROM entity" in call[0]
    ]
    assert len(write_calls) == 1
    assert len(endpoint_lookups) == 1

    _, write_params = write_calls[0]
    rows = cast("list[dict[str, object]]", write_params["rows"])
    assert len(rows) == len(relationships)
    assert write_params["uuids"] == [relationship.id for relationship in relationships]
    assert set(cast("dict[str, object]", write_params["sources"])) == set(write_params["uuids"])
    assert set(cast("dict[str, object]", write_params["targets"])) == set(write_params["uuids"])
    assert all(row["group_id"] == client.group_id for row in rows)
    assert all("in" in row and "out" in row for row in rows)
    assert {str(row["uuid"]) for row in rows} == {relationship.id for relationship in relationships}
    for row in rows:
        assert len(cast("list[float]", row["fact_embedding"])) == 4
        attributes = cast("dict[str, object]", row["attributes"])
        assert attributes["embedding_metadata"] == provider.metadata.to_dict()
        assert "fact_embedding" not in attributes


@pytest.mark.asyncio
async def test_native_relationship_bulk_skips_edges_with_missing_endpoints() -> None:
    client = SurrealGraphClient(group_id="org-native-bulk-rel", url="memory://")
    try:
        await prepare_graph_schema(client)
        entity_manager = EntityManager(client, group_id=client.group_id)
        relationship_manager = RelationshipManager(client, group_id=client.group_id)

        for entity_id in ("alpha", "beta", "gamma"):
            await entity_manager.create_direct(
                Entity(
                    id=entity_id,
                    entity_type=EntityType.TOPIC,
                    name=entity_id.title(),
                    organization_id=client.group_id,
                )
            )

        created_ids = await relationship_manager.create_direct_bulk(
            [
                Relationship(
                    id="rel_alpha_beta",
                    source_id="alpha",
                    target_id="beta",
                    relationship_type=RelationshipType.RELATED_TO,
                ),
                Relationship(
                    id="rel_alpha_gamma",
                    source_id="alpha",
                    target_id="gamma",
                    relationship_type=RelationshipType.RELATED_TO,
                ),
                Relationship(
                    id="rel_dangling",
                    source_id="alpha",
                    target_id="missing_entity",
                    relationship_type=RelationshipType.RELATED_TO,
                ),
            ]
        )

        rows = normalize_records(
            await client.execute_query(
                """
                SELECT uuid, source_id, target_id, in.uuid AS in_uuid, out.uuid AS out_uuid
                FROM relates_to
                WHERE group_id = $group_id
                ORDER BY uuid ASC;
                """,
                group_id=client.group_id,
            )
        )
    finally:
        await client.close()

    assert created_ids == ["rel_alpha_beta", "rel_alpha_gamma"]
    assert [row["uuid"] for row in rows] == ["rel_alpha_beta", "rel_alpha_gamma"]
    assert all(row["source_id"] == "alpha" for row in rows)
    assert {row["target_id"] for row in rows} == {"beta", "gamma"}
    assert all(row["in_uuid"] == "alpha" for row in rows)
    assert {row["out_uuid"] for row in rows} == {"beta", "gamma"}


@pytest.mark.asyncio
async def test_native_relationship_bulk_replaces_changed_endpoints() -> None:
    client = SurrealGraphClient(group_id="org-native-bulk-rel-rewire", url="memory://")
    try:
        await prepare_graph_schema(client)
        entity_manager = EntityManager(client, group_id=client.group_id)
        relationship_manager = RelationshipManager(client, group_id=client.group_id)

        for entity_id in ("alpha", "beta", "gamma"):
            await entity_manager.create_direct(
                Entity(
                    id=entity_id,
                    entity_type=EntityType.TOPIC,
                    name=entity_id.title(),
                    organization_id=client.group_id,
                )
            )

        await relationship_manager.create_direct_bulk(
            [
                Relationship(
                    id="rel_rewired",
                    source_id="alpha",
                    target_id="beta",
                    relationship_type=RelationshipType.RELATED_TO,
                )
            ]
        )
        await relationship_manager.create_direct_bulk(
            [
                Relationship(
                    id="rel_rewired",
                    source_id="gamma",
                    target_id="beta",
                    relationship_type=RelationshipType.BLOCKS,
                )
            ]
        )

        rows = normalize_records(
            await client.execute_query(
                """
                SELECT uuid, name, source_id, target_id, in.uuid AS in_uuid, out.uuid AS out_uuid
                FROM relates_to
                WHERE uuid = 'rel_rewired';
                """
            )
        )
    finally:
        await client.close()

    assert rows == [
        {
            "uuid": "rel_rewired",
            "name": "BLOCKS",
            "source_id": "gamma",
            "target_id": "beta",
            "in_uuid": "gamma",
            "out_uuid": "beta",
        }
    ]


@pytest.mark.asyncio
async def test_native_relationship_delete_bulk_removes_edges() -> None:
    client = SurrealGraphClient(group_id="org-native-bulk-rel-delete", url="memory://")
    try:
        await prepare_graph_schema(client)
        entity_manager = EntityManager(client, group_id=client.group_id)
        relationship_manager = RelationshipManager(client, group_id=client.group_id)

        for entity_id in ("alpha", "beta", "gamma"):
            await entity_manager.create_direct(
                Entity(
                    id=entity_id,
                    entity_type=EntityType.TOPIC,
                    name=entity_id.title(),
                    organization_id=client.group_id,
                )
            )
        await relationship_manager.create_direct_bulk(
            [
                Relationship(
                    id="rel_alpha_beta",
                    source_id="alpha",
                    target_id="beta",
                    relationship_type=RelationshipType.RELATED_TO,
                ),
                Relationship(
                    id="rel_alpha_gamma",
                    source_id="alpha",
                    target_id="gamma",
                    relationship_type=RelationshipType.RELATED_TO,
                ),
            ]
        )

        deleted = await relationship_manager.delete_bulk(
            ["rel_alpha_beta", "rel_alpha_gamma", "missing_rel"]
        )
        rows = normalize_records(
            await client.execute_query(
                """
                SELECT uuid
                FROM relates_to
                WHERE group_id = $group_id
                ORDER BY uuid ASC;
                """,
                group_id=client.group_id,
            )
        )
    finally:
        await client.close()

    assert deleted == 2
    assert rows == []


@pytest.mark.asyncio
async def test_graph_migration_backfills_relationship_endpoint_mirrors() -> None:
    client = SurrealGraphClient(group_id="org-native-endpoint-migration", url="memory://")
    try:
        await prepare_graph_schema(client)
        entity_manager = EntityManager(client, group_id=client.group_id)
        relationship_manager = RelationshipManager(client, group_id=client.group_id)
        for entity_id in ("alpha", "beta"):
            await entity_manager.create_direct(
                Entity(
                    id=entity_id,
                    entity_type=EntityType.TOPIC,
                    name=entity_id.title(),
                    organization_id=client.group_id,
                )
            )
        await relationship_manager.create_direct_bulk(
            [
                Relationship(
                    id="rel_alpha_beta",
                    source_id="alpha",
                    target_id="beta",
                    relationship_type=RelationshipType.RELATED_TO,
                )
            ]
        )
        await client.execute_query(
            """
            UPDATE relates_to SET source_id = 'stale-alpha', target_id = 'stale-beta'
            WHERE uuid = 'rel_alpha_beta';
            """,
        )
        await record_schema_version(
            client.execute_query,
            version=5,
            migrations=(),
            name=GRAPH_SCHEMA_NAME,
        )

        await bootstrap_schema(client)

        rows = normalize_records(
            await client.execute_query(
                """
                SELECT uuid, source_id, target_id
                FROM relates_to
                WHERE uuid = 'rel_alpha_beta';
                """,
            )
        )
        related = await relationship_manager.get_for_entity("alpha", direction="outgoing")

        assert rows == [{"uuid": "rel_alpha_beta", "source_id": "alpha", "target_id": "beta"}]
        assert [relationship.target_id for relationship in related] == ["beta"]
        assert await get_schema_version(client.execute_query) == GRAPH_SCHEMA_CURRENT_VERSION
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_native_relationship_batch_uses_native_traversal_projection() -> None:
    client = _RelatedBatchClient()
    relationship_manager = RelationshipManager(
        cast("SurrealGraphClient", client),
        group_id=client.group_id,
    )

    related = await relationship_manager.get_related_entities_batch(
        ["seed-a", "seed-b"],
        limit_per_entity=3,
    )

    relates_queries = [call for call in client.calls if "FROM relates_to" in call[0]]
    entity_queries = [call for call in client.calls if "FROM entity" in call[0]]
    assert len(relates_queries) == 4
    assert entity_queries == []
    assert all("= $entity_id" in query for query, _ in relates_queries)
    assert {params["entity_id"] for _, params in relates_queries} == {"seed-a", "seed-b"}
    assert all(params["limit"] == 3 for _, params in relates_queries)
    assert any("out.uuid AS related_uuid" in query for query, _ in relates_queries)
    assert any("in.uuid AS related_uuid" in query for query, _ in relates_queries)

    assert [entity.id for entity, _ in related["seed-a"]] == [
        "target-a",
        "seed-b",
        "source-a",
    ]
    assert [entity.id for entity, _ in related["seed-b"]] == ["seed-a"]
    assert [relationship.id for _, relationship in related["seed-b"]] == ["rel_seed_a_seed_b"]


@pytest.mark.asyncio
async def test_native_relationship_batch_reads_directions_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _RelatedBatchClient()
    relationship_manager = RelationshipManager(
        cast("SurrealGraphClient", client),
        group_id=client.group_id,
    )
    started: set[str] = set()
    both_started = asyncio.Event()

    async def coordinated_direction_rows(*_: object, **kwargs: object) -> list[dict[str, object]]:
        started.add(cast("str", kwargs["endpoint_field"]))
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=0.5)
        return []

    monkeypatch.setattr(
        relationship_manager,
        "_get_native_related_entity_direction_rows",
        coordinated_direction_rows,
    )

    related = await relationship_manager.get_related_entities_batch(["seed-a"])

    assert started == {"source_id", "target_id"}
    assert related == {"seed-a": []}


@pytest.mark.asyncio
async def test_native_relationship_batch_applies_limit_per_seed_with_indexed_queries() -> None:
    client = _CappedRelatedBatchClient()
    relationship_manager = RelationshipManager(
        cast("SurrealGraphClient", client),
        group_id=client.group_id,
    )

    related = await relationship_manager.get_related_entities_batch(
        ["seed-a", "seed-b"],
        limit_per_entity=2,
    )

    relates_queries = [call for call in client.calls if "FROM relates_to" in call[0]]
    entity_queries = [call for call in client.calls if "FROM entity" in call[0]]
    assert len(relates_queries) == 4
    assert entity_queries == []
    assert all("= $entity_id" in query for query, _ in relates_queries)
    assert all(params["limit"] == 2 for _, params in relates_queries)
    assert [entity.id for entity, _ in related["seed-a"]] == ["target-0", "target-1"]
    assert [entity.id for entity, _ in related["seed-b"]] == ["target-b"]


@pytest.mark.asyncio
async def test_graph_writes_entities_and_relationships() -> None:
    client = SurrealGraphClient(group_id="org-native-graph", url="memory://")
    try:
        await prepare_graph_schema(client)
        entity_manager = EntityManager(client, group_id=client.group_id)
        relationship_manager = RelationshipManager(client, group_id=client.group_id)

        await entity_manager.create_direct(
            Entity(
                id="project_native",
                entity_type=EntityType.PROJECT,
                name="Native Project",
                description="Project anchor",
                organization_id="org-native-graph",
                metadata={"project_id": "project_native", "tags": ["native"]},
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="decision_native",
                entity_type=EntityType.DECISION,
                name="Native Decision",
                description="Graphiti-free decision",
                content="Native graph writes should not import Graphiti.",
                organization_id="org-native-graph",
                source_file="raw_123",
                metadata={
                    "project_id": "project_native",
                    "source_ids": ["raw_123"],
                    "status": "doing",
                },
            )
        )
        created, failed = await relationship_manager.create_bulk(
            [
                Relationship(
                    id="rel_decision_project",
                    source_id="decision_native",
                    target_id="project_native",
                    relationship_type=RelationshipType.BELONGS_TO,
                    metadata={
                        "native_write_path": "test",
                        "source_ids": ["raw_123"],
                        "confidence": 0.82,
                        "valid_at": "2026-05-13T12:00:00+00:00",
                        "invalid_at": "2026-05-14T12:00:00+00:00",
                    },
                )
            ]
        )

        assert (created, failed) == (1, 0)
        rows = normalize_records(
            await client.execute_query(
                """
                SELECT uuid, name, entity_type, project_id, attributes
                FROM entity
                WHERE uuid = "decision_native";
                """
            )
        )
        assert rows[0]["project_id"] == "project_native"
        attributes = cast("dict[str, object]", rows[0]["attributes"])
        assert attributes["source_file"] == "raw_123"
        assert attributes["metadata"]

        relationships = normalize_records(
            await client.execute_query(
                """
                SELECT uuid,
                       name,
                       in.uuid AS source_uuid,
                       out.uuid AS target_uuid,
                       attributes,
                       valid_at,
                       invalid_at
                FROM relates_to
                WHERE uuid = "rel_decision_project";
                """
            )
        )
        assert len(relationships) == 1
        relationship_row = relationships[0]
        assert relationship_row["uuid"] == "rel_decision_project"
        assert relationship_row["name"] == "BELONGS_TO"
        assert relationship_row["source_uuid"] == "decision_native"
        assert relationship_row["target_uuid"] == "project_native"
        assert relationship_row["attributes"] == {
            "native_write_path": "test",
            "source_ids": ["raw_123"],
            "confidence": 0.82,
            "promotion_confidence": 0.82,
            "reflection_confidence": 0.82,
            "projection_confidence": 0.82,
            "share_confidence": 0.82,
            "valid_at": "2026-05-13T12:00:00+00:00",
            "invalid_at": "2026-05-14T12:00:00+00:00",
        }
        assert relationship_row["valid_at"] == datetime(2026, 5, 13, 12, tzinfo=UTC)
        assert relationship_row["invalid_at"] == datetime(2026, 5, 14, 12, tzinfo=UTC)

        updated = await entity_manager.update(
            "decision_native",
            {"status": "done", "title": "Updated Native Decision"},
        )
        assert updated is not None
        assert updated.name == "Updated Native Decision"
        assert updated.metadata["status"] == "done"
        assert updated.metadata["source_ids"] == ["raw_123"]

        fetched_relationships = await relationship_manager.get_for_entity(
            "decision_native",
            relationship_types=[RelationshipType.BELONGS_TO],
            direction="outgoing",
        )
        assert [rel.target_id for rel in fetched_relationships] == ["project_native"]
        assert fetched_relationships[0].metadata["source_ids"] == ["raw_123"]
        assert fetched_relationships[0].metadata["confidence"] == 0.82
        assert fetched_relationships[0].metadata["direction"] == "outgoing"
        assert fetched_relationships[0].metadata["valid_at"]
        assert fetched_relationships[0].metadata["invalid_at"]
        matched_relationships = await relationship_manager.find_between(
            "decision_native",
            "project_native",
            relationship_type=RelationshipType.BELONGS_TO,
        )
        assert [rel.id for rel in matched_relationships] == ["rel_decision_project"]
        assert matched_relationships[0].metadata["source_ids"] == ["raw_123"]

        deleted = await relationship_manager.delete_between(
            "decision_native",
            "project_native",
            RelationshipType.BELONGS_TO,
        )
        assert deleted == 1

        search_results = await entity_manager.search(
            query="Updated Native Decision",
            entity_types=[EntityType.DECISION],
        )
        assert search_results
        assert all(0.0 <= score <= 1.0 for _, score in search_results)

        await entity_manager.create_direct(
            Entity(
                id="epic_native",
                entity_type=EntityType.EPIC,
                name="Native epic",
                description="Epic with task progress",
                organization_id="org-native-graph",
                metadata={
                    "project_id": "project_native",
                    "status": "planning",
                    "total_tasks": 0,
                    "completed_tasks": 0,
                },
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="epic_native_two",
                entity_type=EntityType.EPIC,
                name="Native blocked epic",
                description="Epic with non-terminal task progress",
                organization_id="org-native-graph",
                metadata={
                    "project_id": "project_native",
                    "status": "blocked",
                    "total_tasks": 0,
                    "completed_tasks": 0,
                },
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="epic_native_empty",
                entity_type=EntityType.EPIC,
                name="Native empty epic",
                description="Epic with no tasks",
                organization_id="org-native-graph",
                metadata={
                    "project_id": "project_native",
                    "status": "completed",
                    "total_tasks": 9,
                    "completed_tasks": 9,
                },
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="task_native_one",
                entity_type=EntityType.TASK,
                name="Native filtered task",
                description="Task with every native list filter",
                organization_id="org-native-graph",
                metadata={
                    "project_id": "project_native",
                    "epic_id": "epic_native",
                    "status": "todo",
                    "priority": "high",
                    "complexity": "complex",
                    "feature": "surreal",
                    "tags": ["native", "graph"],
                },
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="task_native_done",
                entity_type=EntityType.TASK,
                name="Native completed task",
                description="Done task attached to an epic",
                organization_id="org-native-graph",
                metadata={
                    "project_id": "project_native",
                    "epic_id": "epic_native",
                    "status": "done",
                    "priority": "medium",
                    "complexity": "simple",
                    "feature": "surreal",
                    "tags": ["native"],
                },
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="task_native_doing",
                entity_type=EntityType.TASK,
                name="Native active task",
                description="Doing task attached to an epic",
                organization_id="org-native-graph",
                metadata={
                    "project_id": "project_native",
                    "epic_id": "epic_native",
                    "status": "doing",
                    "priority": "medium",
                    "complexity": "simple",
                    "feature": "surreal",
                    "tags": ["native"],
                },
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="task_native_review",
                entity_type=EntityType.TASK,
                name="Native review task",
                description="Review task attached to another epic",
                organization_id="org-native-graph",
                metadata={
                    "project_id": "project_native",
                    "epic_id": "epic_native_two",
                    "status": "review",
                    "priority": "medium",
                    "complexity": "simple",
                    "feature": "surreal",
                    "tags": ["native"],
                },
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="task_native_blocked",
                entity_type=EntityType.TASK,
                name="Native blocked task",
                description="Blocked task attached to another epic",
                organization_id="org-native-graph",
                metadata={
                    "project_id": "project_native",
                    "epic_id": "epic_native_two",
                    "status": "blocked",
                    "priority": "medium",
                    "complexity": "simple",
                    "feature": "surreal",
                    "tags": ["native"],
                },
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="task_native_archived_epic",
                entity_type=EntityType.TASK,
                name="Native archived epic task",
                description="Archived task attached to another epic",
                organization_id="org-native-graph",
                metadata={
                    "project_id": "project_native",
                    "epic_id": "epic_native_two",
                    "status": "archived",
                    "priority": "medium",
                    "complexity": "simple",
                    "feature": "surreal",
                    "tags": ["native"],
                },
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="task_native_two",
                entity_type=EntityType.TASK,
                name="Native unepic task",
                description="Task without an epic",
                organization_id="org-native-graph",
                metadata={
                    "project_id": "project_native",
                    "status": "doing",
                    "priority": "medium",
                    "complexity": "simple",
                    "feature": "surreal",
                    "tags": ["native"],
                },
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="task_native_archived",
                entity_type=EntityType.TASK,
                name="Native archived task",
                description="Archived task hidden by default",
                organization_id="org-native-graph",
                metadata={
                    "project_id": "project_native",
                    "status": "archived",
                    "tags": ["native"],
                },
            )
        )

        fetched_task = await entity_manager.get("task_native_one")
        assert "metadata" not in fetched_task.metadata
        assert fetched_task.metadata["status"] == "todo"
        assert fetched_task.metadata["project_id"] == "project_native"

        filtered = await entity_manager.list_by_type(
            EntityType.TASK,
            project_id="project_native",
            epic_id="epic_native",
            status="todo,done",
            priority="high",
            complexity="complex",
            feature="surreal",
            tags=["graph"],
            include_archived=False,
        )
        assert [entity.id for entity in filtered] == ["task_native_one"]

        no_epic = await entity_manager.list_by_type(
            EntityType.TASK,
            project_id="project_native",
            no_epic=True,
        )
        assert [entity.id for entity in no_epic] == ["task_native_two"]

        archived = await entity_manager.list_by_type(
            EntityType.TASK,
            project_id="project_native",
            include_archived=True,
        )
        assert {entity.id for entity in archived} == {
            "task_native_one",
            "task_native_done",
            "task_native_doing",
            "task_native_review",
            "task_native_blocked",
            "task_native_archived_epic",
            "task_native_two",
            "task_native_archived",
        }

        progress = await entity_manager.get_epic_progress("epic_native")
        assert progress["total_tasks"] == 3
        assert progress["completed_tasks"] == 1
        assert progress["in_progress_tasks"] == 1
        assert progress["completion_pct"] == 33.3

        epics = await entity_manager.list_by_type(
            EntityType.EPIC,
            project_id="project_native",
            enrich_epic_progress=True,
        )
        epics_by_id = {epic.id: epic for epic in epics}
        assert epics_by_id["epic_native"].metadata["total_tasks"] == 3
        assert epics_by_id["epic_native"].metadata["completed_tasks"] == 1
        assert epics_by_id["epic_native"].metadata["in_progress_tasks"] == 1
        assert epics_by_id["epic_native"].metadata["completion_pct"] == 33.3
        assert epics_by_id["epic_native_two"].metadata["total_tasks"] == 3
        assert epics_by_id["epic_native_two"].metadata["completed_tasks"] == 0
        assert epics_by_id["epic_native_two"].metadata["blocked_tasks"] == 1
        assert epics_by_id["epic_native_two"].metadata["in_review_tasks"] == 1
        assert epics_by_id["epic_native_two"].metadata["completion_pct"] == 0.0
        assert epics_by_id["epic_native_empty"].metadata["total_tasks"] == 0
        assert epics_by_id["epic_native_empty"].metadata["completed_tasks"] == 0
        assert epics_by_id["epic_native_empty"].metadata["completion_pct"] == 0

        planning_epics = await entity_manager.list_by_type(
            EntityType.EPIC,
            project_id="project_native",
            status="planning",
            enrich_epic_progress=True,
        )
        assert [epic.id for epic in planning_epics] == ["epic_native"]

        summary = await entity_manager.get_project_summary("project_native", epic_limit=10)
        summary_epics = {epic["id"]: epic for epic in summary["epics"]}
        assert summary_epics["epic_native"]["total_tasks"] == 3
        assert summary_epics["epic_native"]["progress_pct"] == 33.3
        assert summary_epics["epic_native_two"]["total_tasks"] == 3
        assert summary_epics["epic_native_empty"]["total_tasks"] == 0

        visible_ids = {
            entity.id for entity in await entity_manager.list_all(include_archived=False)
        }
        assert "decision_native" in visible_ids
        assert "task_native_archived" not in visible_ids
        assert "task_native_archived_epic" not in visible_ids

        embedding = [0.2] * EMBEDDING_DIM
        await entity_manager.create_direct(
            Entity(
                id="dedup_keep",
                entity_type=EntityType.PATTERN,
                name="Native duplicate pattern",
                description="Kept native dedup entity",
                organization_id="org-native-graph",
                metadata={"source": "keep"},
                embedding=embedding,
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="dedup_remove",
                entity_type=EntityType.PATTERN,
                name="Native duplicate pattern",
                description="Removed native dedup entity",
                organization_id="org-native-graph",
                metadata={"source": "remove", "merged": True},
                embedding=embedding,
            )
        )
        await entity_manager.create_direct(
            Entity(
                id="dedup_neighbor",
                entity_type=EntityType.DECISION,
                name="Native dedup neighbor",
                description="Relationship target for merge redirect",
                organization_id="org-native-graph",
            )
        )
        await relationship_manager.create(
            Relationship(
                id="rel_dedup_remove_neighbor",
                source_id="dedup_remove",
                target_id="dedup_neighbor",
                relationship_type=RelationshipType.RELATED_TO,
            )
        )

        deduplicator = EntityDeduplicator(
            client=client,
            entity_manager=entity_manager,
        )
        assert await deduplicator.merge_entities(
            keep_id="dedup_keep",
            remove_id="dedup_remove",
        )

        with pytest.raises(KeyError):
            await entity_manager.get("dedup_remove")

        redirected = await relationship_manager.get_for_entity(
            "dedup_keep",
            relationship_types=[RelationshipType.RELATED_TO],
            direction="outgoing",
        )
        assert [rel.target_id for rel in redirected] == ["dedup_neighbor"]
        assert await relationship_manager.get_for_entity("dedup_remove", direction="both") == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_entity_update_recomputes_summary_after_server_side_merge() -> None:
    client = SurrealGraphClient(group_id="org-native-summary-update", url="memory://")
    try:
        await prepare_graph_schema(client)
        entity_manager = EntityManager(client, group_id=client.group_id)

        await entity_manager.create_direct(
            Entity(
                id="summary_native",
                entity_type=EntityType.TOPIC,
                name="Original Summary Name",
                description="",
                organization_id=client.group_id,
            )
        )
        created = await entity_manager.get("summary_native")
        assert created.revision == 1

        renamed = await entity_manager.update(
            "summary_native",
            {"title": "Renamed Summary Name", "modified_by": "nova"},
            expected_revision=1,
        )

        assert renamed is not None
        assert renamed.revision == 2
        assert renamed.name == "Renamed Summary Name"
        assert renamed.description == "Renamed Summary Name"
        assert renamed.modified_by == "nova"

        with pytest.raises(RevisionConflictError) as stale_update:
            await entity_manager.update(
                "summary_native",
                {"description": "Stale overwrite"},
                expected_revision=1,
            )
        assert stale_update.value.details == {
            "identifier": "summary_native",
            "expected_revision": 1,
            "actual_revision": 2,
        }

        long_description = "Detailed summary source " * 30
        described = await entity_manager.update(
            "summary_native",
            {"description": long_description},
            expected_revision=2,
        )

        assert described is not None
        assert described.revision == 3
        assert described.description == long_description.strip()

        rows = normalize_records(
            await client.execute_query(
                """
                SELECT summary
                FROM entity
                WHERE group_id = $group_id AND uuid = "summary_native"
                LIMIT 1;
                """,
                group_id=client.group_id,
            )
        )
        assert rows[0]["summary"] == long_description[:500]

        cleared = await entity_manager.update(
            "summary_native",
            {"description": ""},
        )

        assert cleared is not None
        assert cleared.revision == 4
        assert cleared.description == "Renamed Summary Name"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_native_parent_task_id_round_trips_and_lists_subtasks() -> None:
    client = SurrealGraphClient(group_id="org-native-subtasks", url="memory://")
    try:
        await prepare_graph_schema(client)
        entity_manager = EntityManager(client, group_id=client.group_id)

        await entity_manager.create_direct(
            Entity(
                id="parent_task",
                entity_type=EntityType.TASK,
                name="Parent work item",
                description="A task that acts as an epic",
                organization_id="org-native-subtasks",
                metadata={"project_id": "project_native", "status": "doing"},
            )
        )
        for child_id, status in (("child_one", "todo"), ("child_two", "done")):
            await entity_manager.create_direct(
                Entity(
                    id=child_id,
                    entity_type=EntityType.TASK,
                    name=f"Subtask {child_id}",
                    description="Child of parent_task",
                    organization_id="org-native-subtasks",
                    metadata={
                        "project_id": "project_native",
                        "parent_task_id": "parent_task",
                        "epic_id": "epic_legacy",
                        "status": status,
                    },
                )
            )
        await entity_manager.create_direct(
            Entity(
                id="orphan_task",
                entity_type=EntityType.TASK,
                name="Unrelated task",
                description="Has no parent task",
                organization_id="org-native-subtasks",
                metadata={"project_id": "project_native", "status": "todo"},
            )
        )

        # parent_task_id survives the promoted-column round trip.
        fetched_child = await entity_manager.get("child_one")
        assert fetched_child.metadata["parent_task_id"] == "parent_task"
        assert fetched_child.metadata["epic_id"] == "epic_legacy"

        subtasks = await entity_manager.list_subtasks("parent_task")
        assert {entity.id for entity in subtasks} == {"child_one", "child_two"}

        # Status filter still composes; orphan is excluded from child listings.
        todo_children = await entity_manager.list_subtasks("parent_task", status="todo")
        assert [entity.id for entity in todo_children] == ["child_one"]

        # epic_id filtering is unchanged: both children remain reachable by their epic.
        epic_tasks = await entity_manager.list_by_type(
            EntityType.TASK,
            epic_id="epic_legacy",
            include_archived=True,
        )
        assert {entity.id for entity in epic_tasks} == {"child_one", "child_two"}

        # The parent itself (no parent_task_id) is not its own child.
        assert "parent_task" not in {entity.id for entity in subtasks}
        assert "orphan_task" not in {entity.id for entity in subtasks}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_native_derive_epic_from_task_projects_children() -> None:
    client = SurrealGraphClient(group_id="org-native-derive-epic", url="memory://")
    try:
        await prepare_graph_schema(client)
        entity_manager = EntityManager(client, group_id=client.group_id)

        await entity_manager.create_direct(
            Entity(
                id="parent_epic",
                entity_type=EntityType.TASK,
                name="Parent work item",
                description="A task that acts as an epic",
                organization_id="org-native-derive-epic",
                metadata={
                    "title": "Parent work item",
                    "project_id": "project_native",
                    "status": "todo",
                    "priority": "high",
                    "assignees": ["alice"],
                    "tags": ["backend"],
                },
            )
        )
        for child_id, status in (
            ("child_done", "done"),
            ("child_doing", "doing"),
            ("child_todo", "todo"),
        ):
            await entity_manager.create_direct(
                Entity(
                    id=child_id,
                    entity_type=EntityType.TASK,
                    name=f"Subtask {child_id}",
                    description="Child of parent_epic",
                    organization_id="org-native-derive-epic",
                    metadata={
                        "project_id": "project_native",
                        "parent_task_id": "parent_epic",
                        "status": status,
                    },
                )
            )

        epic = await entity_manager.derive_epic_from_task("parent_epic")
        assert epic is not None
        assert epic.id == "parent_epic"
        assert epic.title == "Parent work item"
        assert epic.project_id == "project_native"
        assert epic.priority == TaskPriority.HIGH
        assert epic.assignees == ["alice"]
        assert epic.tags == ["backend"]
        # A DOING child makes the derived container IN_PROGRESS; one of three done.
        assert epic.status == EpicStatus.IN_PROGRESS
        assert epic.total_tasks == 3
        assert epic.completed_tasks == 1

        # A childless task still projects (planning, no progress yet).
        await entity_manager.create_direct(
            Entity(
                id="leaf_task",
                entity_type=EntityType.TASK,
                name="Leaf task",
                organization_id="org-native-derive-epic",
                metadata={"project_id": "project_native", "status": "todo"},
            )
        )
        leaf_epic = await entity_manager.derive_epic_from_task("leaf_task")
        assert leaf_epic is not None
        assert leaf_epic.status == EpicStatus.PLANNING
        assert leaf_epic.total_tasks == 0

        # Missing parent -> None, no raise.
        assert await entity_manager.derive_epic_from_task("does_not_exist") is None
    finally:
        await client.close()
