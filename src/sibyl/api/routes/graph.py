"""Graph visualization data endpoints."""

import structlog
from fastapi import APIRouter, HTTPException, Query

from sibyl.api.schemas import GraphData, GraphEdge, GraphNode, SubgraphRequest
from sibyl.graph.client import get_graph_client
from sibyl.graph.entities import EntityManager
from sibyl.graph.relationships import RelationshipManager
from sibyl.models.entities import EntityType, RelationshipType

log = structlog.get_logger()
router = APIRouter(prefix="/graph", tags=["graph"])

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
}

DEFAULT_COLOR = "#8b85a0"  # Muted for unknown types


def get_entity_color(entity_type: EntityType) -> str:
    """Get the SilkCircuit color for an entity type."""
    return ENTITY_COLORS.get(entity_type, DEFAULT_COLOR)


@router.get("/nodes", response_model=list[GraphNode])
async def get_all_nodes(
    types: list[EntityType] | None = Query(default=None, description="Filter by entity types"),
    limit: int = Query(default=500, ge=1, le=2000, description="Maximum nodes"),
) -> list[GraphNode]:
    """Get all nodes for graph visualization."""
    try:
        client = await get_graph_client()
        entity_manager = EntityManager(client)
        relationship_manager = RelationshipManager(client)

        # Fetch entities
        target_types = types or list(EntityType)
        all_entities = []
        for entity_type in target_types:
            entities = await entity_manager.list_by_type(entity_type, limit=limit)
            all_entities.extend(entities)

        # Build connection counts via single batch fetch (avoids N+1)
        connection_counts: dict[str, int] = {}
        try:
            # Fetch all relationships in one query
            all_relationships = await relationship_manager.list_all(limit=limit * 10)
            # Count connections per entity (both as source and target)
            for rel in all_relationships:
                connection_counts[rel.source_id] = connection_counts.get(rel.source_id, 0) + 1
                connection_counts[rel.target_id] = connection_counts.get(rel.target_id, 0) + 1
        except Exception:
            log.debug("relationship_fetch_failed", msg="falling back to zero connections")

        # Normalize sizes (1.0 to 3.0 based on connections)
        max_connections = max(connection_counts.values()) if connection_counts else 1
        max_connections = max(max_connections, 1)  # Avoid division by zero

        nodes = []
        for entity in all_entities[:limit]:
            conn_count = connection_counts.get(entity.id, 0)
            size = 1.0 + (conn_count / max_connections) * 2.0

            nodes.append(
                GraphNode(
                    id=entity.id,
                    type=entity.entity_type.value,
                    label=entity.name[:50],  # Truncate for display
                    color=get_entity_color(entity.entity_type),
                    size=size,
                    metadata={
                        "description": entity.description[:100] if entity.description else "",
                        "category": getattr(entity, "category", None),
                        "languages": getattr(entity, "languages", []),
                        "connections": conn_count,
                    },
                )
            )

        return nodes

    except Exception as e:
        log.exception("get_nodes_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/edges", response_model=list[GraphEdge])
async def get_all_edges(
    relationship_types: list[RelationshipType] | None = Query(
        default=None, description="Filter by relationship types"
    ),
    limit: int = Query(default=1000, ge=1, le=5000, description="Maximum edges"),
) -> list[GraphEdge]:
    """Get all edges for graph visualization."""
    try:
        client = await get_graph_client()
        relationship_manager = RelationshipManager(client)

        # Get all relationships
        all_relationships = await relationship_manager.list_all(
            relationship_types=relationship_types,
            limit=limit,
        )

        edges = []
        for rel in all_relationships:
            edges.append(
                GraphEdge(
                    id=rel.id,
                    source=rel.source_id,
                    target=rel.target_id,
                    type=rel.relationship_type.value,
                    label=rel.relationship_type.value.replace("_", " ").title(),
                    weight=1.0,  # Could be based on strength/confidence
                )
            )

        return edges

    except Exception as e:
        log.exception("get_edges_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/full", response_model=GraphData)
async def get_full_graph(
    types: list[EntityType] | None = Query(default=None, description="Filter by entity types"),
    max_nodes: int = Query(default=200, ge=1, le=1000, description="Maximum nodes"),
    max_edges: int = Query(default=500, ge=1, le=2000, description="Maximum edges"),
) -> GraphData:
    """Get complete graph data for visualization."""
    try:
        # Fetch nodes and edges
        nodes = await get_all_nodes(types=types, limit=max_nodes)
        edges = await get_all_edges(limit=max_edges)

        # Filter edges to only include those connecting existing nodes
        node_ids = {node.id for node in nodes}
        filtered_edges = [
            edge for edge in edges if edge.source in node_ids and edge.target in node_ids
        ]

        return GraphData(
            nodes=nodes,
            edges=filtered_edges,
            node_count=len(nodes),
            edge_count=len(filtered_edges),
        )

    except Exception as e:
        log.exception("get_full_graph_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/subgraph", response_model=GraphData)
async def get_subgraph(request: SubgraphRequest) -> GraphData:
    """Get a subgraph centered on a specific entity."""
    try:
        client = await get_graph_client()
        entity_manager = EntityManager(client)
        relationship_manager = RelationshipManager(client)

        # Get center entity
        center = await entity_manager.get(request.entity_id)
        if not center:
            raise HTTPException(status_code=404, detail=f"Entity not found: {request.entity_id}")

        # Build subgraph via traversal
        visited_nodes: dict[str, GraphNode] = {}
        edges: list[GraphEdge] = []

        async def traverse(entity_id: str, current_depth: int) -> None:
            if current_depth > request.depth:
                return
            if len(visited_nodes) >= request.max_nodes:
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
                size=2.0 if entity_id == request.entity_id else 1.5,  # Center node larger
                metadata={
                    "description": entity.description[:100] if entity.description else "",
                    "depth": current_depth,
                },
            )

            # Get related entities
            related = await relationship_manager.get_related_entities(
                entity_id=entity_id,
                relationship_types=request.relationship_types,
                depth=1,
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
        await traverse(request.entity_id, 0)

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
        log.exception("get_subgraph_failed", entity_id=request.entity_id, error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e
