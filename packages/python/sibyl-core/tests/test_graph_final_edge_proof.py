"""Final public edge reads require the current protected source generation."""

from importlib import import_module
from unittest.mock import AsyncMock

import pytest

from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services import graph_read_availability as availability
from sibyl_core.services import graph_view_availability
from sibyl_core.services.graph_read_availability import (
    available_graph_entities,
    available_graph_relationships,
)
from tests.test_operational_projection import authority as authority
from tests.test_operational_projection import capture
from tests.test_operational_projection import content_store as content_store
from tests.test_operational_projection import runtime as runtime

exploration = import_module("sibyl_core.tools.explore")


@pytest.fixture(autouse=True)
def relationship_authority(monkeypatch, authority):
    monkeypatch.setattr(
        "sibyl_core.services.operational_relationships.get_source_authority_resolver",
        lambda: AsyncMock(return_value=authority),
    )


@pytest.mark.parametrize("republish_edges", [False, True])
async def test_related_final_edge_proof_denies_prior_generation_with_current_endpoints(
    runtime, content_store, authority, monkeypatch, republish_edges
):
    memory, source = await capture(runtime, authority)
    projection = await runtime.entity_manager.publish_operational_entities(source)
    await runtime.relationship_manager.publish_operational_relationships(source)
    protected = next(
        edge
        for edge in projection.relationships
        if edge.relationship_type == RelationshipType.PART_OF
    )
    ordinary = Entity(
        id="ordinary-neighbor", name="Independent resource", entity_type=EntityType.NOTE
    )
    await runtime.entity_manager.create_direct(ordinary, generate_embedding=False)
    ordinary_edge = Relationship(
        id="ordinary-edge",
        source_id=protected.source_id,
        target_id=ordinary.id,
        relationship_type=RelationshipType.RELATED_TO,
    )
    await runtime.relationship_manager.create_direct_bulk([ordinary_edge])
    monkeypatch.setattr(exploration, "get_graph_runtime", AsyncMock(return_value=runtime))

    async def read():
        return await exploration.explore(
            mode="related",
            entity_id=protected.source_id,
            organization_id=runtime.client.group_id,
            principal_id="user_a",
            accessible_projects={"project_a"},
            limit=100,
        )

    baseline = await read()
    assert {protected.target_id, ordinary.id} <= {entity.id for entity in baseline.entities}
    before = await runtime.client.execute_query(
        "SELECT * FROM relates_to WHERE uuid=$id;", id=protected.id
    )
    assert before[0]["operational_derivation_required"] is True
    original = graph_view_availability.available_graph_entities
    revised = None

    async def republish_during_final_nodes(org, ids, **kwargs):
        nonlocal revised
        nodes = await original(org, ids, **kwargs)
        if {protected.target_id, ordinary.id} <= set(nodes) and revised is None:
            revised, replacement = await capture(
                runtime, authority, revision=memory.revision, metadata={"encoding_revision": 2}
            )
            assert replacement.observation.generation > source.observation.generation
            await runtime.entity_manager.publish_operational_entities(replacement)
            if republish_edges:
                await runtime.relationship_manager.publish_operational_relationships(replacement)
        return nodes

    monkeypatch.setattr(
        graph_view_availability, "available_graph_entities", republish_during_final_nodes
    )
    result = await read()
    assert revised is not None and revised.id == memory.id and revised.revision > memory.revision
    after = await runtime.client.execute_query(
        "SELECT * FROM relates_to WHERE uuid=$id;", id=protected.id
    )
    assert (before == after) is not republish_edges
    endpoints = {protected.source_id, protected.target_id}
    current_endpoints = await available_graph_entities(
        runtime.client.group_id, sorted(endpoints), runtime=runtime
    )
    assert set(current_endpoints) == endpoints
    current_edges = await available_graph_relationships(
        runtime.client.group_id, [protected.id], runtime=runtime
    )
    assert set(current_edges) == ({protected.id} if republish_edges else set())
    # Both paths originate at a root changed after this view collected its proof.
    assert result.entities == []
    ordinary_nodes = await available_graph_entities(
        runtime.client.group_id, [ordinary.id], runtime=runtime
    )
    assert set(ordinary_nodes) == {ordinary.id}
    refreshed = await read()
    refreshed_ids = {entity.id for entity in refreshed.entities}
    assert ordinary.id in refreshed_ids
    assert (protected.target_id in refreshed_ids) is republish_edges


@pytest.mark.parametrize("change", ["unchanged", "delete", "type", "retire"])
async def test_final_edge_body_check_follows_the_new_proof(
    runtime, content_store, monkeypatch, change
):
    await runtime.entity_manager.create_direct_bulk(
        [
            Entity(id=key, name=key, entity_type=EntityType.NOTE)
            for key in ("root", "changed", "healthy")
        ],
        generate_embeddings=False,
    )
    changing = Relationship(
        id="changing-edge",
        source_id="root",
        target_id="changed",
        relationship_type=RelationshipType.RELATED_TO,
    )
    healthy = changing.model_copy(update={"id": "healthy-edge", "target_id": "healthy"})
    await runtime.relationship_manager.create_direct_bulk([changing, healthy])
    before = await available_graph_relationships(
        runtime.client.group_id, [changing.id, healthy.id], runtime=runtime
    )
    assert set(before) == {changing.id, healthy.id}
    original = availability.available_graph_relationships
    boundary_reached = False

    async def mutate_after_fresh_proof(org, ids, **kwargs):
        nonlocal boundary_reached
        proven = await original(org, ids, **kwargs)
        assert set(proven) == set(before)
        boundary_reached = True
        if change == "delete":
            await runtime.relationship_manager.delete(changing.id)
        elif change == "type":
            await runtime.relationship_manager.create_direct_bulk(
                [changing.model_copy(update={"relationship_type": RelationshipType.DEPENDS_ON})]
            )
        elif change == "retire":
            await runtime.relationship_manager.create_direct_bulk(
                [changing.model_copy(update={"metadata": {"excluded_from_recall": True}})]
            )
        return proven

    monkeypatch.setattr(availability, "available_graph_relationships", mutate_after_fresh_proof)
    result = await availability.unchanged_graph_relationships(
        runtime.client.group_id, before, runtime=runtime
    )
    assert boundary_reached
    assert set(result) == ({changing.id, healthy.id} if change == "unchanged" else {healthy.id})
