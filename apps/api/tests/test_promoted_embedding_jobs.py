"""Scheduled lifecycle repair recovers indexing after lexical publication."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from sibyl.jobs import entities as jobs, lifecycle_repair, reflection
from sibyl_core.embeddings.providers import DeterministicEmbeddingProvider, EmbeddingMetadata
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services import memory_embedding
from sibyl_core.services.graph_read_availability import available_graph_entities
from tests.test_ordinary_cohort_learning import (
    capture,
    cohort_runtime as cohort_runtime,  # noqa: PLC0414
    install_model,
)


@pytest.mark.parametrize("source_retired", [False, True])
async def test_promoted_embedding_job_scheduled_repair_after_lost_enqueue(
    cohort_runtime, monkeypatch, source_retired
):
    org, _context, client, runtime = cohort_runtime
    sources = await capture(cohort_runtime)
    install_model(monkeypatch, sources[0])
    provider = DeterministicEmbeddingProvider(
        EmbeddingMetadata(
            provider="deterministic",
            model="scheduled-index",
            dimensions=1024,
            cache_namespace="scheduled-index",
            tokenizer_estimate_method="utf8-byte-length",
        )
    )
    queue = AsyncMock()
    queue.enqueue_entity_embedding_backfill.side_effect = RuntimeError("enqueue lost")
    monkeypatch.setattr(memory_embedding, "configured_embedding_provider", lambda: provider)
    monkeypatch.setattr(memory_embedding, "get_queue_port", lambda: queue)
    monkeypatch.setattr(
        "sibyl_core.services.graph_read_availability.get_surreal_graph_runtime",
        AsyncMock(return_value=runtime),
    )
    receipt = await reflection.run_reflection_dream_cycle({}, str(org.id))
    assert receipt["promoted"] == 1
    assert receipt["failed"] == 0
    queue.enqueue_entity_embedding_backfill.assert_awaited_once()
    original_payload = queue.enqueue_entity_embedding_backfill.call_args.kwargs
    entity_id = original_payload["entities_data"][0]["id"]
    assert not (await runtime.entity_manager.get(entity_id)).embedding
    if source_retired:
        await client.execute_query(
            "UPDATE raw_captures SET deleted_at=time::now() WHERE uuid=$id;", id=sources[0]["uuid"]
        )

    @asynccontextmanager
    async def graph_scope(_org):
        yield runtime

    monkeypatch.setattr(lifecycle_repair, "background_graph_runtime", graph_scope)
    monkeypatch.setattr(lifecycle_repair, "list_org_ids", AsyncMock(return_value=[str(org.id)]))
    execute = runtime.client.execute_query
    scan_plans = []

    async def observe_scan(query, **params):
        if "attributes.reflection_identity.purpose='candidate'" in query:
            plan = await execute(query.removesuffix(";") + " EXPLAIN;", **params)
            scan_plans.append(plan)
        return await execute(query, **params)

    monkeypatch.setattr(runtime.client, "execute_query", observe_scan)
    if not source_retired:
        failed_repair = await lifecycle_repair.repair_lifecycle_all_orgs({})
        assert failed_repair["failed"] == 1
        assert not (await runtime.entity_manager.get(entity_id)).embedding
    queue.enqueue_entity_embedding_backfill.reset_mock(side_effect=True)
    queue.enqueue_entity_embedding_backfill.return_value = "scheduled-job"
    repair = await lifecycle_repair.repair_lifecycle_all_orgs({})
    assert scan_plans
    _assert_repair_scan_plan(scan_plans, embedded=runtime.client._url == "memory://")
    assert repair["failed_organizations"] == 0
    assert repair["checked"] >= 1
    if source_retired:
        queue.enqueue_entity_embedding_backfill.assert_not_awaited()
        assert entity_id not in await available_graph_entities(
            str(org.id), [entity_id], runtime=runtime
        )
        return
    queue.enqueue_entity_embedding_backfill.assert_awaited_once()
    payload = queue.enqueue_entity_embedding_backfill.call_args.kwargs
    assert payload == original_payload
    runtime.entity_manager._embedding_provider = provider
    monkeypatch.setattr(jobs, "configured_embedding_provider", lambda: provider)
    monkeypatch.setattr(jobs, "get_surreal_graph_runtime", AsyncMock(return_value=runtime))
    embedded = await jobs.backfill_entity_embeddings({}, **payload)
    assert embedded["entity_ids"] == [entity_id]
    assert len((await runtime.entity_manager.get(entity_id)).embedding) == 1024
    assert entity_id in await available_graph_entities(str(org.id), [entity_id], runtime=runtime)
    provider_calls = AsyncMock(wraps=provider.embed_texts)
    monkeypatch.setattr(provider, "embed_texts", provider_calls)
    queue.enqueue_entity_embedding_backfill.reset_mock()
    await lifecycle_repair.repair_lifecycle_all_orgs({})
    queue.enqueue_entity_embedding_backfill.assert_not_awaited()
    assert (await jobs.backfill_entity_embeddings({}, **payload))["entity_ids"] == [entity_id]
    provider_calls.assert_not_awaited()


async def test_promoted_embedding_job_stale_entity_keeps_unrelated_relationships(
    cohort_runtime, monkeypatch
):
    org, _, _client, runtime = cohort_runtime
    sources = await capture(cohort_runtime)
    install_model(monkeypatch, sources[0])
    monkeypatch.setattr(memory_embedding, "configured_embedding_provider", lambda: None)
    receipt = await reflection.run_reflection_dream_cycle({}, str(org.id))
    assert receipt["promoted"] == 1
    rows = await runtime.client.execute_query(
        "SELECT uuid FROM entity WHERE derivation_required=true;"
    )
    protected = await runtime.entity_manager.get(rows[0]["uuid"])
    await runtime.client.execute_query(
        "UPDATE memory_derivations SET active=false WHERE target_id=$id;", id=protected.id
    )
    ordinary = [
        Entity(
            id=f"ordinary-{i}",
            name=f"ordinary-{i}",
            entity_type=EntityType.PATTERN,
            organization_id=str(org.id),
        )
        for i in range(2)
    ]
    await runtime.entity_manager.create_direct_bulk(ordinary)
    edge = Relationship(
        id="ordinary-link",
        source_id=ordinary[0].id,
        target_id=ordinary[1].id,
        relationship_type=RelationshipType.RELATED_TO,
        organization_id=str(org.id),
    )
    monkeypatch.setattr(jobs, "configured_embedding_provider", lambda: None)
    monkeypatch.setattr(jobs, "get_surreal_graph_runtime", AsyncMock(return_value=runtime))
    result = await jobs.backfill_entity_embeddings(
        {},
        [protected.model_dump(mode="json")],
        str(org.id),
        relationships=[edge.model_dump(mode="json")],
    )
    assert result["stale_entity_ids"] == [protected.id]
    assert result["relationship_ids"] == [edge.id]
    assert await runtime.relationship_manager.get(edge.id)


@pytest.mark.parametrize(
    "state", ["mismatched_provider", "missing_metadata", "operational", "disabled"]
)
async def test_promoted_embedding_job_repair_provider_and_owner_selection(
    cohort_runtime, monkeypatch, state
):
    org, _, _, runtime = cohort_runtime
    sources = await capture(cohort_runtime)
    install_model(monkeypatch, sources[0])
    monkeypatch.setattr(memory_embedding, "configured_embedding_provider", lambda: None)
    assert (await reflection.run_reflection_dream_cycle({}, str(org.id)))["promoted"] == 1
    rows = await runtime.client.execute_query(
        "SELECT uuid FROM entity WHERE derivation_required=true;"
    )
    entity_id = rows[0]["uuid"]
    provider = DeterministicEmbeddingProvider(
        EmbeddingMetadata(
            provider="deterministic",
            model="repair-provider",
            dimensions=1024,
            cache_namespace="repair-provider",
            tokenizer_estimate_method="utf8-byte-length",
        )
    )
    if state in {"mismatched_provider", "missing_metadata"}:
        await runtime.client.execute_query(
            "UPDATE entity SET name_embedding=$vector, attributes.embedding_metadata=$metadata WHERE uuid=$id;",
            vector=[0.01] * 1024,
            id=entity_id,
            metadata={"model": "old-provider"} if state == "mismatched_provider" else None,
        )
    elif state == "operational":
        await runtime.client.execute_query(
            "UPDATE entity SET attributes.operational_source_id='owned-elsewhere' WHERE uuid=$id;",
            id=entity_id,
        )
    monkeypatch.setattr(
        memory_embedding,
        "configured_embedding_provider",
        lambda: None if state == "disabled" else provider,
    )
    queue = AsyncMock()
    monkeypatch.setattr(memory_embedding, "get_queue_port", lambda: queue)
    summary = await memory_embedding.repair_promoted_embeddings(runtime)
    if state in {"operational", "disabled"}:
        assert summary.checked == 0
        queue.enqueue_entity_embedding_backfill.assert_not_awaited()
    else:
        assert summary.checked == 1
        queue.enqueue_entity_embedding_backfill.assert_awaited_once()
        assert (
            queue.enqueue_entity_embedding_backfill.call_args.kwargs["entities_data"][0]["id"]
            == entity_id
        )


def _assert_repair_scan_plan(scan_plans, *, embedded):
    assert all("idx_entity_reflection_candidate_uuid" in str(plan) for plan in scan_plans)
    assert all(
        "Iterate Table" not in str(plan) and "Fallback" not in str(plan) for plan in scan_plans
    )
    if embedded:
        assert all("'prefix': ['candidate', True]" in str(plan) for plan in scan_plans)
