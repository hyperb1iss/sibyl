"""Graph visualization data endpoints."""

from collections.abc import Sequence

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query

from sibyl.api.dependencies import get_knowledge_read_service
from sibyl.api.schemas import GraphData, GraphEdge, GraphNode, SubgraphRequest
from sibyl.auth.dependencies import get_current_organization, require_org_role
from sibyl.db.models import Organization, OrganizationRole
from sibyl.persistence.graph_runtime import (
    get_entity_graph_runtime as _service_get_entity_graph_runtime,
    get_graph_query_adapter as _service_get_graph_query_adapter,
)
from sibyl_core.errors import EntityNotFoundError
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services import KnowledgeReadService

log = structlog.get_logger()
_READ_ROLES = (
    OrganizationRole.OWNER,
    OrganizationRole.ADMIN,
    OrganizationRole.MEMBER,
    OrganizationRole.VIEWER,
)
_ADMIN_ROLES = (
    OrganizationRole.OWNER,
    OrganizationRole.ADMIN,
)


async def get_entity_graph_runtime(group_id: str):
    return await _service_get_entity_graph_runtime(group_id)


async def get_graph_query_adapter(group_id: str):
    return await _service_get_graph_query_adapter(group_id)


async def get_clusters_for_visualization(
    _client: object,
    group_id: str,
    *,
    force_refresh: bool = False,
):
    adapter = await get_graph_query_adapter(group_id)
    return await adapter.get_clusters_for_visualization(force_refresh=force_refresh)


async def get_cluster_nodes(
    _client: object,
    group_id: str,
    cluster_id: str,
):
    adapter = await get_graph_query_adapter(group_id)
    return await adapter.get_cluster_nodes(cluster_id)


async def get_hierarchical_graph(
    _client: object,
    group_id: str,
    *,
    project_ids: list[str] | None = None,
    entity_types: list[str] | None = None,
    max_nodes: int = 1000,
    max_edges: int = 5000,
    resolution: str = "detail",
    cluster_id: str | None = None,
):
    adapter = await get_graph_query_adapter(group_id)
    return await adapter.get_hierarchical_graph(
        project_ids=project_ids,
        entity_types=entity_types,
        max_nodes=max_nodes,
        max_edges=max_edges,
        resolution=resolution,
        cluster_id=cluster_id,
    )


router = APIRouter(
    prefix="/graph",
    tags=["graph"],
    dependencies=[Depends(require_org_role(*_READ_ROLES))],
)


@router.get("/debug", dependencies=[Depends(require_org_role(*_ADMIN_ROLES))])
async def debug_graph(org: Organization = Depends(get_current_organization)):
    """Debug endpoint to trace graph data issue."""
    group_id = str(org.id)
    runtime = await get_entity_graph_runtime(group_id)

    nodes = await _list_graph_entities(
        runtime.entity_manager,
        limit=500,
        include_archived=True,
    )
    node_ids = {entity.id for entity in nodes if entity.id}
    relationships = await runtime.relationship_manager.list_all(limit=1000)

    matching = sum(
        1
        for relationship in relationships
        if relationship.source_id in node_ids and relationship.target_id in node_ids
    )

    sample_edges = relationships[:3] if relationships else []
    sample_nodes = list(node_ids)[:5]

    return {
        "node_count": len(node_ids),
        "edge_count": len(relationships),
        "matching_edges": matching,
        "sample_nodes": sample_nodes,
        "sample_edges": [{"src": e.source_id, "tgt": e.target_id} for e in sample_edges],
        "first_edge_src_in_nodes": sample_edges[0].source_id in node_ids if sample_edges else None,
        "first_edge_tgt_in_nodes": sample_edges[0].target_id in node_ids if sample_edges else None,
    }


# SilkCircuit color palette for entity types
ENTITY_COLORS: dict[EntityType, str] = {
    EntityType.PATTERN: "#e135ff",  # Electric Purple
    EntityType.RULE: "#ff6363",  # Error Red
    EntityType.TEMPLATE: "#80ffea",  # Neon Cyan
    EntityType.TOOL: "#f1fa8c",  # Electric Yellow
    EntityType.LANGUAGE: "#ff6ac1",  # Coral
    EntityType.TOPIC: "#ff00ff",  # Pure Magenta
    EntityType.EPISODE: "#50fa7b",  # Success Green
    EntityType.KNOWLEDGE_SOURCE: "#8b85a0",  # Muted
    EntityType.CONFIG_FILE: "#f1fa8c",  # Electric Yellow
    EntityType.SLASH_COMMAND: "#80ffea",  # Neon Cyan
    EntityType.TASK: "#ff9580",  # Warm orange
    EntityType.PROJECT: "#bd93f9",  # Soft purple
    EntityType.DOCUMENT: "#8be9fd",  # Light cyan - docs stand out
    EntityType.COMMUNITY: "#ffb86c",  # Orange for clusters
}

DEFAULT_COLOR = "#8b85a0"  # Muted for unknown types


def get_entity_color(entity_type: EntityType) -> str:
    """Get the SilkCircuit color for an entity type."""
    return ENTITY_COLORS.get(entity_type, DEFAULT_COLOR)


async def _list_graph_entities(
    entity_manager: object,
    *,
    entity_types: list[EntityType] | None = None,
    limit: int = 100,
    offset: int = 0,
    include_archived: bool = False,
) -> list[Entity]:
    allowed_types = set(entity_types or [])
    remaining_offset = max(offset, 0)
    page_offset = 0
    page_size = max(200, min(max(limit, 1) * 2, 1000))
    entities: list[Entity] = []

    while len(entities) < limit:
        batch = await entity_manager.list_all(
            limit=page_size,
            offset=page_offset,
            include_archived=include_archived,
        )
        if not batch:
            break

        page_offset += len(batch)
        for entity in batch:
            if allowed_types and entity.entity_type not in allowed_types:
                continue
            if remaining_offset:
                remaining_offset -= 1
                continue
            entities.append(entity)
            if len(entities) >= limit:
                break
        if len(batch) < page_size:
            break

    return entities


async def _get_graph_entity(entity_manager: object, entity_id: str) -> Entity | None:
    try:
        return await entity_manager.get(entity_id)
    except EntityNotFoundError:
        return None


async def _list_graph_relationships_for_entities(
    relationship_manager: object,
    entity_ids: Sequence[str],
    *,
    relationship_types: list[RelationshipType] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Relationship]:
    scoped_entity_ids = {entity_id for entity_id in entity_ids if entity_id}
    if not scoped_entity_ids:
        return []

    relationships: list[Relationship] = []
    remaining_offset = max(offset, 0)
    page_offset = 0
    page_size = max(200, min(max(limit, 1) * 2, 1000))

    while len(relationships) < limit:
        batch = await relationship_manager.list_all(
            relationship_types=relationship_types,
            limit=page_size,
            offset=page_offset,
        )
        if not batch:
            break

        page_offset += len(batch)
        for relationship in batch:
            if (
                relationship.source_id not in scoped_entity_ids
                or relationship.target_id not in scoped_entity_ids
            ):
                continue
            if remaining_offset:
                remaining_offset -= 1
                continue
            relationships.append(relationship)
            if len(relationships) >= limit:
                break
        if len(batch) < page_size:
            break

    return relationships


async def _get_connection_counts(
    relationship_manager: object,
    entity_ids: Sequence[str],
    *,
    relationship_types: list[RelationshipType] | None = None,
) -> dict[str, int]:
    scoped_entity_ids = {entity_id for entity_id in entity_ids if entity_id}
    if not scoped_entity_ids:
        return {}

    counts = dict.fromkeys(scoped_entity_ids, 0)
    page_offset = 0
    page_size = 1000

    while True:
        batch = await relationship_manager.list_all(
            relationship_types=relationship_types,
            limit=page_size,
            offset=page_offset,
        )
        if not batch:
            break

        page_offset += len(batch)
        for relationship in batch:
            if relationship.source_id in counts:
                counts[relationship.source_id] += 1
            if (
                relationship.target_id in counts
                and relationship.target_id != relationship.source_id
            ):
                counts[relationship.target_id] += 1

    return counts


@router.get("/nodes", response_model=list[GraphNode])
async def get_all_nodes(
    org: Organization = Depends(get_current_organization),
    types: list[EntityType] | None = Query(default=None, description="Filter by entity types"),
    limit: int = Query(default=500, ge=1, le=2000, description="Maximum nodes"),
    offset: int = Query(default=0, ge=0, description="Offset for pagination"),
) -> list[GraphNode]:
    """Get all nodes for graph visualization.

    Queries the graph directly to get actual node UUIDs that match edge references.
    """
    try:
        group_id = str(org.id)
        runtime = await get_entity_graph_runtime(group_id)
        entities = await _list_graph_entities(
            runtime.entity_manager,
            entity_types=types,
            limit=limit,
            offset=offset,
            include_archived=True,
        )

        adapter = await get_graph_query_adapter(group_id)
        connection_counts = await adapter.get_connection_counts([entity.id for entity in entities])

        max_connections = max(connection_counts.values()) if connection_counts else 1
        max_connections = max(max_connections, 1)

        nodes = []
        for entity in entities:
            node_id = entity.id
            if not node_id:
                continue
            entity_type = entity.entity_type

            conn_count = connection_counts.get(node_id, 0)
            size = 1.0 + (conn_count / max_connections) * 2.0

            nodes.append(
                GraphNode(
                    id=node_id,
                    type=entity_type.value,
                    label=(entity.name or node_id[:20])[:50],
                    color=get_entity_color(entity_type),
                    size=size,
                    metadata={
                        "description": entity.description[:100],
                        "connections": conn_count,
                    },
                )
            )

        return nodes

    except Exception as e:
        log.exception("get_nodes_failed", error=str(e))
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve graph nodes. Please try again.",
        ) from e


@router.get("/edges", response_model=list[GraphEdge])
async def get_all_edges(
    org: Organization = Depends(get_current_organization),
    relationship_types: list[RelationshipType] | None = Query(
        default=None, description="Filter by relationship types"
    ),
    limit: int = Query(default=1000, ge=1, le=5000, description="Maximum edges"),
    offset: int = Query(default=0, ge=0, description="Offset for pagination"),
) -> list[GraphEdge]:
    """Get all edges for graph visualization."""
    try:
        group_id = str(org.id)
        runtime = await get_entity_graph_runtime(group_id)

        all_relationships = await runtime.relationship_manager.list_all(
            relationship_types=relationship_types,
            limit=limit,
            offset=offset,
        )

        return [
            GraphEdge(
                id=rel.id,
                source=rel.source_id,
                target=rel.target_id,
                type=rel.relationship_type.value,
                label=rel.relationship_type.value.replace("_", " ").title(),
                weight=1.0,  # Could be based on strength/confidence
            )
            for rel in all_relationships
        ]

    except Exception as e:
        log.exception("get_edges_failed", error=str(e))
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve graph edges. Please try again.",
        ) from e


@router.get("/full", response_model=GraphData)
async def get_full_graph(
    org: Organization = Depends(get_current_organization),
    types: list[EntityType] | None = Query(default=None, description="Filter by entity types"),
    max_nodes: int = Query(default=500, ge=1, le=1000, description="Maximum nodes"),
    max_edges: int = Query(default=1000, ge=1, le=5000, description="Maximum edges"),
) -> GraphData:
    """Get complete graph data for visualization."""
    try:
        group_id = str(org.id)
        runtime = await get_entity_graph_runtime(group_id)

        entities = await _list_graph_entities(
            runtime.entity_manager,
            entity_types=types,
            limit=max_nodes,
            include_archived=True,
        )

        nodes = []
        node_ids: set[str] = set()
        for entity in entities:
            node_id = entity.id
            if not node_id:
                continue
            node_ids.add(node_id)
            entity_type = entity.entity_type

            nodes.append(
                GraphNode(
                    id=node_id,
                    type=entity_type.value,
                    label=(entity.name or node_id[:20])[:50],
                    color=get_entity_color(entity_type),
                    size=1.5,
                    metadata={},
                )
            )

        relationships = await _list_graph_relationships_for_entities(
            runtime.relationship_manager,
            node_ids,
            limit=max_edges,
        )

        log.info(
            "graph_full_raw",
            node_count=len(nodes),
            edge_rows=len(relationships),
            node_ids_sample=list(node_ids)[:3],
        )

        edges = []
        for relationship in relationships:
            if relationship.source_id not in node_ids or relationship.target_id not in node_ids:
                continue
            edges.append(
                GraphEdge(
                    id=relationship.id or f"{relationship.source_id}-{relationship.target_id}",
                    source=relationship.source_id,
                    target=relationship.target_id,
                    type=relationship.relationship_type.value,
                    label=relationship.relationship_type.value.replace("_", " ").title(),
                    weight=1.0,
                )
            )

        log.info("graph_full_filtered", edges_after_filter=len(edges))

        return GraphData(
            nodes=nodes,
            edges=edges,
            node_count=len(nodes),
            edge_count=len(edges),
        )

    except Exception as e:
        log.exception("get_full_graph_failed", error=str(e))
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve full graph. Please try again.",
        ) from e


@router.post("/subgraph", response_model=GraphData)
async def get_subgraph(
    payload: SubgraphRequest,
    org: Organization = Depends(get_current_organization),
) -> GraphData:
    """Get a subgraph centered on a specific entity."""
    try:
        group_id = str(org.id)
        runtime = await get_entity_graph_runtime(group_id)

        # Get center entity
        center = await _get_graph_entity(runtime.entity_manager, payload.entity_id)
        if not center:
            raise HTTPException(status_code=404, detail=f"Entity not found: {payload.entity_id}")

        # Build subgraph via traversal
        visited_nodes: dict[str, GraphNode] = {}
        edges: list[GraphEdge] = []

        async def traverse(entity_id: str, current_depth: int) -> None:
            if current_depth > payload.depth:
                return
            if len(visited_nodes) >= payload.max_nodes:
                return
            if entity_id in visited_nodes:
                return

            entity = await _get_graph_entity(runtime.entity_manager, entity_id)
            if not entity:
                return

            # Add node
            visited_nodes[entity_id] = GraphNode(
                id=entity.id,
                type=entity.entity_type.value,
                label=entity.name[:50],
                color=get_entity_color(entity.entity_type),
                size=2.0 if entity_id == payload.entity_id else 1.5,  # Center node larger
                metadata={
                    "description": entity.description[:100] if entity.description else "",
                    "depth": current_depth,
                },
            )

            # Get related entities
            related = await runtime.relationship_manager.get_related_entities(
                entity_id=entity_id,
                relationship_types=payload.relationship_types,
                max_depth=1,
                limit=50,
            )

            for related_entity, relationship in related:
                # Add edge
                edges.append(
                    GraphEdge(
                        id=relationship.id,
                        source=relationship.source_id,
                        target=relationship.target_id,
                        type=relationship.relationship_type.value,
                        label=relationship.relationship_type.value.replace("_", " ").title(),
                        weight=1.0,
                    )
                )

                # Recurse
                await traverse(related_entity.id, current_depth + 1)

        # Start traversal from center
        await traverse(payload.entity_id, 0)

        # Deduplicate edges
        seen_edges: set[str] = set()
        unique_edges = []
        for edge in edges:
            edge_key = f"{edge.source}-{edge.target}-{edge.type}"
            if edge_key not in seen_edges:
                seen_edges.add(edge_key)
                unique_edges.append(edge)

        return GraphData(
            nodes=list(visited_nodes.values()),
            edges=unique_edges,
            node_count=len(visited_nodes),
            edge_count=len(unique_edges),
        )

    except HTTPException:
        raise
    except Exception as e:
        log.exception("get_subgraph_failed", entity_id=payload.entity_id, error=str(e))
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve subgraph. Please try again.",
        ) from e


# =============================================================================
# Cluster Endpoints for Bubble Visualization
# =============================================================================


@router.get("/clusters")
async def get_clusters(
    org: Organization = Depends(get_current_organization),
    refresh: bool = Query(default=False, description="Force refresh clusters"),
) -> dict:
    """Get clusters for bubble visualization.

    Returns community-detected clusters with type distribution for coloring.
    Results are cached for 5 minutes to avoid expensive recomputation.
    """
    try:
        group_id = str(org.id)
        runtime = await get_entity_graph_runtime(group_id)

        clusters = await get_clusters_for_visualization(
            runtime.client,
            group_id,
            force_refresh=refresh,
        )

        # Transform to API response format
        cluster_data = [
            {
                "id": c.id,
                "count": c.member_count,
                "dominant_type": c.dominant_type,
                "type_distribution": c.type_distribution,
                "level": c.level,
            }
            for c in clusters
        ]

        total_nodes = sum(c.member_count for c in clusters)

        return {
            "clusters": cluster_data,
            "total_nodes": total_nodes,
            "total_clusters": len(clusters),
        }

    except Exception as e:
        log.exception("get_clusters_failed", error=str(e))
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve clusters. Please try again.",
        ) from e


@router.get("/clusters/{cluster_id}")
async def get_cluster_detail(
    cluster_id: str,
    org: Organization = Depends(get_current_organization),
) -> dict:
    """Get nodes and edges within a specific cluster for drill-down view."""
    try:
        group_id = str(org.id)
        runtime = await get_entity_graph_runtime(group_id)

        result = await get_cluster_nodes(runtime.client, group_id, cluster_id)

        if result.get("error"):
            raise HTTPException(status_code=404, detail=result["error"])

        # Transform nodes to GraphNode format
        nodes = [
            GraphNode(
                id=n["id"],
                type=n["type"],
                label=(n["name"] or n["id"][:20])[:50],
                color=get_entity_color(
                    EntityType(n["type"])
                    if n["type"] in [e.value for e in EntityType]
                    else EntityType.EPISODE
                ),
                size=1.5,
                metadata={"summary": n.get("summary", "")},
            )
            for n in result["nodes"]
        ]

        # Transform edges to GraphEdge format
        edges = [
            GraphEdge(
                id=f"{e['source']}-{e['target']}",
                source=e["source"],
                target=e["target"],
                type=e["type"],
                label=e["type"].replace("_", " ").title(),
                weight=1.0,
            )
            for e in result["edges"]
        ]

        return {
            "cluster_id": cluster_id,
            "nodes": [n.model_dump() for n in nodes],
            "edges": [e.model_dump() for e in edges],
            "node_count": len(nodes),
            "edge_count": len(edges),
        }

    except HTTPException:
        raise
    except Exception as e:
        log.exception("get_cluster_detail_failed", cluster_id=cluster_id, error=str(e))
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve cluster details. Please try again.",
        ) from e


@router.get("/hierarchical")
async def get_hierarchical_graph_data(
    org: Organization = Depends(get_current_organization),
    projects: list[str] | None = Query(default=None, description="Filter by project IDs"),
    types: list[EntityType] | None = Query(default=None, description="Filter by entity types"),
    max_nodes: int = Query(default=1000, ge=100, le=2000, description="Maximum nodes"),
    max_edges: int = Query(default=5000, ge=500, le=10000, description="Maximum edges"),
    resolution: str = Query(
        default="detail",
        pattern="^(overview|detail)$",
        description="Graph detail level",
    ),
    cluster_id: str | None = Query(default=None, description="Focus a specific cluster"),
) -> dict:
    """Get hierarchical graph data with cluster assignments.

    Returns actual nodes and edges (not aggregated bubbles) with each node
    assigned to a cluster based on Louvain community detection.

    This endpoint is designed for rich graph visualization:
    - Up to 2000 nodes with real edges
    - Each node has cluster_id for coloring
    - Cluster metadata for legends
    - Inter-cluster edges for summary views
    """
    try:
        group_id = str(org.id)
        runtime = await get_entity_graph_runtime(group_id)

        data = await get_hierarchical_graph(
            runtime.client,
            group_id,
            project_ids=projects,
            entity_types=[t.value for t in types] if types else None,
            max_nodes=max_nodes,
            max_edges=max_edges,
            resolution=resolution,
            cluster_id=cluster_id,
        )

        # Guard against focused-mode totals undercounting. If filtered data exists,
        # don't surface 0 totals in the UI.
        has_focus_filters = bool(projects or types)
        if has_focus_filters and data.total_nodes == 0 and data.displayed_nodes > 0:
            data.total_nodes = data.displayed_nodes
        if has_focus_filters and data.total_edges == 0 and data.displayed_edges > 0:
            data.total_edges = data.displayed_edges

        # Transform nodes to include colors
        colored_nodes = []
        for node in data.nodes:
            entity_type_str = node.get("type", "episode")
            try:
                entity_type = EntityType(entity_type_str)
            except ValueError:
                entity_type = EntityType.EPISODE

            colored_nodes.append(
                {
                    **node,
                    "label": (node.get("name") or node["id"][:20])[:50],
                    "color": get_entity_color(entity_type),
                }
            )

        return {
            "nodes": colored_nodes,
            "edges": data.edges,
            "clusters": data.clusters,
            "cluster_edges": data.cluster_edges,
            "total_nodes": data.total_nodes,
            "total_edges": data.total_edges,
            "displayed_nodes": data.displayed_nodes,
            "displayed_edges": data.displayed_edges,
            "resolution": data.resolution,
        }

    except Exception as e:
        log.exception("get_hierarchical_graph_failed", error=str(e))
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve hierarchical graph. Please try again.",
        ) from e


@router.get("/stats")
async def get_graph_stats(
    service: KnowledgeReadService = Depends(get_knowledge_read_service),
) -> dict:
    """Get efficient graph statistics using aggregate queries.

    Does not load the full graph - uses Cypher aggregation for performance.
    """
    try:
        stats = await service.stats()

        return {
            "total_nodes": stats.total_entities,
            "total_edges": stats.total_relationships,
            "by_type": stats.entities_by_type,
        }

    except Exception as e:
        log.exception("get_graph_stats_failed", error=str(e))
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve graph stats. Please try again.",
        ) from e
