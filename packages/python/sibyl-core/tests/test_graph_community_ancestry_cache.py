"""Reader caches must track the current authorized graph input."""

from unittest.mock import AsyncMock, Mock

import pytest

from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services import graph_community_clusters as clusters
from sibyl_core.services import graph_community_hierarchy as hierarchy
from sibyl_core.services.graph_community_models import GraphSnapshot
from sibyl_core.services.graph_community_snapshot import _snapshot_fingerprint


def snapshot(name: str = "original") -> GraphSnapshot:
    entities = [
        Entity(id="one", name=name, entity_type=EntityType.TASK),
        Entity(id="two", name="second", entity_type=EntityType.TASK),
    ]
    edge = Relationship(
        id="edge",
        source_id="one",
        target_id="two",
        relationship_type=RelationshipType.RELATED_TO,
    )
    return GraphSnapshot(entities, [edge], {entity.id: entity for entity in entities})


def test_snapshot_fingerprint_ignores_order_but_tracks_content() -> None:
    initial = snapshot()
    reordered = GraphSnapshot(
        list(reversed(initial.entities)), initial.relationships, initial.entity_by_id
    )
    assert _snapshot_fingerprint(initial) == _snapshot_fingerprint(reordered)
    changed = GraphSnapshot(
        [initial.entities[0].model_copy(update={"name": "replacement"}), initial.entities[1]],
        initial.relationships,
        initial.entity_by_id,
    )
    assert _snapshot_fingerprint(initial) != _snapshot_fingerprint(changed)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["clusters", "hierarchy"])
@pytest.mark.parametrize("networkx_available", [True, False])
async def test_derived_cache_reuses_only_current_visible_input(
    monkeypatch, surface, networkx_available
) -> None:
    clusters.CLUSTER_CACHE.clear()
    hierarchy.GRAPH_LOD_CACHE.clear()
    hierarchy.HIERARCHICAL_CACHE.clear()
    module = clusters if surface == "clusters" else hierarchy
    initial = snapshot()
    replacement = GraphSnapshot(
        [initial.entities[0].model_copy(update={"name": "replacement"}), initial.entities[1]],
        initial.relationships,
        initial.entity_by_id,
    )
    empty = GraphSnapshot([], [], {})
    visible = AsyncMock(side_effect=[initial, initial, replacement, empty])
    detect = Mock(return_value=[])
    convert = Mock(return_value=object())
    if not networkx_available:
        convert.side_effect = ImportError("NetworkX is optional")
    monkeypatch.setattr(module, "_get_visible_graph_snapshot", visible)
    monkeypatch.setattr(module, "_detect_communities_from_graph", detect)
    monkeypatch.setattr(module, "_snapshot_to_networkx", convert)
    call = (
        clusters.get_clusters_for_visualization
        if surface == "clusters"
        else hierarchy.get_hierarchical_graph
    )
    first = await call(object(), "org-cache")
    assert await call(object(), "org-cache") is first
    assert convert.call_count == 1
    assert detect.call_count == (1 if networkx_available else 0)
    updated = await call(object(), "org-cache")
    assert updated is not first
    if surface == "hierarchy":
        assert next(node for node in updated.nodes if node["id"] == "one")["name"] == "replacement"
    assert convert.call_count == 2
    assert detect.call_count == (2 if networkx_available else 0)
    retired = await call(object(), "org-cache")
    assert convert.call_count == 3
    assert detect.call_count == (3 if networkx_available else 0)
    if surface == "clusters":
        assert retired == []
    else:
        assert not retired.nodes
        assert not retired.edges
        assert retired.total_nodes == 0
    assert visible.await_count == 4
