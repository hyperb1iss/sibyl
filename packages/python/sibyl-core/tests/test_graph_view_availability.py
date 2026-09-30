"""A final graph view retains only facts matching its recorded dependencies."""

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services import content_client
from sibyl_core.services import graph_view_availability as view
from sibyl_core.services.graph_read_availability import available_graph_relationships
from sibyl_core.services.graph_read_validation import GraphReadValidation
from sibyl_core.services.memory_correction import apply_memory_correction
from sibyl_core.services.surreal_content import remember_raw_memory, save_raw_memory
from tests.test_capture_corrections import captured_note
from tests.test_operational_projection import authority as authority
from tests.test_operational_projection import capture
from tests.test_reflection_identity import content_store as content_store
from tests.test_reflection_identity import runtime as runtime


async def _ordinary(runtime, *ids):
    await runtime.entity_manager.create_direct_bulk(
        [Entity(id=key, name=key, entity_type=EntityType.NOTE) for key in ids],
        generate_embeddings=False,
    )


async def _healthy_capture(runtime):
    memory = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="healthy-source",
        raw_content="Independent healthy evidence",
        embedding_provider=None,
    )
    node = Entity(
        id="healthy-capture-node",
        name="Healthy captured note",
        entity_type=EntityType.NOTE,
        metadata={"raw_memory_id": memory.id, "memory_scope": "private", "principal_id": "user_a"},
    )
    await runtime.entity_manager.create_direct(node, generate_embedding=False)
    return memory, node


async def test_final_view_source_retirement_after_node_proof_survives_failed_graph_stamp(
    runtime, content_store, monkeypatch
):
    memory, node = await captured_note(runtime, monkeypatch)
    _, healthy = await _healthy_capture(runtime)
    await _ordinary(runtime, "ordinary")
    ids = [node.id, healthy.id, "ordinary"]
    baseline, edges = await view.available_graph_view(
        runtime.client.group_id, ids, {}, runtime=runtime
    )
    assert set(baseline) == set(ids) and not edges
    original_node = await runtime.entity_manager.get(node.id)
    monkeypatch.setattr(
        runtime.entity_manager, "update", AsyncMock(side_effect=RuntimeError("stamp unavailable"))
    )
    snapshot = view._snapshot
    applied = False

    async def retire_at_final_capture(*args, **kwargs):
        nonlocal applied
        result = await apply_memory_correction(
            organization_id=runtime.client.group_id,
            source_id=memory.id,
            principal_id="user_a",
            action="delete",
        )
        assert result.applied and not result.propagation_complete
        applied = True
        return await snapshot(*args, **kwargs)

    monkeypatch.setattr(view, "_snapshot", retire_at_final_capture)
    nodes, edges = await view.available_graph_view(
        runtime.client.group_id, ids, {}, runtime=runtime
    )
    assert applied
    assert set(nodes) == {healthy.id, "ordinary"} and not edges
    assert (await runtime.entity_manager.get(node.id)).model_dump() == original_node.model_dump()


async def test_final_view_retired_transitive_capture_does_not_deny_a_healthy_peer(
    runtime, content_store, monkeypatch
):
    memory, node = await captured_note(runtime, monkeypatch)
    intermediate = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="intermediate",
        raw_content="Intermediate evidence",
        metadata={"raw_source_ids": [memory.id]},
        embedding_provider=None,
    )
    child = node.model_copy(
        update={
            "id": "transitive-child",
            "metadata": {
                "raw_memory_id": intermediate.id,
                "memory_scope": "private",
                "principal_id": "user_a",
            },
        }
    )
    await runtime.entity_manager.create_direct(child, generate_embedding=False)
    _, healthy = await _healthy_capture(runtime)
    await _ordinary(runtime, "ordinary")
    ids = [child.id, healthy.id, "ordinary"]
    assert set(
        (await view.available_graph_view(runtime.client.group_id, ids, {}, runtime=runtime))[0]
    ) == set(ids)
    snapshot = view._snapshot

    async def retire_ancestor(*args, **kwargs):
        tombstone = await save_raw_memory(
            replace(memory, deleted_at=datetime.now(UTC), revision=memory.revision + 1)
        )
        assert tombstone.deleted_at is not None
        return await snapshot(*args, **kwargs)

    monkeypatch.setattr(view, "_snapshot", retire_ancestor)
    nodes, edges = await view.available_graph_view(
        runtime.client.group_id, ids, {}, runtime=runtime
    )
    assert set(nodes) == {healthy.id, "ordinary"} and not edges


async def test_final_view_current_parent_change_denies_only_its_descendant(
    runtime, content_store, monkeypatch
):
    _, parent = await captured_note(runtime, monkeypatch)
    child = Entity(
        id="graph-child",
        name="Graph descendant",
        entity_type=EntityType.PASSAGE,
        metadata={
            "parent_entity_id": parent.id,
            "memory_scope": "private",
            "principal_id": "user_a",
        },
    )
    await runtime.entity_manager.create_direct(child, generate_embedding=False)
    _, healthy = await _healthy_capture(runtime)
    ids = [child.id, healthy.id]
    assert set(
        (await view.available_graph_view(runtime.client.group_id, ids, {}, runtime=runtime))[0]
    ) == set(ids)
    snapshot = view._snapshot

    async def change_parent(*args, **kwargs):
        await runtime.entity_manager.update(
            parent.id,
            {
                "metadata": {
                    **parent.metadata,
                    "principal_id": "other-user",
                }
            },
        )
        return await snapshot(*args, **kwargs)

    monkeypatch.setattr(view, "_snapshot", change_parent)
    nodes, edges = await view.available_graph_view(
        runtime.client.group_id, ids, {}, runtime=runtime
    )
    assert set(nodes) == {healthy.id} and not edges


@pytest.mark.parametrize(
    "mutation", ["unchanged", "edge_delete", "edge_body", "node_delete", "node_label"]
)
async def test_final_view_compares_nodes_and_edges_in_the_same_graph_capture(
    runtime, content_store, monkeypatch, mutation
):
    await _ordinary(runtime, "root", "changing", "healthy")
    changing = Relationship(
        id="changing-edge",
        source_id="root",
        target_id="changing",
        relationship_type=RelationshipType.RELATED_TO,
    )
    healthy = changing.model_copy(update={"id": "healthy-edge", "target_id": "healthy"})
    await runtime.relationship_manager.create_direct_bulk([changing, healthy])
    proven = await available_graph_relationships(
        runtime.client.group_id, [changing.id, healthy.id], runtime=runtime
    )
    assert set(proven) == {changing.id, healthy.id}
    snapshot = view._snapshot
    boundary_reached = False

    async def mutate_final_capture(*args, **kwargs):
        nonlocal boundary_reached
        boundary_reached = True
        if mutation == "edge_delete":
            await runtime.relationship_manager.delete(changing.id)
        elif mutation == "edge_body":
            await runtime.relationship_manager.create_direct_bulk(
                [changing.model_copy(update={"relationship_type": RelationshipType.DEPENDS_ON})]
            )
        elif mutation == "node_delete":
            await runtime.entity_manager.delete(changing.target_id)
        elif mutation == "node_label":
            await runtime.entity_manager.update(changing.target_id, {"name": "Replacement label"})
        return await snapshot(*args, **kwargs)

    monkeypatch.setattr(view, "_snapshot", mutate_final_capture)
    nodes, edges = await view.available_graph_view(
        runtime.client.group_id, ["root", "changing", "healthy"], proven, runtime=runtime
    )
    assert boundary_reached
    assert set(nodes) == (
        {"root", "healthy"} if mutation.startswith("node_") else {"root", "changing", "healthy"}
    )
    assert set(edges) == ({changing.id, healthy.id} if mutation == "unchanged" else {healthy.id})


def test_recorded_dependency_dag_isolates_shared_roots_and_conflicting_inputs():
    read = GraphReadValidation("org")

    def identity(kind, key):
        return SourceIdentity("org", kind, key)

    a = identity(SourceKind.GRAPH_ENTITY, "a")
    b = identity(SourceKind.GRAPH_ENTITY, "b")
    healthy = identity(SourceKind.GRAPH_ENTITY, "healthy")
    raw = identity(SourceKind.RAW_CAPTURE, "raw")
    ancestor = identity(SourceKind.RAW_CAPTURE, "ancestor")
    read.depend_on(a, [raw])
    read.depend_on(b, [raw])
    read.depend_on(raw, [ancestor])
    assert read.affected(a, {ancestor}) and read.affected(b, {ancestor})
    assert not read.affected(healthy, {ancestor})
    node = Entity(id="a", name="Original", entity_type=EntityType.NOTE, organization_id="org")
    read.record_entity(node)
    read.record_entity(node.model_copy(update={"name": "Replacement"}))
    assert read.affected(a, read.conflicts)
    assert not read.affected(healthy, read.conflicts)


@pytest.mark.parametrize("mutation", ["generation_republish", "canonical_body_without_revision"])
async def test_final_view_keeps_protected_generation_bound_to_canonical_source(
    runtime, content_store, authority, monkeypatch, mutation
):
    monkeypatch.setattr(
        "sibyl_core.services.operational_relationships.get_source_authority_resolver",
        lambda: AsyncMock(return_value=authority),
    )
    memory, source = await capture(runtime, authority)
    projection = await runtime.entity_manager.publish_operational_entities(source)
    await runtime.relationship_manager.publish_operational_relationships(source)
    protected = next(
        edge
        for edge in projection.relationships
        if edge.relationship_type is RelationshipType.PART_OF
    )
    _, healthy = await _healthy_capture(runtime)
    await _ordinary(runtime, "ordinary")
    ids = [protected.source_id, protected.target_id, healthy.id, "ordinary"]
    proven = await available_graph_relationships(
        runtime.client.group_id, [protected.id], runtime=runtime
    )
    assert set(proven) == {protected.id}
    baseline_nodes, baseline_edges = await view.available_graph_view(
        runtime.client.group_id, ids, proven, runtime=runtime
    )
    assert set(baseline_nodes) == set(ids) and set(baseline_edges) == {protected.id}
    before = await runtime.client.execute_query(
        "SELECT * FROM relates_to WHERE uuid=$id;", id=protected.id
    )
    snapshot = view._snapshot
    changed = False

    async def change_source_after_proofs(*args, **kwargs):
        nonlocal changed
        changed = True
        if mutation == "generation_republish":
            updated, replacement = await capture(
                runtime, authority, revision=memory.revision, metadata={"encoding_revision": 2}
            )
            assert updated.id == memory.id and updated.revision > memory.revision
            await runtime.entity_manager.publish_operational_entities(replacement)
        else:
            async with content_client.surreal_content_client() as client:
                assert await client.execute_query(
                    "UPDATE raw_captures SET raw_content='replaced canonical evidence' WHERE uuid=$id;",
                    id=memory.id,
                )
        return await snapshot(*args, **kwargs)

    monkeypatch.setattr(view, "_snapshot", change_source_after_proofs)
    nodes, edges = await view.available_graph_view(
        runtime.client.group_id, ids, proven, runtime=runtime
    )
    assert changed
    assert before == await runtime.client.execute_query(
        "SELECT * FROM relates_to WHERE uuid=$id;", id=protected.id
    )
    assert set(nodes) == {healthy.id, "ordinary"} and not edges
