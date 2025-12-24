"""Relationship management using Graphiti's EntityEdge API.

This module provides relationship operations between entities in the knowledge graph
using Graphiti's native edge system rather than custom Cypher queries.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import structlog
from graphiti_core.edges import EntityEdge

from sibyl.errors import ConventionsMCPError
from sibyl.graph.client import GraphClient
from sibyl.models.entities import Entity, Relationship, RelationshipType

log = structlog.get_logger()


class RelationshipManager:
    """Manages relationship operations using Graphiti's EntityEdge API."""

    def __init__(self, client: GraphClient, *, group_id: str) -> None:
        """Initialize relationship manager with graph client.

        Creates a cloned driver targeting the org-specific graph for multi-tenancy.

        Args:
            client: The GraphClient instance.
            group_id: Organization ID (required). No default - callers must provide org context.

        Raises:
            ValueError: If group_id is empty.
        """
        if not group_id:
            raise ValueError("group_id is required - cannot access graph without org context")
        self._client = client
        self._group_id = group_id
        # Clone the driver to use the org-specific graph
        self._driver = client.client.driver.clone(group_id)

    def _to_graphiti_edge(self, relationship: Relationship) -> EntityEdge:
        """Convert our Relationship model to Graphiti's EntityEdge.

        Args:
            relationship: Our relationship model

        Returns:
            Graphiti EntityEdge
        """
        return EntityEdge(
            uuid=relationship.id or str(uuid4()),
            group_id=self._group_id,
            source_node_uuid=relationship.source_id,
            target_node_uuid=relationship.target_id,
            created_at=datetime.now(UTC),
            name=relationship.relationship_type.value,
            fact=f"{relationship.relationship_type.value} relationship",
            fact_embedding=None,
            episodes=[],
            expired_at=None,
            valid_at=datetime.now(UTC),
            invalid_at=None,
            attributes={
                "weight": relationship.weight,
                **(relationship.metadata or {}),
            },
        )

    def _from_graphiti_edge(self, edge: EntityEdge) -> Relationship:
        """Convert Graphiti's EntityEdge to our Relationship model.

        Args:
            edge: Graphiti EntityEdge

        Returns:
            Our Relationship model
        """
        # Parse relationship type from edge name
        try:
            rel_type = RelationshipType(edge.name)
        except ValueError:
            rel_type = RelationshipType.RELATED_TO

        # Extract our metadata from attributes
        attributes = edge.attributes or {}
        weight = float(attributes.pop("weight", 1.0))

        return Relationship(
            id=edge.uuid,
            relationship_type=rel_type,
            source_id=edge.source_node_uuid,
            target_id=edge.target_node_uuid,
            weight=weight,
            metadata=attributes,
        )

    async def create(self, relationship: Relationship) -> str:
        """Create a new relationship between entities.

        Args:
            relationship: The relationship to create.

        Returns:
            The ID of the created relationship.

        Raises:
            ConventionsMCPError: If relationship creation fails.
        """
        log.info(
            "Creating relationship",
            type=relationship.relationship_type.value,
            source=relationship.source_id,
            target=relationship.target_id,
        )

        try:
            # Check for existing relationship
            existing = await EntityEdge.get_between_nodes(
                self._client.driver,
                relationship.source_id,
                relationship.target_id,
            )

            # Filter by relationship type
            for edge in existing:
                if edge.name == relationship.relationship_type.value:
                    log.info(
                        "Relationship already exists; skipping duplicate",
                        relationship_id=edge.uuid,
                    )
                    return edge.uuid

            # Create edge via direct Cypher (more reliable than Graphiti's EntityEdge.save)
            # EntityEdge.save() has issues finding Episodic nodes by UUID
            edge = self._to_graphiti_edge(relationship)
            rel_type = relationship.relationship_type.value

            query = f"""
                MATCH (source {{uuid: $source_uuid}})
                MATCH (target {{uuid: $target_uuid}})
                MERGE (source)-[r:{rel_type} {{uuid: $edge_uuid}}]->(target)
                SET r.name = $name,
                    r.group_id = $group_id,
                    r.created_at = $created_at,
                    r.weight = $weight
                RETURN r.uuid as uuid
            """

            async with self._client.write_lock:
                await self._driver.execute_query(
                    query,
                    source_uuid=relationship.source_id,
                    target_uuid=relationship.target_id,
                    edge_uuid=edge.uuid,
                    name=rel_type,
                    group_id=self._group_id,
                    created_at=edge.created_at.isoformat(),
                    weight=relationship.weight,
                )

            log.info("Created relationship", relationship_id=edge.uuid)
            return edge.uuid

        except Exception as e:
            log.exception("Failed to create relationship", error=str(e))
            raise ConventionsMCPError(
                f"Failed to create relationship: {e}",
                details={
                    "source_id": relationship.source_id,
                    "target_id": relationship.target_id,
                    "type": relationship.relationship_type.value,
                },
            ) from e

    async def create_bulk(self, relationships: list[Relationship]) -> tuple[int, int]:
        """Create multiple relationships in bulk.

        Args:
            relationships: List of relationships to create.

        Returns:
            Tuple of (created_count, failed_count)
        """
        log.info("Creating relationships in bulk", count=len(relationships))

        created = 0
        failed = 0

        for rel in relationships:
            try:
                await self.create(rel)
                created += 1
            except Exception as e:
                log.warning("Failed to create relationship", error=str(e))
                failed += 1

        log.info("Bulk create complete", created=created, failed=failed)
        return created, failed

    async def get_for_entity(
        self,
        entity_id: str,
        relationship_types: list[RelationshipType] | None = None,
        direction: str = "both",
    ) -> list[Relationship]:
        """Get all relationships for an entity.

        Args:
            entity_id: The entity UUID.
            relationship_types: Optional filter by relationship types.
            direction: "outgoing", "incoming", or "both" (default).

        Returns:
            List of relationships involving this entity.
        """
        log.debug(
            "Getting relationships for entity",
            entity_id=entity_id,
            types=relationship_types,
            direction=direction,
        )

        try:
            # Get all edges connected to this node
            edges = await EntityEdge.get_by_node_uuid(
                self._client.driver,
                entity_id,
            )

            # Defensive: ensure edges is iterable (FalkorDB can return Query object on connection issues)
            if not isinstance(edges, list):
                log.warning("get_by_node_uuid returned non-list", type=type(edges).__name__)
                return []

            relationships = []
            for edge in edges:
                if edge.group_id != self._group_id:
                    continue
                # Filter by direction
                if direction == "outgoing" and edge.source_node_uuid != entity_id:
                    continue
                if direction == "incoming" and edge.target_node_uuid != entity_id:
                    continue

                # Filter by relationship type
                if relationship_types:
                    type_values = [t.value for t in relationship_types]
                    if edge.name not in type_values:
                        continue

                relationships.append(self._from_graphiti_edge(edge))

            log.debug(
                "Retrieved relationships",
                entity_id=entity_id,
                count=len(relationships),
            )
            return relationships

        except Exception as e:
            log.warning(
                "Failed to fetch relationships, returning empty list",
                error=str(e),
                entity_id=entity_id,
            )
            # Return empty list instead of crashing - relationships are often optional
            return []

    async def get_related_entities(
        self,
        entity_id: str,
        relationship_types: list[RelationshipType] | None = None,
        max_depth: int = 1,
        limit: int = 50,
    ) -> list[tuple[Entity, Relationship]]:
        """Get entities related to a given entity.

        Args:
            entity_id: The entity UUID.
            relationship_types: Optional filter by relationship types.
            max_depth: Maximum traversal depth (currently only supports 1).
            limit: Maximum number of results.

        Returns:
            List of (entity, relationship) tuples.
        """
        from sibyl.graph.entities import EntityManager

        log.debug(
            "Getting related entities",
            entity_id=entity_id,
            types=relationship_types,
            max_depth=max_depth,
        )

        try:
            # Get relationships
            relationships = await self.get_for_entity(
                entity_id, relationship_types, direction="both"
            )

            # Get the other entity for each relationship
            entity_manager = EntityManager(self._client, group_id=self._group_id)
            results: list[tuple[Entity, Relationship]] = []

            for rel in relationships[:limit]:
                other_id = rel.target_id if rel.source_id == entity_id else rel.source_id
                try:
                    entity = await entity_manager.get(other_id)
                    if entity:
                        results.append((entity, rel))
                except Exception:
                    # Skip entities that can't be fetched
                    continue

            log.debug("Retrieved related entities", count=len(results))
            return results

        except Exception as e:
            log.warning(
                "Failed to get related entities, returning empty list",
                error=str(e),
                entity_id=entity_id,
            )
            return []

    async def delete(self, relationship_id: str) -> bool:
        """Delete a relationship by ID.

        Args:
            relationship_id: The relationship UUID.

        Returns:
            True if deleted successfully.

        Raises:
            ConventionsMCPError: If deletion fails.
        """
        log.info("Deleting relationship", relationship_id=relationship_id)

        try:
            edge = await EntityEdge.get_by_uuid(self._client.driver, relationship_id)
            if edge:
                # Use write lock to prevent FalkorDB connection corruption
                async with self._client.write_lock:
                    await edge.delete(self._client.driver)
                log.info("Deleted relationship", relationship_id=relationship_id)
                return True
            log.warning("Relationship not found", relationship_id=relationship_id)
            return False

        except Exception as e:
            log.exception("Failed to delete relationship", error=str(e))
            raise ConventionsMCPError(
                f"Failed to delete relationship: {e}",
                details={"relationship_id": relationship_id},
            ) from e

    async def delete_for_entity(self, entity_id: str) -> int:
        """Delete all relationships for an entity.

        Args:
            entity_id: The entity UUID.

        Returns:
            Number of relationships deleted.
        """
        log.info("Deleting all relationships for entity", entity_id=entity_id)

        try:
            edges = await EntityEdge.get_by_node_uuid(self._client.driver, entity_id)

            # Defensive: ensure edges is iterable (FalkorDB can return Query object on connection issues)
            if not isinstance(edges, list):
                log.warning(
                    "get_by_node_uuid returned non-list for delete", type=type(edges).__name__
                )
                return 0

            deleted = 0

            # Use write lock to prevent FalkorDB connection corruption
            async with self._client.write_lock:
                for edge in edges:
                    try:
                        await edge.delete(self._client.driver)
                        deleted += 1
                    except Exception as e:
                        log.warning("Failed to delete edge", edge_uuid=edge.uuid, error=str(e))

            log.info("Deleted relationships for entity", entity_id=entity_id, count=deleted)
            return deleted

        except Exception as e:
            log.warning("Failed to delete relationships for entity", error=str(e))
            return 0

    async def list_all(
        self,
        relationship_types: list[RelationshipType] | None = None,
        limit: int = 100,
    ) -> list[Relationship]:
        """List all relationships in the graph.

        Args:
            relationship_types: Optional list of relationship types to filter by.
            limit: Maximum number to return.

        Returns:
            List of relationships.
        """
        log.debug("Listing all relationships", limit=limit, types=relationship_types)

        try:
            # Direct Cypher query to get edges (Graphiti's EntityEdge.get_by_group_ids
            # expects ENTITY_EDGE label but our edges have RELATES_TO, MENTIONS, etc.)
            type_filter = ""
            if relationship_types:
                type_names = [rt.value for rt in relationship_types]
                type_filter = f"AND type(r) IN {type_names}"

            query = f"""
                MATCH (source)-[r]->(target)
                WHERE r.group_id = $group_id
                {type_filter}
                RETURN r.uuid as id,
                       source.uuid as source_id,
                       target.uuid as target_id,
                       type(r) as rel_type,
                       r.created_at as created_at
                LIMIT {limit}
            """

            result = await self._driver.execute_query(query, group_id=self._group_id)
            rows = GraphClient.normalize_result(result)

            relationships = []
            for row in rows:
                # Row is a dict with keys: id, source_id, target_id, rel_type, created_at
                rel_type = row.get("rel_type", "RELATED_TO")
                # Parse relationship type
                try:
                    relationship_type = RelationshipType(rel_type)
                except ValueError:
                    relationship_type = RelationshipType.RELATED_TO

                relationships.append(
                    Relationship(
                        id=row.get("id") or str(uuid4()),
                        source_id=row.get("source_id", ""),
                        target_id=row.get("target_id", ""),
                        relationship_type=relationship_type,
                        weight=1.0,
                    )
                )

            log.debug("Listed relationships", count=len(relationships))
            return relationships

        except Exception as e:
            log.warning("Failed to list relationships", error=str(e))
            return []
