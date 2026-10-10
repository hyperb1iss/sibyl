"""A cold graph build leaves the event loop free to answer other requests.

The build's queries stay on the loop with the async SDK; its decoding,
proving and comparing run on compute threads. The store here answers from
memory without decoding anything, so the lag a ticker measures beside the
build is the build's own CPU on the loop. The rows are genuine stored shapes
read back from a real embedded store and cloned, so every proof step
decodes what it decodes in production.
"""

from __future__ import annotations

import asyncio
import copy
import gc
import time
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Any

import pytest
from surrealdb import RecordID

from sibyl_core.backends.surreal.records import normalize_records
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services import graph_communities as communities
from sibyl_core.services import graph_community_snapshot as snapshots
from sibyl_core.services.graph_client import SurrealGraphClient, prepare_graph_schema
from sibyl_core.services.graph_derivations import _VERDICT_SNAPSHOT_QUERY
from sibyl_core.services.graph_entities import EntityManager
from sibyl_core.services.graph_records import (
    entity_from_surreal_row,
    readable_relationship_from_surreal_row,
)
from sibyl_core.services.graph_relationships import RelationshipManager
from sibyl_core.services.operational_relationships import _snapshot
from tests.test_reflection_identity import content_store as content_store

ORG = "org-loop-lag"
READER = {
    "principal_id": "user_a",
    "accessible_projects": {f"project_{index}" for index in range(4)},
}
ENTITIES = 1200
# Facets per row, each a list of this many strings: enough metadata that
# the build's per-row work is measurable at a size CI runs in seconds.
WIDTH = 10
TICK = 0.005
# The loop's worst stall during the build, as a share of the build's own
# wall time. Run on the loop, the build's longest step grows with the graph
# and so does the build, so main stalls for 15 to 33 percent of it whatever
# the runner's speed. Off the loop what is left is a GIL handoff of a few
# switch intervals, a shrinking share on a slower or busier runner.
LAG_SHARE = 0.08
# Below this a stall is GIL and scheduler noise on any machine.
LAG_FLOOR = 0.05


_DECODER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="memory-graph-decode")


class _MemoryGraphClient(SurrealGraphClient):
    """A pooled graph client whose store is a dict of stored rows.

    It answers the four statements a cold graph build sends, and fails on
    any other so a new statement cannot slip past the measurement.
    """

    def __init__(self, entity_rows: dict[str, dict], edge_rows: dict[str, dict]) -> None:
        super().__init__(group_id=ORG, url="ws://memory-graph.invalid:1/rpc", pool_size=4)
        self.entity_rows = entity_rows
        self.edge_rows = edge_rows

    async def execute_query(self, query: str, **params: Any) -> Any:
        # Rows are handed out by reference: no proof step mutates a row it
        # reads, and copying them here would put the store's work on the loop.
        def entities(ids) -> list[dict]:
            return [self.entity_rows[key] for key in sorted(set(ids)) if key in self.entity_rows]

        def edges(ids) -> list[dict]:
            return [self.edge_rows[key] for key in sorted(set(ids)) if key in self.edge_rows]

        await asyncio.sleep(0)
        if "array::clump($uuids, 32)" in query:
            return entities(params["uuids"])
        if "in.uuid AS source_uuid, out.uuid AS target_uuid FROM relates_to" in query:
            return [
                {"source_uuid": row["source_uuid"], "target_uuid": row["target_uuid"]}
                for row in edges(params["ids"])
            ]
        if "LET $relationship_lookups" in query:
            return [
                {
                    "targets": entities(params["ids"]),
                    "associations": [],
                    "states": [],
                    "relationships": edges(params["relationship_ids"]),
                }
            ]
        if query == _VERDICT_SNAPSHOT_QUERY:
            return [{"associations": [], "targets": entities(params["ids"])}]
        raise AssertionError(f"unexpected graph statement: {query[:120]}")


async def _stored_shapes() -> tuple[dict, dict]:
    """One stored entity row and one stored edge row, as the proof reads them."""
    client = SurrealGraphClient(group_id=ORG, url="memory://")
    try:
        await prepare_graph_schema(client)
        await EntityManager(client, group_id=ORG).create_direct_bulk(
            [
                Entity(id=key, name=key, entity_type=EntityType.TOPIC, organization_id=ORG)
                for key in ("shape-a", "shape-b")
            ]
        )
        await RelationshipManager(client, group_id=ORG).create_direct_bulk(
            [
                Relationship(
                    id="shape-edge",
                    source_id="shape-a",
                    target_id="shape-b",
                    relationship_type=RelationshipType.RELATED_TO,
                )
            ]
        )
        snapshot = await _snapshot(
            client,
            organization_id=ORG,
            ids=["shape-a"],
            relationship_ids=["shape-edge"],
            include_embeddings=False,
        )
        (entity_row,) = snapshot["targets"]
        (edge_row,) = snapshot["relationships"]
        return entity_row, edge_row
    finally:
        await client.close()


def _graph_rows(entity_shape: dict, edge_shape: dict) -> tuple[dict[str, dict], dict[str, dict]]:
    entity_rows: dict[str, dict] = {}
    for index in range(ENTITIES):
        key = f"node-{index:05d}"
        row = copy.deepcopy(entity_shape)
        row.update(
            uuid=key,
            id=RecordID("entity", f"row{index}"),
            name=f"node {index}",
            summary=f"node {index}",
            entity_type=("topic", "task", "note", "pattern")[index % 4],
            project_id=f"project_{index % 4}",
        )
        row["attributes"].update(
            {
                "entity_type": row["entity_type"],
                "project_id": row["project_id"],
                **{
                    f"facet_{facet}": [f"value {index} {facet} {item}" for item in range(WIDTH)]
                    for facet in range(WIDTH)
                },
            }
        )
        entity_rows[key] = row
    edge_rows: dict[str, dict] = {}
    pairs = [(index, max(0, index - 1 - index % 7)) for index in range(1, ENTITIES)]
    pairs += [(index, (index * 31 + 17) % index) for index in range(2, ENTITIES, 2)]
    for number, (source, target) in enumerate(pairs):
        key = f"edge-{number:05d}"
        row = copy.deepcopy(edge_shape)
        source_key, target_key = f"node-{source:05d}", f"node-{target:05d}"
        row.update(
            uuid=key,
            id=RecordID("relates_to", f"row{number}"),
            source_id=source_key,
            target_id=target_key,
            source_uuid=source_key,
            target_uuid=target_key,
            fact=f"{source_key} related_to {target_key}",
            attributes={f"facet_{facet}": [f"edge {number} {facet}"] for facet in range(WIDTH)},
            **{"in": entity_rows[source_key]["id"], "out": entity_rows[target_key]["id"]},
        )
        edge_rows[key] = row
    return entity_rows, edge_rows


@pytest.fixture
async def memory_graph(
    content_store, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[_MemoryGraphClient]:
    entity_rows, edge_rows = _graph_rows(*await _stored_shapes())
    client = _MemoryGraphClient(entity_rows, edge_rows)

    def decode_entities() -> list[Entity]:
        return [
            entity_from_surreal_row(row) for row in normalize_records(list(entity_rows.values()))
        ]

    def decode_relationships() -> list[Relationship]:
        rows = normalize_records(list(edge_rows.values()))
        return [edge for row in rows if (edge := readable_relationship_from_surreal_row(row))]

    # The enumeration walk decodes its pages off the loop too.
    async def list_entities(*_args, **_kwargs) -> list[Entity]:
        return await asyncio.get_running_loop().run_in_executor(_DECODER, decode_entities)

    async def list_relationships(*_args, **_kwargs) -> list[Relationship]:
        return await asyncio.get_running_loop().run_in_executor(_DECODER, decode_relationships)

    monkeypatch.setattr(snapshots, "_list_all_entities", list_entities)
    monkeypatch.setattr(snapshots, "_list_all_relationships", list_relationships)
    _clear_graph_caches()
    yield client
    _clear_graph_caches()


def _clear_graph_caches() -> None:
    for cache in (
        snapshots.GRAPH_SNAPSHOT_CACHE,
        snapshots.GRAPH_VISIBLE_SNAPSHOT_CACHE,
        communities.HIERARCHICAL_CACHE,
        communities.GRAPH_LOD_CACHE,
    ):
        cache.clear()


async def _measured_build(client: _MemoryGraphClient) -> tuple[Any, float, float]:
    """Build cold beside a ticker; the graph, its wall time and the worst lag."""
    done = asyncio.Event()
    worst = 0.0

    async def ticker() -> None:
        nonlocal worst
        while not done.is_set():
            started = time.perf_counter()
            await asyncio.sleep(TICK)
            worst = max(worst, time.perf_counter() - started - TICK)

    # A full collection stops every thread, whichever one triggers it, so it
    # stalls the loop wherever the build runs; it is held off here so the
    # ticker measures where the build's own work runs.
    gc.collect()
    gc.disable()
    ticking = asyncio.create_task(ticker())
    await asyncio.sleep(TICK)
    started = time.perf_counter()
    try:
        data = await communities.get_hierarchical_graph(client, ORG, **READER)
    finally:
        elapsed = time.perf_counter() - started
        done.set()
        await ticking
        gc.enable()
    return data, elapsed, worst


def _rendered(data) -> dict[str, Any]:
    return asdict(data)


async def test_cold_build_keeps_the_event_loop_answering(memory_graph) -> None:
    data, elapsed, worst = await _measured_build(memory_graph)

    # The whole graph was proven: every node and edge survives the proof.
    assert data.total_nodes == ENTITIES
    assert data.total_edges == len(memory_graph.edge_rows)
    assert data.displayed_nodes == 1000
    limit = max(LAG_FLOOR, LAG_SHARE * elapsed)
    assert worst < limit, (
        f"the event loop stalled {worst * 1000:.0f} ms during a {elapsed:.2f} s cold build"
        f" (limit {limit * 1000:.0f} ms)"
    )


async def test_off_loop_build_renders_exactly_what_an_inline_build_does(
    memory_graph, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sibyl_core.services import graph_compute

    builds: list[dict[str, Any]] = []
    for inline_limit in (0, ENTITIES * 10):
        monkeypatch.setattr(graph_compute, "INLINE_ROW_LIMIT", inline_limit)
        _clear_graph_caches()
        data = await communities.get_hierarchical_graph(memory_graph, ORG, **READER)
        builds.append(_rendered(data))

    off_loop, inline = builds
    assert off_loop == inline
    assert off_loop["total_nodes"] == ENTITIES
    assert off_loop["total_edges"] == len(memory_graph.edge_rows)
