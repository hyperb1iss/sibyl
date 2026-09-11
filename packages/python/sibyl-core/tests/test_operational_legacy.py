"""Legacy graph adoption uses actual complete inventory and retained bytes."""

import pytest

from sibyl_core.config import core_config
from sibyl_core.projection.experience import persist_operational_experience
from sibyl_core.services.operational_legacy import legacy_adoption_proof
from sibyl_core.services.operational_projection import OperationalProjectionSource
from sibyl_core.services.source_observations import SourceUnavailableError
from tests.test_operational_projection import authority as authority
from tests.test_operational_projection import capture
from tests.test_operational_projection import content_store as content_store
from tests.test_operational_projection import runtime as runtime


async def legacy(runtime, source):
    _, experience = await source.current()
    return await persist_operational_experience(
        entity_manager=runtime.entity_manager,
        relationship_manager=runtime.relationship_manager,
        experience=experience,
        organization_id=runtime.client.group_id,
        created_by=source.creator_id,
    )


async def test_exact_complete_legacy_inventory_is_adoptable(runtime, content_store, authority):
    _, source = await capture(runtime, authority)
    result = await legacy(runtime, source)
    proof = await legacy_adoption_proof(runtime.client, source)
    assert {key for key, _ in proof.entity_rows} == set(result.projection.manifest.entity_ids)
    assert {key for key, _ in proof.relationship_rows} == set(
        result.projection.manifest.relationship_ids
    )


@pytest.mark.parametrize("mutation", ["entity", "edge", "manifest", "missing"])
async def test_legacy_adoption_rejects_changed_or_partial_inventory(
    runtime, content_store, authority, mutation
):
    _, source = await capture(runtime, authority)
    result = await legacy(runtime, source)
    projection = result.projection
    if mutation == "entity":
        await runtime.client.execute_query(
            "UPDATE entity SET content='changed' WHERE uuid=$id;", id=projection.entities[0].id
        )
    elif mutation == "edge":
        await runtime.client.execute_query(
            "UPDATE relates_to SET fact='changed' WHERE uuid=$id;",
            id=projection.relationships[0].id,
        )
    elif mutation == "manifest":
        await runtime.client.execute_query(
            "UPDATE entity SET attributes.operational_projection_state='pending' WHERE uuid=$id;",
            id=projection.manifest.manifest_entity_id,
        )
    else:
        await runtime.client.execute_query(
            "DELETE relates_to WHERE uuid=$id;", id=projection.relationships[0].id
        )
    with pytest.raises(SourceUnavailableError):
        await source.legacy_adoption_proof(runtime.client)


async def test_entity_adoption_reconstructs_proof_after_pending_checkpoint(
    runtime, content_store, authority
):
    _, source = await capture(runtime, authority)
    await legacy(runtime, source)
    await runtime.entity_manager.publish_operational_entities(source)
    # Re-resolve from durable raw bytes, discarding the original source object.
    from sibyl_core.services.operational_projection import load_operational_projection_source

    resumed = await load_operational_projection_source(source.observation.source, authority)
    proof = await resumed.legacy_adoption_proof(runtime.client)
    assert proof.relationship_rows
    await runtime.entity_manager.publish_operational_entities(resumed)


async def test_new_actor_rebinds_entities_before_resuming_edge_adoption(
    runtime, content_store, authority, monkeypatch
):
    from dataclasses import replace
    from unittest.mock import AsyncMock

    from sibyl_core.services.operational_projection import load_operational_projection_source

    _, source = await capture(runtime, authority)
    await legacy(runtime, source)
    await runtime.entity_manager.publish_operational_entities(source)
    new_authority = replace(authority, principal_id="user_b")
    monkeypatch.setattr(
        "sibyl_core.services.operational_projection.get_source_authority_resolver",
        lambda: AsyncMock(return_value=new_authority),
    )
    resumed = await load_operational_projection_source(source.observation.source, new_authority)
    with pytest.raises(SourceUnavailableError):
        await resumed.legacy_adoption_proof(runtime.client)
    await runtime.entity_manager.publish_operational_entities(resumed)
    proof = await resumed.legacy_adoption_proof(runtime.client)
    assert proof.relationship_rows


async def test_legacy_adoption_rejects_supporting_edge_changed_after_proof(
    runtime, content_store, authority, monkeypatch
):
    _, source = await capture(runtime, authority)
    result = await legacy(runtime, source)
    edge_id = result.projection.relationships[0].id
    original = OperationalProjectionSource.legacy_adoption_proof
    entered = False

    async def changed_after_proof(self, client):
        nonlocal entered
        proof = await original(self, client)
        await client.execute_query(
            "UPDATE relates_to SET fact='concurrent authoritative edit' WHERE uuid=$uuid;",
            uuid=edge_id,
        )
        entered = True
        return proof

    monkeypatch.setattr(OperationalProjectionSource, "legacy_adoption_proof", changed_after_proof)
    with pytest.raises(SourceUnavailableError):
        await runtime.entity_manager.publish_operational_entities(source)
    assert entered
    assert not await runtime.client.execute_query("SELECT * FROM memory_derivations;")
    rows = await runtime.client.execute_query(
        "SELECT * FROM relates_to WHERE uuid=$uuid;", uuid=edge_id
    )
    assert rows[0]["fact"] == "concurrent authoritative edit"


async def test_legacy_adoption_conflicts_with_edge_edit_during_transaction(
    runtime, content_store, authority, monkeypatch
):
    import asyncio
    import os

    if not os.environ.get("SIBYL_COHORT_NATIVE_URL"):
        pytest.skip("requires independent native pooled transactions")
    _, source = await capture(runtime, authority)
    result = await legacy(runtime, source)
    edge_id = result.projection.relationships[0].id
    original = runtime.client.execute_query
    entered = asyncio.Event()

    async def delayed(query, **params):
        if "legacy_fingerprint" in params:
            assert "FOR $entry IN $entries" in query
            query = query.replace("FOR $entry IN $entries", "SLEEP 1s; FOR $entry IN $entries", 1)
            entered.set()
        return await original(query, **params)

    monkeypatch.setattr(runtime.client, "execute_query", delayed)
    pending = asyncio.create_task(runtime.entity_manager.publish_operational_entities(source))
    await asyncio.wait_for(entered.wait(), timeout=10)
    await asyncio.sleep(0.3)
    await original(
        "UPDATE relates_to SET fact='edit during adoption transaction' WHERE uuid=$uuid;",
        uuid=edge_id,
    )
    assert not pending.done()
    with pytest.raises(Exception, match=r"(?i)conflict|changed|unavailable|retry"):
        await pending
    assert not await original("SELECT * FROM memory_derivations;")
    rows = await original("SELECT * FROM relates_to WHERE uuid=$uuid;", uuid=edge_id)
    assert rows[0]["fact"] == "edit during adoption transaction"


async def test_legacy_adoption_witness_preserves_edge_body_and_vectors(
    runtime, content_store, authority
):
    _, source = await capture(runtime, authority)
    result = await legacy(runtime, source)
    edge_id = result.projection.relationships[0].id
    await runtime.client.execute_query(
        "UPDATE relates_to SET fact_embedding=$vector WHERE uuid=$uuid;",
        uuid=edge_id,
        vector=[0.25] * core_config.graph_embedding_dimensions,
    )
    query = "SELECT * FROM relates_to WHERE uuid=$uuid;"
    before = await runtime.client.execute_query(query, uuid=edge_id)
    raw_before, _ = await source.current()
    await runtime.entity_manager.publish_operational_entities(source)
    after = await runtime.client.execute_query(query, uuid=edge_id)
    assert isinstance(after[0]["attributes"].pop("operational_write_witness"), str)
    assert after == before
    raw_after, _ = await source.current()
    assert raw_after == raw_before
