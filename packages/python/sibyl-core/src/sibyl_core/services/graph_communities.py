"""Community detection using Leiden/Louvain algorithm.

Detects hierarchical communities in the knowledge graph for
GraphRAG-style retrieval and summarization.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType

log = structlog.get_logger()
type _ManagerFactory = Callable[[Any, str], Any]
_entity_manager_factory: _ManagerFactory | None = None
_relationship_manager_factory: _ManagerFactory | None = None

# =============================================================================
# Cluster Cache for Visualization
# =============================================================================

CLUSTER_CACHE: dict[str, tuple[datetime, list[ClusterSummary]]] = {}
CLUSTER_CACHE_TTL = timedelta(minutes=5)

# Cache for hierarchical graph community detection (expensive operation)
HIERARCHICAL_CACHE: dict[str, tuple[datetime, dict[str, str], list[dict]]] = {}
HIERARCHICAL_CACHE_TTL = timedelta(minutes=5)
GRAPH_SNAPSHOT_CACHE: dict[tuple[str, int | None, int | None], tuple[datetime, GraphSnapshot]] = {}
GRAPH_SNAPSHOT_CACHE_TTL = timedelta(minutes=5)
GRAPH_SNAPSHOT_LOADS: dict[tuple[str, int | None, int | None], asyncio.Task[GraphSnapshot]] = {}
GRAPH_LOD_CACHE: dict[tuple[Any, ...], tuple[datetime, HierarchicalGraphData]] = {}
GRAPH_LOD_CACHE_TTL = timedelta(minutes=2)
_COMMUNITY_PAGE_SIZE = 500
_GRAPH_DIVERSITY_THRESHOLD = 100
_GRAPH_PRIMARY_SAMPLE_SHARE = 0.8
_GRAPH_MISSING_TYPE_MIN_RESERVE = 5
GRAPH_RESOLUTION_OVERVIEW = "overview"
GRAPH_RESOLUTION_DETAIL = "detail"


def _entity_summary(entity: Entity) -> str:
    summary = entity.metadata.get("summary")
    if isinstance(summary, str) and summary:
        return summary
    return entity.description or ""


def _community_name(community: DetectedCommunity) -> str:
    return f"Community L{community.level} ({community.member_count} members)"


def _community_metadata(entity: Entity) -> dict[str, Any]:
    return entity.metadata if isinstance(entity.metadata, dict) else {}


def _community_level(entity: Entity) -> int:
    level = _community_metadata(entity).get("level")
    return level if isinstance(level, int) else 0


def _community_member_count(entity: Entity) -> int:
    member_count = _community_metadata(entity).get("member_count")
    return member_count if isinstance(member_count, int) else 0


def _build_community_entity(community: DetectedCommunity, *, created_at: datetime) -> Entity:
    summary = ""
    return Entity(
        id=community.id,
        entity_type=EntityType.COMMUNITY,
        name=_community_name(community),
        description=summary,
        content=summary,
        created_at=created_at,
        metadata={
            "member_ids": list(community.member_ids),
            "member_count": community.member_count,
            "level": community.level,
            "resolution": community.resolution,
            "modularity": community.modularity,
            "parent_community_id": community.parent_id,
            "child_community_ids": list(community.child_ids),
            "summary": summary,
        },
    )


async def _list_community_entities(
    entity_manager: Any,
) -> list[Entity]:
    communities: list[Entity] = []
    offset = 0

    while True:
        kwargs: dict[str, Any] = {
            "limit": _COMMUNITY_PAGE_SIZE,
            "offset": offset,
            "include_archived": True,
        }
        if getattr(entity_manager, "supports_lightweight_entity_list", False):
            kwargs["include_content"] = False
        batch = await entity_manager.list_by_type(EntityType.COMMUNITY, **kwargs)
        if not batch:
            break
        communities.extend(batch)
        if len(batch) < _COMMUNITY_PAGE_SIZE:
            break
        offset += _COMMUNITY_PAGE_SIZE

    return communities


def _attached_manager(client: Any, name: str) -> Any | None:
    try:
        client_state = vars(client)
    except TypeError:
        return None
    manager = client_state.get(name)
    return manager if manager is not None else None


def _entity_manager_for_client(client: Any, organization_id: str) -> Any:
    from sibyl_core.services.graph import EntityManager, SurrealGraphClient

    if _entity_manager_factory is not None:
        return _entity_manager_factory(client, organization_id)

    if isinstance(client, SurrealGraphClient):
        return EntityManager(client, group_id=organization_id)

    manager = _attached_manager(client, "entity_manager")
    if manager is not None:
        return manager

    raise RuntimeError(
        "Community graph operations require a native graph client or attached entity_manager"
    )


def _relationship_manager_for_client(client: Any, organization_id: str) -> Any:
    from sibyl_core.services.graph import RelationshipManager, SurrealGraphClient

    if _relationship_manager_factory is not None:
        return _relationship_manager_factory(client, organization_id)

    if isinstance(client, SurrealGraphClient):
        return RelationshipManager(client, group_id=organization_id)

    manager = _attached_manager(client, "relationship_manager")
    if manager is not None:
        return manager

    raise RuntimeError(
        "Community graph operations require a native graph client or attached relationship_manager"
    )


async def _list_all_entities(
    client: Any,
    organization_id: str,
    *,
    batch_size: int = 1000,
    max_items: int | None = None,
) -> list[Entity]:
    manager = _entity_manager_for_client(client, organization_id)
    entities: list[Entity] = []
    offset = 0

    while True:
        if max_items is not None and len(entities) >= max_items:
            break
        page_limit = batch_size
        if max_items is not None:
            page_limit = min(page_limit, max(max_items - len(entities), 0))
        if page_limit <= 0:
            break
        kwargs: dict[str, Any] = {
            "limit": page_limit,
            "offset": offset,
            "include_archived": True,
        }
        if getattr(manager, "supports_lightweight_entity_list", False):
            kwargs["include_content"] = False
        batch = await manager.list_all(**kwargs)
        if not batch:
            break
        entities.extend(batch)
        if len(batch) < page_limit:
            break
        offset += page_limit

    return entities


async def _list_all_relationships(
    client: Any,
    organization_id: str,
    *,
    batch_size: int = 1000,
    max_items: int | None = None,
    relationship_types: list[RelationshipType] | None = None,
) -> list[Relationship]:
    manager = _relationship_manager_for_client(client, organization_id)
    relationships: list[Relationship] = []
    offset = 0

    while True:
        if max_items is not None and len(relationships) >= max_items:
            break
        page_limit = batch_size
        if max_items is not None:
            page_limit = min(page_limit, max(max_items - len(relationships), 0))
        if page_limit <= 0:
            break
        batch = await manager.list_all(
            relationship_types=relationship_types,
            limit=page_limit,
            offset=offset,
        )
        if not batch:
            break
        relationships.extend(batch)
        if len(batch) < page_limit:
            break
        offset += page_limit

    return relationships


async def _get_graph_snapshot(
    client: Any,
    organization_id: str,
    *,
    max_entities: int | None = None,
    max_relationships: int | None = None,
) -> GraphSnapshot:
    cache_key = (organization_id, max_entities, max_relationships)
    cached = GRAPH_SNAPSHOT_CACHE.get(cache_key)
    if cached is not None:
        cached_at, snapshot = cached
        if datetime.now(UTC) - cached_at < GRAPH_SNAPSHOT_CACHE_TTL:
            log.debug(
                "graph_snapshot_cache_hit",
                org_id=organization_id,
                max_entities=max_entities,
                max_relationships=max_relationships,
            )
            return snapshot

    in_flight = GRAPH_SNAPSHOT_LOADS.get(cache_key)
    if in_flight is not None:
        log.debug(
            "graph_snapshot_load_joined",
            org_id=organization_id,
            max_entities=max_entities,
            max_relationships=max_relationships,
        )
        # Joiners shield the shared load: a cancelled joiner must not
        # cancel the task that the owner (and other joiners) still await.
        return await asyncio.shield(in_flight)

    task = asyncio.create_task(
        _load_graph_snapshot(
            client,
            organization_id,
            max_entities=max_entities,
            max_relationships=max_relationships,
        )
    )
    GRAPH_SNAPSHOT_LOADS[cache_key] = task
    try:
        # Owner awaits unshielded so request cancellation propagates to
        # the loader task and abandoned background scans are stopped.
        return await task
    finally:
        if GRAPH_SNAPSHOT_LOADS.get(cache_key) is task:
            GRAPH_SNAPSHOT_LOADS.pop(cache_key, None)


async def _load_graph_snapshot(
    client: Any,
    organization_id: str,
    *,
    max_entities: int | None,
    max_relationships: int | None,
) -> GraphSnapshot:
    entities, relationships = await asyncio.gather(
        _list_all_entities(
            client,
            organization_id,
            batch_size=max_entities or 1000,
            max_items=max_entities,
        ),
        _list_all_relationships(
            client,
            organization_id,
            batch_size=max_relationships or 1000,
            max_items=max_relationships,
        ),
    )
    entity_by_id = _entity_index(entities)
    snapshot = GraphSnapshot(
        entities=entities,
        relationships=relationships,
        entity_by_id=entity_by_id,
    )
    GRAPH_SNAPSHOT_CACHE[(organization_id, max_entities, max_relationships)] = (
        datetime.now(UTC),
        snapshot,
    )
    log.info(
        "graph_snapshot_cache_updated",
        org_id=organization_id,
        entity_count=len(entities),
        relationship_count=len(relationships),
        max_entities=max_entities,
        max_relationships=max_relationships,
    )
    return snapshot


def _entity_index(entities: list[Entity]) -> dict[str, Entity]:
    return {entity.id: entity for entity in entities if entity.id}


def _count_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float | str):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    return 0


async def _graph_totals(
    client: Any,
    organization_id: str,
) -> tuple[int, int] | None:
    from sibyl_core.services.graph import SurrealGraphClient, normalize_records

    if not isinstance(client, SurrealGraphClient):
        return None

    try:
        rows = normalize_records(
            await client.execute_query(
                """
                RETURN {
                    total_nodes: count(
                        SELECT VALUE uuid
                        FROM entity
                        WHERE group_id = $group_id
                    ),
                    total_edges: count(
                        SELECT VALUE uuid
                        FROM relates_to
                        WHERE group_id = $group_id
                    )
                };
                """,
                group_id=organization_id,
            )
        )
    except Exception as exc:
        log.warning("graph_totals_failed", org_id=organization_id, error=str(exc))
        return None

    if not rows:
        return None
    row = rows[0]
    return _count_int(row.get("total_nodes")), _count_int(row.get("total_edges"))


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
) -> tuple[Any, ...]:
    return (
        organization_id,
        _normalized_cache_list(project_ids),
        _normalized_cache_list(entity_types),
        resolution,
        cluster_id,
        max_nodes,
        max_edges,
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

    for entity in entities:
        if entity.id:
            G.add_node(entity.id, name=entity.name, type=entity.entity_type.value)

    for relationship in relationships:
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

    selected_ids = _pick_representative_node_ids(
        focused_ids,
        entity_by_id,
        degrees,
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
    edges: list[dict[str, Any]] = []

    for relationship in relationships:
        if relationship.source_id not in node_ids or relationship.target_id not in node_ids:
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

    return edges


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
    data = _build_cluster_detail_graph_from_snapshot(
        entities,
        relationships,
        node_to_cluster,
        clusters_meta,
        cluster_id=None,
        project_ids=project_ids,
        entity_types=entity_types,
        max_nodes=max_nodes,
        max_edges=max_edges,
    )
    data.resolution = GRAPH_RESOLUTION_OVERVIEW
    return data


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


@dataclass
class ClusterSummary:
    """Lightweight cluster summary for visualization.

    Attributes:
        id: Cluster identifier.
        member_count: Number of entities in cluster.
        dominant_type: Most common entity type.
        type_distribution: Entity type -> count mapping.
        member_ids: List of member entity IDs.
        level: Hierarchy level (0 = finest).
    """

    id: str
    member_count: int
    dominant_type: str
    type_distribution: dict[str, int]
    member_ids: list[str]
    level: int = 0


async def get_clusters_for_visualization(
    client: Any,
    organization_id: str,
    force_refresh: bool = False,
) -> list[ClusterSummary]:
    """Get clusters optimized for bubble visualization.

    Uses caching to avoid expensive community detection on every request.

    Args:
        client: Graph client.
        organization_id: Organization UUID.
        force_refresh: Bypass cache and recompute.

    Returns:
        List of ClusterSummary objects for visualization.
    """
    cache_key = organization_id

    # Check cache
    if not force_refresh and cache_key in CLUSTER_CACHE:
        cached_at, clusters = CLUSTER_CACHE[cache_key]
        if datetime.now(UTC) - cached_at < CLUSTER_CACHE_TTL:
            log.debug("cluster_cache_hit", org_id=organization_id, count=len(clusters))
            return clusters

    log.info("cluster_cache_miss", org_id=organization_id)

    # Run community detection
    try:
        detected = await detect_communities(
            client,
            organization_id,
            config=CommunityConfig(
                resolutions=[1.0],  # Single level for now
                min_community_size=2,
                max_levels=1,
                store_in_graph=False,  # Don't persist, just visualize
            ),
            algorithm="louvain",
        )
    except ImportError:
        # Fallback: Group by entity type if networkx not available
        log.warning("networkx_not_available", msg="falling back to type-based clustering")
        detected = []

    if not detected:
        # Fallback: Create pseudo-clusters by entity type
        clusters = await _create_type_based_clusters(client, organization_id)
    else:
        # Convert DetectedCommunity to ClusterSummary
        clusters = await _enrich_cluster_summaries(client, organization_id, detected)

    # Cache result
    CLUSTER_CACHE[cache_key] = (datetime.now(UTC), clusters)
    log.info("cluster_cache_updated", org_id=organization_id, count=len(clusters))

    return clusters


async def _create_type_based_clusters(
    client: Any,
    organization_id: str,
) -> list[ClusterSummary]:
    """Create clusters based on entity type (fallback when no networkx)."""
    try:
        entities = await _list_all_entities(client, organization_id)
        grouped_ids: dict[str, list[str]] = {}
        for entity in entities:
            if not entity.id:
                continue
            grouped_ids.setdefault(entity.entity_type.value, []).append(entity.id)

        clusters = []
        for i, (entity_type, member_ids) in enumerate(sorted(grouped_ids.items())):
            if not member_ids:
                continue

            clusters.append(
                ClusterSummary(
                    id=f"type_{entity_type}_{i}",
                    member_count=len(member_ids),
                    dominant_type=entity_type or "unknown",
                    type_distribution={entity_type or "unknown": len(member_ids)},
                    member_ids=member_ids,
                    level=0,
                )
            )

        return clusters

    except Exception as e:
        log.warning("type_based_clusters_failed", error=str(e))
        return []


async def _enrich_cluster_summaries(
    client: Any,
    organization_id: str,
    detected: list[DetectedCommunity],
) -> list[ClusterSummary]:
    """Convert DetectedCommunity to ClusterSummary with type distribution."""
    entity_by_id = _entity_index(await _list_all_entities(client, organization_id))
    summaries = []

    for community in detected:
        if not community.member_ids:
            continue

        type_dist: dict[str, int] = {}
        for member_id in community.member_ids:
            entity = entity_by_id.get(member_id)
            entity_type = entity.entity_type.value if entity is not None else "unknown"
            type_dist[entity_type] = type_dist.get(entity_type, 0) + 1

        # Find dominant type
        dominant = max(type_dist.items(), key=lambda x: x[1])[0] if type_dist else "unknown"

        summaries.append(
            ClusterSummary(
                id=community.id,
                member_count=community.member_count,
                dominant_type=dominant,
                type_distribution=type_dist,
                member_ids=community.member_ids,
                level=community.level,
            )
        )

    return summaries


async def get_cluster_nodes(
    client: Any,
    organization_id: str,
    cluster_id: str,
) -> dict[str, Any]:
    """Get nodes and edges for a specific cluster.

    Args:
        client: Graph client.
        organization_id: Organization UUID.
        cluster_id: Cluster ID to drill into.

    Returns:
        Dict with 'nodes' and 'edges' for the cluster.
    """
    # Get cluster from cache
    clusters = await get_clusters_for_visualization(client, organization_id)
    cluster = next((c for c in clusters if c.id == cluster_id), None)

    if not cluster:
        return {"nodes": [], "edges": [], "error": "Cluster not found"}

    member_ids = cluster.member_ids
    member_id_set = set(member_ids)
    entity_by_id = _entity_index(await _list_all_entities(client, organization_id))
    relationships = await _list_all_relationships(client, organization_id)

    nodes = [
        {
            "id": member_id,
            "name": entity.name or member_id[:20],
            "type": entity.entity_type.value,
            "summary": _entity_summary(entity),
        }
        for member_id in member_ids
        if (entity := entity_by_id.get(member_id)) is not None
    ]

    edges = [
        {
            "source": relationship.source_id,
            "target": relationship.target_id,
            "type": relationship.relationship_type.value,
        }
        for relationship in relationships
        if relationship.source_id in member_id_set and relationship.target_id in member_id_set
    ]

    return {
        "nodes": nodes,
        "edges": edges,
        "cluster_id": cluster_id,
        "member_count": len(nodes),
    }


# =============================================================================
# Hierarchical Graph Data for Rich Visualization
# =============================================================================


@dataclass
class HierarchicalGraphData:
    """Graph data with cluster assignments for rich visualization.

    This structure enables frontend to:
    - Render all nodes with edges (real graph structure)
    - Color nodes by cluster membership
    - Show cluster summary overlays
    - Enable cluster-based filtering
    """

    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    clusters: list[dict[str, Any]]
    cluster_edges: list[dict[str, Any]]  # Aggregated edges between clusters
    total_nodes: int  # REAL total in graph (not limited)
    total_edges: int  # REAL total in graph (not limited)
    displayed_nodes: int  # How many we're sending to UI
    displayed_edges: int  # How many we're sending to UI
    resolution: str = GRAPH_RESOLUTION_DETAIL


@dataclass
class GraphSnapshot:
    """Cached graph snapshot for fast LOD rendering."""

    entities: list[Entity]
    relationships: list[Relationship]
    entity_by_id: dict[str, Entity]


async def _get_graph_totals(
    client: Any,
    organization_id: str,
    project_ids: list[str] | None = None,
    entity_types: list[str] | None = None,
    include_neighbors: bool = True,
) -> tuple[int, int]:
    """Get total node and edge counts (no LIMIT) for stats display.

    Args:
        client: Graph client.
        organization_id: Organization UUID.
        project_ids: Optional list of project IDs to filter by.
        include_neighbors: If True, include 1-hop neighbors of project entities.

    Returns:
        Tuple of (total_nodes, total_edges) matching the filter criteria.
    """
    # NOTE: include_neighbors is intentionally ignored for totals.
    # Totals reflect the focused subset selected by project filters.
    try:
        entities = await _list_all_entities(client, organization_id)
        relationships = await _list_all_relationships(client, organization_id)
        return _graph_totals_from_snapshot(
            entities,
            relationships,
            project_ids=project_ids,
            entity_types=entity_types,
        )
    except Exception as e:
        log.warning("count_graph_totals_failed", error=str(e))
        return 0, 0


async def _fetch_graph_nodes(
    client: Any,
    organization_id: str,
    node_to_cluster: dict[str, str],
    max_nodes: int,
    project_ids: list[str] | None = None,
    entity_types: list[str] | None = None,
) -> tuple[list[dict[str, Any]], set[str]]:
    """Fetch nodes with cluster assignments, optionally filtered by project/type."""
    try:
        entities = await _list_all_entities(client, organization_id)
        relationships = await _list_all_relationships(client, organization_id)
        return _build_graph_nodes_from_snapshot(
            entities,
            relationships,
            node_to_cluster,
            max_nodes=max_nodes,
            project_ids=project_ids,
            entity_types=entity_types,
        )
    except Exception as e:
        log.warning("fetch_nodes_failed", error=str(e))
        return [], set()


async def _fetch_graph_edges(
    client: Any,
    organization_id: str,
    node_ids: set[str],
    max_edges: int,
) -> list[dict[str, Any]]:
    """Fetch edges between nodes in our set."""
    if not node_ids:
        return []

    try:
        relationships = await _list_all_relationships(client, organization_id)
        return _build_graph_edges_from_snapshot(
            relationships,
            node_ids,
            max_edges=max_edges,
        )
    except Exception as e:
        log.warning("fetch_edges_failed", error=str(e))
        return []


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


def _detect_communities_from_graph(
    G: Any,
    *,
    config: CommunityConfig,
    algorithm: str,
) -> list[DetectedCommunity]:
    if G.number_of_nodes() < config.min_community_size:
        log.info("detect_communities_too_few_nodes", nodes=G.number_of_nodes())
        return []

    detect_fn = detect_communities_leiden if algorithm == "leiden" else detect_communities_louvain
    all_level_communities: list[list[DetectedCommunity]] = []

    for level, resolution in enumerate(config.resolutions[: config.max_levels]):
        try:
            partition, modularity = detect_fn(G, resolution=resolution)

            communities = partition_to_communities(
                partition=partition,
                level=level,
                resolution=resolution,
                modularity=modularity,
                min_size=config.min_community_size,
            )

            all_level_communities.append(communities)

            log.debug(
                "detect_communities_level_complete",
                level=level,
                resolution=resolution,
                communities=len(communities),
                modularity=modularity,
            )

        except ImportError as e:
            log.exception("detect_communities_missing_dependency", error=str(e))
            raise
        except Exception as e:
            log.warning("detect_communities_level_failed", level=level, error=str(e))
            continue

    all_communities = link_hierarchy(all_level_communities)

    log.info(
        "detect_communities_complete",
        total_communities=len(all_communities),
        levels=len(all_level_communities),
    )

    return all_communities


async def get_hierarchical_graph(
    client: Any,
    organization_id: str,
    project_ids: list[str] | None = None,
    entity_types: list[str] | None = None,
    max_nodes: int = 1000,
    max_edges: int = 5000,
    resolution: str = GRAPH_RESOLUTION_DETAIL,
    cluster_id: str | None = None,
) -> HierarchicalGraphData:
    """Get graph data with cluster assignments for rich visualization.

    Returns actual nodes and edges (not aggregated bubbles) with each node
    assigned to a cluster based on Louvain community detection.

    Args:
        client: Graph client.
        organization_id: Organization UUID.
        project_ids: Optional list of project IDs to filter by.
        entity_types: Optional list of entity types to filter by.
        max_nodes: Maximum nodes to return (will sample if exceeded).
        max_edges: Maximum edges to return.

    Returns:
        HierarchicalGraphData with nodes, edges, and cluster metadata.
    """
    log.info(
        "get_hierarchical_graph_start",
        org_id=organization_id,
        max_nodes=max_nodes,
        projects=project_ids,
        types=entity_types,
        resolution=resolution,
        cluster_id=cluster_id,
    )

    cache_key = _lod_cache_key(
        organization_id=organization_id,
        project_ids=project_ids,
        entity_types=entity_types,
        resolution=resolution,
        cluster_id=cluster_id,
        max_nodes=max_nodes,
        max_edges=max_edges,
    )
    cached_lod = GRAPH_LOD_CACHE.get(cache_key)
    if cached_lod is not None:
        cached_at, data = cached_lod
        if datetime.now(UTC) - cached_at < GRAPH_LOD_CACHE_TTL:
            log.info(
                "graph_lod_cache_hit",
                org_id=organization_id,
                resolution=resolution,
                cluster_id=cluster_id,
            )
            return data

    snapshot = await _get_graph_snapshot(
        client,
        organization_id,
        max_entities=max_nodes,
        max_relationships=max_edges,
    )
    entities = snapshot.entities
    relationships = snapshot.relationships
    totals = None
    if not project_ids and not entity_types:
        totals = await _graph_totals(client, organization_id)
    if totals is None:
        total_node_count, total_edge_count = _graph_totals_from_snapshot(
            entities,
            relationships,
            project_ids=project_ids,
            entity_types=entity_types,
        )
    else:
        total_node_count, total_edge_count = totals
    log.info(
        "graph_totals_queried",
        total_nodes=total_node_count,
        total_edges=total_edge_count,
        filtered_by_projects=bool(project_ids),
    )

    # Check cache for community detection (expensive operation)
    # Cache key includes org only - community structure is org-wide
    community_cache_key = organization_id
    node_to_cluster: dict[str, str] = {}
    clusters_meta: list[dict[str, Any]] = []

    if community_cache_key in HIERARCHICAL_CACHE:
        cached_at, cached_clusters, cached_meta = HIERARCHICAL_CACHE[community_cache_key]
        if datetime.now(UTC) - cached_at < HIERARCHICAL_CACHE_TTL:
            log.info("hierarchical_cache_hit", org_id=organization_id)
            node_to_cluster = cached_clusters
            clusters_meta = cached_meta
        else:
            log.debug("hierarchical_cache_expired", org_id=organization_id)

    # Run community detection if not cached
    if not node_to_cluster:
        try:
            detected = _detect_communities_from_graph(
                _snapshot_to_networkx(entities, relationships),
                config=CommunityConfig(
                    resolutions=[1.0], min_community_size=2, max_levels=1, store_in_graph=False
                ),
                algorithm="louvain",
            )
            if detected:
                for community in detected:
                    for member_id in community.member_ids:
                        node_to_cluster[member_id] = community.id
                clusters_meta = [
                    {"id": c.id, "member_count": c.member_count, "level": c.level} for c in detected
                ]
                log.info(
                    "community_detection_success",
                    clusters=len(detected),
                    assigned_nodes=len(node_to_cluster),
                )
                # Cache the result
                HIERARCHICAL_CACHE[community_cache_key] = (
                    datetime.now(UTC),
                    node_to_cluster,
                    clusters_meta,
                )
            else:
                log.warning("community_detection_empty", msg="no communities detected")
        except ImportError:
            log.warning("networkx_not_available", msg="community detection unavailable")
        except Exception as e:
            log.warning("community_detection_failed", error=str(e))

    if resolution == GRAPH_RESOLUTION_OVERVIEW:
        data = _build_overview_graph_from_snapshot(
            entities,
            relationships,
            node_to_cluster,
            clusters_meta,
            project_ids=project_ids,
            entity_types=entity_types,
            max_nodes=max_nodes,
            max_edges=max_edges,
        )
    else:
        data = _build_cluster_detail_graph_from_snapshot(
            entities,
            relationships,
            node_to_cluster,
            clusters_meta,
            cluster_id=cluster_id,
            project_ids=project_ids,
            entity_types=entity_types,
            max_nodes=max_nodes,
            max_edges=max_edges,
        )

    if (project_ids or entity_types) and total_node_count == 0 and data.displayed_nodes > 0:
        total_node_count = data.displayed_nodes
    if (project_ids or entity_types) and total_edge_count == 0 and data.displayed_edges > 0:
        total_edge_count = data.displayed_edges

    data.total_nodes = total_node_count
    data.total_edges = total_edge_count

    log.info(
        "get_hierarchical_graph_complete",
        total_nodes=data.total_nodes,
        total_edges=data.total_edges,
        displayed_nodes=data.displayed_nodes,
        displayed_edges=data.displayed_edges,
        clusters=len(data.clusters),
        resolution=data.resolution,
        cluster_id=cluster_id,
    )

    GRAPH_LOD_CACHE[cache_key] = (datetime.now(UTC), data)
    return data


@dataclass
class CommunityConfig:
    """Configuration for community detection.

    Attributes:
        resolutions: Resolution parameters for hierarchical levels.
                    Higher resolution = more smaller communities.
        min_community_size: Minimum members to form a community.
        max_levels: Maximum hierarchy levels to compute.
        store_in_graph: Whether to persist communities to graph.
    """

    resolutions: list[float] = field(default_factory=lambda: [0.5, 1.0, 2.0])
    min_community_size: int = 2
    max_levels: int = 3
    store_in_graph: bool = True


@dataclass
class DetectedCommunity:
    """A detected community before being stored.

    Attributes:
        id: Unique community identifier.
        member_ids: Entity IDs in this community.
        level: Hierarchy level (0 = leaf, higher = broader).
        resolution: Resolution parameter used for detection.
        modularity: Modularity score.
    """

    id: str
    member_ids: list[str]
    level: int
    resolution: float
    modularity: float = 0.0
    parent_id: str | None = None
    child_ids: list[str] = field(default_factory=list)

    @property
    def member_count(self) -> int:
        """Number of members in this community."""
        return len(self.member_ids)


async def export_to_networkx(
    client: Any,
    organization_id: str,
    type_affinity_weight: float = 2.0,
) -> Any:
    """Export knowledge graph to NetworkX format with type affinity.

    Edges between nodes of the same entity type get higher weight,
    encouraging the Louvain algorithm to cluster same-type nodes together.

    Args:
        client: Graph client.
        organization_id: Organization UUID for filtering.
        type_affinity_weight: Extra weight for same-type connections (default 2.0).

    Returns:
        NetworkX graph object.

    Raises:
        ImportError: If networkx is not installed.
    """
    log.info("export_to_networkx_start", org_id=organization_id, type_affinity=type_affinity_weight)

    try:
        entities = await _list_all_entities(client, organization_id)
    except Exception as e:
        log.warning("export_nodes_failed", error=str(e))
        entities = []

    try:
        relationships = await _list_all_relationships(client, organization_id)
    except Exception as e:
        log.warning("export_edges_failed", error=str(e))
        relationships = []

    G = _snapshot_to_networkx(
        entities,
        relationships,
        type_affinity_weight=type_affinity_weight,
    )

    log.info(
        "export_to_networkx_complete",
        org_id=organization_id,
        nodes=G.number_of_nodes(),
        edges=G.number_of_edges(),
    )

    return G


def detect_communities_louvain(
    G: Any,
    resolution: float = 1.0,
) -> tuple[dict[str, int], float]:
    """Detect communities using Louvain algorithm.

    Args:
        G: NetworkX graph.
        resolution: Resolution parameter (higher = more communities).

    Returns:
        Tuple of (node_id -> community_id mapping, modularity score).

    Raises:
        ImportError: If python-louvain is not installed.
    """
    try:
        import community as community_louvain
    except ImportError as e:
        raise ImportError(
            "python-louvain is required for community detection. "
            "Install with: pip install python-louvain"
        ) from e

    if G.number_of_nodes() == 0:
        return {}, 0.0

    # Run Louvain algorithm
    partition = community_louvain.best_partition(G, resolution=resolution)
    modularity = community_louvain.modularity(partition, G)

    return partition, modularity


def detect_communities_leiden(
    G: Any,
    resolution: float = 1.0,
) -> tuple[dict[str, int], float]:
    """Detect communities using Leiden algorithm.

    Args:
        G: NetworkX graph.
        resolution: Resolution parameter (higher = more communities).

    Returns:
        Tuple of (node_id -> community_id mapping, modularity score).

    Raises:
        ImportError: If leidenalg/igraph is not installed.
    """
    try:
        import igraph as ig
        import leidenalg
    except ImportError as e:
        raise ImportError(
            "leidenalg and igraph are required for Leiden algorithm. "
            "Install with: pip install leidenalg igraph"
        ) from e

    if G.number_of_nodes() == 0:
        return {}, 0.0

    # Convert NetworkX to igraph
    G_ig = ig.Graph.from_networkx(G)

    # Run Leiden algorithm
    partition = leidenalg.find_partition(
        G_ig,
        leidenalg.CPMVertexPartition,
        resolution_parameter=resolution,
    )

    # Map back to node IDs
    node_ids = list(G.nodes())
    partition_dict = {node_ids[i]: partition.membership[i] for i in range(len(node_ids))}

    # Calculate modularity
    modularity = partition.quality() / (2 * G.number_of_edges()) if G.number_of_edges() > 0 else 0.0

    return partition_dict, modularity


def partition_to_communities(
    partition: dict[str, int],
    level: int,
    resolution: float,
    modularity: float,
    min_size: int = 2,
) -> list[DetectedCommunity]:
    """Convert partition dict to list of communities.

    Args:
        partition: Node ID -> community number mapping.
        level: Hierarchy level.
        resolution: Resolution used for detection.
        modularity: Overall modularity score.
        min_size: Minimum community size.

    Returns:
        List of DetectedCommunity objects.
    """
    # Group nodes by community
    community_members: dict[int, list[str]] = {}
    for node_id, comm_id in partition.items():
        if comm_id not in community_members:
            community_members[comm_id] = []
        community_members[comm_id].append(node_id)

    # Create community objects
    communities: list[DetectedCommunity] = []
    for comm_num, members in community_members.items():
        if len(members) < min_size:
            continue

        community = DetectedCommunity(
            id=f"comm_L{level}_{comm_num}_{uuid.uuid4().hex[:8]}",
            member_ids=sorted(members),
            level=level,
            resolution=resolution,
            modularity=modularity,
        )
        communities.append(community)

    return communities


def link_hierarchy(
    all_communities: list[list[DetectedCommunity]],
) -> list[DetectedCommunity]:
    """Link communities across hierarchy levels.

    Lower-level communities that are subsets of higher-level
    communities become children.

    Args:
        all_communities: List of community lists by level.

    Returns:
        Flattened list with parent/child links set.
    """
    if not all_communities:
        return []

    flat: list[DetectedCommunity] = []

    for level_idx, level_communities in enumerate(all_communities):
        for community in level_communities:
            # Find parent at next level
            if level_idx < len(all_communities) - 1:
                parent_level = all_communities[level_idx + 1]
                member_set = set(community.member_ids)

                for parent in parent_level:
                    parent_set = set(parent.member_ids)
                    # Check if this community is a subset of parent
                    if member_set <= parent_set:
                        community.parent_id = parent.id
                        parent.child_ids.append(community.id)
                        break

            flat.append(community)

    return flat


async def detect_communities(
    client: Any,
    organization_id: str,
    config: CommunityConfig | None = None,
    algorithm: str = "louvain",
) -> list[DetectedCommunity]:
    """Detect hierarchical communities in the knowledge graph.

    Args:
        client: Graph client.
        config: Detection configuration.
        algorithm: "louvain" or "leiden".

    Returns:
        List of detected communities with hierarchy links.
    """
    if config is None:
        config = CommunityConfig()

    log.info(
        "detect_communities_start",
        algorithm=algorithm,
        resolutions=config.resolutions,
        max_levels=config.max_levels,
    )

    # Export graph to NetworkX
    G = await export_to_networkx(client, organization_id)
    return _detect_communities_from_graph(G, config=config, algorithm=algorithm)


async def store_communities(
    client: Any,
    organization_id: str,
    communities: list[DetectedCommunity],
    clear_existing: bool = True,
) -> int:
    """Store detected communities in the graph.

    Args:
        client: Graph client.
        communities: Communities to store.
        clear_existing: Whether to clear existing communities first.

    Returns:
        Number of communities stored.
    """
    if not communities:
        return 0

    log.info("store_communities_start", count=len(communities), clear_existing=clear_existing)
    entity_manager = _entity_manager_for_client(client, organization_id)
    relationship_manager = _relationship_manager_for_client(client, organization_id)

    # Clear existing communities if requested
    if clear_existing:
        try:
            for community in await _list_community_entities(entity_manager):
                with contextlib.suppress(Exception):
                    await entity_manager.delete(community.id)
        except Exception as e:
            log.warning("clear_communities_failed", error=str(e))

    # Store each community
    stored = 0
    now = datetime.now(UTC)

    for community in communities:
        try:
            await entity_manager.create(_build_community_entity(community, created_at=now))
            stored += 1
        except Exception as e:
            log.warning("store_community_failed", community_id=community.id, error=str(e))

    # Create BELONGS_TO relationships from members to communities
    membership_edges = [
        Relationship(
            id=str(uuid.uuid4()),
            source_id=member_id,
            target_id=community.id,
            relationship_type=RelationshipType.BELONGS_TO,
        )
        for community in communities
        for member_id in community.member_ids
    ]
    if membership_edges:
        with contextlib.suppress(Exception):
            await relationship_manager.create_bulk(membership_edges)

    log.info("store_communities_complete", stored=stored)
    return stored


async def get_entity_communities(
    client: Any,
    organization_id: str,
    entity_id: str,
) -> list[dict[str, Any]]:
    """Get communities that an entity belongs to.

    Args:
        client: Graph client.
        entity_id: Entity UUID.

    Returns:
        List of community info dicts.
    """
    communities: list[dict[str, Any]] = []
    entity_manager = _entity_manager_for_client(client, organization_id)
    relationship_manager = _relationship_manager_for_client(client, organization_id)

    try:
        relationships = await relationship_manager.get_for_entity(
            entity_id,
            [RelationshipType.BELONGS_TO],
            direction="outgoing",
        )

        for relationship in relationships:
            with contextlib.suppress(Exception):
                community = await entity_manager.get(relationship.target_id)
                if community.entity_type != EntityType.COMMUNITY:
                    continue
                communities.append(
                    {
                        "id": community.id,
                        "name": community.name,
                        "level": _community_level(community),
                        "member_count": _community_member_count(community),
                        "summary": _entity_summary(community),
                    }
                )

    except Exception as e:
        log.warning("get_entity_communities_failed", entity_id=entity_id, error=str(e))

    communities.sort(key=lambda community: community["level"])
    return communities


async def get_community_members(
    client: Any,
    organization_id: str,
    community_id: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Get members of a community.

    Args:
        client: Graph client.
        community_id: Community UUID.
        limit: Maximum members to return.

    Returns:
        List of member entity info.
    """
    members: list[dict[str, Any]] = []
    entity_manager = _entity_manager_for_client(client, organization_id)
    relationship_manager = _relationship_manager_for_client(client, organization_id)

    try:
        relationships = await relationship_manager.get_for_entity(
            community_id,
            [RelationshipType.BELONGS_TO],
            direction="incoming",
        )

        for relationship in relationships[:limit]:
            with contextlib.suppress(Exception):
                member = await entity_manager.get(relationship.source_id)
                members.append(
                    {
                        "id": member.id,
                        "name": member.name,
                        "type": member.entity_type.value,
                        "description": member.description,
                    }
                )

    except Exception as e:
        log.warning("get_community_members_failed", community_id=community_id, error=str(e))

    return members
