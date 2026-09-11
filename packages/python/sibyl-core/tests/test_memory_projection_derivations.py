from unittest.mock import AsyncMock

import pytest

from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.models.memory_extraction import ExtractedMemoryEntity
from sibyl_core.models.reflection import ReflectionCandidate
from sibyl_core.projection.memory import (
    project_extracted_memory_entities,
    project_memory_entities,
    project_memory_entity,
)
from sibyl_core.services.eval_publication_guards import unavailable_publication_ids
from sibyl_core.services.memory_source_validation import SourceReadAuthority
from sibyl_core.tools.reflect import reflect_memory
from tests.test_synthesis_source_observations import content_store as content_store
from tests.test_synthesis_source_observations import (
    disable_embeddings_and_bind_runtime as disable_embeddings_and_bind_runtime,
)
from tests.test_synthesis_source_observations import materialized_synthesis
from tests.test_synthesis_source_observations import runtime as runtime


async def protected_session(runtime, monkeypatch, *, source_id="projection-source"):
    resolver = AsyncMock(
        return_value=SourceReadAuthority("user_a", projects=frozenset({"project_a"}))
    )
    monkeypatch.setattr(
        "sibyl_core.services.graph_derivations.get_source_authority_resolver", lambda: resolver
    )
    body = "Alice prefers Earl Grey tea. Alice visited Paris yesterday. We use SurrealDB for graph storage."
    source = Entity(
        id=source_id,
        entity_type=EntityType.SESSION,
        name="Original source",
        content=body,
        metadata={
            "memory_scope": "project",
            "scope_key": "project_a",
            "project_id": "project_a",
            "principal_id": "user_a",
        },
    )
    await runtime.entity_manager.create_direct(source)

    class Extractor:
        async def extract(self, request):
            return [
                ReflectionCandidate(
                    kind="session",
                    title="Derived session",
                    content=request.content,
                    reason="retain evidence",
                    confidence=1.0,
                    tags=[],
                )
            ]

    pack = await reflect_memory(
        body,
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        project="project_a",
        accessible_projects={"project_a"},
        writable_projects={"project_a"},
        memory_scope="project",
        scope_key="project_a",
        existing_source_id=source.id,
        persist=True,
        persist_source=False,
        extractor=Extractor(),
    )
    assert pack.persisted_count == 1
    target = await runtime.entity_manager.get(pack.candidates[0].persisted_id)
    return source, target, resolver


async def project(runtime, source):
    return await project_memory_entity(
        entity_manager=runtime.entity_manager,
        relationship_manager=runtime.relationship_manager,
        source=source,
        group_id=runtime.client.group_id,
        generate_embeddings=False,
    )


@pytest.mark.parametrize("change", ["content", "hide", "recreate", "authority", "markers"])
async def test_memory_handles_retain_protected_ancestry(
    runtime, content_store, monkeypatch, change
):
    source, target, resolver = await protected_session(runtime, monkeypatch)
    result = await project(runtime, target)
    assert result.projected_entities > 0 and not result.errors
    handle = await runtime.entity_manager.get(result.created_projected_entities[0].id)
    assert not await unavailable_publication_ids(
        runtime.client.group_id, {target.id: target.metadata, handle.id: handle.metadata}
    )
    if change == "authority":
        resolver.return_value = SourceReadAuthority("user_a")
    elif change == "hide":
        await runtime.entity_manager.update(
            source.id, {"metadata": {**source.metadata, "excluded_from_recall": True}}
        )
    elif change == "recreate":
        await runtime.entity_manager.delete(source.id)
        await runtime.entity_manager.create_direct(source)
    else:
        if change == "markers":
            await runtime.entity_manager.update(handle.id, {"metadata": {"source_entity_id": None}})
        await runtime.entity_manager.update(
            source.id, {"content": "The source evidence was revoked."}
        )
    denied = await unavailable_publication_ids(
        runtime.client.group_id, {target.id: target.metadata, handle.id: handle.metadata}
    )
    assert target.id in denied and handle.id in denied
    materialized = await materialized_synthesis(
        runtime,
        handle.id,
        handle.entity_type.value,
        handle.revision,
        handle.content,
        accessible_projects={"project_a"},
    )
    assert not any(pack.sources for pack in materialized.source_packs)


async def test_protected_named_handles_keep_independent_evidence_owners(
    runtime, content_store, monkeypatch
):
    first_source, first, _ = await protected_session(runtime, monkeypatch, source_id="first-source")
    _, second, _ = await protected_session(runtime, monkeypatch, source_id="second-source")
    one, two = await project(runtime, first), await project(runtime, second)
    first_ids = {row.id for row in one.created_projected_entities}
    second_ids = {row.id for row in two.created_projected_entities}
    assert first_ids and second_ids and not first_ids.intersection(second_ids)
    assert {row.name for row in one.created_projected_entities}.intersection(
        row.name for row in two.created_projected_entities
    )
    await runtime.entity_manager.update(first_source.id, {"content": "Withdraw first evidence."})
    rows = {
        row.id: row.metadata
        for row in (*one.created_projected_entities, *two.created_projected_entities)
    }
    denied = await unavailable_publication_ids(runtime.client.group_id, rows)
    assert first_ids <= denied and not second_ids.intersection(denied)


async def test_protected_projection_parent_change_rolls_back_handles(
    runtime, content_store, monkeypatch
):
    _, target, _ = await protected_session(runtime, monkeypatch)
    writer = runtime.entity_manager.create_direct_bulk
    attempted = []

    async def change_before_write(rows, **kwargs):
        if kwargs.get("projection_source"):
            attempted.extend(row.id for row in rows)
            await runtime.entity_manager.update(target.id, {"content": "New parent body."})
        return await writer(rows, **kwargs)

    monkeypatch.setattr(runtime.entity_manager, "create_direct_bulk", change_before_write)
    result = await project(runtime, target)
    assert result.errors and attempted
    assert not await runtime.entity_manager.get_many(attempted)


@pytest.mark.parametrize("mutate", [False, True])
async def test_external_extraction_keeps_captured_parent(
    runtime, content_store, monkeypatch, mutate
):
    from sibyl_core.models.memory_extraction import ExtractedMemoryEntity
    from sibyl_core.projection.memory import project_extracted_memory_entities

    source, target, _ = await protected_session(runtime, monkeypatch)
    captured = await runtime.entity_manager.load_projection_source(target.id)
    assert captured is not None
    if mutate:
        await runtime.entity_manager.update(
            target.id, {"content": "The parent changed during extraction."}
        )
    result = await project_extracted_memory_entities(
        entity_manager=runtime.entity_manager,
        relationship_manager=runtime.relationship_manager,
        sources=[target],
        group_id=runtime.client.group_id,
        extractions_by_source_id={
            target.id: [
                ExtractedMemoryEntity(
                    name="SurrealDB",
                    entity_type="tool",
                    summary="Native graph database",
                    confidence=0.9,
                )
            ]
        },
        projection_sources={target.id: captured},
        generate_embeddings=False,
    )
    if mutate:
        assert result.errors and not result.created_projected_entities
    else:
        assert not result.errors and result.created_projected_entities
        await runtime.entity_manager.update(source.id, {"content": "Withdraw original evidence."})
        rows = {row.id: row.metadata for row in result.created_projected_entities}
        assert set(rows) <= await unavailable_publication_ids(runtime.client.group_id, rows)


async def test_external_extraction_cannot_rebind_protected_parent(
    runtime, content_store, monkeypatch
):
    from sibyl_core.models.memory_extraction import ExtractedMemoryEntity
    from sibyl_core.projection.memory import project_extracted_memory_entities

    _, target, _ = await protected_session(runtime, monkeypatch)
    with pytest.raises(ValueError, match="pre-extraction observation"):
        await project_extracted_memory_entities(
            entity_manager=runtime.entity_manager,
            relationship_manager=runtime.relationship_manager,
            sources=[target],
            group_id=runtime.client.group_id,
            extractions_by_source_id={
                target.id: [
                    ExtractedMemoryEntity(
                        name="SurrealDB",
                        entity_type="tool",
                        summary="Native graph database",
                        confidence=0.9,
                    )
                ]
            },
            generate_embeddings=False,
        )


async def test_memory_handle_reprojection_preserves_only_same_evidence(
    runtime, content_store, monkeypatch
):
    from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind
    from sibyl_core.services.source_state_store import load_source_snapshot

    _, target, _ = await protected_session(runtime, monkeypatch)
    result = await project(runtime, target)
    handle = result.created_projected_entities[0]

    async def observe():
        return await load_source_snapshot(
            SourceIdentity(runtime.client.group_id, SourceKind.GRAPH_ENTITY, handle.id),
            organization_id=runtime.client.group_id,
            execute_query=runtime.client.execute_query,
        )

    first = await observe()
    repeated = await project(runtime, target)
    assert not repeated.errors
    assert (await observe()).observation.same_evidence(first.observation)
    await runtime.client.execute_query(
        "UPDATE entity SET attributes.excluded_from_recall=true WHERE uuid=$id; UPDATE entity SET attributes.excluded_from_recall=false WHERE uuid=$id;",
        id=target.id,
    )
    replayed = await project(runtime, target)
    assert not replayed.errors
    assert (await observe()).observation.generation > first.observation.generation


@pytest.mark.parametrize("mismatch", ["source", "organization"])
async def test_wrong_capture_cannot_bind_another_source(
    runtime, content_store, monkeypatch, mismatch
):
    _, a, _ = await protected_session(runtime, monkeypatch, source_id="source_a")
    _, b, _ = await protected_session(runtime, monkeypatch, source_id="source_b")
    captured = await runtime.entity_manager.load_projection_source(
        b.id if mismatch == "source" else a.id
    )
    if mismatch == "organization":
        from dataclasses import replace

        from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind

        snapshot, association = captured
        captured = (
            replace(
                snapshot,
                observation=replace(
                    snapshot.observation,
                    source=SourceIdentity("other-organization", SourceKind.GRAPH_ENTITY, a.id),
                ),
            ),
            association,
        )
    result = await project_extracted_memory_entities(
        entity_manager=runtime.entity_manager,
        relationship_manager=runtime.relationship_manager,
        sources=[a],
        group_id=runtime.client.group_id,
        extractions_by_source_id={
            a.id: [
                ExtractedMemoryEntity(
                    name="SurrealDB", entity_type="tool", summary="Graph database", confidence=0.9
                )
            ]
        },
        projection_sources={a.id: captured},
        generate_embeddings=False,
    )
    assert result.errors and not result.created_projected_entities
    assert not await runtime.client.execute_query(
        "SELECT uuid FROM entity WHERE attributes.category='memory_projection';"
    )


async def test_projection_waits_for_all_owned_concurrent_writes(
    runtime, content_store, monkeypatch
):
    import asyncio

    _, a, _ = await protected_session(runtime, monkeypatch, source_id="failure_parent")
    _, b, _ = await protected_session(runtime, monkeypatch, source_id="slow_parent")
    original = runtime.entity_manager.create_direct_bulk
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def writer(rows, **kwargs):
        captured = kwargs.get("projection_source")
        if captured and captured[0].entity.id == a.id:
            raise RuntimeError("Controlled first-source failure")
        if captured and captured[0].entity.id == b.id:
            started.set()
            await release.wait()
            try:
                return await original(rows, **kwargs)
            finally:
                finished.set()
        return await original(rows, **kwargs)

    monkeypatch.setattr(runtime.entity_manager, "create_direct_bulk", writer)
    operation = asyncio.create_task(
        project_memory_entities(
            entity_manager=runtime.entity_manager,
            relationship_manager=runtime.relationship_manager,
            sources=[a, b],
            group_id=runtime.client.group_id,
            generate_embeddings=False,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 2)
        done, _ = await asyncio.wait({operation}, timeout=0.15)
        returned_before_owned_write_finished = bool(done) and not finished.is_set()
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 2)
        await operation
    assert not returned_before_owned_write_finished, (
        "projection returned its terminal partial receipt while another owned source write remained live"
    )
