"""Operational completion must cover current persisted evidence embeddings."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sibyl.jobs import entities as jobs
from sibyl_core.embeddings.providers import DeterministicEmbeddingProvider, EmbeddingMetadata
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services.graph import EntityManager
from sibyl_core.services.graph_client import SurrealGraphClient, prepare_graph_schema


@asynccontextmanager
async def unlocked(*args, **kwargs):
    yield "owned-test-lock"


@pytest.fixture
async def projection(monkeypatch):
    org = "manifest-reconcile-" + uuid4().hex
    client = SurrealGraphClient(group_id=org, url="memory://")
    provider = DeterministicEmbeddingProvider(
        EmbeddingMetadata(
            provider="deterministic",
            model="offline",
            dimensions=1024,
            cache_namespace=org,
            tokenizer_estimate_method="utf8-byte-length",
        )
    )
    manager = EntityManager(client, group_id=org, embedding_provider=provider)
    source = Entity(
        id="session-source",
        entity_type=EntityType.SESSION,
        name="Source",
        content="original evidence",
        organization_id=org,
    )
    manifest = Entity(
        id="manifest",
        entity_type=EntityType.ARTIFACT,
        name="Manifest",
        content="capture",
        organization_id=org,
        metadata={
            "projection_kind": "manifest",
            "operational_source_id": "capture",
            "operational_schema_version": 1,
            "operational_content_hash": "capture-hash",
            "project_id": "project",
            "operational_projection_state": "embedding_pending",
            "expected_entity_ids": [source.id, "manifest"],
            "expected_relationship_ids": [],
        },
    )
    complete = manifest.model_copy(
        update={"metadata": {**manifest.metadata, "operational_projection_state": "complete"}}
    )
    runtime = SimpleNamespace(entity_manager=manager, relationship_manager=None)

    async def get_runtime(*args, **kwargs):
        return runtime

    monkeypatch.setattr(jobs, "get_surreal_graph_runtime", get_runtime)
    monkeypatch.setattr(jobs, "configured_embedding_provider", lambda: provider)
    monkeypatch.setattr("sibyl.locks.entity_lock", unlocked)

    async def run():
        return await jobs.backfill_entity_embeddings(
            {},
            [source.model_dump(mode="json")],
            org,
            completion_manifest=complete.model_dump(mode="json"),
        )

    try:
        await prepare_graph_schema(client)
        await manager.create_direct_bulk([source, manifest], generate_embeddings=False)
        yield SimpleNamespace(
            client=client,
            provider=provider,
            manager=manager,
            source=source,
            manifest=manifest,
            complete=complete,
            run=run,
        )
    finally:
        await client.close()


async def test_manifest_reconciles_recapture_before_marking_complete(projection):
    p = projection
    original_embed = p.provider.embed_texts
    seen = []

    async def recapture(texts, **kwargs):
        seen.extend(texts)
        if len(seen) == 1:
            await p.manager.create_direct(
                p.source.model_copy(update={"content": "current evidence"}),
                generate_embedding=False,
            )
        return await original_embed(texts, **kwargs)

    p.provider.embed_texts = recapture
    result = await p.run()
    current = await p.manager.get(p.source.id)
    assert current.content == "current evidence"
    assert current.embedding, "a completed manifest must not hide a refused embedding write"
    assert len(seen) == 2
    assert "current evidence" in seen[-1]
    assert result["manifest_state"] == "completed"


@pytest.mark.parametrize("mutation", ["change_again", "delete"])
async def test_manifest_keeps_unresolved_current_evidence_pending(projection, mutation):
    p = projection
    original_embed = p.provider.embed_texts
    calls = 0

    async def mutate(texts, **kwargs):
        nonlocal calls
        calls += 1
        if mutation == "delete":
            await p.manager.delete(p.source.id)
        else:
            await p.manager.create_direct(
                p.source.model_copy(update={"content": f"capture {calls}"}),
                generate_embedding=False,
            )
        return await original_embed(texts, **kwargs)

    p.provider.embed_texts = mutate
    with pytest.raises(
        RuntimeError, match=r"partial entity embedding backfill|changed during reconciliation"
    ):
        await p.run()
    assert (await p.manager.get(p.manifest.id)).metadata[
        "operational_projection_state"
    ] == "embedding_pending"
    assert calls == (1 if mutation == "delete" else 2)


async def test_manifest_completion_transaction_rechecks_current_vectors(projection, monkeypatch):
    p = projection
    complete = p.manager.complete_embedding_manifest

    async def mutate_before_commit(expected, **kwargs):
        await p.manager.create_direct(
            p.source.model_copy(update={"content": "changed after embedding"}),
            generate_embedding=False,
        )
        return await complete(expected, **kwargs)

    monkeypatch.setattr(p.manager, "complete_embedding_manifest", mutate_before_commit)
    with pytest.raises(RuntimeError, match="incomplete current embeddings"):
        await p.run()
    assert (await p.manager.get(p.manifest.id)).metadata[
        "operational_projection_state"
    ] == "embedding_pending"
    assert (await p.manager.get(p.source.id)).embedding is None


async def test_manifest_valid_replay_avoids_provider_and_repairs_missing_coverage(projection):
    p = projection
    await p.run()
    original = p.provider.embed_texts

    async def forbidden(*args, **kwargs):
        raise AssertionError("valid manifest must not repeat embeddings")

    p.provider.embed_texts = forbidden
    assert (await p.run())["manifest_state"] == "complete"
    await p.manager.create_direct(p.source, generate_embedding=False)
    p.provider.embed_texts = original
    assert (await p.run())["manifest_state"] == "complete"
    assert (await p.manager.get(p.source.id)).embedding


async def test_manifest_capture_change_cannot_be_completed_by_old_job(projection):
    p = projection
    original = p.provider.embed_texts

    async def recapture_manifest(texts, **kwargs):
        replacement = p.manifest.model_copy(
            update={"metadata": {**p.manifest.metadata, "operational_content_hash": "new-capture"}}
        )
        await p.manager.create_direct(replacement, generate_embedding=False)
        return await original(texts, **kwargs)

    p.provider.embed_texts = recapture_manifest
    assert (await p.run())["manifest_state"] == "stale"
    current = await p.manager.get(p.manifest.id)
    assert current.metadata["operational_content_hash"] == "new-capture"
    assert current.metadata["operational_projection_state"] == "embedding_pending"


async def test_manifest_completion_preserves_source_rows_and_rejects_missing_inventory(projection):
    p = projection
    await p.manager.backfill_embeddings_if_current([p.source])
    before = await p.manager._get_many_rows([p.source.id])
    ledger_before = await p.client.execute_query(
        "SELECT * OMIT validation_write_witness FROM source_states WHERE source_id=$source;",
        source=p.source.id,
    )
    assert await p.manager.complete_embedding_manifest(p.complete) == "completed"
    assert (
        await p.client.execute_query(
            "SELECT * OMIT validation_write_witness FROM source_states WHERE source_id=$source;",
            source=p.source.id,
        )
        == ledger_before
    )
    assert await p.manager._get_many_rows([p.source.id]) == before
    await p.manager.delete(p.source.id)
    assert await p.manager.complete_embedding_manifest(p.complete, complete=False) == "incomplete"


@pytest.mark.parametrize("field", ["name", "description", "content", "summary"])
async def test_manifest_patch_invalidates_previous_text_embedding(projection, field):
    p = projection
    await p.run()
    updates = (
        {"metadata": {"summary": "new summary"}} if field == "summary" else {field: "new evidence"}
    )
    previous = await p.manager.get(p.source.id)
    updates["embedding"] = previous.embedding
    changed = await p.manager.update(p.source.id, updates)
    assert changed.embedding is None
    assert "embedding_metadata" not in changed.metadata
    assert await p.manager.complete_embedding_manifest(p.complete, complete=False) == "incomplete"


async def test_manifest_patch_preserves_noop_and_bookkeeping_embeddings(projection):
    p = projection
    await p.run()
    before = await p.manager.get(p.source.id)
    after = await p.manager.update(p.source.id, {"content": before.content})
    assert after.embedding == before.embedding
    after = await p.manager.update(p.source.id, {"retrieval_count": 7})
    assert after.embedding == before.embedding
    assert after.metadata["embedding_metadata"] == before.metadata["embedding_metadata"]
    assert await p.manager.complete_embedding_manifest(p.complete, complete=False) == "complete"


async def test_native_manifest_transaction_fences_concurrent_source_write(projection, monkeypatch):
    import asyncio

    p = projection
    if p.client._url.startswith("memory://"):
        pytest.skip("requires the pinned multi-connection server qualification")
    await p.manager.backfill_embeddings_if_current([p.source])
    original = p.client.execute_query
    entered = asyncio.Event()

    async def delayed(sql, **params):
        if "LET $manifest =" in sql:
            sql = sql.replace("IF $complete = false", "SLEEP 1s; IF $complete = false")
            entered.set()
        return await original(sql, **params)

    monkeypatch.setattr(p.client, "execute_query", delayed)
    completing = asyncio.create_task(p.manager.complete_embedding_manifest(p.complete))
    await entered.wait()
    await asyncio.sleep(0.3)
    await p.manager.update(p.source.id, {"content": "concurrent evidence"})
    assert not completing.done()
    outcome = (await asyncio.gather(completing, return_exceptions=True))[0]
    if isinstance(outcome, BaseException):
        assert "transaction conflict" in str(outcome).lower()
        outcome = "conflict"
    assert outcome in {"incomplete", "conflict"}
    assert (await p.manager.get(p.manifest.id)).metadata[
        "operational_projection_state"
    ] == "embedding_pending"


async def test_manifest_missing_source_ledger_cannot_complete(projection):
    p = projection
    await p.manager.backfill_embeddings_if_current([p.source])
    await p.client.execute_query(
        "DELETE source_states WHERE source_id=$source;", source=p.source.id
    )
    assert await p.manager.complete_embedding_manifest(p.complete) == "incomplete"
    assert (await p.manager.get(p.manifest.id)).metadata[
        "operational_projection_state"
    ] == "embedding_pending"


async def test_graph_witness_upgrade_preserves_sources_and_retained_identity(projection):
    from sibyl_core.backends.surreal.schema_version import get_schema_version
    from sibyl_core.services.graph_client import mark_graph_schema_dirty

    p = projection
    await p.manager.backfill_embeddings_if_current([p.source])
    source_before = await p.manager._get_many_rows([p.source.id])
    ledger_before = await p.client.execute_query("SELECT * FROM source_states ORDER BY id;")
    await p.client.execute_query("REMOVE FIELD validation_write_witness ON source_states;")
    await p.client.execute_query("UPDATE schema_version SET version=26 WHERE name='graph';")
    mark_graph_schema_dirty(p.client.group_id)
    await prepare_graph_schema(p.client)
    assert await get_schema_version(p.client.execute_query) == 27
    assert await p.manager._get_many_rows([p.source.id]) == source_before
    assert await p.client.execute_query("SELECT * FROM source_states ORDER BY id;") == ledger_before
    assert await p.manager.complete_embedding_manifest(p.complete) == "completed"
