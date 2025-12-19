"""Relationship management for the knowledge graph."""

import json
from typing import TYPE_CHECKING, Any, NoReturn

import structlog

from sibyl.errors import ConventionsMCPError
from sibyl.models.entities import Entity, Relationship, RelationshipType

if TYPE_CHECKING:
    from sibyl.graph.client import GraphClient

log = structlog.get_logger()


class RelationshipManager:
    """Manages relationship operations in the knowledge graph."""

    def __init__(self, client: "GraphClient") -> None:
        """Initialize relationship manager with graph client."""
        self._client = client

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
            type=relationship.relationship_type,
            source=relationship.source_id,
            target=relationship.target_id,
        )

        try:
            # Serialize metadata as JSON string (FalkorDB doesn't support inline props)
            metadata_json = json.dumps(relationship.metadata) if relationship.metadata else "{}"

            # Create the edge using explicit property assignment
            # FalkorDB doesn't support $props for inline relationship properties
            query = """
            MATCH (source), (target)
            WHERE source.uuid = $source_id AND target.uuid = $target_id
            CREATE (source)-[r:RELATIONSHIP {
                relationship_type: $rel_type,
                weight: $weight,
                created_at: $created_at,
                metadata: $metadata
            }]->(target)
            RETURN id(r) as edge_id
            """

            result = await self._client.client.driver.execute_query(
                query,
                source_id=relationship.source_id,
                target_id=relationship.target_id,
                rel_type=relationship.relationship_type.value,
                weight=relationship.weight,
                created_at=relationship.created_at.isoformat(),
                metadata=metadata_json,
            )

            # Check if relationship creation succeeded
            if result and len(result) > 0:
                log.info(
                    "Relationship created successfully",
                    relationship_id=relationship.id,
                    type=relationship.relationship_type,
                )
                return relationship.id

            # Handle failure case
            self._raise_creation_error(relationship)

        except ConventionsMCPError:
            raise
        except Exception as e:
            log.exception(
                "Failed to create relationship",
                error=str(e),
                source=relationship.source_id,
                target=relationship.target_id,
            )
            raise ConventionsMCPError(
                f"Failed to create relationship: {e}",
                details={"error": str(e)},
            ) from e

    def _raise_creation_error(self, relationship: Relationship) -> NoReturn:
        """Raise error for failed relationship creation.

        Args:
            relationship: The relationship that failed to create.

        Raises:
            ConventionsMCPError: Always raises with details.
        """
        error_msg = "Failed to create relationship - no result returned"
        log.error(
            error_msg,
            source=relationship.source_id,
            target=relationship.target_id,
        )
        raise ConventionsMCPError(
            error_msg,
            details={
                "source_id": relationship.source_id,
                "target_id": relationship.target_id,
                "type": relationship.relationship_type,
            },
        )

    async def get_for_entity(
        self,
        entity_id: str,
        relationship_types: list[RelationshipType] | None = None,
        direction: str = "both",
    ) -> list[Relationship]:
        """Get all relationships for an entity.

        Args:
            entity_id: The entity's unique identifier.
            relationship_types: Optional filter by relationship types.
            direction: "outgoing", "incoming", or "both".

        Returns:
            List of relationships.

        Raises:
            ConventionsMCPError: If query fails.
        """
        log.debug(
            "Fetching relationships",
            entity_id=entity_id,
            types=relationship_types,
            direction=direction,
        )

        try:
            # Build the Cypher query based on direction
            if direction == "outgoing":
                match_pattern = "(source)-[r:RELATIONSHIP]->(target)"
                where_clause = "source.uuid = $entity_id"
            elif direction == "incoming":
                match_pattern = "(source)-[r:RELATIONSHIP]->(target)"
                where_clause = "target.uuid = $entity_id"
            else:  # both
                match_pattern = "(n1)-[r:RELATIONSHIP]-(n2)"
                where_clause = "n1.uuid = $entity_id OR n2.uuid = $entity_id"

            # Add relationship type filter if specified
            type_filter = ""
            if relationship_types:
                types_str = ", ".join(f"'{t.value}'" for t in relationship_types)
                type_filter = f" AND r.relationship_type IN [{types_str}]"

            query = f"""
            MATCH {match_pattern}
            WHERE {where_clause}{type_filter}
            RETURN r, id(r) as rel_id,
                   CASE
                     WHEN source.uuid = $entity_id THEN target.uuid
                     WHEN target.uuid = $entity_id THEN source.uuid
                     ELSE n2.uuid
                   END as other_id,
                   CASE
                     WHEN source.uuid = $entity_id THEN source.uuid
                     WHEN n1.uuid = $entity_id THEN n1.uuid
                     ELSE source.uuid
                   END as source_id
            """

            result = await self._client.client.driver.execute_query(query, entity_id=entity_id)

            relationships = []
            for record in result:
                rel_props = record.get("r", {})
                rel_id = str(record.get("rel_id", ""))
                source_id = record.get("source_id", "")
                target_id = record.get("other_id", "")

                # Parse the relationship type
                rel_type_str = rel_props.get("relationship_type", "RELATED_TO")
                rel_type = RelationshipType(rel_type_str)

                # Extract metadata (stored as JSON string)
                metadata_raw = rel_props.get("metadata", "{}")
                try:
                    metadata = json.loads(metadata_raw) if isinstance(metadata_raw, str) else metadata_raw or {}
                except json.JSONDecodeError:
                    metadata = {}

                relationships.append(
                    Relationship(
                        id=rel_id,
                        relationship_type=rel_type,
                        source_id=source_id,
                        target_id=target_id,
                        weight=float(rel_props.get("weight", 1.0)),
                        metadata=metadata,
                    )
                )

            log.debug(
                "Retrieved relationships",
                entity_id=entity_id,
                count=len(relationships),
            )
            return relationships

        except Exception as e:
            log.exception("Failed to fetch relationships", error=str(e), entity_id=entity_id)
            raise ConventionsMCPError(
                f"Failed to fetch relationships: {e}",
                details={"entity_id": entity_id, "error": str(e)},
            ) from e

    async def get_related_entities(
        self,
        entity_id: str,
        relationship_types: list[RelationshipType] | None = None,
        depth: int = 1,
        limit: int = 20,
    ) -> list[tuple[Entity, Relationship]]:
        """Get entities related to a given entity.

        Args:
            entity_id: The entity's unique identifier.
            relationship_types: Optional filter by relationship types.
            depth: How many hops to traverse (1-3).
            limit: Maximum results to return.

        Returns:
            List of (related_entity, relationship) tuples.

        Raises:
            ConventionsMCPError: If graph traversal fails.
        """
        log.info(
            "Finding related entities",
            entity_id=entity_id,
            types=relationship_types,
            depth=depth,
        )

        # Clamp depth to reasonable bounds
        depth = max(1, min(3, depth))

        try:
            # Build relationship type filter
            type_filter = ""
            if relationship_types:
                types_str = ", ".join(f"'{t.value}'" for t in relationship_types)
                type_filter = (
                    f" AND ALL(r IN relationships(path) WHERE r.relationship_type IN [{types_str}])"
                )

            # Build variable-length path query
            # Use shortestPath to avoid duplicate paths
            query = f"""
            MATCH path = (start)-[*1..{depth}]-(related)
            WHERE start.uuid = $entity_id{type_filter}
            WITH related, relationships(path) as rels, length(path) as hops
            WHERE related.uuid <> $entity_id
            RETURN DISTINCT related, rels[0] as first_rel, hops
            ORDER BY hops, related.name
            LIMIT $limit
            """

            result = await self._client.client.driver.execute_query(
                query, entity_id=entity_id, limit=limit
            )

            related_entities = []
            for record in result:
                entity_data = record.get("related", {})
                rel_data = record.get("first_rel", {})

                # Build Entity from node properties
                entity = self._build_entity_from_node(entity_data)

                # Build Relationship from edge properties
                rel_type_str = rel_data.get("relationship_type", "RELATED_TO")
                rel_type = RelationshipType(rel_type_str)

                # Extract metadata (stored as JSON string)
                metadata_raw = rel_data.get("metadata", "{}")
                try:
                    metadata = json.loads(metadata_raw) if isinstance(metadata_raw, str) else metadata_raw or {}
                except json.JSONDecodeError:
                    metadata = {}

                # Build the relationship
                relationship = Relationship(
                    id=str(record.get("first_rel_id", "")),
                    relationship_type=rel_type,
                    source_id=entity_id,  # Simplified: assume source is the start entity
                    target_id=entity_data.get("uuid", ""),
                    weight=float(rel_data.get("weight", 1.0)),
                    metadata=metadata,
                )

                related_entities.append((entity, relationship))

            log.info(
                "Found related entities",
                entity_id=entity_id,
                count=len(related_entities),
            )
            return related_entities

        except Exception as e:
            log.exception("Failed to find related entities", error=str(e), entity_id=entity_id)
            raise ConventionsMCPError(
                f"Failed to find related entities: {e}",
                details={"entity_id": entity_id, "error": str(e)},
            ) from e

    async def delete(self, relationship_id: str) -> None:
        """Delete a relationship from the graph.

        Args:
            relationship_id: The relationship's unique identifier.

        Raises:
            ConventionsMCPError: If deletion fails.
        """
        log.info("Deleting relationship", relationship_id=relationship_id)

        try:
            # Delete relationship by internal ID
            query = """
            MATCH ()-[r:RELATIONSHIP]->()
            WHERE id(r) = $rel_id
            DELETE r
            RETURN count(r) as deleted_count
            """

            result = await self._client.client.driver.execute_query(
                query, rel_id=int(relationship_id)
            )

            if result and len(result) > 0:
                deleted_count = result[0].get("deleted_count", 0)
                if deleted_count > 0:
                    log.info(
                        "Relationship deleted successfully",
                        relationship_id=relationship_id,
                    )
                    return

            log.warning("Relationship not found for deletion", relationship_id=relationship_id)

        except Exception as e:
            log.exception(
                "Failed to delete relationship",
                error=str(e),
                relationship_id=relationship_id,
            )
            raise ConventionsMCPError(
                f"Failed to delete relationship: {e}",
                details={"relationship_id": relationship_id, "error": str(e)},
            ) from e

    def _build_entity_from_node(self, node_data: dict[str, Any]) -> Entity:
        """Build an Entity model from graph node data.

        Args:
            node_data: Node properties from the graph.

        Returns:
            Entity instance.
        """
        from datetime import UTC, datetime

        from sibyl.models.entities import EntityType

        # Parse entity type
        entity_type_str = node_data.get("entity_type", "topic")
        try:
            entity_type = EntityType(entity_type_str)
        except ValueError:
            entity_type = EntityType.TOPIC

        # Build base entity
        return Entity(
            id=node_data.get("uuid", ""),
            entity_type=entity_type,
            name=node_data.get("name", ""),
            description=node_data.get("description", ""),
            content=node_data.get("content", ""),
            metadata=node_data.get("metadata", {}),
            created_at=datetime.fromisoformat(
                node_data.get("created_at", datetime.now(UTC).isoformat())
            ),
            updated_at=datetime.fromisoformat(
                node_data.get("updated_at", datetime.now(UTC).isoformat())
            ),
            source_file=node_data.get("source_file"),
        )
