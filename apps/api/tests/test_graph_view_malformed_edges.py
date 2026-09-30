"""A malformed final edge cannot hide independent native graph facts."""

from uuid import uuid4

import httpx

from sibyl.api.routes import graph as graph_routes
from sibyl_core.backends.surreal.records import normalize_records
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services import graph_view_availability as view
from sibyl_core.services.graph_runtime import get_surreal_graph_runtime
from tests.test_entity_membership_reads import (
    membership_entities as membership_entities,  # noqa: PLC0414
    reader_token,
)
from tests.test_public_membership_adapters import (
    public_membership_runtime as public_membership_runtime,  # noqa: PLC0414
)


async def test_native_final_malformed_edge_preserves_independent_graph_facts(
    membership_entities, monkeypatch
):
    fixture = membership_entities
    runtime = await get_surreal_graph_runtime(fixture.org)
    first, second, malformed_id, healthy_id = [str(uuid4()) for _ in range(4)]
    await runtime.entity_manager.create_direct_bulk(
        [
            Entity(id=identifier, name=identifier, entity_type=EntityType.NOTE)
            for identifier in (first, second)
        ],
        generate_embeddings=False,
    )
    malformed = Relationship(
        id=malformed_id,
        source_id=fixture.anchor_id,
        target_id=first,
        relationship_type=RelationshipType.RELATED_TO,
    )
    healthy = malformed.model_copy(update={"id": healthy_id, "target_id": second})
    await runtime.relationship_manager.create_direct_bulk([malformed, healthy])
    fixture.app.include_router(graph_routes.router, prefix="/api")
    token = await reader_token(fixture, "matching_key")
    changed = False
    collect_nodes = view.available_graph_entities
    ordinary_ids = {fixture.anchor_id, first, second}

    async def replace_after_actual_node_proof(org, ids, **kwargs):
        nonlocal changed
        nodes = await collect_nodes(org, ids, **kwargs)
        if org == fixture.org and ordinary_ids <= set(ids) and not changed:
            assert ordinary_ids <= nodes.keys()
            replacement = Relationship(
                id=malformed_id,
                source_id=fixture.anchor_id,
                target_id=first,
                relationship_type=RelationshipType.RELATED_TO,
                weight=1,
                metadata={"weight": -1},
            )
            assert replacement.weight == 1
            assert await runtime.relationship_manager.create_direct_bulk([replacement]) == [
                malformed_id
            ]
            stored = normalize_records(
                await runtime.client.execute_query(
                    "SELECT uuid,attributes FROM relates_to WHERE group_id=$org AND uuid=$id;",
                    org=fixture.org,
                    id=malformed_id,
                )
            )
            assert stored[0]["uuid"] == malformed_id
            assert stored[0]["attributes"]["weight"] == -1
            changed = True
        return nodes

    async with httpx.AsyncClient(
        base_url=fixture.base, headers={"Authorization": "Bearer " + token}
    ) as client:
        baseline = await client.get("/api/graph/full")
        assert baseline.status_code == 200, baseline.text
        before = baseline.json()
        assert ordinary_ids <= {row["id"] for row in before["nodes"]}
        expected_edges = {(fixture.anchor_id, first), (fixture.anchor_id, second)}
        assert expected_edges <= {(row["source"], row["target"]) for row in before["edges"]}
        monkeypatch.setattr(view, "available_graph_entities", replace_after_actual_node_proof)
        response = await client.get("/api/graph/full")
    assert changed
    assert response.status_code == 200, response.text
    after = response.json()
    assert ordinary_ids <= {row["id"] for row in after["nodes"]}
    edges = {(row["source"], row["target"]) for row in after["edges"]}
    assert (fixture.anchor_id, first) not in edges
    assert (fixture.anchor_id, second) in edges
