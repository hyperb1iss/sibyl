"""Published ordinary lessons use the configured deferred embedding owner."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from sibyl_core.ai.llm.extractor import Extractor
from sibyl_core.embeddings.providers import DeterministicEmbeddingProvider, EmbeddingMetadata
from sibyl_core.services import memory_embedding, ordinary_cohort, procedure_validation
from sibyl_core.services.content_raw_persistence import save_raw_memory
from sibyl_core.services.graph_read_availability import available_graph_entities
from sibyl_core.services.memory_reflection import promote_reflection_candidate_review
from sibyl_core.services.memory_source_validation import SourceReadAuthority
from sibyl_core.services.ordinary_publication import ordinary_promotion_binding
from sibyl_core.services.reflection_validation import (
    prepare_stored_reflection,
    validate_reflection_stage,
)
from sibyl_core.tasks.memory_validation import CriticOutput
from tests.test_ordinary_cohort import (
    cohort_sources as cohort_sources,
)
from tests.test_ordinary_cohort import (
    content_store as content_store,
)
from tests.test_ordinary_cohort import (
    install_proposal,
)
from tests.test_ordinary_cohort import (
    runtime as runtime,
)


@pytest.fixture
async def publication(cohort_sources, content_store, runtime, monkeypatch):
    install_proposal(monkeypatch, cohort_sources)
    resolver = AsyncMock(return_value=SourceReadAuthority("owner"))
    authorize = AsyncMock()
    candidate, _ = await ordinary_cohort.propose_stored_cohort(
        "org", "owner", [s.id for s in cohort_sources], resolver, authorize=authorize
    )
    prepared = await prepare_stored_reflection("org", "owner", candidate.id, resolver)
    critic = Extractor(
        CriticOutput,
        agent=Agent(TestModel(custom_output_args={"findings": []}), output_type=CriticOutput),
    )
    monkeypatch.setattr(
        procedure_validation,
        "validation_extractor",
        AsyncMock(return_value=(critic, '{"model":"offline"}')),
    )
    validation = await validate_reflection_stage(prepared, resolver)
    binding = await ordinary_promotion_binding(
        "org", "owner", candidate.id, validation["execution_id"], resolver, authorize
    )
    monkeypatch.setattr(
        "sibyl_core.services.graph_read_availability.get_surreal_graph_runtime",
        AsyncMock(return_value=runtime),
    )
    provider = DeterministicEmbeddingProvider(
        EmbeddingMetadata(
            provider="deterministic",
            model="promoted-lesson",
            dimensions=1024,
            cache_namespace="promoted-lesson",
            tokenizer_estimate_method="utf8-byte-length",
        )
    )
    monkeypatch.setattr(memory_embedding, "configured_embedding_provider", lambda: provider)
    queue = AsyncMock()
    queue.enqueue_entity_embedding_backfill.return_value = "owned-embedding-job"
    monkeypatch.setattr(memory_embedding, "get_queue_port", lambda: queue)
    kwargs = dict(
        organization_id="org",
        principal_id="owner",
        candidate_id=candidate.id,
        promote_to_scope="private",
        promote_to_scope_key="owner",
        validation_promotion=binding,
    )
    return kwargs, provider, queue


async def test_promoted_embedding_publication_queue_replay_and_vector(publication, runtime):
    kwargs, provider, queue = publication
    first = await promote_reflection_candidate_review(**kwargs)
    assert first.success and first.metadata["embedding_backfill"]["status"] == "queued"
    entity = await runtime.entity_manager.get(first.promoted_id)
    assert entity.embedding is None
    assert entity.id in await available_graph_entities("org", [entity.id], runtime=runtime)
    queue.enqueue_entity_embedding_backfill.side_effect = RuntimeError("queue unavailable")
    failed = await promote_reflection_candidate_review(**kwargs)
    assert failed.success and failed.metadata["embedding_backfill"]["status"] == "failed"
    queue.enqueue_entity_embedding_backfill.side_effect = None
    repaired = await promote_reflection_candidate_review(**kwargs)
    assert repaired.success and repaired.metadata["embedding_backfill"]["status"] == "queued"
    assert queue.enqueue_entity_embedding_backfill.await_count == 3
    runtime.entity_manager._embedding_provider = provider
    assert await runtime.entity_manager.backfill_embeddings_if_current([entity]) == [entity.id]
    current = await runtime.entity_manager.get(entity.id)
    assert current.embedding and len(current.embedding) == 1024
    assert current.id in await available_graph_entities("org", [current.id], runtime=runtime)
    assert await runtime.entity_manager.backfill_embeddings_if_current([current]) == [current.id]
    ready = await promote_reflection_candidate_review(**kwargs)
    assert ready.metadata["embedding_backfill"]["status"] == "ready"
    assert queue.enqueue_entity_embedding_backfill.await_count == 3


@pytest.mark.parametrize(
    "mutation", ["retired", "revised", "association", "replacement", "authority"]
)
async def test_promoted_embedding_provider_race_refuses_stale_vector(
    publication, runtime, cohort_sources, content_store, monkeypatch, mutation
):
    kwargs, provider, _ = publication
    result = await promote_reflection_candidate_review(**kwargs)
    assert result.success
    entity = await runtime.entity_manager.get(result.promoted_id)
    original = provider.embed_texts

    async def mutate(texts, **options):
        if mutation == "association":
            await runtime.client.execute_query(
                "UPDATE memory_derivations SET active=false WHERE target_id=$id;", id=entity.id
            )
        elif mutation == "replacement":
            await runtime.client.execute_query(
                "UPDATE memory_derivations SET authority_ceiling.projects=['new-project'] WHERE target_id=$id;",
                id=entity.id,
            )
        elif mutation == "authority":
            monkeypatch.setattr(
                "sibyl_core.services.graph_derivations.get_source_authority_resolver",
                lambda: AsyncMock(return_value=None),
            )
        elif mutation == "retired":
            await content_store.execute_query(
                "UPDATE raw_captures SET deleted_at=time::now() WHERE uuid=$id;",
                id=cohort_sources[0].id,
            )
        else:
            await save_raw_memory(
                replace(cohort_sources[0], raw_content="revised source"),
                expected_revision=cohort_sources[0].revision,
                embedding_provider=None,
            )
        return await original(texts, **options)

    monkeypatch.setattr(provider, "embed_texts", mutate)
    runtime.entity_manager._embedding_provider = provider
    assert await runtime.entity_manager.backfill_embeddings_if_current([entity]) == []
    assert not (await runtime.entity_manager.get(entity.id)).embedding
    available = await available_graph_entities("org", [entity.id], runtime=runtime)
    assert (entity.id in available) == (mutation == "replacement")


async def test_promoted_embedding_disabled_keeps_lexical_publication(
    publication, runtime, monkeypatch
):
    kwargs, _, queue = publication
    monkeypatch.setattr(memory_embedding, "configured_embedding_provider", lambda: None)
    result = await promote_reflection_candidate_review(**kwargs)
    assert result.success and result.metadata["embedding_backfill"]["status"] == "disabled"
    queue.enqueue_entity_embedding_backfill.assert_not_awaited()
    assert result.promoted_id in await available_graph_entities(
        "org", [result.promoted_id], runtime=runtime
    )


@pytest.mark.parametrize("phase", ["unchanged", "before_write", "during_transaction"])
async def test_promoted_embedding_native_association_write_fence(
    publication, runtime, monkeypatch, phase
):
    import asyncio
    import os

    if not os.environ.get("SIBYL_COHORT_NATIVE_URL"):
        pytest.skip("native concurrent transaction control")
    kwargs, provider, _ = publication
    result = await promote_reflection_candidate_review(**kwargs)
    assert result.success
    entity = await runtime.entity_manager.get(result.promoted_id)
    runtime.entity_manager._embedding_provider = provider
    execute = runtime.client.execute_query
    observed = False

    async def changed():
        rows = await execute(
            "UPDATE memory_derivations SET active=false WHERE target_id=$id RETURN AFTER;",
            id=entity.id,
        )
        assert len(rows) == 1 and rows[0]["active"] is False

    async def race(query, **params):
        nonlocal observed
        if "$expected_fingerprint" not in query or observed:
            return await execute(query, **params)
        observed = True
        if phase == "before_write":
            await changed()
        if phase != "during_transaction":
            return await execute(query, **params)
        pending = asyncio.create_task(
            execute(
                query.replace(
                    "LET $source_states_to_fence = $states;",
                    "SLEEP 1s; LET $source_states_to_fence = $states;",
                    1,
                ),
                **params,
            )
        )
        try:
            await asyncio.sleep(0.25)
            await changed()
            return await pending
        finally:
            if not pending.done():
                await pending

    monkeypatch.setattr(runtime.client, "execute_query", race)
    try:
        ids = await runtime.entity_manager.backfill_embeddings_if_current([entity])
    except Exception as exc:
        assert phase == "during_transaction" and "conflict" in str(exc).lower()
        ids = []
    assert observed
    assert ids == ([entity.id] if phase == "unchanged" else [])
    assert bool((await runtime.entity_manager.get(entity.id)).embedding) == (phase == "unchanged")


async def test_promoted_embedding_manager_retains_explicit_client(
    publication, runtime, cohort_sources, content_store, monkeypatch
):
    kwargs, provider, _queue = publication
    result = await promote_reflection_candidate_review(**kwargs)
    assert result.success
    entity = await runtime.entity_manager.get(result.promoted_id)
    factory = AsyncMock(side_effect=AssertionError("manager must retain its scoped client"))
    monkeypatch.setattr(
        "sibyl_core.services.graph_read_availability.get_surreal_graph_runtime", factory
    )
    runtime.entity_manager._embedding_provider = provider
    assert await runtime.entity_manager.backfill_embeddings_if_current([entity]) == [entity.id]
    current = await runtime.entity_manager.get(entity.id)
    assert current.embedding
    await content_store.execute_query(
        "UPDATE raw_captures SET deleted_at=time::now() WHERE uuid=$id;", id=cohort_sources[0].id
    )
    assert await runtime.entity_manager.backfill_embeddings_if_current([current]) == []
    factory.assert_not_awaited()
