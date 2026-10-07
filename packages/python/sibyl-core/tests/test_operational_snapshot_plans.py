"""Operational snapshots and community hops keep their point lookups.

On SurrealDB 3.x an ORDER BY on an indexed column wins over a selective
`IN` list on that column and becomes a full ordered walk of the index. The
statements here order the union lookup in an outer select instead, which
keeps the point lookups and the same deterministic row order the
fingerprints depend on. Past 32 values an `IN` list is no longer
index-served at all, so the operational snapshot looks rows up in clumps.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl_core.backends.surreal.schema import EMBEDDING_DIM
from sibyl_core.backends.surreal.url_schemes import is_embedded_surreal_url
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.retrieval import _search_expansion
from sibyl_core.services import graph_entity_store, operational_relationships
from sibyl_core.services.graph_client import SurrealGraphClient, prepare_graph_schema
from sibyl_core.services.graph_entities import EntityManager
from sibyl_core.services.graph_relationships import RelationshipManager

_DIRECT_ORDERED_UUID_LIST = re.compile(r"uuid IN \$\w+ ORDER BY uuid")
_SORTED_UNION_LOOKUP = re.compile(r"SELECT \* FROM \(SELECT \*.*? uuid IN \$\w+\) ORDER BY uuid")
_CLUMP_LOOKUP = re.compile(r"\(SELECT (?:(?!\(SELECT).)*?IN \$lookup\[1\]\)", re.S)

# The single-IN shape the clumped lookups replaced: the oracle for their rows,
# row order and fingerprint.
_SINGLE_IN_SNAPSHOT = """
LET $targets = SELECT * FROM (SELECT * FROM entity WHERE group_id=$org AND uuid IN $ids) ORDER BY uuid;
LET $associations = SELECT * OMIT validation_write_witness FROM memory_derivations WHERE organization_id=$org
    AND target_kind='graph_entity' AND target_id IN $ids ORDER BY target_id;
LET $states = SELECT * OMIT validation_write_witness FROM source_states
    WHERE organization_id=$org AND source_kind='graph_entity' AND source_id IN $ids ORDER BY source_id;
LET $relationships = SELECT * FROM (SELECT *, in.uuid AS source_uuid, out.uuid AS target_uuid FROM relates_to
    WHERE group_id=$org AND uuid IN $relationship_ids) ORDER BY uuid;
LET $relationship_evidence = SELECT * OMIT attributes.operational_write_witness FROM $relationships;
LET $operational_snapshot_fingerprint = crypto::sha256(type::string([$targets,$associations,$states,$relationship_evidence]));
"""


def test_entity_target_snapshot_sorts_the_union_lookup_outside_the_where() -> None:
    statement = graph_entity_store._OPERATIONAL_TARGET_SNAPSHOT
    assert not _DIRECT_ORDERED_UUID_LIST.search(statement)
    assert _SORTED_UNION_LOOKUP.search(statement)


@pytest.mark.parametrize(
    "statement",
    [operational_relationships._SNAPSHOT, operational_relationships._SNAPSHOT_WITHOUT_VECTORS],
    ids=["with_vectors", "without_vectors"],
)
def test_operational_snapshot_looks_rows_up_in_index_served_clumps(statement: str) -> None:
    assert operational_relationships._LOOKUP_CLUMP <= 32
    assert re.findall(r"\bIN \$[\w\[\]]+", statement) == ["IN $lookup[1]"] * 4
    assert "array::clump(array::distinct($ids), 32)" in statement
    assert "array::clump(array::distinct($relationship_ids), 32)" in statement
    lookups = _CLUMP_LOOKUP.findall(statement)
    assert len(lookups) == 4
    for lookup in lookups:
        # The closure names only its argument; an ORDER BY there would walk
        # the whole index.
        assert "$org" not in lookup
        assert "ORDER BY" not in lookup
    assert not _DIRECT_ORDERED_UUID_LIST.search(statement)


@pytest.fixture
async def graph_client() -> AsyncIterator[SurrealGraphClient]:
    client = SurrealGraphClient(group_id=f"snapshot-{uuid4().hex}", url="memory://")
    try:
        await prepare_graph_schema(client)
        yield client
    finally:
        await client.close()


async def test_snapshot_rows_stay_sorted_and_fingerprints_stable(
    graph_client: SurrealGraphClient,
) -> None:
    manager = EntityManager(graph_client, group_id=graph_client.group_id)
    ids = ["zeta", "alpha", "mid"]
    for entity_id in ids:
        await manager.create_direct(
            Entity(id=entity_id, entity_type=EntityType.NOTE, name=entity_id),
            generate_embedding=False,
        )

    first = await operational_relationships._snapshot(
        graph_client, organization_id=graph_client.group_id, ids=ids, relationship_ids=[]
    )
    second = await operational_relationships._snapshot(
        graph_client,
        organization_id=graph_client.group_id,
        ids=list(reversed(ids)),
        relationship_ids=[],
    )
    assert [row["uuid"] for row in first["targets"]] == sorted(ids)
    assert first["fingerprint"] == second["fingerprint"]

    target_statement = (
        "RETURN {" + graph_entity_store._OPERATIONAL_TARGET_SNAPSHOT + " RETURN {targets:$targets,"
        " fingerprint:crypto::sha256(type::string([$targets,$associations,$states]))}; };"
    )
    snapshots = [
        await graph_client.execute_query(target_statement, ids=order, org=graph_client.group_id)
        for order in (sorted(ids), sorted(ids, reverse=True))
    ]
    rows = [snapshot[0] if isinstance(snapshot, list) else snapshot for snapshot in snapshots]
    assert [row["uuid"] for row in rows[0]["targets"]] == sorted(ids)
    assert rows[0]["fingerprint"] == rows[1]["fingerprint"]


@pytest.fixture(params=["embedded", "live"])
async def snapshot_client(request) -> AsyncIterator[tuple[SurrealGraphClient, bool]]:
    """An embedded store, then a SurrealDB server (the engine whose planner
    stops index-serving IN past 32 values) when SIBYL_LIVE_SURREAL_TESTS=1 and
    SIBYL_SURREAL_URL name one."""
    group_id = f"snapshot-{uuid4().hex[:10]}"
    live = request.param == "live"
    if not live:
        client = SurrealGraphClient(group_id=group_id, url="memory://")
    else:
        if os.environ.get("SIBYL_LIVE_SURREAL_TESTS") != "1":
            pytest.skip("live SurrealDB tests are disabled")
        url = os.environ.get("SIBYL_SURREAL_URL", "")
        if not url or is_embedded_surreal_url(url):
            pytest.skip("live SurrealDB tests require SIBYL_SURREAL_URL to point at a server")
        client = SurrealGraphClient(
            group_id=group_id,
            url=url,
            username=os.environ.get("SIBYL_SURREAL_USERNAME", "root"),
            password=os.environ.get("SIBYL_SURREAL_PASSWORD", "root"),
            namespace_prefix="verify_",
            database="graph",
            pool_size=2,
        )
    try:
        await prepare_graph_schema(client)
        yield client, live
    finally:
        if live:
            await client.execute_query(f"REMOVE NAMESPACE IF EXISTS {client.namespace};")
        await client.close()


def _single_in(statement_params: dict[str, object]) -> tuple[str, dict[str, object]]:
    return (
        "RETURN {" + _SINGLE_IN_SNAPSHOT + " RETURN {targets:$targets, associations:$associations,"
        " states:$states, relationships:$relationships,"
        " fingerprint:$operational_snapshot_fingerprint}; };",
        statement_params,
    )


async def test_clumped_snapshot_matches_the_single_in_rows_and_fingerprint(
    snapshot_client: tuple[SurrealGraphClient, bool],
) -> None:
    graph_client, _ = snapshot_client
    group_id = graph_client.group_id
    manager = EntityManager(graph_client, group_id=group_id)
    # More ids than three clumps hold, created out of order.
    ids = [f"e{index:03d}" for index in range(70)]
    for entity_id in reversed(ids):
        await manager.create_direct(
            Entity(id=entity_id, entity_type=EntityType.NOTE, name=entity_id),
            generate_embedding=False,
        )
    await graph_client.execute_query(
        "UPDATE entity SET name_embedding = $vector WHERE uuid IN ['e001', 'e040', 'e069'];",
        vector=[0.5, *([0.0] * (EMBEDDING_DIM - 1))],
    )
    # Another group's rows share the namespace; the clumps must still scope.
    foreign = EntityManager(graph_client, group_id="other-group")
    for entity_id in ("f1", "f2"):
        await foreign.create_direct(
            Entity(id=entity_id, entity_type=EntityType.NOTE, name=entity_id),
            generate_embedding=False,
        )
    edges = [
        Relationship(
            id=f"rel_{index:03d}",
            source_id=ids[index],
            target_id=ids[(index * 7 + 1) % len(ids)],
            relationship_type=RelationshipType.RELATED_TO,
        )
        for index in range(40)
    ]
    assert await RelationshipManager(graph_client, group_id=group_id).create_bulk(edges) == (40, 0)
    foreign_edge = Relationship(
        id="rel_foreign",
        source_id="f1",
        target_id="f2",
        relationship_type=RelationshipType.RELATED_TO,
    )
    assert await RelationshipManager(graph_client, group_id="other-group").create_bulk(
        [foreign_edge]
    ) == (1, 0)
    # Ledgers for in-scope ids, with decoys under another org and another kind.
    stated = [*ids[::2], *ids[1::3], "f1"]
    for revision, entity_id in enumerate(stated):
        for org, kind in (
            (group_id, "graph_entity"),
            ("other-group", "graph_entity"),
            (group_id, "raw_capture"),
        ):
            await graph_client.execute_query(
                "IF (SELECT * FROM source_states WHERE organization_id=$org AND source_kind=$kind"
                " AND source_id=$id) = [] { CREATE source_states CONTENT {organization_id:$org,"
                " source_kind:$kind, source_id:$id, generation:1, revision:$revision,"
                " deleted:false, incarnation:'i'}; };",
                org=org,
                kind=kind,
                id=entity_id,
                revision=revision,
            )
    for index, entity_id in enumerate([*ids[::2], "f1"]):
        for org in (group_id, "other-group"):
            await graph_client.execute_query(
                "CREATE memory_derivations CONTENT {organization_id:$org, target_kind:'graph_entity',"
                " target_id:$id, body_sha256:type::string($index), principal_id:'p',"
                " authority_ceiling:{}, observations:[], active:true};",
                org=org,
                id=entity_id,
                index=index,
            )
    requested = [*ids, "f1", "missing"]
    requested_edges = [edge.id for edge in edges] + ["rel_foreign", "rel_missing"]

    statement, params = _single_in(
        {"org": group_id, "ids": sorted(requested), "relationship_ids": sorted(requested_edges)}
    )
    oracle = await graph_client.execute_query(statement, **params)
    expected = oracle[0] if isinstance(oracle, list) else oracle
    snapshot = await operational_relationships._snapshot(
        graph_client, organization_id=group_id, ids=requested, relationship_ids=requested_edges
    )

    assert [row["uuid"] for row in snapshot["targets"]] == ids
    assert [row["uuid"] for row in snapshot["relationships"]] == sorted(e.id for e in edges)
    for key in ("targets", "associations", "states", "relationships", "fingerprint"):
        assert snapshot[key] == expected[key], key
    assert sum("name_embedding" in row for row in snapshot["targets"]) == 3
    # Entity writes keep their own source state; f1 belongs to another group,
    # but its ledger rows under this org still count.
    assert [row["target_id"] for row in snapshot["associations"]] == [*ids[::2], "f1"]
    assert [row["source_id"] for row in snapshot["states"]] == [*ids, "f1"]

    # The completion fence hands the raw statement unfiltered ids; a duplicate
    # on a clump boundary must not return its row twice.
    straddling = sorted([*requested, sorted(requested)[31]])
    straddling_edges = sorted([*requested_edges, sorted(requested_edges)[31]])
    assert straddling[31] == straddling[32]
    fenced = await graph_client.execute_query(
        "RETURN {" + operational_relationships._SNAPSHOT + " RETURN {targets:$targets,"
        " associations:$associations, states:$states, relationships:$relationships,"
        " fingerprint:$operational_snapshot_fingerprint}; };",
        org=group_id,
        ids=straddling,
        relationship_ids=straddling_edges,
    )
    fenced_rows = fenced[0] if isinstance(fenced, list) else fenced
    for key in ("targets", "associations", "states", "relationships", "fingerprint"):
        assert fenced_rows[key] == expected[key], key

    rows = await operational_relationships._snapshot(
        graph_client,
        organization_id=group_id,
        ids=requested,
        relationship_ids=requested_edges,
        include_embeddings=False,
    )
    assert "fingerprint" not in rows
    vector_fields = {"embedding", "name_embedding", "fact_embedding"}
    for key in ("targets", "associations", "states", "relationships"):
        assert rows[key] == [
            {field: value for field, value in row.items() if field not in vector_fields}
            for row in expected[key]
        ], key


async def test_clumped_lookups_are_index_served_on_a_server(
    snapshot_client: tuple[SurrealGraphClient, bool],
) -> None:
    client, live = snapshot_client
    if not live:
        pytest.skip("the embedded engine plans IN lists its own way")
    ids = [f"id{index:03d}" for index in range(40)]
    for table, predicate, index in (
        ("entity", "group_id=$lookup[0] AND uuid IN $lookup[1]", "idx_entity_uuid"),
        (
            "memory_derivations",
            "organization_id=$lookup[0] AND target_kind='graph_entity' AND target_id IN $lookup[1]",
            "memory_derivation_target",
        ),
        (
            "source_states",
            "organization_id=$lookup[0] AND source_kind='graph_entity' AND source_id IN $lookup[1]",
            "source_state_identity",
        ),
        ("relates_to", "group_id=$lookup[0] AND uuid IN $lookup[1]", "idx_relates_uuid"),
    ):
        plans = await client.execute_query(
            f"RETURN {operational_relationships.org_lookup_clumps('ids')}.map(|$lookup|"
            f" (SELECT * FROM {table} WHERE {predicate} EXPLAIN));",
            org=client.group_id,
            ids=ids,
        )
        text = json.dumps(plans, default=str)
        assert index in text, (table, text[:400])
        assert "TableScan" not in text, (table, text[:400])


class _RecordingClient:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> object:
        self.calls.append((query, dict(params)))
        return [{"status": "OK", "result": self.rows}]


async def test_community_member_hop_hints_the_target_index_and_sorts_outside(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _RecordingClient(
        [{"uuid": "member-1", "community_id": "community-1", "relationship_id": "rel-1"}]
    )
    monkeypatch.setattr(
        _search_expansion, "_community_ids_for_entities", AsyncMock(return_value=["community-1"])
    )

    hops = await _search_expansion._community_member_hops(
        client=client, source_uuids=["seed"], group_id="org", depth=1, limit=5
    )

    query, params = client.calls[0]
    inner = re.search(
        r"SELECT \* FROM \((.*)\)\s*ORDER BY community_id\s*LIMIT \$limit;", query, re.S
    )
    assert inner is not None, query
    assert "FROM relates_to WITH INDEX idx_relates_target" in inner.group(1)
    assert "ORDER BY" not in inner.group(1)
    assert params["community_uuids"] == ["community-1"]
    assert [(hop.uuid, hop.community_id) for hop in hops] == [("member-1", "community-1")]


async def test_member_hop_limit_keeps_the_lowest_community_first(
    graph_client: SurrealGraphClient,
) -> None:
    """ORDER BY must name the projected key, or LIMIT picks an arbitrary member set."""
    group_id = graph_client.group_id
    entities = EntityManager(graph_client, group_id=group_id)
    relationships = RelationshipManager(graph_client, group_id=group_id)
    for entity_id, entity_type in (
        ("seed", EntityType.TASK),
        ("aa_comm2", EntityType.COMMUNITY),
        ("zz_comm2", EntityType.COMMUNITY),
        *((f"mem_a{index}", EntityType.NOTE) for index in range(3)),
        *((f"mem_z{index}", EntityType.NOTE) for index in range(3)),
    ):
        await entities.create_direct(
            Entity(id=entity_id, entity_type=entity_type, name=entity_id),
            generate_embedding=False,
        )
    edges = [("seed", "aa_comm2"), ("seed", "zz_comm2")]
    edges += [(f"mem_a{index}", "aa_comm2") for index in range(3)]
    edges += [(f"mem_z{index}", "zz_comm2") for index in range(3)]
    created, failed = await relationships.create_bulk(
        [
            Relationship(
                id=f"rel_{source}_{target}",
                source_id=source,
                target_id=target,
                relationship_type=RelationshipType.BELONGS_TO,
            )
            for source, target in edges
        ]
    )
    assert (created, failed) == (len(edges), 0)

    hops = await _search_expansion._community_member_hops(
        client=graph_client, source_uuids=["seed"], group_id=group_id, depth=1, limit=3
    )

    assert {hop.uuid for hop in hops} == {"mem_a0", "mem_a1", "mem_a2"}
    assert {hop.community_id for hop in hops} == {"aa_comm2"}
