"""Cached graph views must retire descendants of corrected raw sources."""

from unittest.mock import AsyncMock

from sibyl_core.models.entities import Relationship, RelationshipType
from sibyl_core.services import graph_community_clusters as clusters
from sibyl_core.services import graph_community_hierarchy as hierarchy
from sibyl_core.services import graph_community_snapshot as snapshots
from sibyl_core.services.memory_correction import apply_memory_correction
from sibyl_core.services.memory_source_validation import SourceReadAuthority
from sibyl_core.services.surreal_content import remember_raw_memory
from tests.test_graph_derivation_publication import publish, share_plan
from tests.test_synthesis_source_observations import content_store as content_store
from tests.test_synthesis_source_observations import (
    disable_embeddings_and_bind_runtime as disable_embeddings_and_bind_runtime,
)
from tests.test_synthesis_source_observations import runtime as runtime


async def test_warm_community_views_retire_actual_protected_source(
    runtime, content_store, monkeypatch
):
    org = runtime.client.group_id
    monkeypatch.setattr(
        "sibyl_core.services.graph_read_availability.get_surreal_graph_runtime",
        AsyncMock(return_value=runtime),
    )
    resolver = AsyncMock(
        return_value=SourceReadAuthority("user_a", projects=frozenset({"project_a"}))
    )
    monkeypatch.setattr(
        "sibyl_core.services.graph_derivations.get_source_authority_resolver", lambda: resolver
    )
    sources, targets = [], []
    for index in range(2):
        source = await remember_raw_memory(
            organization_id=org,
            principal_id="user_a",
            source_id=f"view-parent-{index}",
            raw_content=f"Deployment gate {index} requires approval from blue team.",
            embedding_provider=None,
        )
        result = await publish(runtime, await share_plan(runtime, source.id))
        assert result.success
        sources.append(source)
        targets.append(result.promoted_id)
    await runtime.relationship_manager.create(
        Relationship(
            id="protected-edge",
            source_id=targets[0],
            target_id=targets[1],
            relationship_type=RelationshipType.RELATED_TO,
        )
    )
    await runtime.relationship_manager.create(
        Relationship(
            id="private-edge",
            source_id=targets[0],
            target_id=targets[1],
            relationship_type=RelationshipType.DEPENDS_ON,
            metadata={"memory_scope": "private", "principal_id": "user_a"},
        )
    )
    snapshots.GRAPH_SNAPSHOT_CACHE.clear()
    clusters.CLUSTER_CACHE.clear()
    hierarchy.HIERARCHICAL_CACHE.clear()
    hierarchy.GRAPH_LOD_CACHE.clear()
    reader = dict(principal_id="user_b", accessible_projects={"project_a"})
    before = await snapshots._get_visible_graph_snapshot(runtime.client, org, **reader)
    assert set(targets) <= set(before.entity_by_id)
    assert any(edge.id == "protected-edge" for edge in before.relationships)
    assert all(edge.id != "private-edge" for edge in before.relationships)
    cluster_before = await clusters.get_clusters_for_visualization(runtime.client, org, **reader)
    assert set(targets) <= {member for cluster in cluster_before for member in cluster.member_ids}
    hierarchy_before = await hierarchy.get_hierarchical_graph(runtime.client, org, **reader)
    assert set(targets) <= {node["id"] for node in hierarchy_before.nodes}
    await apply_memory_correction(
        organization_id=org, principal_id="user_a", source_id=sources[0].id, action="hide"
    )
    after = await snapshots._get_visible_graph_snapshot(runtime.client, org, **reader)
    assert targets[0] not in after.entity_by_id
    assert targets[1] in after.entity_by_id
    assert all(targets[0] not in (edge.source_id, edge.target_id) for edge in after.relationships)
    cluster_after = await clusters.get_clusters_for_visualization(runtime.client, org, **reader)
    assert targets[0] not in {member for cluster in cluster_after for member in cluster.member_ids}
    hierarchy_after = await hierarchy.get_hierarchical_graph(runtime.client, org, **reader)
    assert targets[0] not in {node["id"] for node in hierarchy_after.nodes}
    assert all(targets[0] not in (edge["source"], edge["target"]) for edge in hierarchy_after.edges)
    assert hierarchy_after.total_edges < hierarchy_before.total_edges
