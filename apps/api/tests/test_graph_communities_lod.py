from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from sibyl_core.graph import communities
from sibyl_core.graph.communities import (
    GRAPH_RESOLUTION_OVERVIEW,
    _build_cluster_detail_graph_from_snapshot,
    _build_overview_graph_from_snapshot,
)
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType


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
    assert any(
        edge["source"] == "task-1" and edge["target"] == "epic-1" for edge in graph.edges
    )
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
    assert any(
        edge["source"] == "task-1" and edge["target"] == "project-1" for edge in graph.edges
    )


@pytest.mark.asyncio
async def test_get_graph_snapshot_merges_surreal_episodic_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entities = [
        _entity("episode-1", EntityType.EPISODE),
        _entity("task-1", EntityType.TASK),
    ]
    mention_edge = SimpleNamespace(
        uuid="mention-1",
        source_node_uuid="episode-1",
        target_node_uuid="task-1",
        created_at=datetime.now(UTC),
    )

    async def fake_list_all_entities(*args, **kwargs) -> list[Entity]:
        return entities

    async def fake_list_all_relationships(*args, **kwargs) -> list[Relationship]:
        return []

    class FakeEpisodicEdgeOps:
        async def get_by_group_ids(self, driver, group_ids: list[str]) -> list[SimpleNamespace]:
            assert group_ids == ["org-mentions"]
            return [mention_edge]

    class FakeDriver:
        episodic_edge_ops = FakeEpisodicEdgeOps()

    class FakeClient:
        _store = "surreal"

        def get_org_driver(self, organization_id: str) -> FakeDriver:
            assert organization_id == "org-mentions"
            return FakeDriver()

    communities.GRAPH_SNAPSHOT_CACHE.clear()
    monkeypatch.setattr(communities, "_list_all_entities", fake_list_all_entities)
    monkeypatch.setattr(communities, "_list_all_relationships", fake_list_all_relationships)

    snapshot = await communities._get_graph_snapshot(FakeClient(), "org-mentions")

    assert len(snapshot.relationships) == 1
    relationship = snapshot.relationships[0]
    assert relationship.id == "mention-1"
    assert relationship.source_id == "episode-1"
    assert relationship.target_id == "task-1"
    assert relationship.relationship_type == RelationshipType.MENTIONS
