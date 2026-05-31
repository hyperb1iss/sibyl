from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from sibyl.jobs import raw_promotion
from sibyl_core.models.entities import RelationshipType
from sibyl_core.models.sources import SourceImportManifest
from sibyl_core.services.source_adapters import build_source_record_id
from sibyl_core.services.surreal_content import MemoryScope, RawMemory


def _raw_memory(
    *,
    raw_id: str | None = None,
    organization_id: str | None = None,
    source_id: str = "source-record:abc",
    raw_content: str = "alpha beta gamma\n\nsecond paragraph",
    review_state: str = "pending",
    metadata: dict[str, object] | None = None,
    deleted_at: datetime | None = None,
    entity_id: str | None = None,
    memory_scope: MemoryScope = MemoryScope.ORGANIZATION,
) -> RawMemory:
    return RawMemory(
        id=raw_id or str(uuid4()),
        organization_id=organization_id or str(uuid4()),
        source_id=source_id,
        principal_id="user-1",
        memory_scope=memory_scope,
        review_state=review_state,
        entity_id=entity_id,
        title="Imported note",
        raw_content=raw_content,
        tags=["mail"],
        metadata=dict(metadata or {}),
        provenance={"source_uri": "mailbox://archive#1"},
        capture_surface="source_import",
        created_by_user_id="user-1",
        captured_at=datetime(2026, 5, 30, 12, 0, tzinfo=UTC),
        created_at=datetime(2026, 5, 30, 12, 0, tzinfo=UTC),
        deleted_at=deleted_at,
    )


class FakeEmbedder:
    async def embed_chunks(self, chunks):
        return [[float(index), 0.25, 0.5] for index, _chunk in enumerate(chunks)]


def _fake_embedder() -> FakeEmbedder:
    return FakeEmbedder()


def _list_memories(memories: list[RawMemory]):
    async def fake_list(**_kwargs):
        return memories

    return fake_list


def _source_record_id(adapter_record_id: str) -> str:
    return build_source_record_id(
        manifest=SourceImportManifest(
            adapter_name="claude_code_jsonl",
            adapter_version="1.0",
            source_identity="session-1",
        ),
        adapter_record_id=adapter_record_id,
    )


@pytest.mark.asyncio
async def test_promote_raw_captures_writes_chunks_and_graph_entity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = _raw_memory()
    saved_documents = []
    deleted_documents = []
    saved_chunks = []
    saved_memories = []
    created_entities = []

    @asynccontextmanager
    async def fake_session():
        yield None

    async def fake_save_document(_session, *, document):
        saved_documents.append(document)
        return document

    async def fake_delete_chunks(_session, *, document_id, organization_id):
        deleted_documents.append((document_id, organization_id))
        return 0

    async def fake_save_chunks(_session, *, chunks):
        saved_chunks.extend(chunks)
        return chunks

    async def fake_save_memory(updated: RawMemory) -> RawMemory:
        saved_memories.append(updated)
        return updated

    class FakeEntityManager:
        async def create_direct(self, entity, *, generate_embedding: bool = False):
            created_entities.append((entity, generate_embedding))
            return entity.id

    async def fake_graph_runtime(group_id: str):
        assert group_id == memory.organization_id
        return SimpleNamespace(entity_manager=FakeEntityManager())

    monkeypatch.setattr(
        raw_promotion,
        "list_raw_memories_for_promotion",
        _list_memories([memory]),
    )
    monkeypatch.setattr(raw_promotion, "EmbeddingService", _fake_embedder)
    monkeypatch.setattr(raw_promotion, "get_content_read_session", fake_session)
    monkeypatch.setattr(raw_promotion, "save_crawled_document_record", fake_save_document)
    monkeypatch.setattr(raw_promotion, "delete_document_chunks_for_document", fake_delete_chunks)
    monkeypatch.setattr(raw_promotion, "save_document_chunks", fake_save_chunks)
    monkeypatch.setattr(raw_promotion, "get_entity_graph_runtime", fake_graph_runtime)
    monkeypatch.setattr(raw_promotion, "save_raw_memory", fake_save_memory)
    monkeypatch.setattr(raw_promotion.settings, "auto_extract_entities", False)

    result = await raw_promotion.promote_raw_captures({}, memory.organization_id)

    assert result["promoted_count"] == 1
    assert result["failed_count"] == 0
    assert result["chunk_count"] == len(saved_chunks)
    assert saved_documents[0].id == UUID(memory.id)
    assert saved_documents[0].source_id == memory.source_id
    assert deleted_documents == [(UUID(memory.id), UUID(memory.organization_id))]
    assert saved_chunks
    assert all(chunk.document_id == UUID(memory.id) for chunk in saved_chunks)
    assert all(chunk.organization_id == UUID(memory.organization_id) for chunk in saved_chunks)
    assert all(chunk.source_id == memory.source_id for chunk in saved_chunks)
    assert all(chunk.embedding is not None for chunk in saved_chunks)
    assert created_entities[0][0].id == memory.id
    assert created_entities[0][0].entity_type.value == "document"
    assert created_entities[0][1] is False
    assert saved_memories[-1].entity_id == memory.id
    assert saved_memories[-1].metadata["raw_promotion_state"] == "promoted"
    assert saved_memories[-1].metadata["raw_promotion_chunk_count"] == len(saved_chunks)
    assert saved_memories[-1].metadata["source_extraction_state"] == "disabled"


@pytest.mark.asyncio
async def test_promote_raw_captures_skips_private_memories_without_materializing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = _raw_memory(memory_scope=MemoryScope.PRIVATE)

    async def fail_save_memory(_memory: RawMemory) -> RawMemory:
        raise AssertionError("private raw memories should not be marked or materialized")

    monkeypatch.setattr(
        raw_promotion,
        "list_raw_memories_for_promotion",
        _list_memories([memory]),
    )
    monkeypatch.setattr(raw_promotion, "EmbeddingService", _fake_embedder)
    monkeypatch.setattr(raw_promotion, "save_raw_memory", fail_save_memory)

    result = await raw_promotion.promote_raw_captures({}, memory.organization_id)

    assert result["promoted_count"] == 0
    assert result["skipped_scope_count"] == 1
    assert result["failed_count"] == 0
    assert result["document_ids"] == []


@pytest.mark.asyncio
async def test_promote_raw_captures_materializes_transcript_lineage_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_id = str(uuid4())
    parent_reply = _raw_memory(
        organization_id=organization_id,
        source_id=_source_record_id("session.jsonl:parent"),
        entity_id="parent-reply-entity",
    )
    parent_fork = _raw_memory(
        organization_id=organization_id,
        source_id=_source_record_id("session.jsonl:fork"),
        entity_id="parent-fork-entity",
    )
    parent_tool = _raw_memory(
        organization_id=organization_id,
        source_id=_source_record_id("session.jsonl:assistant"),
        entity_id="parent-tool-entity",
    )
    child = _raw_memory(
        organization_id=organization_id,
        source_id=_source_record_id("subagents/worker.jsonl:child"),
        metadata={
            "adapter_name": "claude_code_jsonl",
            "adapter_version": "1.0",
            "adapter_record_id": "subagents/worker.jsonl:child",
            "source_identity": "session-1",
            "source_record_metadata": {
                "forked_from": "fork",
                "forked_from_adapter_record_id": "session.jsonl:fork",
                "parent_adapter_record_id": "session.jsonl:parent",
                "parent_uuid": "parent",
                "source_tool_assistant_adapter_record_id": "session.jsonl:assistant",
                "source_tool_assistant_uuid": "assistant",
                "turn_uuid": "child",
            },
        },
    )
    raw_by_source = {
        parent_reply.source_id: parent_reply,
        parent_fork.source_id: parent_fork,
        parent_tool.source_id: parent_tool,
    }
    saved_memories = []
    created_relationships = []

    @asynccontextmanager
    async def fake_session():
        yield None

    async def fake_save_document(_session, *, document):
        return document

    async def fake_delete_chunks(_session, *, document_id, organization_id):
        return 0

    async def fake_save_chunks(_session, *, chunks):
        return chunks

    async def fake_save_memory(updated: RawMemory) -> RawMemory:
        saved_memories.append(updated)
        return updated

    async def fake_get_raw_by_source_id(*, organization_id: str, source_id: str, **_kwargs):
        assert organization_id == child.organization_id
        assert _kwargs["memory_scope"] is child.memory_scope
        assert _kwargs["scope_key"] == child.scope_key
        assert _kwargs["principal_id"] is None
        return raw_by_source.get(source_id)

    class FakeEntityManager:
        async def create_direct(self, entity, *, generate_embedding: bool = False):
            assert generate_embedding is False
            return entity.id

    class FakeRelationshipManager:
        async def create_direct_bulk(self, relationships, *, generate_embeddings: bool = False):
            assert generate_embeddings is False
            created_relationships.extend(relationships)
            return [relationship.id for relationship in relationships]

    async def fake_graph_runtime(_group_id: str):
        return SimpleNamespace(
            entity_manager=FakeEntityManager(),
            relationship_manager=FakeRelationshipManager(),
        )

    monkeypatch.setattr(
        raw_promotion,
        "list_raw_memories_for_promotion",
        _list_memories([child]),
    )
    monkeypatch.setattr(raw_promotion, "EmbeddingService", _fake_embedder)
    monkeypatch.setattr(raw_promotion, "get_content_read_session", fake_session)
    monkeypatch.setattr(raw_promotion, "save_crawled_document_record", fake_save_document)
    monkeypatch.setattr(raw_promotion, "delete_document_chunks_for_document", fake_delete_chunks)
    monkeypatch.setattr(raw_promotion, "save_document_chunks", fake_save_chunks)
    monkeypatch.setattr(raw_promotion, "get_entity_graph_runtime", fake_graph_runtime)
    monkeypatch.setattr(raw_promotion, "get_raw_memory_by_source_id", fake_get_raw_by_source_id)
    monkeypatch.setattr(raw_promotion, "save_raw_memory", fake_save_memory)
    monkeypatch.setattr(raw_promotion.settings, "auto_extract_entities", False)

    result = await raw_promotion.promote_raw_captures({}, child.organization_id)

    assert result["promoted_count"] == 1
    assert len(created_relationships) == 3
    by_type = {
        relationship.relationship_type: relationship for relationship in created_relationships
    }
    assert by_type[RelationshipType.REPLIES_TO].source_id == child.id
    assert by_type[RelationshipType.REPLIES_TO].target_id == "parent-reply-entity"
    assert by_type[RelationshipType.FORKED_FROM].source_id == child.id
    assert by_type[RelationshipType.FORKED_FROM].target_id == "parent-fork-entity"
    assert by_type[RelationshipType.SPAWNED_SUBAGENT].source_id == "parent-tool-entity"
    assert by_type[RelationshipType.SPAWNED_SUBAGENT].target_id == child.id
    assert saved_memories[-1].metadata["raw_promotion_lineage_edge_count"] == 3
    assert saved_memories[-1].metadata["raw_promotion_lineage_missing_count"] == 0


@pytest.mark.asyncio
async def test_promote_raw_captures_repairs_missing_lineage_for_existing_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_id = str(uuid4())
    parent = _raw_memory(
        organization_id=organization_id,
        source_id=_source_record_id("session.jsonl:parent"),
        entity_id="parent-entity",
    )
    child = _raw_memory(
        organization_id=organization_id,
        source_id=_source_record_id("session.jsonl:child"),
        entity_id="child-entity",
        metadata={
            "adapter_name": "claude_code_jsonl",
            "adapter_version": "1.0",
            "adapter_record_id": "session.jsonl:child",
            "raw_promotion_lineage_missing_count": 1,
            "raw_promotion_state": "promoted",
            "source_identity": "session-1",
            "source_record_metadata": {
                "parent_adapter_record_id": "session.jsonl:parent",
                "parent_uuid": "parent",
                "turn_uuid": "child",
            },
        },
    )
    saved_memories = []
    created_relationships = []

    async def fake_get_raw_by_source_id(*, source_id: str, **_kwargs):
        assert source_id == parent.source_id
        return parent

    async def fake_save_memory(updated: RawMemory) -> RawMemory:
        saved_memories.append(updated)
        return updated

    class FakeRelationshipManager:
        async def create_direct_bulk(self, relationships, *, generate_embeddings: bool = False):
            assert generate_embeddings is False
            created_relationships.extend(relationships)
            return [relationship.id for relationship in relationships]

    async def fake_graph_runtime(_group_id: str):
        return SimpleNamespace(relationship_manager=FakeRelationshipManager())

    monkeypatch.setattr(
        raw_promotion,
        "list_raw_memories_for_promotion",
        _list_memories([child]),
    )
    monkeypatch.setattr(raw_promotion, "EmbeddingService", _fake_embedder)
    monkeypatch.setattr(raw_promotion, "get_entity_graph_runtime", fake_graph_runtime)
    monkeypatch.setattr(raw_promotion, "get_raw_memory_by_source_id", fake_get_raw_by_source_id)
    monkeypatch.setattr(raw_promotion, "save_raw_memory", fake_save_memory)

    result = await raw_promotion.promote_raw_captures({}, child.organization_id)

    assert result["promoted_count"] == 0
    assert result["skipped_existing_count"] == 1
    assert len(created_relationships) == 1
    assert created_relationships[0].relationship_type is RelationshipType.REPLIES_TO
    assert saved_memories[-1].metadata["raw_promotion_lineage_edge_count"] == 1
    assert saved_memories[-1].metadata["raw_promotion_lineage_missing_count"] == 0


@pytest.mark.asyncio
async def test_promote_raw_captures_keeps_promotion_when_lineage_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_id = str(uuid4())
    parent = _raw_memory(
        organization_id=organization_id,
        source_id=_source_record_id("session.jsonl:parent"),
        entity_id="parent-entity",
    )
    child = _raw_memory(
        organization_id=organization_id,
        source_id=_source_record_id("session.jsonl:child"),
        metadata={
            "adapter_name": "claude_code_jsonl",
            "adapter_version": "1.0",
            "adapter_record_id": "session.jsonl:child",
            "source_identity": "session-1",
            "source_record_metadata": {
                "parent_adapter_record_id": "session.jsonl:parent",
                "parent_uuid": "parent",
                "turn_uuid": "child",
            },
        },
    )
    saved_memories = []

    @asynccontextmanager
    async def fake_session():
        yield None

    async def fake_save_document(_session, *, document):
        return document

    async def fake_delete_chunks(_session, *, document_id, organization_id):
        return 0

    async def fake_save_chunks(_session, *, chunks):
        return chunks

    async def fake_get_raw_by_source_id(*, source_id: str, **_kwargs):
        assert source_id == parent.source_id
        return parent

    async def fake_save_memory(updated: RawMemory) -> RawMemory:
        saved_memories.append(updated)
        return updated

    class FakeEntityManager:
        async def create_direct(self, entity, *, generate_embedding: bool = False):
            assert generate_embedding is False
            return entity.id

    class FakeRelationshipManager:
        async def create_direct_bulk(self, relationships, *, generate_embeddings: bool = False):
            assert relationships
            assert generate_embeddings is False
            raise RuntimeError("relationship backend unavailable")

    async def fake_graph_runtime(_group_id: str):
        return SimpleNamespace(
            entity_manager=FakeEntityManager(),
            relationship_manager=FakeRelationshipManager(),
        )

    monkeypatch.setattr(
        raw_promotion,
        "list_raw_memories_for_promotion",
        _list_memories([child]),
    )
    monkeypatch.setattr(raw_promotion, "EmbeddingService", _fake_embedder)
    monkeypatch.setattr(raw_promotion, "get_content_read_session", fake_session)
    monkeypatch.setattr(raw_promotion, "save_crawled_document_record", fake_save_document)
    monkeypatch.setattr(raw_promotion, "delete_document_chunks_for_document", fake_delete_chunks)
    monkeypatch.setattr(raw_promotion, "save_document_chunks", fake_save_chunks)
    monkeypatch.setattr(raw_promotion, "get_entity_graph_runtime", fake_graph_runtime)
    monkeypatch.setattr(raw_promotion, "get_raw_memory_by_source_id", fake_get_raw_by_source_id)
    monkeypatch.setattr(raw_promotion, "save_raw_memory", fake_save_memory)
    monkeypatch.setattr(raw_promotion.settings, "auto_extract_entities", False)

    result = await raw_promotion.promote_raw_captures({}, child.organization_id)

    assert result["promoted_count"] == 1
    assert result["failed_count"] == 0
    assert saved_memories[-1].metadata["raw_promotion_state"] == "promoted"
    assert saved_memories[-1].metadata["raw_promotion_lineage_edge_count"] == 0
    assert saved_memories[-1].metadata["raw_promotion_lineage_missing_count"] == 1
    assert (
        "relationship backend unavailable"
        in saved_memories[-1].metadata["raw_promotion_lineage_error"]
    )


@pytest.mark.asyncio
async def test_promote_raw_captures_enqueues_extraction_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = _raw_memory(raw_content="SurrealDB extraction should see this imported note.")
    extraction_calls = []
    saved_memories = []

    @asynccontextmanager
    async def fake_session():
        yield None

    async def fake_save_document(_session, *, document):
        return document

    async def fake_delete_chunks(_session, *, document_id, organization_id):
        return 0

    async def fake_save_chunks(_session, *, chunks):
        return chunks

    async def fake_save_memory(updated: RawMemory) -> RawMemory:
        saved_memories.append(updated)
        return updated

    async def fake_enqueue(sources_data, group_id, *, created_source_ids=None):
        extraction_calls.append((sources_data, group_id, created_source_ids))
        return SimpleNamespace(
            status="queued",
            job_ids=("extract-1",),
            queued_sources=1,
            skipped_sources=0,
            queue_depth=0,
            reason=None,
        )

    class FakeEntityManager:
        async def create_direct(self, entity, *, generate_embedding: bool = False):
            return entity.id

    async def fake_graph_runtime(_group_id: str):
        return SimpleNamespace(entity_manager=FakeEntityManager())

    monkeypatch.setattr(
        raw_promotion,
        "list_raw_memories_for_promotion",
        _list_memories([memory]),
    )
    monkeypatch.setattr(raw_promotion, "EmbeddingService", _fake_embedder)
    monkeypatch.setattr(raw_promotion, "get_content_read_session", fake_session)
    monkeypatch.setattr(raw_promotion, "save_crawled_document_record", fake_save_document)
    monkeypatch.setattr(raw_promotion, "delete_document_chunks_for_document", fake_delete_chunks)
    monkeypatch.setattr(raw_promotion, "save_document_chunks", fake_save_chunks)
    monkeypatch.setattr(raw_promotion, "get_entity_graph_runtime", fake_graph_runtime)
    monkeypatch.setattr(raw_promotion, "save_raw_memory", fake_save_memory)
    monkeypatch.setattr(raw_promotion, "enqueue_memory_extraction_batches", fake_enqueue)
    monkeypatch.setattr(raw_promotion.settings, "auto_extract_entities", True)

    result = await raw_promotion.promote_raw_captures({}, memory.organization_id)

    assert result["promoted_count"] == 1
    assert len(extraction_calls) == 1
    sources_data, group_id, created_source_ids = extraction_calls[0]
    assert group_id == memory.organization_id
    assert created_source_ids == [memory.id]
    assert sources_data[0]["entity_type"] == "document"
    assert sources_data[0]["content"] == memory.raw_content
    assert sources_data[0]["metadata"]["raw_memory_id"] == memory.id
    assert saved_memories[-1].metadata["source_extraction_state"] == "queued"
    assert saved_memories[-1].metadata["source_extraction_job_ids"] == ["extract-1"]


@pytest.mark.asyncio
async def test_promote_raw_captures_skips_existing_without_rewriting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = _raw_memory(
        metadata={"raw_promotion_state": "promoted"},
        entity_id="document-entity",
    )

    async def fail_save_memory(_memory: RawMemory) -> RawMemory:
        raise AssertionError("already promoted captures should not be rewritten")

    monkeypatch.setattr(
        raw_promotion,
        "list_raw_memories_for_promotion",
        _list_memories([memory]),
    )
    monkeypatch.setattr(raw_promotion, "EmbeddingService", _fake_embedder)
    monkeypatch.setattr(raw_promotion, "save_raw_memory", fail_save_memory)

    result = await raw_promotion.promote_raw_captures({}, memory.organization_id)

    assert result["promoted_count"] == 0
    assert result["skipped_existing_count"] == 1
    assert result["failed_count"] == 0


@pytest.mark.asyncio
async def test_promote_raw_captures_skips_deleted_and_superseded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted = _raw_memory(deleted_at=datetime(2026, 5, 30, 12, 30, tzinfo=UTC))
    superseded = _raw_memory(
        metadata={"superseded_by_raw_memory_id": "replacement"},
    )
    saved_statuses = []

    async def fake_save_memory(memory: RawMemory) -> RawMemory:
        saved_statuses.append(memory.metadata["raw_promotion_state"])
        return memory

    monkeypatch.setattr(
        raw_promotion,
        "list_raw_memories_for_promotion",
        _list_memories([deleted, superseded]),
    )
    monkeypatch.setattr(raw_promotion, "EmbeddingService", _fake_embedder)
    monkeypatch.setattr(raw_promotion, "save_raw_memory", fake_save_memory)

    result = await raw_promotion.promote_raw_captures({}, deleted.organization_id)

    assert result["promoted_count"] == 0
    assert result["skipped_deleted_count"] == 1
    assert result["skipped_superseded_count"] == 1
    assert saved_statuses == ["skipped_deleted", "skipped_superseded"]
