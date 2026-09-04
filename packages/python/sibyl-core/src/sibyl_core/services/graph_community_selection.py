"""Graph focus, representative selection, and level-of-detail rendering."""

from __future__ import annotations

import heapq
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services.graph_community_managers import _entity_summary
from sibyl_core.services.graph_community_models import (
    GRAPH_RESOLUTION_OVERVIEW,
    HierarchicalGraphData,
)
from sibyl_core.services.graph_community_snapshot import (
    _entity_index,
    _reader_cache_key,
)

_GRAPH_DIVERSITY_THRESHOLD = 100
_GRAPH_PRIMARY_SAMPLE_SHARE = 0.8
_GRAPH_MISSING_TYPE_MIN_RESERVE = 5

DETECTION_MAX_ENTITIES = 25_000
DETECTION_MAX_RELATIONSHIPS = 100_000
CLUSTER_DETAIL_MAX_NODES = 1000
CLUSTER_DETAIL_MAX_EDGES = 5000
CLUSTER_SUMMARY_MEMBER_SAMPLE = 1000
OVERVIEW_NODE_THRESHOLD = 400
OVERVIEW_MAX_CLUSTERS = 18

_NOISE_RELATIONSHIP_TYPES = frozenset({RelationshipType.MENTIONS})
_GRAPH_HUB_SEED_SHARE = 0.4


def _normalized_cache_list(values: list[str] | None) -> tuple[str, ...]:
    if not values:
        return ()
    return tuple(dict.fromkeys(values))


def _lod_cache_key(
    *,
    organization_id: str,
    project_ids: list[str] | None,
    entity_types: list[str] | None,
    resolution: str,
    cluster_id: str | None,
    max_nodes: int,
    max_edges: int,
    principal_id: str | None,
    accessible_projects: set[str] | None,
    allowed_memory_scope_keys: set[str] | None = None,
) -> tuple[Any, ...]:
    return (
        organization_id,
        _normalized_cache_list(project_ids),
        _normalized_cache_list(entity_types),
        resolution,
        cluster_id,
        max_nodes,
        max_edges,
        _reader_cache_key(principal_id, accessible_projects, allowed_memory_scope_keys),
    )


def _entity_timestamp(entity: Entity | None) -> datetime:
    if entity is None:
        return datetime.min.replace(tzinfo=UTC)
    return entity.updated_at or entity.created_at or datetime.min.replace(tzinfo=UTC)


def _entity_priority_key(
    entity_id: str,
    entity_by_id: dict[str, Entity],
    degrees: Counter[str],
) -> tuple[int, datetime, str]:
    return (
        degrees.get(entity_id, 0),
        _entity_timestamp(entity_by_id.get(entity_id)),
        entity_id,
    )


def _allocate_diversity_quotas(
    remaining_by_type: dict[str, list[str]],
    *,
    represented_types: set[str],
    budget: int,
) -> dict[str, int]:
    quotas = {entity_type: 0 for entity_type, ids in remaining_by_type.items() if ids}
    if budget <= 0 or not quotas:
        return quotas

    missing_types = [
        entity_type
        for entity_type, ids in remaining_by_type.items()
        if ids and entity_type not in represented_types
    ]
    for entity_type in missing_types:
        if budget <= 0:
            break
        reserve = min(_GRAPH_MISSING_TYPE_MIN_RESERVE, len(remaining_by_type[entity_type]), budget)
        quotas[entity_type] += reserve
        budget -= reserve

    while budget > 0:
        eligible_types = [
            entity_type
            for entity_type, ids in remaining_by_type.items()
            if quotas.get(entity_type, 0) < len(ids)
        ]
        if not eligible_types:
            break
        next_type = max(
            eligible_types,
            key=lambda entity_type: (
                len(remaining_by_type[entity_type]) - quotas.get(entity_type, 0),
                entity_type,
            ),
        )
        quotas[next_type] += 1
        budget -= 1

    return quotas


def _pick_representative_node_ids(
    focused_ids: set[str],
    entity_by_id: dict[str, Entity],
    degrees: Counter[str],
    *,
    max_nodes: int,
) -> list[str]:
    ranked_ids = sorted(
        focused_ids,
        key=lambda entity_id: _entity_priority_key(entity_id, entity_by_id, degrees),
        reverse=True,
    )
    if len(ranked_ids) <= max_nodes or max_nodes < _GRAPH_DIVERSITY_THRESHOLD:
        return ranked_ids[:max_nodes]

    primary_target = max(1, min(len(ranked_ids), int(max_nodes * _GRAPH_PRIMARY_SAMPLE_SHARE)))
    selected_ids = set(ranked_ids[:primary_target])
    remaining_budget = max_nodes - len(selected_ids)
    if remaining_budget <= 0:
        return ranked_ids[:max_nodes]

    represented_types = {
        entity.entity_type.value
        for entity_id in selected_ids
        if (entity := entity_by_id.get(entity_id)) is not None
    }

    remaining_by_type: dict[str, list[str]] = {}
    for entity_id in ranked_ids[primary_target:]:
        entity = entity_by_id.get(entity_id)
        if entity is None:
            continue
        remaining_by_type.setdefault(entity.entity_type.value, []).append(entity_id)

    quotas = _allocate_diversity_quotas(
        remaining_by_type,
        represented_types=represented_types,
        budget=remaining_budget,
    )
    for entity_type, quota in quotas.items():
        if quota <= 0:
            continue
        selected_ids.update(remaining_by_type[entity_type][:quota])

    if len(selected_ids) < max_nodes:
        for entity_id in ranked_ids[primary_target:]:
            if entity_id in selected_ids:
                continue
            selected_ids.add(entity_id)
            if len(selected_ids) >= max_nodes:
                break

    return [entity_id for entity_id in ranked_ids if entity_id in selected_ids][:max_nodes]


def _build_focused_adjacency(
    relationships: list[Relationship],
    focused_ids: set[str],
) -> dict[str, set[str]]:
    """Undirected adjacency among focused nodes, for connectivity-aware sampling."""
    adjacency: dict[str, set[str]] = {}
    for relationship in relationships:
        source = relationship.source_id
        target = relationship.target_id
        if source == target or source not in focused_ids or target not in focused_ids:
            continue
        adjacency.setdefault(source, set()).add(target)
        adjacency.setdefault(target, set()).add(source)
    return adjacency


def _pick_connected_node_ids(
    focused_ids: set[str],
    entity_by_id: dict[str, Entity],
    degrees: Counter[str],
    adjacency: dict[str, set[str]],
    *,
    max_nodes: int,
) -> list[str]:
    """Select up to max_nodes that form a DENSE, connected subgraph.

    The previous selector took the top nodes by degree/recency, which over a
    large graph picks hubs from unrelated neighborhoods whose neighbors fall
    outside the slice — so almost no edge has both endpoints selected and the
    render is a starfield. Instead: seed with the top-degree hubs, then grow by
    repeatedly attaching the highest-degree unselected neighbor of the current
    set, so every added node carries at least one surviving edge.
    """
    # Only nodes that actually connect to something. Isolated singletons (no
    # focused edge) add no signal and render as a starfield halo around the
    # connected core, so they are excluded from the displayed subgraph.
    connected_ids = {entity_id for entity_id in focused_ids if degrees.get(entity_id, 0) > 0}
    ranked_ids = sorted(
        connected_ids,
        key=lambda entity_id: _entity_priority_key(entity_id, entity_by_id, degrees),
        reverse=True,
    )
    if len(ranked_ids) <= max_nodes:
        return ranked_ids

    seed_count = max(1, min(max_nodes, int(max_nodes * _GRAPH_HUB_SEED_SHARE)))
    selected: set[str] = set(ranked_ids[:seed_count])

    queued: set[str] = set(selected)
    frontier: list[tuple[int, str]] = []

    def _enqueue_neighbors(node_id: str) -> None:
        for neighbor in adjacency.get(node_id, ()):
            if neighbor in queued or degrees.get(neighbor, 0) == 0:
                continue
            queued.add(neighbor)
            heapq.heappush(frontier, (-degrees.get(neighbor, 0), neighbor))

    for node_id in selected:
        _enqueue_neighbors(node_id)

    while len(selected) < max_nodes and frontier:
        _, neighbor = heapq.heappop(frontier)
        if neighbor in selected:
            continue
        selected.add(neighbor)
        _enqueue_neighbors(neighbor)

    # Disconnected remainder: spend any leftover budget on a type-diversity
    # reserve (so rare types still appear) then the highest-degree leftovers.
    if len(selected) < max_nodes:
        remaining_by_type: dict[str, list[str]] = {}
        for entity_id in ranked_ids:
            if entity_id in selected:
                continue
            entity = entity_by_id.get(entity_id)
            if entity is None:
                continue
            remaining_by_type.setdefault(entity.entity_type.value, []).append(entity_id)
        represented_types = {
            entity.entity_type.value
            for entity_id in selected
            if (entity := entity_by_id.get(entity_id)) is not None
        }
        quotas = _allocate_diversity_quotas(
            remaining_by_type,
            represented_types=represented_types,
            budget=max_nodes - len(selected),
        )
        for entity_type, quota in quotas.items():
            for entity_id in remaining_by_type[entity_type][:quota]:
                selected.add(entity_id)
        if len(selected) < max_nodes:
            for entity_id in ranked_ids:
                if entity_id in selected:
                    continue
                selected.add(entity_id)
                if len(selected) >= max_nodes:
                    break

    return [entity_id for entity_id in ranked_ids if entity_id in selected][:max_nodes]


def _cluster_type_counts(
    entity_ids: set[str],
    entity_by_id: dict[str, Entity],
    node_to_cluster: dict[str, str],
) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}

    for entity_id in entity_ids:
        entity = entity_by_id.get(entity_id)
        if entity is None:
            continue
        cluster_id = node_to_cluster.get(entity_id, "unclustered")
        entity_type = entity.entity_type.value
        cluster_counts = counts.setdefault(cluster_id, {})
        cluster_counts[entity_type] = cluster_counts.get(entity_type, 0) + 1

    return counts


def _dominant_type(type_counts: dict[str, int]) -> str:
    if not type_counts:
        return "unknown"
    return max(type_counts.items(), key=lambda item: (item[1], item[0]))[0]


def _snapshot_to_networkx(
    entities: list[Entity],
    relationships: list[Relationship],
    *,
    type_affinity_weight: float = 2.0,
) -> Any:
    try:
        import networkx as nx
    except ImportError as e:
        raise ImportError(
            "networkx is required for community detection. Install with: pip install networkx"
        ) from e

    G = nx.Graph()

    for entity in sorted(entities, key=lambda item: item.id or ""):
        if entity.id:
            G.add_node(entity.id, name=entity.name, type=entity.entity_type.value)

    for relationship in sorted(
        relationships,
        key=lambda item: (
            item.source_id,
            item.target_id,
            item.relationship_type.value,
            item.id or "",
        ),
    ):
        if relationship.source_id not in G or relationship.target_id not in G:
            continue

        source_type = G.nodes[relationship.source_id].get("type", "")
        target_type = G.nodes[relationship.target_id].get("type", "")
        weight = 1.0
        if source_type and target_type and source_type == target_type:
            weight += type_affinity_weight

        if G.has_edge(relationship.source_id, relationship.target_id):
            G[relationship.source_id][relationship.target_id]["weight"] += weight
        else:
            G.add_edge(
                relationship.source_id,
                relationship.target_id,
                rel_type=relationship.relationship_type.value,
                weight=weight,
            )

    return G


def _matches_project_focus(entity: Entity, project_ids: list[str] | None) -> bool:
    if not project_ids:
        return True

    unassigned_id = "__unassigned__"
    has_unassigned = unassigned_id in project_ids
    real_project_ids = {project_id for project_id in project_ids if project_id != unassigned_id}
    entity_project_id = entity.metadata.get("project_id")
    if not isinstance(entity_project_id, str) or not entity_project_id:
        entity_project_id = None

    if has_unassigned and real_project_ids:
        return (
            entity_project_id is None
            or entity.id in real_project_ids
            or entity_project_id in real_project_ids
        )
    if has_unassigned:
        return entity_project_id is None
    return entity.id in real_project_ids or entity_project_id in real_project_ids


def _document_neighbor_ids(
    entity_by_id: dict[str, Entity],
    relationships: list[Relationship],
    focused_ids: set[str],
) -> set[str]:
    document_ids: set[str] = set()

    for relationship in relationships:
        if relationship.relationship_type != RelationshipType.DOCUMENTED_IN:
            continue
        source = entity_by_id.get(relationship.source_id)
        target = entity_by_id.get(relationship.target_id)
        if source is None or target is None:
            continue

        if source.entity_type == EntityType.DOCUMENT and target.id in focused_ids:
            document_ids.add(source.id)
        if target.entity_type == EntityType.DOCUMENT and source.id in focused_ids:
            document_ids.add(target.id)

    return document_ids


def _focused_entity_ids(
    entities: list[Entity],
    relationships: list[Relationship],
    *,
    project_ids: list[str] | None = None,
    entity_types: list[str] | None = None,
) -> set[str]:
    entity_by_id = _entity_index(entities)
    allowed_types = {entity_type.lower() for entity_type in entity_types} if entity_types else None
    focused_ids = {
        entity.id
        for entity in entities
        if entity.id and _matches_project_focus(entity, project_ids)
    }

    if project_ids:
        focused_ids.update(_document_neighbor_ids(entity_by_id, relationships, focused_ids))

    if allowed_types is not None:
        focused_ids = {
            entity_id
            for entity_id in focused_ids
            if (entity := entity_by_id.get(entity_id)) is not None
            and entity.entity_type.value.lower() in allowed_types
        }

    return focused_ids


def _graph_totals_from_snapshot(
    entities: list[Entity],
    relationships: list[Relationship],
    *,
    project_ids: list[str] | None = None,
    entity_types: list[str] | None = None,
) -> tuple[int, int]:
    node_ids = _focused_entity_ids(
        entities,
        relationships,
        project_ids=project_ids,
        entity_types=entity_types,
    )
    edge_count = sum(
        1
        for relationship in relationships
        if relationship.source_id in node_ids and relationship.target_id in node_ids
    )
    return len(node_ids), edge_count


def _build_graph_nodes_from_snapshot(
    entities: list[Entity],
    relationships: list[Relationship],
    node_to_cluster: dict[str, str],
    *,
    max_nodes: int,
    project_ids: list[str] | None = None,
    entity_types: list[str] | None = None,
) -> tuple[list[dict[str, Any]], set[str]]:
    focused_ids = _focused_entity_ids(
        entities,
        relationships,
        project_ids=project_ids,
        entity_types=entity_types,
    )
    entity_by_id = _entity_index(entities)
    degrees: Counter[str] = Counter()
    for relationship in relationships:
        if relationship.source_id not in focused_ids or relationship.target_id not in focused_ids:
            continue
        degrees[relationship.source_id] += 1
        if relationship.target_id != relationship.source_id:
            degrees[relationship.target_id] += 1

    adjacency = _build_focused_adjacency(relationships, focused_ids)
    selected_ids = _pick_connected_node_ids(
        focused_ids,
        entity_by_id,
        degrees,
        adjacency,
        max_nodes=max_nodes,
    )

    nodes: list[dict[str, Any]] = []
    node_ids = set(selected_ids)

    for entity_id in selected_ids:
        entity = entity_by_id.get(entity_id)
        if entity is None:
            continue

        nodes.append(
            {
                "id": entity.id,
                "name": entity.name or entity.id[:20],
                "type": entity.entity_type.value,
                "summary": _entity_summary(entity),
                "cluster_id": node_to_cluster.get(entity.id, "unclustered"),
            }
        )

    return nodes, node_ids


def _build_graph_edges_from_snapshot(
    relationships: list[Relationship],
    node_ids: set[str],
    *,
    max_edges: int,
) -> list[dict[str, Any]]:
    candidates = [
        relationship
        for relationship in relationships
        if relationship.source_id in node_ids and relationship.target_id in node_ids
    ]

    # Over budget: keep the edges between the most-connected nodes so the
    # densest, most legible structure survives instead of whatever iterates
    # first.
    if len(candidates) > max_edges:
        local_degree: Counter[str] = Counter()
        for relationship in candidates:
            local_degree[relationship.source_id] += 1
            local_degree[relationship.target_id] += 1
        candidates.sort(
            key=lambda r: local_degree[r.source_id] + local_degree[r.target_id],
            reverse=True,
        )
        candidates = candidates[:max_edges]

    return [
        {
            "source": relationship.source_id,
            "target": relationship.target_id,
            "type": relationship.relationship_type.value,
        }
        for relationship in candidates
    ]


def _overview_cluster_label(
    cluster_id: str,
    member_ids: list[str],
    entity_by_id: dict[str, Entity],
    degrees: Counter[str],
    dominant_type: str,
) -> str:
    """Human label for an aggregate cluster: its top members by degree."""
    if cluster_id == "unclustered":
        return "Unclustered"
    ranked = sorted(member_ids, key=lambda entity_id: degrees.get(entity_id, 0), reverse=True)
    names: list[str] = []
    for entity_id in ranked[:2]:
        entity = entity_by_id.get(entity_id)
        name = (entity.name if entity is not None else "") or ""
        if name:
            names.append(name if len(name) <= 24 else f"{name[:21]}…")
    if names:
        return ", ".join(names)
    return dominant_type.replace("_", " ").title() if dominant_type else "Cluster"


def _build_overview_graph_from_snapshot(
    entities: list[Entity],
    relationships: list[Relationship],
    node_to_cluster: dict[str, str],
    clusters_meta: list[dict[str, Any]],
    *,
    project_ids: list[str] | None = None,
    entity_types: list[str] | None = None,
    max_nodes: int = 1000,
    max_edges: int = 5000,
) -> HierarchicalGraphData:
    """Aggregate overview: one bubble per community, bridged by inter-cluster edges.

    This is the legible entry point for a large graph — the detail view is
    reached by drilling into a cluster. Each aggregate node carries
    `aggregate=True` and `member_count` (the frontend sizes and styles bubbles
    from these) and a dominant `type` so it picks up the entity color.
    """
    entity_by_id = _entity_index(entities)
    focused_ids = _focused_entity_ids(
        entities,
        relationships,
        project_ids=project_ids,
        entity_types=entity_types,
    )
    total_node_count, total_edge_count = _graph_totals_from_snapshot(
        entities,
        relationships,
        project_ids=project_ids,
        entity_types=entity_types,
    )
    if not focused_ids:
        return HierarchicalGraphData(
            nodes=[],
            edges=[],
            clusters=[],
            cluster_edges=[],
            total_nodes=total_node_count,
            total_edges=total_edge_count,
            displayed_nodes=0,
            displayed_edges=0,
            resolution=GRAPH_RESOLUTION_OVERVIEW,
        )

    degrees: Counter[str] = Counter()
    for relationship in relationships:
        if relationship.source_id not in focused_ids or relationship.target_id not in focused_ids:
            continue
        degrees[relationship.source_id] += 1
        if relationship.target_id != relationship.source_id:
            degrees[relationship.target_id] += 1

    members_by_cluster: dict[str, list[str]] = {}
    type_counts_by_cluster: dict[str, dict[str, int]] = {}
    for entity_id in focused_ids:
        entity = entity_by_id.get(entity_id)
        if entity is None:
            continue
        cluster_id = node_to_cluster.get(entity_id, "unclustered")
        members_by_cluster.setdefault(cluster_id, []).append(entity_id)
        type_counts = type_counts_by_cluster.setdefault(cluster_id, {})
        entity_type = entity.entity_type.value
        type_counts[entity_type] = type_counts.get(entity_type, 0) + 1

    # Map the largest real communities only: exclude the unclustered remainder
    # and the long tail of tiny communities so the overview reads as a clean
    # domain map, not a field of dots. A community whose focused members are
    # all isolated inside the focus (their edges lead out of it) is excluded
    # too: the detail sample and the totals keep only connected nodes, so a
    # bubble for it would stand for nothing the detail can ever show. Member
    # counts still cover the whole focused membership, matching the legend.
    ranked_clusters = sorted(
        (
            (cluster_id, members)
            for cluster_id, members in members_by_cluster.items()
            if cluster_id != "unclustered"
            and any(degrees.get(member_id, 0) > 0 for member_id in members)
        ),
        key=lambda item: len(item[1]),
        reverse=True,
    )[:OVERVIEW_MAX_CLUSTERS]

    # Fallback: if community detection produced no real clusters (e.g. networkx
    # unavailable, or every node landed unclustered), show the unclustered set
    # as a single bubble so the overview is never blank. type_counts_by_cluster
    # already holds the correct distribution for the unclustered bucket.
    if not ranked_clusters and members_by_cluster.get("unclustered"):
        ranked_clusters = [("unclustered", members_by_cluster["unclustered"])]

    kept_cluster_ids = {cluster_id for cluster_id, _ in ranked_clusters}

    aggregate_nodes: list[dict[str, Any]] = []
    enriched_clusters: list[dict[str, Any]] = []
    for cluster_id, members in ranked_clusters:
        type_dist = type_counts_by_cluster.get(cluster_id, {})
        dominant = _dominant_type(type_dist)
        label = _overview_cluster_label(cluster_id, members, entity_by_id, degrees, dominant)
        summary = ", ".join(
            f"{count} {entity_type.replace('_', ' ')}"
            for entity_type, count in sorted(
                type_dist.items(), key=lambda item: item[1], reverse=True
            )[:4]
        )
        aggregate_nodes.append(
            {
                "id": cluster_id,
                "name": label,
                "type": dominant,
                "summary": summary,
                "cluster_id": cluster_id,
                "aggregate": True,
                "member_count": len(members),
            }
        )
        enriched_clusters.append(
            {
                "id": cluster_id,
                "label": label,
                "member_count": len(members),
                "displayed_member_count": len(members),
                "level": 0,
                "type_distribution": type_dist,
                "displayed_type_distribution": type_dist,
                "dominant_type": dominant,
                "displayed_dominant_type": dominant,
            }
        )

    cluster_edge_counts: dict[tuple[str, str], int] = {}
    for relationship in relationships:
        if relationship.source_id not in focused_ids or relationship.target_id not in focused_ids:
            continue
        source_cluster = node_to_cluster.get(relationship.source_id, "unclustered")
        target_cluster = node_to_cluster.get(relationship.target_id, "unclustered")
        if source_cluster == target_cluster:
            continue
        if source_cluster not in kept_cluster_ids or target_cluster not in kept_cluster_ids:
            continue
        ordered = sorted((source_cluster, target_cluster))
        pair: tuple[str, str] = (ordered[0], ordered[1])
        cluster_edge_counts[pair] = cluster_edge_counts.get(pair, 0) + 1

    overview_edges = [
        {"source": pair[0], "target": pair[1], "type": "inter_cluster", "weight": weight}
        for pair, weight in cluster_edge_counts.items()
        if weight > 0
    ]
    if len(overview_edges) > max_edges:
        overview_edges.sort(key=lambda edge: edge["weight"], reverse=True)
        overview_edges = overview_edges[:max_edges]

    return HierarchicalGraphData(
        nodes=aggregate_nodes,
        edges=overview_edges,
        clusters=enriched_clusters,
        cluster_edges=overview_edges,
        total_nodes=total_node_count,
        total_edges=total_edge_count,
        displayed_nodes=len(aggregate_nodes),
        displayed_edges=len(overview_edges),
        resolution=GRAPH_RESOLUTION_OVERVIEW,
    )


def _build_cluster_detail_graph_from_snapshot(
    entities: list[Entity],
    relationships: list[Relationship],
    node_to_cluster: dict[str, str],
    clusters_meta: list[dict[str, Any]],
    *,
    cluster_id: str | None = None,
    project_ids: list[str] | None = None,
    entity_types: list[str] | None = None,
    max_nodes: int = 1000,
    max_edges: int = 5000,
) -> HierarchicalGraphData:
    entity_by_id = _entity_index(entities)
    focused_ids = _focused_entity_ids(
        entities,
        relationships,
        project_ids=project_ids,
        entity_types=entity_types,
    )
    total_node_count, total_edge_count = _graph_totals_from_snapshot(
        entities,
        relationships,
        project_ids=project_ids,
        entity_types=entity_types,
    )
    if not focused_ids:
        return HierarchicalGraphData(
            nodes=[],
            edges=[],
            clusters=[],
            cluster_edges=[],
            total_nodes=total_node_count,
            total_edges=total_edge_count,
            displayed_nodes=0,
            displayed_edges=0,
        )

    if not cluster_id:
        nodes, node_ids = _build_graph_nodes_from_snapshot(
            entities,
            relationships,
            node_to_cluster,
            max_nodes=max_nodes,
            project_ids=project_ids,
            entity_types=entity_types,
        )
        edges = _build_graph_edges_from_snapshot(
            relationships,
            node_ids,
            max_edges=max_edges,
        )
        enriched_clusters, cluster_edges = _build_cluster_metadata(
            nodes,
            clusters_meta,
            node_to_cluster,
            edges,
            entity_by_id,
            focused_ids,
        )
        return HierarchicalGraphData(
            nodes=nodes,
            edges=edges,
            clusters=enriched_clusters,
            cluster_edges=cluster_edges,
            total_nodes=total_node_count,
            total_edges=total_edge_count,
            displayed_nodes=len(nodes),
            displayed_edges=len(edges),
        )

    cluster_member_ids = {
        entity_id
        for entity_id in focused_ids
        if node_to_cluster.get(entity_id, "unclustered") == cluster_id
    }
    if not cluster_member_ids:
        return HierarchicalGraphData(
            nodes=[],
            edges=[],
            clusters=[],
            cluster_edges=[],
            total_nodes=total_node_count,
            total_edges=total_edge_count,
            displayed_nodes=0,
            displayed_edges=0,
        )

    degrees: Counter[str] = Counter()
    for relationship in relationships:
        if relationship.source_id not in focused_ids or relationship.target_id not in focused_ids:
            continue
        degrees[relationship.source_id] += 1
        if relationship.target_id != relationship.source_id:
            degrees[relationship.target_id] += 1

    selected_cluster_ids = set(
        _pick_representative_node_ids(
            cluster_member_ids,
            entity_by_id,
            degrees,
            max_nodes=min(len(cluster_member_ids), max_nodes),
        )
    )

    neighbor_ids: set[str] = set()
    for relationship in relationships:
        if relationship.source_id not in focused_ids or relationship.target_id not in focused_ids:
            continue
        src_in_cluster = relationship.source_id in cluster_member_ids
        tgt_in_cluster = relationship.target_id in cluster_member_ids
        if src_in_cluster and not tgt_in_cluster:
            neighbor_ids.add(relationship.target_id)
        elif tgt_in_cluster and not src_in_cluster:
            neighbor_ids.add(relationship.source_id)

    remaining_budget = max(max_nodes - len(selected_cluster_ids), 0)
    selected_neighbor_ids = set(
        _pick_representative_node_ids(
            neighbor_ids,
            entity_by_id,
            degrees,
            max_nodes=remaining_budget,
        )
    )
    visible_ids = selected_cluster_ids | selected_neighbor_ids

    nodes = [
        {
            "id": entity.id,
            "name": entity.name or entity.id[:20],
            "type": entity.entity_type.value,
            "summary": _entity_summary(entity),
            "cluster_id": node_to_cluster.get(entity.id, "unclustered"),
            "aggregate": False,
            "member_count": 1,
        }
        for entity_id in visible_ids
        if (entity := entity_by_id.get(entity_id)) is not None
    ]

    edges: list[dict[str, Any]] = []
    for relationship in relationships:
        if relationship.source_id not in visible_ids or relationship.target_id not in visible_ids:
            continue
        if (
            relationship.source_id not in selected_cluster_ids
            and relationship.target_id not in selected_cluster_ids
        ):
            continue
        edges.append(
            {
                "source": relationship.source_id,
                "target": relationship.target_id,
                "type": relationship.relationship_type.value,
            }
        )
        if len(edges) >= max_edges:
            break

    enriched_clusters, cluster_edges = _build_cluster_metadata(
        nodes,
        clusters_meta,
        node_to_cluster,
        edges,
        entity_by_id,
        focused_ids,
    )

    return HierarchicalGraphData(
        nodes=nodes,
        edges=edges,
        clusters=enriched_clusters,
        cluster_edges=cluster_edges,
        total_nodes=total_node_count,
        total_edges=total_edge_count,
        displayed_nodes=len(nodes),
        displayed_edges=len(edges),
    )


def _build_cluster_metadata(
    nodes: list[dict[str, Any]],
    clusters_meta: list[dict[str, Any]],
    node_to_cluster: dict[str, str],
    edges: list[dict[str, Any]],
    entity_by_id: dict[str, Entity],
    focused_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build enriched cluster metadata and inter-cluster edges."""
    focused_cluster_type_counts = _cluster_type_counts(focused_ids, entity_by_id, node_to_cluster)
    displayed_cluster_type_counts: dict[str, dict[str, int]] = {}
    node_cluster_lookup = {node["id"]: node["cluster_id"] for node in nodes}
    for node in nodes:
        cluster_id = node["cluster_id"]
        entity_type = node["type"]
        if cluster_id not in displayed_cluster_type_counts:
            displayed_cluster_type_counts[cluster_id] = {}
        displayed_cluster_type_counts[cluster_id][entity_type] = displayed_cluster_type_counts[
            cluster_id
        ].get(entity_type, 0) + int(node.get("member_count", 1))

    enriched_clusters = []
    for cluster in clusters_meta:
        cluster_id = cluster["id"]
        displayed_type_dist = displayed_cluster_type_counts.get(cluster_id, {})
        if not displayed_type_dist:
            continue
        total_type_dist = focused_cluster_type_counts.get(cluster_id, {})
        enriched_clusters.append(
            {
                **cluster,
                "type_distribution": total_type_dist,
                "displayed_type_distribution": displayed_type_dist,
                "dominant_type": _dominant_type(total_type_dist),
                "displayed_dominant_type": _dominant_type(displayed_type_dist),
                "member_count": sum(total_type_dist.values()),
                "displayed_member_count": sum(displayed_type_dist.values()),
            }
        )

    unclustered_total_types = focused_cluster_type_counts.get("unclustered", {})
    unclustered_displayed_types = displayed_cluster_type_counts.get("unclustered", {})
    if unclustered_displayed_types:
        enriched_clusters.append(
            {
                "id": "unclustered",
                "member_count": sum(unclustered_total_types.values()),
                "displayed_member_count": sum(unclustered_displayed_types.values()),
                "level": 0,
                "type_distribution": unclustered_total_types,
                "displayed_type_distribution": unclustered_displayed_types,
                "dominant_type": _dominant_type(unclustered_total_types),
                "displayed_dominant_type": _dominant_type(unclustered_displayed_types),
            }
        )

    # Calculate inter-cluster edges
    cluster_edge_counts: dict[tuple[str, str], int] = {}
    for edge in edges:
        src_cluster = node_cluster_lookup.get(
            edge["source"], node_to_cluster.get(edge["source"], "unclustered")
        )
        tgt_cluster = node_cluster_lookup.get(
            edge["target"], node_to_cluster.get(edge["target"], "unclustered")
        )
        if src_cluster != tgt_cluster:
            sorted_pair = sorted([src_cluster, tgt_cluster])
            pair: tuple[str, str] = (sorted_pair[0], sorted_pair[1])
            cluster_edge_counts[pair] = cluster_edge_counts.get(pair, 0) + int(
                edge.get("weight", 1)
            )

    cluster_edges = [
        {"source": p[0], "target": p[1], "weight": c}
        for p, c in cluster_edge_counts.items()
        if c > 0
    ]

    return enriched_clusters, cluster_edges
