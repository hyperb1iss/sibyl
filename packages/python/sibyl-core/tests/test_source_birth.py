"""Derived capture admission under source corrections and concurrent writes."""

from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import EMBEDDING_DIM, bootstrap_content_schema
from sibyl_core.memory_pipeline.source_lifecycle import (
    CORRECTION_BLOCKERS_KEY,
    SOURCE_BINDINGS_KEY,
    SOURCE_VALIDATION_PENDING_KEY,
)
from sibyl_core.models.reflection import ReflectionCandidate
from sibyl_core.services import content_client, content_raw_persistence
from sibyl_core.services.graph_client import SurrealGraphClient, prepare_graph_schema
from sibyl_core.services.graph_entities import EntityManager
from sibyl_core.services.graph_relationships import RelationshipManager
from sibyl_core.services.graph_runtime import GraphRuntime
from sibyl_core.services.memory_correction import apply_memory_correction
from sibyl_core.services.surreal_content import (
    get_raw_memory,
    raw_memory_recallable,
    remember_raw_memory,
    remember_reflection_candidate_review,
)


@pytest.fixture
async def store(monkeypatch):
    org = str(uuid4())
    content = SurrealContentClient(url="memory://")
    graph = SurrealGraphClient(group_id=org, url="memory://")
    try:
        await bootstrap_content_schema(content, reset=True)
        await prepare_graph_schema(graph)
        runtime = GraphRuntime(
            client=graph,
            entity_manager=EntityManager(graph, group_id=org),
            relationship_manager=RelationshipManager(graph, group_id=org),
        )

        @asynccontextmanager
        async def session():
            yield content

        monkeypatch.setattr(content_client, "surreal_content_client", session)
        monkeypatch.setattr(
            "sibyl_core.services.memory_lifecycle.get_surreal_graph_runtime",
            AsyncMock(return_value=runtime),
        )
        yield SimpleNamespace(org=org, content=content)
    finally:
        await content.close()
        await graph.close()


async def source(store, *, owner="owner", content="Source advice"):
    return await remember_raw_memory(
        organization_id=store.org,
        principal_id=owner,
        source_id="source",
        raw_content=content,
    )


async def review(store, sources, *, snapshots=(), owner="owner", accessible_projects=None):
    return await remember_reflection_candidate_review(
        organization_id=store.org,
        principal_id=owner,
        raw_source_ids=[item.id for item in sources],
        source_memories=snapshots,
        accessible_projects=accessible_projects,
        candidate=ReflectionCandidate(
            kind="procedure",
            title="Advice",
            content="Derived advice",
            reason="Evidence",
            confidence=1.0,
        ),
    )


async def correct(store, root, action, **kwargs):
    return await apply_memory_correction(
        organization_id=store.org,
        principal_id="owner",
        source_id=root.id,
        action=action,
        **kwargs,
    )


@pytest.mark.parametrize("snapshot", ["current", "stale", "absent"])
async def test_review_created_after_hide_follows_later_restore(store, snapshot, monkeypatch):
    from sibyl_core.embeddings.providers import DeterministicEmbeddingProvider

    root = await source(store)
    hidden = (await correct(store, root, "hide")).updated_memory
    snapshots = [hidden] if snapshot == "current" else [root] if snapshot == "stale" else []
    child = await review(store, [root], snapshots=snapshots)
    assert not raw_memory_recallable(child)
    assert child.metadata[SOURCE_VALIDATION_PENDING_KEY] is False
    assert child.metadata[CORRECTION_BLOCKERS_KEY][root.id]["blocking"] is True
    assert child.embedding is None
    provider = DeterministicEmbeddingProvider(
        replace(DeterministicEmbeddingProvider().metadata, dimensions=EMBEDDING_DIM)
    )
    monkeypatch.setattr(
        content_raw_persistence.models, "configured_raw_memory_embedding_provider", lambda: provider
    )
    await correct(store, root, "restore")
    restored = await get_raw_memory(organization_id=store.org, memory_id=child.id)
    assert raw_memory_recallable(restored)
    assert not restored.metadata.get("excluded_from_recall")
    expected = await provider.embed_texts(
        [
            content_raw_persistence.models.raw_memory_embedding_text(
                title=child.title, raw_content=child.raw_content
            )
        ],
        input_kind="document",
    )
    assert restored.embedding == pytest.approx(expected[0])


@pytest.mark.parametrize("race", ["insert", "admit"])
@pytest.mark.parametrize("action", ["hide", "revise"])
@pytest.mark.parametrize("owner", ["owner", "another-owner"])
async def test_correction_during_birth_cannot_publish_stale_evidence(
    store, monkeypatch, race, action, owner
):
    root = await remember_raw_memory(
        organization_id=store.org,
        principal_id="owner",
        source_id="project-source",
        raw_content="Shared project advice",
        memory_scope="project",
        scope_key="project-a",
    )
    raced = False
    original_insert = content_client.replace_record
    original_save = content_raw_persistence.save_raw_memory

    async def fire():
        nonlocal raced
        raced = True
        await correct(
            store,
            root,
            action,
            accessible_projects={"project-a"},
            **({"revised_content": "Corrected advice"} if action == "revise" else {}),
        )

    async def insert(client, table, **kwargs):
        record = await original_insert(client, table, **kwargs)
        if not raced and record.get("metadata", {}).get("raw_source_ids"):
            born = content_raw_persistence.models.raw_memory_from_record(record)
            assert not raw_memory_recallable(born)
            await fire()
        return record

    async def save(memory, **kwargs):
        if not raced and memory.metadata.get("raw_source_ids"):
            await fire()
        return await original_save(memory, **kwargs)

    if race == "insert":
        monkeypatch.setattr(content_client, "replace_record", insert)
    else:
        monkeypatch.setattr(content_raw_persistence, "save_raw_memory", save)
    child = await review(
        store, [root], snapshots=[root], owner=owner, accessible_projects={"project-a"}
    )
    assert raced
    assert not raw_memory_recallable(child)
    assert child.metadata[SOURCE_BINDINGS_KEY][root.id] == root.revision
    await correct(store, root, "restore", accessible_projects={"project-a"})
    restored = await get_raw_memory(organization_id=store.org, memory_id=child.id)
    assert raw_memory_recallable(restored) is (action == "hide")


async def test_unreadable_source_stays_pending_without_exposing_its_clock(store):
    foreign = await source(store, owner="someone-else")
    child = await review(store, [foreign])
    assert not raw_memory_recallable(child)
    assert child.metadata[SOURCE_VALIDATION_PENDING_KEY] is True
    assert foreign.id not in child.metadata.get(CORRECTION_BLOCKERS_KEY, {})
    assert foreign.id not in child.metadata.get(SOURCE_BINDINGS_KEY, {})


async def test_old_and_new_review_paths_preserve_the_oldest_content_epoch(store):
    root = await source(store)
    old = await review(store, [root], snapshots=[root])
    revised = (await correct(store, root, "revise", revised_content="New advice")).updated_memory
    new = await review(store, [revised], snapshots=[revised])
    assert raw_memory_recallable(new)
    combined = await review(store, [old, new], snapshots=[old, new])
    assert not raw_memory_recallable(combined)
    assert combined.metadata[SOURCE_BINDINGS_KEY][root.id] == root.revision
    await correct(store, root, "restore")
    combined = await get_raw_memory(organization_id=store.org, memory_id=combined.id)
    assert not raw_memory_recallable(combined)


async def test_projection_inherits_pending_source_validation(store):
    from sibyl_core.memory_pipeline.lifecycle import (
        graph_lifecycle_stamp,
        graph_metadata_recallable,
    )
    from sibyl_core.projection.inheritance import inherited_lifecycle_metadata

    foreign = await source(store, owner="someone-else")
    child = await review(store, [foreign])
    stamp = graph_lifecycle_stamp(child)
    assert stamp[SOURCE_VALIDATION_PENDING_KEY] is True
    assert not graph_metadata_recallable(stamp)
    inherited = inherited_lifecycle_metadata(stamp)
    assert inherited[SOURCE_VALIDATION_PENDING_KEY] is True
    assert not graph_metadata_recallable(inherited)


async def test_bulk_capture_creation_cannot_bypass_source_validation(store):
    from sibyl_core.services.content_models import RawMemoryWrite

    root = await source(store)
    await correct(store, root, "hide")
    writes = [
        RawMemoryWrite(
            organization_id=store.org,
            principal_id="owner",
            source_id=f"derived-{index}",
            raw_content="Derived advice",
            metadata={"raw_source_ids": [root.id]},
        )
        for index in range(3)
    ]
    children = await content_raw_persistence.remember_raw_memories(writes)
    assert len(children) == 3
    assert all(not raw_memory_recallable(child) for child in children)
    await correct(store, root, "restore")
    for child in children:
        restored = await get_raw_memory(organization_id=store.org, memory_id=child.id)
        assert raw_memory_recallable(restored)


async def test_promotion_retry_embeds_a_newly_admitted_capture(store, monkeypatch):
    from sibyl_core.embeddings.providers import DeterministicEmbeddingProvider
    from sibyl_core.services.memory_reflection import _resolve_reflection_promotion_plan

    root = await remember_raw_memory(
        organization_id=store.org,
        principal_id="owner",
        source_id="project-source",
        raw_content="Verified project advice",
        memory_scope="project",
        scope_key="project-a",
    )
    child = await review(store, [root], snapshots=[root])
    assert child.metadata[SOURCE_VALIDATION_PENDING_KEY] is True
    assert child.embedding is None
    provider = DeterministicEmbeddingProvider(
        replace(DeterministicEmbeddingProvider().metadata, dimensions=EMBEDDING_DIM)
    )
    embed = AsyncMock(wraps=provider.embed_texts)
    monkeypatch.setattr(provider, "embed_texts", embed)
    monkeypatch.setattr(
        content_raw_persistence.models,
        "configured_raw_memory_embedding_provider",
        lambda: provider,
    )

    plan = await _resolve_reflection_promotion_plan(
        candidate_id=child.id,
        organization_id=store.org,
        principal_id="owner",
        promote_to_scope="project",
        promote_to_scope_key="project-a",
        accessible_projects={"project-a"},
    )

    stored = await get_raw_memory(organization_id=store.org, memory_id=child.id)
    assert stored.metadata[SOURCE_VALIDATION_PENDING_KEY] is False
    assert raw_memory_recallable(stored)
    assert stored.embedding is not None
    assert len(stored.embedding) == provider.metadata.dimensions
    assert plan.candidate_memory.embedding == stored.embedding
    assert stored.metadata["embedding_metadata"]["model"] == provider.metadata.model
    embed.assert_awaited_once_with(
        [
            content_raw_persistence.models.raw_memory_embedding_text(
                title=stored.title, raw_content=stored.raw_content
            )
        ],
        input_kind="document",
    )


@pytest.mark.parametrize("embedding_mode", ["configured", "disabled", "explicit"])
async def test_admission_retry_preserves_embedding_provider_selection(
    store, monkeypatch, embedding_mode
):
    from sibyl_core.embeddings.providers import DeterministicEmbeddingProvider
    from sibyl_core.services.memory_source_validation import reconcile_raw_source_lifecycle

    root = await remember_raw_memory(
        organization_id=store.org,
        principal_id="owner",
        source_id="project-source",
        raw_content="Verified project advice",
        memory_scope="project",
        scope_key="project-a",
    )
    child = await review(store, [root], snapshots=[root])
    assert child.embedding is None
    assert not raw_memory_recallable(child)
    provider = DeterministicEmbeddingProvider(
        replace(DeterministicEmbeddingProvider().metadata, dimensions=EMBEDDING_DIM)
    )
    embed = AsyncMock(wraps=provider.embed_texts)
    monkeypatch.setattr(provider, "embed_texts", embed)
    configured = Mock(return_value=provider)
    monkeypatch.setattr(
        content_raw_persistence.models, "configured_raw_memory_embedding_provider", configured
    )
    options = {}
    if embedding_mode == "disabled":
        options["embedding_provider"] = None
    elif embedding_mode == "explicit":
        options["embedding_provider"] = provider
    admitted = await reconcile_raw_source_lifecycle(
        child,
        principal_id="owner",
        accessible_projects={"project-a"},
        **options,
    )
    stored = await get_raw_memory(organization_id=store.org, memory_id=child.id)
    assert raw_memory_recallable(stored)
    assert stored.embedding == admitted.embedding
    if embedding_mode == "disabled":
        assert stored.embedding is None
        embed.assert_not_awaited()
    else:
        assert stored.embedding is not None
        embed.assert_awaited_once()
    if embedding_mode == "configured":
        configured.assert_called_once_with()
    else:
        configured.assert_not_called()


@pytest.mark.parametrize("action", ["hide", "revise"])
async def test_shared_source_correction_retires_private_derivative_without_disclosure(
    store, action
):
    root = await remember_raw_memory(
        organization_id=store.org,
        principal_id="owner",
        source_id="project-source",
        raw_content="Shared project advice",
        memory_scope="project",
        scope_key="project-a",
    )
    child = await remember_reflection_candidate_review(
        organization_id=store.org,
        principal_id="another-owner",
        raw_source_ids=[root.id],
        source_memories=[root],
        accessible_projects={"project-a"},
        candidate=ReflectionCandidate(
            kind="procedure",
            title="Private derived advice",
            content="Keep this owner's operational details private.",
            reason="Derived from the shared source",
            confidence=1.0,
        ),
    )
    assert raw_memory_recallable(child)
    result = await correct(
        store,
        root,
        action,
        accessible_projects={"project-a"},
        **({"revised_content": "Corrected shared advice"} if action == "revise" else {}),
    )
    stored = await get_raw_memory(organization_id=store.org, memory_id=child.id)
    assert not raw_memory_recallable(stored)
    assert stored.metadata[CORRECTION_BLOCKERS_KEY][root.id]["blocking"] is True
    assert child.id not in result.affected_raw_memory_ids
    assert child.id not in str(result)
    assert result.propagation_complete is True
