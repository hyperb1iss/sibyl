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

from sibyl_core.backends.surreal.in_clumps import clumped
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
_CLUMP_LOOKUP = re.compile(r"\(SELECT (?:(?!\(SELECT).)*?IN \$snapshot_\w+\)", re.S)

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

# The clumps-in-SQL shape the client-param clumps replaced, verbatim: on 4.0
# its `$lookup` operands are unknown at plan time and every clump scanned.
# A second oracle, for snapshots taken before and after the rewrite.
_CLOSURE_CLUMPS = (
    "array::transpose([array::repeat($org, array::len(array::clump(array::distinct(${param}), 32))),"
    " array::clump(array::distinct(${param}), 32)])"
)
_CLOSURE_SNAPSHOT = f"""
LET $id_lookups = {_CLOSURE_CLUMPS.format(param="ids")};
LET $relationship_lookups = {_CLOSURE_CLUMPS.format(param="relationship_ids")};
LET $targets = SELECT * FROM array::flatten($id_lookups.map(|$lookup|
    (SELECT * FROM entity WHERE group_id=$lookup[0] AND uuid IN $lookup[1])))
    ORDER BY uuid;
LET $associations = SELECT * OMIT validation_write_witness FROM array::flatten($id_lookups.map(|$lookup|
    (SELECT * FROM memory_derivations WHERE organization_id=$lookup[0] AND target_kind='graph_entity'
        AND target_id IN $lookup[1]))) ORDER BY target_id;
LET $states = SELECT * OMIT validation_write_witness FROM array::flatten($id_lookups.map(|$lookup|
    (SELECT * FROM source_states WHERE organization_id=$lookup[0] AND source_kind='graph_entity'
        AND source_id IN $lookup[1]))) ORDER BY source_id;
LET $relationships = SELECT * FROM array::flatten($relationship_lookups.map(|$lookup|
    (SELECT *, in.uuid AS source_uuid, out.uuid AS target_uuid FROM relates_to
        WHERE group_id=$lookup[0] AND uuid IN $lookup[1]))) ORDER BY uuid;
LET $relationship_evidence = SELECT * OMIT attributes.operational_write_witness FROM $relationships;
LET $operational_snapshot_fingerprint = crypto::sha256(type::string([$targets,$associations,$states,$relationship_evidence]));
"""
_RETURNED = (
    " RETURN {targets:$targets, associations:$associations, states:$states,"
    " relationships:$relationships, fingerprint:$operational_snapshot_fingerprint}; };"
)


def test_entity_target_snapshot_sorts_the_union_lookup_outside_the_where() -> None:
    statement = graph_entity_store._OPERATIONAL_TARGET_SNAPSHOT
    assert not _DIRECT_ORDERED_UUID_LIST.search(statement)
    assert _SORTED_UNION_LOOKUP.search(statement)


@pytest.mark.parametrize("vectors", [True, False], ids=["with_vectors", "without_vectors"])
def test_operational_snapshot_looks_rows_up_in_index_served_clumps(vectors: bool) -> None:
    ids = [f"e{index:03d}" for index in reversed(range(70))]
    edges = [f"r{index:03d}" for index in range(40)]
    statement, params = operational_relationships.snapshot_statement(
        "org-a", [*ids, ids[5]], edges, vectors=vectors
    )
    assert sorted(params) == [
        "snapshot_ids",
        "snapshot_ids_0",
        "snapshot_ids_1",
        "snapshot_ids_2",
        "snapshot_relationship_ids_0",
        "snapshot_relationship_ids_1",
        "snapshot_state_identities",
    ]
    # Entities and edges: unique-index clumps of at most 32 client values.
    assert params["snapshot_ids_0"] == sorted(ids)[:32]
    assert sum(len(params[f"snapshot_ids_{index}"]) for index in range(3)) == 70
    assert [len(params[f"snapshot_relationship_ids_{index}"]) for index in range(2)] == [32, 8]
    lookups = _CLUMP_LOOKUP.findall(statement)
    # Three entity clumps, the one derivation walk, two edge clumps.
    assert len(lookups) == 3 + 1 + 2
    for lookup in lookups:
        # An ORDER BY beside the IN would walk the whole index.
        assert "ORDER BY" not in lookup
        assert "$org" in lookup
    # The derivation ledger: one IN over every id, one walk of its prefix.
    assert params["snapshot_ids"] == sorted(ids)
    assert statement.count("target_id IN $snapshot_ids)") == 1
    # Ledger states by their record keys; the closure reads only its argument.
    assert params["snapshot_state_identities"] == [
        ["org-a", "graph_entity", identity] for identity in sorted(ids)
    ]
    closures = re.findall(r"\.map\(\|\$(\w+)\|([^|]*?)\)\)\)", statement)
    assert [name for name, _ in closures] == ["identity"]
    assert re.findall(r"\$\w+", closures[0][1]) == ["$identity"]
    assert "source_id IN" not in statement
    assert not _DIRECT_ORDERED_UUID_LIST.search(statement)
    assert ("$operational_snapshot_fingerprint" in statement) is vectors
    assert ("OMIT embedding, name_embedding" in statement) is not vectors


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
            # Keyed as every ledger writer keys them (the maintain event,
            # the migration, the archive store).
            await graph_client.execute_query(
                "RETURN { LET $key = type::record(string::concat('source_states:',"
                " crypto::sha256(type::string([$org, $kind, $id]))));"
                " IF (SELECT * FROM source_states WHERE organization_id=$org AND source_kind=$kind"
                " AND source_id=$id) = [] { CREATE $key CONTENT {organization_id:$org,"
                " source_kind:$kind, source_id:$id, generation:1, revision:$revision,"
                " deleted:false, incarnation:'i'}; }; };",
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
    fence, lookups = operational_relationships.snapshot_statement(
        group_id, straddling, straddling_edges
    )
    fenced = await graph_client.execute_query(
        "RETURN {" + fence + _RETURNED, org=group_id, **lookups
    )
    fenced_rows = fenced[0] if isinstance(fenced, list) else fenced
    for key in ("targets", "associations", "states", "relationships", "fingerprint"):
        assert fenced_rows[key] == expected[key], key

    # A snapshot taken by the statement this one replaced, over the same rows
    # with the same duplicates, gives the same rows and fingerprint bytes.
    for order in (straddling, list(reversed(straddling))):
        before = await graph_client.execute_query(
            "RETURN {" + _CLOSURE_SNAPSHOT + _RETURNED,
            org=group_id,
            ids=order,
            relationship_ids=straddling_edges,
        )
        before_rows = before[0] if isinstance(before, list) else before
        for key in ("targets", "associations", "states", "relationships", "fingerprint"):
            assert fenced_rows[key] == before_rows[key], key

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
    """Each clump, inside the block it runs in, is an index lookup on 3.x and 4.0."""
    client, live = snapshot_client
    if not live:
        pytest.skip("the embedded engine plans IN lists its own way")
    ids = [f"id{index:03d}" for index in range(40)]
    _, params = operational_relationships.snapshot_statement(client.group_id, ids, ids)
    for name, lookup, index in (
        (
            "entity",
            clumped(
                operational_relationships._TARGET_LOOKUP.format(fields="*") + " EXPLAIN",
                "snapshot_ids",
                ids,
            )[0],
            "idx_entity_uuid",
        ),
        (
            "relates_to",
            clumped(
                operational_relationships._RELATIONSHIP_LOOKUP.format(omit="") + " EXPLAIN",
                "snapshot_relationship_ids",
                ids,
            )[0],
            "idx_relates_uuid",
        ),
        (
            "memory_derivations",
            f"[({operational_relationships._ASSOCIATION_LOOKUP} EXPLAIN)]",
            "memory_derivation_target",
        ),
        ("source_states", f"[({operational_relationships._STATE_LOOKUP} EXPLAIN)]", None),
    ):
        plans = await client.execute_query(
            f"RETURN {{ LET $plans = {lookup}; RETURN $plans; }};", org=client.group_id, **params
        )
        text = json.dumps(plans, default=str)
        if index is not None:
            assert index in text, (name, text[:400])
        assert "TableScan" not in text, (name, text[:400])
        assert "Iterate Table" not in text, (name, text[:400])


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
