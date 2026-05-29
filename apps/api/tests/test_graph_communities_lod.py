from __future__ import annotations

import asyncio

import pytest

import sibyl_core.services.graph_communities as communities
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services.graph_communities import (
    GRAPH_RESOLUTION_OVERVIEW,
    _build_cluster_detail_graph_from_snapshot,
    _build_overview_graph_from_snapshot,
)


def _entity(entity_id: str, entity_type: EntityType, *, project_id: str = "project-1") -> Entity:
    return Entity(
        id=entity_id,
        entity_type=entity_type,
        name=entity_id,
        description=f"{entity_type.value}:{entity_id}",
        metadata={"project_id": project_id},
    )


def _relationship(
    relationship_id: str,
    source_id: str,
    target_id: str,
    relationship_type: RelationshipType = RelationshipType.RELATED_TO,
) -> Relationship:
    return Relationship(
        id=relationship_id,
        source_id=source_id,
        target_id=target_id,
        relationship_type=relationship_type,
    )


def test_build_overview_graph_preserves_rare_categories() -> None:
    entities = [
        _entity("task-1", EntityType.TASK),
        _entity("task-2", EntityType.TASK),
        _entity("task-3", EntityType.TASK),
        _entity("episode-1", EntityType.EPISODE),
        _entity("epic-1", EntityType.EPIC),
    ]
    relationships = [
        _relationship("rel-1", "task-1", "task-2"),
        _relationship("rel-2", "task-2", "task-3"),
        _relationship("rel-3", "task-1", "epic-1", RelationshipType.BELONGS_TO),
    ]
    node_to_cluster = {
        "task-1": "cluster-a",
        "task-2": "cluster-a",
        "task-3": "cluster-a",
        "episode-1": "cluster-a",
        "epic-1": "cluster-b",
    }
    clusters_meta = [
        {"id": "cluster-a", "member_count": 4, "level": 0},
        {"id": "cluster-b", "member_count": 1, "level": 0},
    ]

    graph = _build_overview_graph_from_snapshot(
        entities,
        relationships,
        node_to_cluster,
        clusters_meta,
        project_ids=["project-1"],
        max_nodes=10,
        max_edges=10,
    )

    assert graph.resolution == GRAPH_RESOLUTION_OVERVIEW
    assert {node["id"] for node in graph.nodes} == {
        "task-1",
        "task-2",
        "task-3",
        "episode-1",
        "epic-1",
    }
    assert any(node["type"] == "episode" and node["name"] == "episode-1" for node in graph.nodes)
    assert any(node["type"] == "epic" and node["name"] == "epic-1" for node in graph.nodes)
    assert all(not node.get("aggregate", False) for node in graph.nodes)
    assert any(edge["source"] == "task-1" and edge["target"] == "epic-1" for edge in graph.edges)
    cluster_a = next(cluster for cluster in graph.clusters if cluster["id"] == "cluster-a")
    assert cluster_a["displayed_member_count"] == 4
    assert cluster_a["displayed_type_distribution"]["task"] == 3
    assert cluster_a["displayed_type_distribution"]["episode"] == 1


def test_build_cluster_detail_graph_includes_cluster_members_and_neighbors() -> None:
    entities = [
        _entity("task-1", EntityType.TASK),
        _entity("task-2", EntityType.TASK),
        _entity("episode-1", EntityType.EPISODE),
        _entity("project-1", EntityType.PROJECT),
    ]
    relationships = [
        _relationship("rel-1", "task-1", "task-2"),
        _relationship("rel-2", "task-1", "project-1", RelationshipType.BELONGS_TO),
    ]
    node_to_cluster = {
        "task-1": "cluster-a",
        "task-2": "cluster-a",
        "episode-1": "cluster-a",
        "project-1": "cluster-b",
    }
    clusters_meta = [
        {"id": "cluster-a", "member_count": 3, "level": 0},
        {"id": "cluster-b", "member_count": 1, "level": 0},
    ]

    graph = _build_cluster_detail_graph_from_snapshot(
        entities,
        relationships,
        node_to_cluster,
        clusters_meta,
        cluster_id="cluster-a",
        project_ids=["project-1"],
        max_nodes=10,
        max_edges=10,
    )

    assert graph.resolution == "detail"
    assert {node["id"] for node in graph.nodes} >= {"task-1", "task-2", "episode-1", "project-1"}
    assert any(edge["source"] == "task-1" and edge["target"] == "project-1" for edge in graph.edges)


@pytest.mark.asyncio
async def test_get_graph_snapshot_fetches_entities_and_edges_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entities_started = asyncio.Event()
    relationships_started = asyncio.Event()

    async def fake_list_all_entities(*args, **kwargs) -> list[Entity]:
        entities_started.set()
        await relationships_started.wait()
        return [_entity("task-1", EntityType.TASK)]

    async def fake_list_all_relationships(*args, **kwargs) -> list[Relationship]:
        relationships_started.set()
        await entities_started.wait()
        return []

    communities.GRAPH_SNAPSHOT_CACHE.clear()
    monkeypatch.setattr(communities, "_list_all_entities", fake_list_all_entities)
    monkeypatch.setattr(communities, "_list_all_relationships", fake_list_all_relationships)

    snapshot = await asyncio.wait_for(
        communities._get_graph_snapshot(object(), "org-concurrent"),
        timeout=1,
    )

    assert [entity.id for entity in snapshot.entities] == ["task-1"]


@pytest.mark.asyncio
async def test_get_graph_snapshot_joins_concurrent_loads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load_count = 0

    async def fake_list_all_entities(*args, **kwargs) -> list[Entity]:
        nonlocal load_count
        load_count += 1
        await asyncio.sleep(0)
        return [_entity("task-1", EntityType.TASK)]

    async def fake_list_all_relationships(*args, **kwargs) -> list[Relationship]:
        await asyncio.sleep(0)
        return []

    communities.GRAPH_SNAPSHOT_CACHE.clear()
    communities.GRAPH_SNAPSHOT_LOADS.clear()
    monkeypatch.setattr(communities, "_list_all_entities", fake_list_all_entities)
    monkeypatch.setattr(communities, "_list_all_relationships", fake_list_all_relationships)

    first, second = await asyncio.gather(
        communities._get_graph_snapshot(
            object(),
            "org-single-flight",
            max_entities=100,
            max_relationships=200,
        ),
        communities._get_graph_snapshot(
            object(),
            "org-single-flight",
            max_entities=100,
            max_relationships=200,
        ),
    )

    assert first is second
    assert load_count == 1


@pytest.mark.asyncio
async def test_get_graph_snapshot_cancels_inflight_load_on_request_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake_load_graph_snapshot(*args, **kwargs) -> communities.GraphSnapshot:
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("snapshot load should have been cancelled")

    communities.GRAPH_SNAPSHOT_CACHE.clear()
    communities.GRAPH_SNAPSHOT_LOADS.clear()
    monkeypatch.setattr(communities, "_load_graph_snapshot", fake_load_graph_snapshot)

    task = asyncio.create_task(
        communities._get_graph_snapshot(
            object(),
            "org-cancel",
            max_entities=100,
            max_relationships=200,
        )
    )
    await started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancelled.is_set()
    assert communities.GRAPH_SNAPSHOT_LOADS == {}


@pytest.mark.asyncio
async def test_get_graph_snapshot_joiner_cancel_does_not_cancel_shared_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_load_graph_snapshot(*args, **kwargs) -> communities.GraphSnapshot:
        started.set()
        await release.wait()
        return communities.GraphSnapshot(entities=[], relationships=[], entity_by_id={})

    communities.GRAPH_SNAPSHOT_CACHE.clear()
    communities.GRAPH_SNAPSHOT_LOADS.clear()
    monkeypatch.setattr(communities, "_load_graph_snapshot", fake_load_graph_snapshot)

    owner = asyncio.create_task(
        communities._get_graph_snapshot(
            object(),
            "org-join-cancel",
            max_entities=100,
            max_relationships=200,
        )
    )
    await started.wait()

    joiner = asyncio.create_task(
        communities._get_graph_snapshot(
            object(),
            "org-join-cancel",
            max_entities=100,
            max_relationships=200,
        )
    )
    await asyncio.sleep(0)

    joiner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await joiner

    # The joiner's cancellation must not abort the shared loader the owner
    # still awaits; the owner must complete normally.
    release.set()
    snapshot = await asyncio.wait_for(owner, timeout=1)
    assert snapshot.entities == []
