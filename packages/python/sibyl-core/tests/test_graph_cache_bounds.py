"""Graph caches hold a bounded number of compact entries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services import graph_community_clusters as clusters
from sibyl_core.services import graph_community_hierarchy as hierarchy
from sibyl_core.services import graph_community_managers as managers
from sibyl_core.services import graph_community_snapshot as snapshots
from sibyl_core.services import graph_view_availability
from sibyl_core.services.graph_community_snapshot import BoundedTTLCache


def test_cache_evicts_least_recently_used_beyond_its_bound() -> None:
    cache: BoundedTTLCache[str, int] = BoundedTTLCache(maxsize=3, ttl=timedelta(minutes=5))
    for index in range(3):
        cache[f"key-{index}"] = index
    assert cache["key-0"] == 0

    cache["key-3"] = 3

    assert len(cache) == 3
    assert set(cache) == {"key-0", "key-2", "key-3"}
    assert "key-1" not in cache


def test_cache_sweeps_expired_entries_on_write_and_refuses_them_on_read(monkeypatch) -> None:
    cache: BoundedTTLCache[str, int] = BoundedTTLCache(maxsize=10, ttl=timedelta(seconds=30))
    clock = {"now": 1000.0}
    monkeypatch.setattr(snapshots.time, "monotonic", lambda: clock["now"])
    cache["old"] = 1
    clock["now"] += 31
    assert "old" not in cache
    cache["old"] = 1
    clock["now"] += 31
    cache["new"] = 2

    assert set(cache) == {"new"}
    assert cache.copy() == {"new": 2}


@pytest.mark.parametrize(
    ("cache", "size"),
    [
        (snapshots.GRAPH_SNAPSHOT_CACHE, snapshots.GRAPH_SNAPSHOT_CACHE_SIZE),
        (snapshots.GRAPH_VISIBLE_SNAPSHOT_CACHE, snapshots.GRAPH_VISIBLE_SNAPSHOT_CACHE_SIZE),
        (hierarchy.HIERARCHICAL_CACHE, hierarchy.HIERARCHICAL_CACHE_SIZE),
        (hierarchy.GRAPH_LOD_CACHE, hierarchy.GRAPH_LOD_CACHE_SIZE),
        (clusters.CLUSTER_CACHE, clusters.CLUSTER_CACHE_SIZE),
    ],
)
def test_module_caches_are_bounded(cache, size) -> None:
    assert isinstance(cache, BoundedTTLCache)
    assert cache.maxsize == size


async def test_reader_snapshots_are_cached_without_bodies_or_vectors(monkeypatch) -> None:
    assert not snapshots.GRAPH_VISIBLE_SNAPSHOT_LOADS
    snapshots.GRAPH_SNAPSHOT_CACHE.clear()
    snapshots.GRAPH_VISIBLE_SNAPSHOT_CACHE.clear()
    org = "org-compact"
    entities = [
        Entity(
            id="fat",
            name="Fat node",
            entity_type=EntityType.TOPIC,
            organization_id=org,
            content="a very long body " * 100,
            embedding=[0.25] * 8,
            metadata={"summary": "kept", "content": "dropped", "name_embedding": [0.5] * 8},
        )
    ]

    async def list_entities(*args, **kwargs):
        return list(entities)

    async def list_relationships(*args, **kwargs):
        return []

    async def view(organization_id, ids, edges, *, runtime, source_visible=None):
        return {entity.id: entity for entity in entities}, dict(edges)

    monkeypatch.setattr(snapshots, "_list_all_entities", list_entities)
    monkeypatch.setattr(snapshots, "_list_all_relationships", list_relationships)
    monkeypatch.setattr(graph_view_availability, "available_graph_view", view)
    monkeypatch.setattr(
        managers, "_runtime_for_client", lambda client, _org: SimpleNamespace(client=client)
    )

    visible = await snapshots._get_visible_graph_snapshot(
        object(), org, principal_id="user_a", accessible_projects=set()
    )

    cached = visible.entity_by_id["fat"]
    assert cached.content == ""
    assert cached.embedding is None
    assert cached.metadata == {"summary": "kept"}
    assert cached.name == "Fat node"
    assert visible.fingerprint is not None
    assert (
        snapshots.GRAPH_VISIBLE_SNAPSHOT_CACHE[next(iter(snapshots.GRAPH_VISIBLE_SNAPSHOT_CACHE))][
            1
        ]
        is visible
    )
    stamp = datetime.now(UTC)
    assert all(
        stamp - cached_at < snapshots.GRAPH_SNAPSHOT_CACHE_TTL
        for cached_at, _ in snapshots.GRAPH_SNAPSHOT_CACHE.values()
    )
