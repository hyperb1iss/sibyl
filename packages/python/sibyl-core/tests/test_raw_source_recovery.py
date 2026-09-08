"""Accepted captures recover using live authority bounded by their original grants."""

from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.errors import RevisionConflictError
from sibyl_core.memory_pipeline.source_lifecycle import public_memory_metadata
from sibyl_core.services import content_client
from sibyl_core.services import content_raw_persistence as store
from sibyl_core.services import memory_source_validation as validation
from sibyl_core.services.content_models import RawMemoryWrite, raw_memory_recallable
from sibyl_core.services.memory_source_validation import SourceReadAuthority


@pytest.fixture
async def content_store(monkeypatch):
    client = SurrealContentClient(url="memory://")
    try:
        await bootstrap_content_schema(client, reset=True)

        @asynccontextmanager
        async def session():
            yield client

        monkeypatch.setattr(content_client, "surreal_content_client", session)
        yield str(uuid4())
    finally:
        await client.close()


async def pending_child(org, *, scope="private", ceiling=True, bulk=False):
    source = await store.remember_raw_memory(
        organization_id=org,
        principal_id="owner",
        source_id="source",
        raw_content="Original source",
        memory_scope=scope,
        scope_key=None if scope == "private" else "source-scope",
        embedding_provider=None,
    )
    # A missing source at birth, later restored with precisely the observed identity.
    async with content_client.surreal_content_client() as client:
        await client.execute_query("DELETE raw_captures WHERE uuid = $id;", id=source.id)
    grants = {
        "accessible_projects": ["source-scope"] if ceiling else [],
        "accessible_teams": ["source-scope"] if ceiling else [],
        "accessible_delegations": ["source-scope"] if ceiling else [],
    }
    common = {
        "organization_id": org,
        "principal_id": "owner",
        "source_id": str(uuid4()),
        "raw_content": "Derived source",
        "metadata": {"raw_source_ids": [source.id]},
    }
    if bulk:
        child = (
            await store.remember_raw_memories(
                [RawMemoryWrite(**common)], embedding_provider=None, **grants
            )
        )[0]
    else:
        child = await store.remember_raw_memory(
            **common, source_memories=[source], embedding_provider=None, **grants
        )
    assert child.metadata["source_validation_pending"] is True
    async with content_client.surreal_content_client() as client:
        from sibyl_core.services.content_models import raw_memory_record

        await content_client.replace_record(
            client, "raw_captures", uuid=source.id, record=raw_memory_record(source)
        )
    return source, child


@pytest.mark.parametrize("bulk", [False, True])
async def test_missing_source_recovers_without_rebinding(content_store, bulk):
    _, child = await pending_child(content_store, bulk=bulk)
    bindings = child.metadata.get("source_bindings")
    resolver = AsyncMock(return_value=SourceReadAuthority("owner"))
    result = await validation.repair_raw_source_lifecycle(
        content_store, embedding_provider=None, authority_resolver=resolver
    )
    saved = await store.get_raw_memory(organization_id=content_store, memory_id=child.id)
    assert result.recovered == 1
    assert raw_memory_recallable(saved)
    assert saved.metadata.get("source_bindings") == bindings
    assert "source_validation_context" not in public_memory_metadata(saved.metadata)
    resolver.assert_awaited_once_with(content_store, "owner")


@pytest.mark.parametrize("scope", ["project", "team", "delegated"])
@pytest.mark.parametrize("revoked", [True, False])
async def test_current_membership_and_original_ceiling_are_both_required(
    content_store, scope, revoked
):
    _, child = await pending_child(content_store, scope=scope, ceiling=revoked)
    current = frozenset() if revoked else frozenset({"source-scope"})
    resolver = AsyncMock(return_value=SourceReadAuthority("owner", current, current, current))
    result = await validation.repair_raw_source_lifecycle(
        content_store, embedding_provider=None, authority_resolver=resolver
    )
    saved = await store.get_raw_memory(organization_id=content_store, memory_id=child.id)
    assert result.pending == 1
    assert not raw_memory_recallable(saved)
    assert saved.metadata == child.metadata


@pytest.mark.parametrize("context", [None, {}, {"version": 99}])
async def test_legacy_or_malformed_context_stays_pending(content_store, context):
    _, child = await pending_child(content_store)
    async with content_client.surreal_content_client() as client:
        await client.execute_query(
            "UPDATE raw_captures UNSET metadata.source_validation_context WHERE uuid = $id;",
            id=child.id,
        )
        if context is not None:
            await client.execute_query(
                "UPDATE raw_captures SET metadata.source_validation_context = $context "
                "WHERE uuid = $id;",
                id=child.id,
                context=context,
            )
    resolver = AsyncMock(return_value=SourceReadAuthority("owner"))
    result = await validation.repair_raw_source_lifecycle(
        content_store, embedding_provider=None, authority_resolver=resolver
    )
    assert result.pending == 1
    resolver.assert_not_awaited()


async def test_revoked_principal_remains_pending(content_store):
    await pending_child(content_store)
    result = await validation.repair_raw_source_lifecycle(
        content_store, embedding_provider=None, authority_resolver=AsyncMock(return_value=None)
    )
    assert result.pending == 1


async def test_cas_loss_resolves_authority_again(content_store, monkeypatch):
    _, child = await pending_child(content_store)
    resolver = AsyncMock(side_effect=[SourceReadAuthority("owner"), None])
    original_save = store.save_raw_memory

    async def lose_once(memory, **kwargs):
        await original_save(replace(child, tags=["concurrent"]), embedding_provider=None)
        raise RevisionConflictError(child.id, child.revision, child.revision + 1)

    monkeypatch.setattr(store, "save_raw_memory", lose_once)
    result = await validation.repair_raw_source_lifecycle(
        content_store, embedding_provider=None, authority_resolver=resolver
    )
    assert result.pending == 1
    assert resolver.await_count == 2
    saved = await store.get_raw_memory(organization_id=content_store, memory_id=child.id)
    assert saved.tags == ["concurrent"]
    assert saved.metadata["source_validation_pending"] is True


async def test_paging_does_not_starve_later_rows(content_store, monkeypatch):
    children = [(await pending_child(content_store))[1] for _ in range(5)]
    first = min(children, key=lambda row: row.id)
    async with content_client.surreal_content_client() as client:
        await client.execute_query(
            "UPDATE raw_captures UNSET metadata.source_validation_context WHERE uuid = $id;",
            id=first.id,
        )
    monkeypatch.setattr(validation, "_PAGE_SIZE", 2)
    result = await validation.repair_raw_source_lifecycle(
        content_store,
        embedding_provider=None,
        authority_resolver=AsyncMock(return_value=SourceReadAuthority("owner")),
    )
    assert result.checked == 5
    assert result.pending == 1
    assert result.recovered == 4


async def test_explicit_empty_scope_ceiling_survives_storage(content_store):
    _, child = await pending_child(content_store)
    metadata = dict(child.metadata)
    metadata["source_validation_context"] = SourceReadAuthority(
        "owner", scope_keys=frozenset()
    ).ceiling_metadata()
    await store.save_raw_memory(replace(child, metadata=metadata), embedding_provider=None)
    result = await validation.repair_raw_source_lifecycle(
        content_store,
        embedding_provider=None,
        authority_resolver=AsyncMock(return_value=SourceReadAuthority("owner")),
    )
    assert result.pending == 1


async def test_public_input_cannot_supply_recovery_authority(content_store):
    forged = SourceReadAuthority("other", projects=frozenset({"secret"})).ceiling_metadata()
    child = await store.remember_raw_memory(
        organization_id=content_store,
        principal_id="owner",
        source_id="forged",
        raw_content="Forged authority",
        metadata={"raw_source_ids": [str(uuid4())], "source_validation_context": forged},
        embedding_provider=None,
    )
    saved = child.metadata["source_validation_context"]
    assert saved["principal_id"] == "owner"
    assert saved["projects"] == []
