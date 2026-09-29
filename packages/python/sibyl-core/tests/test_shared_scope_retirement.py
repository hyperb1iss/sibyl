"""Retired scope writes fail before embedding and at the persistence boundary."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from sibyl_core.backends.surreal.content_client import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.models.memory_scope import MemoryScope
from sibyl_core.services import content_client, content_models, content_raw_persistence
from sibyl_core.services.content_models import RawMemory, RawMemoryWrite


@pytest.fixture
async def shared_retirement_store(monkeypatch: pytest.MonkeyPatch):
    client = SurrealContentClient(url="memory://", namespace=f"retirement_{uuid4().hex}")
    await bootstrap_content_schema(client)

    async def shared_client():
        return client

    monkeypatch.setattr(content_client, "get_shared_surreal_content_client", shared_client)
    try:
        yield client
    finally:
        await client.close()


def legacy_shared_memory() -> RawMemory:
    return RawMemory(
        id=str(uuid4()),
        organization_id=str(uuid4()),
        source_id="legacy:shared",
        principal_id=str(uuid4()),
        memory_scope=MemoryScope.SHARED,
        scope_key=str(uuid4()),
        title="Retained shared capture",
        raw_content="Keep the original content and source identity.",
        captured_at=content_models.utcnow(),
        created_at=content_models.utcnow(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("bulk", [False, True])
async def test_shared_new_capture_refused_before_embedding(monkeypatch, bulk):
    def forbidden_provider():
        raise AssertionError("retired scope reached embedding setup")

    monkeypatch.setattr(
        content_models, "configured_raw_memory_embedding_provider", forbidden_provider
    )
    write = RawMemoryWrite(
        organization_id="org",
        principal_id="writer",
        source_id="manual",
        raw_content="Retired scope",
        memory_scope=MemoryScope.SHARED,
        scope_key="team",
    )
    with pytest.raises(ValueError, match="shared memory scope is retired"):
        if bulk:
            await content_raw_persistence.remember_raw_memories([write])
        else:
            await content_raw_persistence.remember_raw_memory(
                organization_id=write.organization_id,
                principal_id=write.principal_id,
                source_id=write.source_id,
                raw_content=write.raw_content,
                memory_scope=write.memory_scope,
                scope_key=write.scope_key,
            )


@pytest.mark.asyncio
async def test_shared_bulk_insert_refused_in_storage_transaction(shared_retirement_store):
    memory = legacy_shared_memory()
    with pytest.raises(RuntimeError):
        await content_raw_persistence.replace_raw_memory_records_bulk(
            shared_retirement_store,
            [content_models.raw_memory_record(memory)],
        )
    assert (
        await content_raw_persistence.get_raw_memory(
            organization_id=memory.organization_id,
            memory_id=memory.id,
        )
        is None
    )


@pytest.mark.asyncio
async def test_shared_existing_capture_remains_mutable_without_new_shared_writes(
    shared_retirement_store,
):
    memory = legacy_shared_memory()
    await shared_retirement_store.execute_query(
        "CREATE raw_captures CONTENT $record;",
        record=content_models.raw_memory_record(memory),
    )
    stored = await content_raw_persistence.get_raw_memory(
        organization_id=memory.organization_id,
        memory_id=memory.id,
    )
    assert stored is not None
    saved = await content_raw_persistence.save_raw_memory(
        replace(stored, metadata={"migration_diagnostic": True}),
        expected_revision=stored.revision,
        embedding_provider=None,
    )
    assert saved.memory_scope is MemoryScope.SHARED
    assert saved.raw_content == memory.raw_content
    assert saved.revision == stored.revision + 1
    with pytest.raises(ValueError, match="shared memory scope is retired"):
        await content_raw_persistence.save_raw_memory(
            replace(memory, id=str(uuid4())),
            embedding_provider=None,
        )


@pytest.mark.asyncio
async def test_live_capture_cannot_be_changed_into_retired_shared_scope(shared_retirement_store):
    memory = replace(legacy_shared_memory(), memory_scope=MemoryScope.PRIVATE, scope_key=None)
    stored = await content_raw_persistence.save_raw_memory(memory, embedding_provider=None)
    with pytest.raises(ValueError, match="shared memory scope is retired"):
        await content_raw_persistence.save_raw_memory(
            replace(stored, memory_scope=MemoryScope.SHARED, scope_key="team"),
            expected_revision=stored.revision,
            embedding_provider=None,
        )
    unchanged = await content_raw_persistence.get_raw_memory(
        organization_id=memory.organization_id,
        memory_id=memory.id,
    )
    assert unchanged is not None
    assert unchanged.memory_scope is MemoryScope.PRIVATE
    assert unchanged.revision == stored.revision


@pytest.mark.asyncio
async def test_shared_in_mixed_batch_rolls_back_every_insert(shared_retirement_store):
    legacy = legacy_shared_memory()
    private = replace(legacy, id=str(uuid4()), memory_scope=MemoryScope.PRIVATE, scope_key=None)
    with pytest.raises(RuntimeError):
        await content_raw_persistence.replace_raw_memory_records_bulk(
            shared_retirement_store,
            [content_models.raw_memory_record(private), content_models.raw_memory_record(legacy)],
        )
    assert await shared_retirement_store.execute_query("SELECT VALUE uuid FROM raw_captures;") == []


@pytest.mark.asyncio
async def test_bulk_upsert_cannot_reclassify_live_scope_as_shared(shared_retirement_store):
    memory = replace(legacy_shared_memory(), memory_scope=MemoryScope.PRIVATE, scope_key=None)
    stored = await content_raw_persistence.save_raw_memory(memory, embedding_provider=None)
    with pytest.raises(RuntimeError):
        await content_raw_persistence.replace_raw_memory_records_bulk(
            shared_retirement_store,
            [content_models.raw_memory_record(replace(stored, memory_scope=MemoryScope.SHARED))],
        )
    unchanged = await content_raw_persistence.get_raw_memory(
        organization_id=memory.organization_id,
        memory_id=memory.id,
    )
    assert unchanged is not None
    assert unchanged.memory_scope is MemoryScope.PRIVATE
    assert unchanged.revision == stored.revision
