"""Operational snapshots and community hops keep their point lookups.

On SurrealDB 3.x an ORDER BY on an indexed column wins over a selective
`IN` list on that column and becomes a full ordered walk of the index. The
statements here order the union lookup in an outer select instead, which
keeps the point lookups and the same deterministic row order the
fingerprints depend on.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.retrieval import _search_expansion
from sibyl_core.services import graph_entity_store, operational_relationships
from sibyl_core.services.graph_client import SurrealGraphClient, prepare_graph_schema
from sibyl_core.services.graph_entities import EntityManager

_DIRECT_ORDERED_UUID_LIST = re.compile(r"uuid IN \$\w+ ORDER BY uuid")
_SORTED_UNION_LOOKUP = re.compile(r"SELECT \* FROM \(SELECT \*.*? uuid IN \$\w+\) ORDER BY uuid")


@pytest.mark.parametrize(
    "statement",
    [operational_relationships._SNAPSHOT, graph_entity_store._OPERATIONAL_TARGET_SNAPSHOT],
    ids=["relationship_snapshot", "entity_target_snapshot"],
)
def test_snapshot_statements_sort_the_union_lookup_outside_the_where(statement: str) -> None:
    assert not _DIRECT_ORDERED_UUID_LIST.search(statement)
    assert _SORTED_UNION_LOOKUP.search(statement)


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
    inner = re.search(r"SELECT \* FROM \((.*)\)\s*ORDER BY target_id\s*LIMIT \$limit;", query, re.S)
    assert inner is not None, query
    assert "FROM relates_to WITH INDEX idx_relates_target" in inner.group(1)
    assert "ORDER BY" not in inner.group(1)
    assert params["community_uuids"] == ["community-1"]
    assert [(hop.uuid, hop.community_id) for hop in hops] == [("member-1", "community-1")]
