"""A malformed final edge cannot hide independent native graph facts."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx

from sibyl.api.routes import graph as graph_routes
from sibyl.persistence import graph_runtime as adapters
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
        monkeypatch.setattr(view, "available_graph_entities", collect_nodes)
        fresh_response = await client.get("/api/graph/full")
    assert changed
    assert response.status_code == 200, response.text
    after = response.json()
    assert ordinary_ids <= {row["id"] for row in after["nodes"]}
    edges = {(row["source"], row["target"]) for row in after["edges"]}
    assert (fixture.anchor_id, first) not in edges
    assert (fixture.anchor_id, second) in edges
    assert fresh_response.status_code == 200, fresh_response.text
    fresh = fresh_response.json()
    assert ordinary_ids <= {row["id"] for row in fresh["nodes"]}
    fresh_edges = {(row["source"], row["target"]) for row in fresh["edges"]}
    assert (fixture.anchor_id, first) not in fresh_edges
    assert (fixture.anchor_id, second) in fresh_edges


async def test_native_discovery_pages_preserve_healthy_tail_after_legacy_carrier_changes(
    membership_entities, monkeypatch
):
    fixture = membership_entities
    runtime = await get_surreal_graph_runtime(fixture.org)
    adapter = adapters.GraphQueryAdapter(runtime, fixture.org)
    anchor, endpoint = [str(uuid4()) for _ in range(2)]
    await runtime.entity_manager.create_direct_bulk(
        [Entity(id=key, name=key, entity_type=EntityType.NOTE) for key in (anchor, endpoint)],
        generate_embeddings=False,
    )
    healthy_metadata = [
        ({"weight": 2.5}, 2.5),
        ({"weight": True}, 1),
        ({"weight": False}, 0),
        ({"weight": "-1"}, 1),
        ({"metadata": {"weight": 3}}, 3),
        ({"metadata": '{"weight":0.75}'}, 0.75),
        ({"weight": 4, "metadata": '{"weight":-1}'}, 4),
        ({"weight": "ignored", "metadata": {"weight": -1}}, 1),
        ({"metadata": "invalid JSON"}, 1),
    ]
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    expected = {}
    edges = []
    for index, (metadata, weight) in enumerate(healthy_metadata):
        key = f"yy-{index:02}-{uuid4()}"
        expected[key] = weight
        edges.append(
            Relationship(
                id=key,
                source_id=anchor,
                target_id=endpoint,
                relationship_type=RelationshipType.RELATED_TO,
                metadata=metadata,
                created_at=stamp + timedelta(seconds=index),
            )
        )
    for index, metadata in enumerate(
        ({"weight": -1}, {"metadata": {"weight": -1}}, {"metadata": '{"weight":-1}'})
    ):
        edges.append(
            Relationship(
                id=f"zz-{index:02}-{uuid4()}",
                source_id=anchor,
                target_id=endpoint,
                relationship_type=RelationshipType.RELATED_TO,
                weight=1,
                metadata=metadata,
                created_at=stamp + timedelta(seconds=100 + index),
            )
        )
    assert await runtime.relationship_manager.create_direct_bulk(edges) == [
        edge.id for edge in edges
    ]
    stored = normalize_records(
        await runtime.client.execute_query(
            "SELECT uuid, attributes FROM relates_to WHERE uuid IN $ids;",
            ids=[edge.id for edge in edges],
        )
    )
    assert len(stored) == len(edges)
    assert any(row["attributes"].get("weight") == -1 for row in stored)
    expected_ids = sorted(expected, reverse=True)
    pages = []
    for offset in range(0, len(expected) + 2, 2):
        page = await adapter.list_relationships_for_entities(
            {anchor, endpoint}, limit=2, offset=offset
        )
        assert [edge.id for edge in page] == expected_ids[offset : offset + 2]
        pages.extend(page)
    assert {edge.id: edge.weight for edge in pages} == expected
    fixture.app.include_router(graph_routes.router, prefix="/api")
    token = await reader_token(fixture, "matching_key")
    collect = adapters.readable_legacy_relationship_metadata
    carrier = Relationship(
        id=f"zzz-carrier-{uuid4()}",
        source_id=anchor,
        target_id=endpoint,
        relationship_type=RelationshipType.RELATED_TO,
        metadata={"metadata": '{"weight":0.5}'},
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fixture.app, raise_app_exceptions=False),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        baseline = await client.get("/api/graph/full", params={"max_edges": 2})
        assert baseline.status_code == 200, baseline.text
        assert {row["id"] for row in baseline.json()["edges"]} == set(expected_ids[:2])
        for next_weight in (-2, 2):
            assert await runtime.relationship_manager.create_direct_bulk([carrier]) == [carrier.id]
            changed = False

            async def replace_after_actual_carrier_capture(
                *args, replacement_weight=next_weight, **kwargs
            ):
                nonlocal changed
                captured = await collect(*args, **kwargs)
                if not changed:
                    assert '{"weight":0.5}' in captured
                    replacement = carrier.model_copy(
                        update={"metadata": {"metadata": f'{{"weight":{replacement_weight}}}'}}
                    )
                    assert await runtime.relationship_manager.create_direct_bulk([replacement]) == [
                        carrier.id
                    ]
                    current = normalize_records(
                        await runtime.client.execute_query(
                            "SELECT attributes FROM relates_to WHERE uuid = $uuid;", uuid=carrier.id
                        )
                    )
                    assert (
                        current[0]["attributes"]["metadata"] == f'{{"weight":{replacement_weight}}}'
                    )
                    changed = True
                return captured

            monkeypatch.setattr(
                adapters,
                "readable_legacy_relationship_metadata",
                replace_after_actual_carrier_capture,
            )
            guarded = await client.get("/api/graph/full", params={"max_edges": 2})
            assert changed
            assert guarded.status_code == 200, guarded.text
            assert {row["id"] for row in guarded.json()["edges"]} == set(expected_ids[:2])
            monkeypatch.setattr(adapters, "readable_legacy_relationship_metadata", collect)
            fresh = await client.get("/api/graph/full", params={"max_edges": 2})
            assert fresh.status_code == 200, fresh.text
            wanted = set(expected_ids[:2]) if next_weight < 0 else {carrier.id, expected_ids[0]}
            assert {row["id"] for row in fresh.json()["edges"]} == wanted
