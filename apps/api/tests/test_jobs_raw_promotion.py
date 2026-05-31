from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from sibyl.jobs import raw_promotion
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
    scope_key: str | None = None,
    project_id: str | None = None,
) -> RawMemory:
    return RawMemory(
        id=raw_id or str(uuid4()),
        organization_id=organization_id or str(uuid4()),
        source_id=source_id,
        principal_id="user-1",
        memory_scope=memory_scope,
        scope_key=scope_key,
        project_id=project_id,
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
    assert created_entities[0][0].metadata["memory_scope"] == "organization"
    assert created_entities[0][0].metadata["principal_id"] == memory.principal_id
    assert created_entities[0][1] is False
    assert saved_memories[-1].entity_id == memory.id
    assert saved_memories[-1].metadata["raw_promotion_state"] == "promoted"
    assert saved_memories[-1].metadata["raw_promotion_chunk_count"] == len(saved_chunks)
    assert saved_memories[-1].metadata["source_extraction_state"] == "disabled"


@pytest.mark.asyncio
async def test_promote_raw_captures_skips_non_org_visible_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = _raw_memory(
        memory_scope=MemoryScope.PRIVATE,
        scope_key="user-1",
        project_id="secret-project",
    )
    saved_memories = []

    async def fake_save_memory(updated: RawMemory) -> RawMemory:
        saved_memories.append(updated)
        return updated

    monkeypatch.setattr(
        raw_promotion,
        "list_raw_memories_for_promotion",
        _list_memories([memory]),
    )
    monkeypatch.setattr(raw_promotion, "EmbeddingService", _fake_embedder)
    monkeypatch.setattr(raw_promotion, "save_raw_memory", fake_save_memory)

    result = await raw_promotion.promote_raw_captures({}, memory.organization_id)

    assert result["promoted_count"] == 0
    assert result["skipped_scoped_count"] == 1
    assert result["failed_count"] == 0
    assert saved_memories[-1].metadata["raw_promotion_state"] == "skipped_scoped"
    assert saved_memories[-1].entity_id is None


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
