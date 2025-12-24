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
