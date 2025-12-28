"""Graph visualization data endpoints."""

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query

from sibyl.api.schemas import GraphData, GraphEdge, GraphNode, SubgraphRequest
from sibyl.auth.dependencies import get_current_organization, require_org_role
from sibyl.db.models import Organization, OrganizationRole
from sibyl.graph.client import GraphClient, get_graph_client
from sibyl.graph.entities import EntityManager
from sibyl.graph.relationships import RelationshipManager
from sibyl.models.entities import EntityType, RelationshipType

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

router = APIRouter(
    prefix="/graph",
    tags=["graph"],
    dependencies=[Depends(require_org_role(*_READ_ROLES))],
)


@router.get("/debug", dependencies=[Depends(require_org_role(*_ADMIN_ROLES))])
async def debug_graph(org: Organization = Depends(get_current_organization)):
    """Debug endpoint to trace graph data issue."""
    client = await get_graph_client()
    group_id = str(org.id)

    # Use org-scoped driver to query the correct graph
    driver = client.get_org_driver(group_id)

    # Get nodes
    node_query = """
        MATCH (n)
        WHERE (n:Episodic OR n:Entity) AND n.group_id = $group_id
        RETURN n.uuid as id LIMIT 500
    """
    node_result = await driver.execute_query(node_query, group_id=group_id)
    node_rows = GraphClient.normalize_result(node_result)
    node_ids = {row.get("id") for row in node_rows if row.get("id")}

    # Get edges
    edge_query = """
        MATCH (s)-[r]->(t)
        WHERE r.group_id = $group_id
        RETURN s.uuid as src, t.uuid as tgt LIMIT 1000
    """
    edge_result = await driver.execute_query(edge_query, group_id=group_id)
    edge_rows = GraphClient.normalize_result(edge_result)

    # Check overlap
    matching = sum(
        1 for row in edge_rows if row.get("src") in node_ids and row.get("tgt") in node_ids
    )

    sample_edges = edge_rows[:3] if edge_rows else []
    sample_nodes = list(node_ids)[:5]

    return {
        "node_count": len(node_ids),
        "edge_count": len(edge_rows),
        "matching_edges": matching,
        "sample_nodes": sample_nodes,
        "sample_edges": [{"src": e.get("src"), "tgt": e.get("tgt")} for e in sample_edges],
        "first_edge_src_in_nodes": sample_edges[0].get("src") in node_ids if sample_edges else None,
        "first_edge_tgt_in_nodes": sample_edges[0].get("tgt") in node_ids if sample_edges else None,
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
        client = await get_graph_client()

        # Build type filter for Cypher query
        type_filter = ""
        if types:
            type_values = [f"'{t.value}'" for t in types]
            type_filter = f"AND n.entity_type IN [{', '.join(type_values)}]"

        # Query nodes directly from graph - both Episodic and Entity labels
        query = f"""
            MATCH (n)
            WHERE (n:Episodic OR n:Entity)
            AND n.group_id = $group_id
            {type_filter}
            RETURN n.uuid as id,
                   n.name as name,
                   n.entity_type as entity_type,
                   n.summary as summary
            SKIP {offset}
            LIMIT {limit}
        """

        # Use org-scoped driver to query the correct graph
        driver = client.get_org_driver(group_id)
        result = await driver.execute_query(query, group_id=group_id)
        rows = GraphClient.normalize_result(result)

        # Count connections for sizing
        connection_counts: dict[str, int] = {}
        try:
            conn_query = """
                MATCH (n)-[r]-(m)
                WHERE n.group_id = $group_id
                RETURN n.uuid as id, count(r) as cnt
            """
            conn_result = await driver.execute_query(conn_query, group_id=group_id)
            for row in GraphClient.normalize_result(conn_result):
                connection_counts[row.get("id", "")] = row.get("cnt", 0)
        except Exception:
            log.debug("connection_count_failed", msg="falling back to zero")

        max_connections = max(connection_counts.values()) if connection_counts else 1
        max_connections = max(max_connections, 1)

        nodes = []
        for row in rows:
            node_id = row.get("id", "")
            if not node_id:
                continue

            entity_type_str = row.get("entity_type", "episode")
            try:
                entity_type = EntityType(entity_type_str)
            except ValueError:
                entity_type = EntityType.EPISODE

            conn_count = connection_counts.get(node_id, 0)
            size = 1.0 + (conn_count / max_connections) * 2.0

            nodes.append(
                GraphNode(
                    id=node_id,
                    type=entity_type.value,
                    label=(row.get("name") or node_id[:20])[:50],
                    color=get_entity_color(entity_type),
                    size=size,
                    metadata={
                        "description": (row.get("summary") or "")[:100],
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
        client = await get_graph_client()
        relationship_manager = RelationshipManager(client, group_id=group_id)

        # Get all relationships with pagination
        all_relationships = await relationship_manager.list_all(
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
        # Fetch nodes and edges - call underlying logic directly
        client = await get_graph_client()
        group_id = str(org.id)

        # Use org-scoped driver to query the correct graph
        driver = client.get_org_driver(group_id)

        # === NODES: Direct Cypher query ===
        type_filter = ""
        if types:
            type_values = [f"'{t.value}'" for t in types]
            type_filter = f"AND n.entity_type IN [{', '.join(type_values)}]"

        node_query = f"""
            MATCH (n)
            WHERE (n:Episodic OR n:Entity)
            AND n.group_id = $group_id
            {type_filter}
            RETURN n.uuid as id,
                   n.name as name,
                   n.entity_type as entity_type,
                   n.summary as summary
            LIMIT {max_nodes}
        """
        node_result = await driver.execute_query(node_query, group_id=group_id)
        node_rows = GraphClient.normalize_result(node_result)

        nodes = []
        node_ids: set[str] = set()
        for row in node_rows:
            node_id = row.get("id", "")
            if not node_id:
                continue
            node_ids.add(node_id)

            entity_type_str = row.get("entity_type", "episode")
            try:
                entity_type = EntityType(entity_type_str)
            except ValueError:
                entity_type = EntityType.EPISODE

            nodes.append(
                GraphNode(
                    id=node_id,
                    type=entity_type.value,
                    label=(row.get("name") or node_id[:20])[:50],
                    color=get_entity_color(entity_type),
                    size=1.5,
                    metadata={},
                )
            )

        # === EDGES: Direct Cypher query ===
        # Use r.name for semantic type (BELONGS_TO, etc), not type(r) which returns graph label
        edge_query = f"""
            MATCH (source)-[r]->(target)
            WHERE r.group_id = $group_id
            RETURN r.uuid as id,
                   source.uuid as source_id,
                   target.uuid as target_id,
                   COALESCE(r.name, type(r)) as rel_type
            LIMIT {max_edges}
        """
        edge_result = await driver.execute_query(edge_query, group_id=group_id)
        edge_rows = GraphClient.normalize_result(edge_result)

        log.info(
            "graph_full_raw",
            node_count=len(nodes),
            edge_rows=len(edge_rows),
            node_ids_sample=list(node_ids)[:3],
        )

        # Filter edges to nodes we have
        edges = []
        for row in edge_rows:
            source_id = row.get("source_id", "")
            target_id = row.get("target_id", "")
            if source_id in node_ids and target_id in node_ids:
                edges.append(
                    GraphEdge(
                        id=row.get("id") or f"{source_id}-{target_id}",
                        source=source_id,
                        target=target_id,
                        type=row.get("rel_type", "RELATED_TO"),
                        label=row.get("rel_type", "").replace("_", " ").title(),
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
        client = await get_graph_client()
        group_id = str(org.id)
        entity_manager = EntityManager(client, group_id=group_id)
        relationship_manager = RelationshipManager(client, group_id=group_id)

        # Get center entity
        center = await entity_manager.get(payload.entity_id)
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

            entity = await entity_manager.get(entity_id)
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
            related = await relationship_manager.get_related_entities(
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
    from sibyl.graph.communities import get_clusters_for_visualization

    try:
        client = await get_graph_client()
        group_id = str(org.id)

        clusters = await get_clusters_for_visualization(client, group_id, force_refresh=refresh)

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
    from sibyl.graph.communities import get_cluster_nodes

    try:
        client = await get_graph_client()
        group_id = str(org.id)

        result = await get_cluster_nodes(client, group_id, cluster_id)

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
    max_nodes: int = Query(default=1000, ge=100, le=2000, description="Maximum nodes"),
    max_edges: int = Query(default=5000, ge=500, le=10000, description="Maximum edges"),
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
    from sibyl.graph.communities import get_hierarchical_graph

    try:
        client = await get_graph_client()
        group_id = str(org.id)

        data = await get_hierarchical_graph(
            client,
            group_id,
            max_nodes=max_nodes,
            max_edges=max_edges,
        )

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
        }

    except Exception as e:
        log.exception("get_hierarchical_graph_failed", error=str(e))
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve hierarchical graph. Please try again.",
        ) from e


@router.get("/stats")
async def get_graph_stats(
    org: Organization = Depends(get_current_organization),
) -> dict:
    """Get efficient graph statistics using aggregate queries.

    Does not load the full graph - uses Cypher aggregation for performance.
    """
    try:
        client = await get_graph_client()
        group_id = str(org.id)
        driver = client.get_org_driver(group_id)

        # Count nodes by type - single aggregation query
        node_query = """
        MATCH (n)
        WHERE (n:Episodic OR n:Entity) AND n.group_id = $group_id
        RETURN n.entity_type AS type, count(*) AS cnt
        """
        node_result = await driver.execute_query(node_query, group_id=group_id)
        node_rows = GraphClient.normalize_result(node_result)

        type_counts: dict[str, int] = {}
        for row in node_rows:
            t = row.get("type") or "unknown"
            c = row.get("cnt", 0)
            type_counts[t] = c

        total_nodes = sum(type_counts.values())

        # Count edges - single count query
        edge_query = """
        MATCH ()-[r]->()
        WHERE r.group_id = $group_id
        RETURN count(r) AS cnt
        """
        edge_result = await driver.execute_query(edge_query, group_id=group_id)
        edge_rows = GraphClient.normalize_result(edge_result)
        total_edges = edge_rows[0].get("cnt", 0) if edge_rows else 0

        return {
            "total_nodes": total_nodes,
            "total_edges": total_edges,
            "by_type": type_counts,
        }

    except Exception as e:
        log.exception("get_graph_stats_failed", error=str(e))
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve graph stats. Please try again.",
        ) from e
