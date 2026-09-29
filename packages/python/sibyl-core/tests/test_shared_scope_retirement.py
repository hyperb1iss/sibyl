"""Retired scope writes fail before embedding and at the persistence boundary."""

from __future__ import annotations

import os
from dataclasses import replace
from uuid import uuid4

import pytest

from sibyl_core.backends.surreal.content_client import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.migrate import shared_scope_retirement as retirement
from sibyl_core.models.memory_scope import MemoryScope
from sibyl_core.services import content_client, content_models, content_raw_persistence
from sibyl_core.services.content_models import RawMemory, RawMemoryWrite


@pytest.fixture
async def shared_retirement_store(monkeypatch: pytest.MonkeyPatch):
    client = SurrealContentClient(
        url=os.environ.get("SIBYL_SHARED_RETIREMENT_TEST_URL", "memory://"),
        username="root",
        password="root",
        namespace=f"retirement_{uuid4().hex}",
    )
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("member", None),
        ("admin", None),
        ("missing_team", "team_not_in_organization"),
        ("malformed_key", "noncanonical_team_key"),
        ("foreign_team", "team_not_in_organization"),
        ("missing_principal", "principal_not_in_organization"),
        ("not_team_member", "principal_not_in_team"),
    ],
)
async def test_shared_retirement_dry_run_and_apply_retains_identity(
    shared_retirement_store,
    case,
    reason,
):
    memory = legacy_shared_memory()
    team_id = memory.scope_key
    assert team_id is not None
    memory = replace(memory, provenance={"source_path": "retained/transcript.jsonl"})
    if case == "malformed_key":
        memory = replace(memory, scope_key="remembered-team-name")
    authority = retirement.SharedScopeAuthority(
        organization_id=memory.organization_id,
        team_ids=frozenset() if case in {"missing_team", "foreign_team"} else frozenset({team_id}),
        organization_members=frozenset()
        if case == "missing_principal"
        else frozenset({memory.principal_id}),
        organization_admins=frozenset({memory.principal_id}) if case == "admin" else frozenset(),
        team_memberships=frozenset()
        if case in {"admin", "not_team_member"}
        else frozenset({(team_id, memory.principal_id)}),
    )
    await shared_retirement_store.execute_query(
        "CREATE raw_captures CONTENT $record;",
        record=content_models.raw_memory_record(memory),
    )
    before_states = await shared_retirement_store.execute_query("SELECT * FROM source_states;")

    async def resolve_authority():
        return authority

    dry = await retirement.retire_shared_captures(
        organization_id=memory.organization_id,
        authority_provider=resolve_authority,
    )
    assert dry.success
    assert len(dry.entries) == 1
    assert dry.entries[0].applied_revision is None
    assert dry.entries[0].disposition == ("tombstone" if reason else "team")
    assert (
        await shared_retirement_store.execute_query("SELECT * FROM source_states;") == before_states
    )
    unchanged = await content_raw_persistence.get_raw_memory(
        organization_id=memory.organization_id,
        memory_id=memory.id,
    )
    assert unchanged is not None and unchanged.memory_scope is MemoryScope.SHARED
    applied = await retirement.retire_shared_captures(
        organization_id=memory.organization_id,
        authority_provider=resolve_authority,
        dry_run=False,
    )
    assert applied.success
    assert applied.entries[0].applied_revision == memory.revision + 1
    stored_rows = await shared_retirement_store.execute_query(
        "SELECT * FROM raw_captures WHERE uuid=$uuid;",
        uuid=memory.id,
    )
    stored = content_models.raw_memory_from_record(stored_rows[0])
    assert stored.id == memory.id
    assert stored.source_id == memory.source_id
    assert stored.raw_content == memory.raw_content
    assert stored.provenance == memory.provenance
    assert stored.memory_scope is (MemoryScope.SHARED if reason else MemoryScope.TEAM)
    assert bool(stored.deleted_at) == bool(reason)
    assert stored.metadata[retirement.RETIREMENT_METADATA_KEY]["reason"] == (
        reason or "canonical_team_and_principal_verified"
    )
    states = await shared_retirement_store.execute_query("SELECT * FROM source_states;")
    assert states[0]["generation"] == before_states[0]["generation"] + 1
    assert await shared_retirement_store.execute_query("SELECT * FROM memory_derivations;") == []
    replay = await retirement.retire_shared_captures(
        organization_id=memory.organization_id,
        authority_provider=resolve_authority,
        dry_run=False,
    )
    assert replay.success and replay.entries == []
    assert await shared_retirement_store.execute_query("SELECT * FROM source_states;") == states


@pytest.mark.asyncio
async def test_shared_retirement_authority_failure_keeps_captures_untouched(
    shared_retirement_store,
):
    memory = legacy_shared_memory()
    await shared_retirement_store.execute_query(
        "CREATE raw_captures CONTENT $record;",
        record=content_models.raw_memory_record(memory),
    )

    async def unavailable_authority():
        raise ConnectionError("authority unavailable")

    receipt = await retirement.retire_shared_captures(
        organization_id=memory.organization_id,
        authority_provider=unavailable_authority,
        dry_run=False,
    )
    assert not receipt.success and receipt.entries == []
    stored = await content_raw_persistence.get_raw_memory(
        organization_id=memory.organization_id,
        memory_id=memory.id,
    )
    assert stored is not None and stored.deleted_at is None and stored.revision == memory.revision


@pytest.mark.asyncio
async def test_shared_retirement_conflict_preserves_concurrent_content(
    shared_retirement_store, monkeypatch
):
    memory = legacy_shared_memory()
    await shared_retirement_store.execute_query(
        "CREATE raw_captures CONTENT $record;",
        record=content_models.raw_memory_record(memory),
    )

    async def authority():
        return retirement.SharedScopeAuthority(
            organization_id=memory.organization_id,
            team_ids=frozenset({memory.scope_key}),
            organization_members=frozenset({memory.principal_id}),
            organization_admins=frozenset({memory.principal_id}),
            team_memberships=frozenset(),
        )

    save = retirement.save_raw_memory

    async def racing_save(changed, **kwargs):
        await save(
            replace(memory, raw_content="concurrent writer owns these bytes"),
            expected_revision=memory.revision,
            embedding_provider=None,
        )
        return await save(changed, **kwargs)

    monkeypatch.setattr(retirement, "save_raw_memory", racing_save)
    receipt = await retirement.retire_shared_captures(
        organization_id=memory.organization_id,
        authority_provider=authority,
        dry_run=False,
    )
    assert not receipt.success and receipt.entries[0].disposition == "conflict"
    stored = await content_raw_persistence.get_raw_memory(
        organization_id=memory.organization_id,
        memory_id=memory.id,
    )
    assert stored is not None and stored.raw_content == "concurrent writer owns these bytes"
    assert stored.memory_scope is MemoryScope.SHARED
    assert retirement.RETIREMENT_METADATA_KEY not in stored.metadata


@pytest.mark.asyncio
async def test_shared_retirement_interrupt_resume_and_org_isolation(
    shared_retirement_store, monkeypatch
):
    monkeypatch.setattr(retirement, "_PAGE_SIZE", 1)
    first = replace(legacy_shared_memory(), id="00000000-0000-0000-0000-000000000001")
    second = replace(first, id="00000000-0000-0000-0000-000000000002")
    foreign = replace(first, id=str(uuid4()), organization_id=str(uuid4()))
    for memory in (first, second, foreign):
        await shared_retirement_store.execute_query(
            "CREATE raw_captures CONTENT $record;",
            record=content_models.raw_memory_record(memory),
        )
    authority = retirement.SharedScopeAuthority(
        organization_id=first.organization_id,
        team_ids=frozenset({first.scope_key}),
        organization_members=frozenset({first.principal_id}),
        organization_admins=frozenset({first.principal_id}),
        team_memberships=frozenset(),
    )
    calls = 0

    async def interrupted_authority():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ConnectionError("stopped between committed pages")
        return authority

    partial = await retirement.retire_shared_captures(
        organization_id=first.organization_id,
        authority_provider=interrupted_authority,
        dry_run=False,
    )
    assert not partial.success
    assert [entry.capture_id for entry in partial.entries] == [first.id]
    assert partial.entries[0].applied_revision == first.revision + 1

    async def available_authority():
        return authority

    resumed = await retirement.retire_shared_captures(
        organization_id=first.organization_id,
        authority_provider=available_authority,
        dry_run=False,
    )
    assert resumed.success and [entry.capture_id for entry in resumed.entries] == [second.id]
    stored = await content_raw_persistence.get_raw_memory(
        organization_id=first.organization_id,
        memory_id=first.id,
    )
    assert stored is not None and stored.revision == first.revision + 1
    preserved_foreign = await content_raw_persistence.get_raw_memory(
        organization_id=foreign.organization_id,
        memory_id=foreign.id,
    )
    assert preserved_foreign is not None
    assert preserved_foreign.memory_scope is MemoryScope.SHARED
    assert preserved_foreign.revision == foreign.revision
