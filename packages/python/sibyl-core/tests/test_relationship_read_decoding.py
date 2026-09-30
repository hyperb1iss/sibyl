"""Malformed stored edges do not consume healthy discovery page slots."""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from sibyl_core.backends.surreal.records import normalize_records
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services import graph_relationships as relationships
from sibyl_core.services.graph_client import SurrealGraphClient, prepare_graph_schema
from sibyl_core.services.graph_entities import EntityManager
from sibyl_core.services.graph_records import (
    readable_relationship_from_surreal_row,
    relationship_from_surreal_row,
    relationship_weight_predicate,
)
from sibyl_core.services.graph_relationships import RelationshipManager


@pytest.mark.parametrize(
    ("attributes", "weight"),
    [
        ({"weight": True}, 1),
        ({"weight": False}, 0),
        ({"weight": "-1"}, 1),
        ({"weight": None, "metadata": {"weight": -1}}, 1),
        ({"metadata": {"weight": 2.5}}, 2.5),
        ({"metadata": '{"weight":2.5}'}, 2.5),
        ({"weight": 3, "metadata": '{"weight":-1}'}, 3),
        ({"weight": "ignored", "metadata": {"weight": -1}}, 1),
        ({"metadata": "invalid JSON"}, 1),
        ({"metadata": "[-1]"}, 1),
    ],
)
def test_relationship_reader_preserves_legacy_weight_precedence(attributes, weight):
    row = {"uuid": "edge", "source_uuid": "a", "target_uuid": "b", "attributes": attributes}
    assert relationship_from_surreal_row(row).weight == weight
    assert readable_relationship_from_surreal_row(row).weight == weight


@pytest.mark.parametrize("weight", [-1, -0.5, float("nan"), -float("inf")])
def test_relationship_reader_isolates_only_the_invalid_candidate(weight):
    row = {
        "uuid": "malformed",
        "source_uuid": "a",
        "target_uuid": "b",
        "attributes": {"weight": weight},
    }
    with pytest.raises(ValidationError):
        relationship_from_surreal_row(row)
    assert readable_relationship_from_surreal_row(row) is None
    assert readable_relationship_from_surreal_row(row | {"attributes": {"weight": 2}}).weight == 2


async def test_relationship_native_pages_preserve_readable_tail_and_weight_semantics(monkeypatch):
    client = SurrealGraphClient(
        group_id=f"edge-pages-{uuid4().hex}",
        url=os.environ.get("SIBYL_SHARED_RETIREMENT_TEST_URL", "memory://"),
        username="root",
        password="root",
    )
    try:
        await prepare_graph_schema(client)
        info = await client.execute_query("INFO FOR TABLE relates_to;")
        assert "TYPE object" in info["fields"]["attributes"]
        assert "FLEXIBLE" in info["fields"]["attributes"]
        literal_rows = [
            {"uuid": "missing-negative", "attributes": {"metadata": {"weight": -1}}},
            {"uuid": "array-default", "attributes": {"metadata": [-1]}},
            {"uuid": "null-default", "attributes": {"metadata": None}},
            {"uuid": "empty-default", "attributes": {}},
        ]
        eligible = normalize_records(
            await client.execute_query(
                f"SELECT uuid FROM array::concat($rows, "
                "[{uuid: 'null-override', attributes: {weight: NULL, metadata: {weight: -1}}}]) "
                f"WHERE true {relationship_weight_predicate(client)};",
                rows=literal_rows,
                readable_relationship_metadata=[],
                nan_relationship_weight=float("nan"),
            )
        )
        assert {row["uuid"] for row in eligible} == {
            "null-override",
            "array-default",
            "null-default",
            "empty-default",
        }
        entities = EntityManager(client, group_id=client.group_id)
        manager = RelationshipManager(client, group_id=client.group_id)
        await entities.create_direct_bulk(
            [Entity(id=key, name=key, entity_type=EntityType.NOTE) for key in ("a", "b")],
            generate_embeddings=False,
        )
        variants = [
            ({"weight": 2.5}, 2.5),
            ({"weight": 0}, 0),
            ({"weight": True}, 1),
            ({"weight": False}, 0),
            ({"weight": "-1"}, 1),
            ({"metadata": {"weight": 3}}, 3),
            ({"metadata": '{"weight":0.75}'}, 0.75),
            ({"weight": 4, "metadata": '{"weight":-1}'}, 4),
            ({"weight": "ignored", "metadata": {"weight": -1}}, 1),
            ({"metadata": "invalid JSON"}, 1),
            ({"metadata": "[-1]"}, 1),
            ({"metadata": {"weight": "-1"}}, 1),
            ({"metadata": '{"weight":"-1"}'}, 1),
            ({"weight": -1}, None),
            ({"weight": -0.5}, None),
            ({"weight": float("nan")}, None),
            ({"metadata": {"weight": -1}}, None),
            ({"metadata": '{"weight":-1}'}, None),
            ({"weight": -1, "metadata": '{"weight":2}'}, None),
        ]
        stamp = datetime(2026, 1, 1, tzinfo=UTC)
        expected = {}
        edges = []
        for index, (metadata, expected_weight) in enumerate(variants):
            identifier = f"edge-{index:02}"
            edges.append(
                Relationship(
                    id=identifier,
                    source_id="a",
                    target_id="b",
                    relationship_type=RelationshipType.RELATED_TO,
                    weight=1,
                    metadata=metadata,
                    created_at=stamp + timedelta(seconds=index),
                )
            )
            if expected_weight is not None:
                expected[identifier] = expected_weight
        assert await manager.create_direct_bulk(edges) == [edge.id for edge in edges]
        stored = normalize_records(await client.execute_query("SELECT * FROM relates_to;"))
        assert len(stored) == len(variants)
        assert any(row["attributes"].get("weight") == -1 for row in stored)
        expected_ids = sorted(expected, reverse=True)
        observed = []
        for offset in range(0, len(expected) + 2, 2):
            page = await manager.list_all(limit=2, offset=offset)
            assert [edge.id for edge in page] == expected_ids[offset : offset + 2]
            observed.extend(page)
        assert {edge.id: edge.weight for edge in observed} == expected
        assert {edge.id for edge in await manager.get_for_entity("a")} == set(expected)
        assert {edge.id for edge in await manager.find_between("a", "b")} == set(expected)
        related = await manager.get_related_entities_batch(["a"], limit_per_entity=2)
        assert [edge.id for _, edge in related["a"]] == expected_ids[:2]
        with pytest.raises(KeyError):
            await manager.get("edge-13")

        # Change a legacy carrier after its actual collection. Unknown encodings
        # must not occupy native page slots, whether negative or newly valid.
        collect = relationships.readable_legacy_relationship_metadata
        target = "edge-12"
        for next_weight in (-2, 2):
            await manager.create_direct_bulk(
                [edges[12].model_copy(update={"metadata": {"metadata": '{"weight":"-1"}'}})]
            )
            changed = False

            async def replace_after_collection(*args, replacement_weight=next_weight, **kwargs):
                nonlocal changed
                captured = await collect(*args, **kwargs)
                if not changed:
                    assert '{"weight":"-1"}' in captured
                    assert await manager.create_direct_bulk(
                        [
                            edges[12].model_copy(
                                update={
                                    "metadata": {"metadata": f'{{"weight":{replacement_weight}}}'}
                                }
                            )
                        ]
                    ) == [target]
                    changed = True
                return captured

            monkeypatch.setattr(
                relationships, "readable_legacy_relationship_metadata", replace_after_collection
            )
            page = await manager.list_all(limit=2)
            assert changed
            assert [edge.id for edge in page] == expected_ids[1:3]
            monkeypatch.setattr(relationships, "readable_legacy_relationship_metadata", collect)
            fresh = await manager.list_all(limit=2)
            wanted = expected_ids[:2] if next_weight > 0 else expected_ids[1:3]
            assert [edge.id for edge in fresh] == wanted
    finally:
        await client.close()
