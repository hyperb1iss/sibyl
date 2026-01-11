"""Relationship management using Graphiti's EntityEdge API.

This module provides relationship operations between entities in the knowledge graph
using Graphiti's native edge system rather than custom Cypher queries.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import structlog
from graphiti_core.edges import EntityEdge

from sibyl_core.errors import ConventionsMCPError
from sibyl_core.graph.client import GraphClient
from sibyl_core.models.entities import Entity, Relationship, RelationshipType

log = structlog.get_logger()

# Whitelist of valid relationship types for Cypher queries
# Cypher doesn't support parameterized relationship types, so we validate against this
VALID_RELATIONSHIP_TYPES = frozenset(rt.value for rt in RelationshipType)


def _validate_relationship_type(rel_type: str) -> str:
    """Validate relationship type is in the allowed whitelist.

    Cypher doesn't support parameterized relationship types, so we must
    validate against a strict whitelist to prevent injection.

    Args:
        rel_type: The relationship type string to validate.

    Returns:
        The validated relationship type.

    Raises:
        ValueError: If the relationship type is not in the whitelist.
    """
    if rel_type not in VALID_RELATIONSHIP_TYPES:
        raise ValueError(
            f"Invalid relationship type: {rel_type!r}. "
            f"Must be one of: {sorted(VALID_RELATIONSHIP_TYPES)}"
        )
    return rel_type


def _sanitize_pagination(value: int, max_value: int = 10000) -> int:
    """Sanitize pagination parameters to prevent injection.

    Args:
        value: The pagination value (offset or limit).
        max_value: Maximum allowed value.

    Returns:
        Sanitized integer within bounds.
    """
    if not isinstance(value, int):
        raise TypeError(f"Pagination value must be int, got {type(value).__name__}")
    return max(0, min(value, max_value))


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

            # Validate relationship type against whitelist to prevent Cypher injection
            # (Cypher doesn't support parameterized relationship types)
            rel_type = _validate_relationship_type(relationship.relationship_type.value)

            query = f"""
                MATCH (source {{uuid: $source_uuid}})
                MATCH (target {{uuid: $target_uuid}})
                MERGE (source)-[r:{rel_type} {{uuid: $edge_uuid}}]->(target)
                SET r.name = $name,
                    r.group_id = $group_id,
                    r.source_node_uuid = $source_uuid,
                    r.target_node_uuid = $target_uuid,
                    r.created_at = $created_at,
                    r.weight = $weight,
                    r.fact = $fact
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
                    fact=edge.fact,
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
            # Build direction-aware query
            if direction == "outgoing":
                match_clause = "MATCH (n {uuid: $entity_id})-[r]->(m)"
            elif direction == "incoming":
                match_clause = "MATCH (n {uuid: $entity_id})<-[r]-(m)"
            else:  # both
                match_clause = "MATCH (n {uuid: $entity_id})-[r]-(m)"

            # Handle both edge property schemas:
            # - Our edges: source_node_uuid, target_node_uuid
            # - Graphiti edges: source_uuid, target_uuid
            # Fallback to node UUIDs if edge properties are missing
            query = f"""
                {match_clause}
                WHERE r.group_id = $group_id
                RETURN r.uuid as uuid,
                       COALESCE(r.name, type(r)) as name,
                       COALESCE(r.source_node_uuid, r.source_uuid, n.uuid) as source_id,
                       COALESCE(r.target_node_uuid, r.target_uuid, m.uuid) as target_id,
                       COALESCE(r.weight, 1.0) as weight
            """

            result = await self._driver.execute_query(
                query,
                entity_id=entity_id,
                group_id=self._group_id,
            )
            rows = self._client.normalize_result(result)

            relationships = []
            type_values = [t.value for t in relationship_types] if relationship_types else None

            for row in rows:
                # Handle both dict (FalkorDB) and list results
                if isinstance(row, dict):
                    rel_name = row.get("name")
                    rel_uuid = row.get("uuid")
                    source_id = row.get("source_id")
                    target_id = row.get("target_id")
                    weight = row.get("weight", 1.0)
                else:
                    rel_name = row[1]
                    rel_uuid = row[0]
                    source_id = row[2]
                    target_id = row[3]
                    weight = row[4] if len(row) > 4 else 1.0

                # Skip invalid relationships with missing source/target
                if not source_id or not target_id:
                    continue

                # Filter by type if specified
                if type_values and rel_name not in type_values:
                    continue

                try:
                    rel_type = (
                        RelationshipType(rel_name) if rel_name else RelationshipType.RELATED_TO
                    )
                except ValueError:
                    rel_type = RelationshipType.RELATED_TO

                relationships.append(
                    Relationship(
                        id=str(rel_uuid) if rel_uuid else "",
                        relationship_type=rel_type,
                        source_id=str(source_id),
                        target_id=str(target_id),
                        weight=float(weight) if weight else 1.0,
                        metadata={},
                    )
                )

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
        from sibyl_core.graph.entities import EntityManager

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

            # Collect all related entity IDs (avoiding N+1 query problem)
            relationships = relationships[:limit]
            other_ids = [
                rel.target_id if rel.source_id == entity_id else rel.source_id
                for rel in relationships
            ]

            if not other_ids:
                return []

            # Batch fetch all related entities in a single query
            from graphiti_core.nodes import EntityNode

            entity_manager = EntityManager(self._client, group_id=self._group_id)
            query = """
                MATCH (n)
                WHERE n.uuid IN $ids
                RETURN n
            """
            rows = await self._client.execute_read_org(
                query, organization_id=self._group_id, ids=other_ids
            )

            # Build entity lookup map - convert FalkorDB nodes to EntityNode then to Entity
            entities_by_id: dict[str, Entity] = {}
            for row in rows:
                fdb_node = row.get("n")
                if fdb_node and hasattr(fdb_node, "properties"):
                    try:
                        props = fdb_node.properties
                        # Convert FalkorDB node to Graphiti EntityNode
                        node = EntityNode(
                            uuid=props.get("uuid", ""),
                            name=props.get("name", ""),
                            group_id=props.get("group_id", self._group_id),
                            labels=list(fdb_node.labels) if hasattr(fdb_node, "labels") else [],
                            created_at=props.get("created_at"),
                            name_embedding=props.get("name_embedding"),
                            summary=props.get("summary", ""),
                            attributes={
                                k: v
                                for k, v in props.items()
                                if k
                                not in (
                                    "uuid",
                                    "name",
                                    "group_id",
                                    "labels",
                                    "created_at",
                                    "name_embedding",
                                    "summary",
                                )
                            },
                        )
                        entity = entity_manager.node_to_entity(node)
                        entities_by_id[entity.id] = entity
                    except Exception:
                        continue

            # Match entities back to relationships
            results: list[tuple[Entity, Relationship]] = []
            for rel, other_id in zip(relationships, other_ids, strict=False):
                if other_id in entities_by_id:
                    results.append((entities_by_id[other_id], rel))

            log.debug("Retrieved related entities", count=len(results), batch_size=len(other_ids))
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
            # Use direct Cypher to delete by UUID (consistent with create/get)
            query = """
                MATCH ()-[r {uuid: $relationship_id}]-()
                WHERE r.group_id = $group_id
                DELETE r
                RETURN count(r) as deleted
            """

            async with self._client.write_lock:
                result = await self._driver.execute_query(
                    query,
                    relationship_id=relationship_id,
                    group_id=self._group_id,
                )

            rows = self._client.normalize_result(result)
            deleted = (
                rows[0]["deleted"]
                if rows and isinstance(rows[0], dict)
                else (rows[0][0] if rows else 0)
            )

            if deleted > 0:
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
            # Use direct Cypher to delete all relationships for entity
            query = """
                MATCH (n {uuid: $entity_id})-[r]-()
                WHERE r.group_id = $group_id
                DELETE r
                RETURN count(r) as deleted
            """

            async with self._client.write_lock:
                result = await self._driver.execute_query(
                    query,
                    entity_id=entity_id,
                    group_id=self._group_id,
                )

            rows = self._client.normalize_result(result)
            deleted = (
                rows[0]["deleted"]
                if rows and isinstance(rows[0], dict)
                else (rows[0][0] if rows else 0)
            )

            log.info("Deleted relationships for entity", entity_id=entity_id, count=deleted)
            return deleted

        except Exception as e:
            log.warning("Failed to delete relationships for entity", error=str(e))
            return 0

    async def list_all(
        self,
        relationship_types: list[RelationshipType] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Relationship]:
        """List all relationships in the graph.

        Args:
            relationship_types: Optional list of relationship types to filter by.
            limit: Maximum number to return.
            offset: Offset for pagination.

        Returns:
            List of relationships.
        """
        log.debug("Listing all relationships", limit=limit, offset=offset, types=relationship_types)

        try:
            # Sanitize pagination to prevent injection
            # Note: max_value for limit increased to support backup exports
            safe_offset = _sanitize_pagination(offset, max_value=100000)
            safe_limit = _sanitize_pagination(limit, max_value=100000)

            # Direct Cypher query to get edges (Graphiti's EntityEdge.get_by_group_ids
            # expects ENTITY_EDGE label but our edges have RELATES_TO, MENTIONS, etc.)
            type_filter = ""
            if relationship_types:
                # Validate each type against whitelist to prevent Cypher injection
                validated_types = [
                    _validate_relationship_type(rt.value) for rt in relationship_types
                ]
                # Build safe IN clause with quoted strings
                quoted_types = ", ".join(f"'{t}'" for t in validated_types)
                type_filter = f"AND type(r) IN [{quoted_types}]"

            query = f"""
                MATCH (source)-[r]->(target)
                WHERE r.group_id = $group_id
                {type_filter}
                RETURN r.uuid as id,
                       source.uuid as source_id,
                       target.uuid as target_id,
                       type(r) as rel_type,
                       r.created_at as created_at
                SKIP {safe_offset}
                LIMIT {safe_limit}
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
