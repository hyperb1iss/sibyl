"""Relationship compatibility surface for the knowledge graph.

Default memory flows use native Surreal relationship managers. This module remains
for named compatibility and admin paths that still use the legacy manager surface.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import structlog

from sibyl_core.errors import GraphError
from sibyl_core.graph.client import GraphClient
from sibyl_core.models.entities import Entity, Relationship, RelationshipType

log = structlog.get_logger()
_MISSING = object()

# Whitelist of valid relationship types for Cypher queries
# Cypher doesn't support parameterized relationship types, so we validate against this
VALID_RELATIONSHIP_TYPES = frozenset(rt.value for rt in RelationshipType)


@dataclass(slots=True)
class _RelationshipEdge:
    uuid: str
    group_id: str
    source_node_uuid: str
    target_node_uuid: str
    created_at: datetime
    name: str
    fact: str
    fact_embedding: list[float] | None = None
    episodes: list[str] = field(default_factory=list)
    expired_at: datetime | None = None
    valid_at: datetime | None = None
    invalid_at: datetime | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class _EpisodeMentionEdge:
    uuid: str
    group_id: str
    source_node_uuid: str
    target_node_uuid: str
    created_at: datetime


def _is_episode_id(node_id: str) -> bool:
    return node_id.startswith("episode")


def _is_edge_not_found_error(error: Exception) -> bool:
    return type(error).__name__ == "EdgeNotFoundError"


def _declared_driver_attr(driver: object, attr: str) -> object | None:
    try:
        attrs = vars(driver)
    except TypeError:
        return None

    value = attrs.get(attr, _MISSING)
    return None if value is _MISSING else value


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
    """Manages relationship operations for the knowledge graph."""

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
        self._driver = client.get_org_driver(group_id)

    def _surreal_entity_edge_ops(self):
        ops = _declared_driver_attr(self._driver, "entity_edge_ops")
        if ops is not None:
            return ops

        try:
            from sibyl_core.backends.surreal import SurrealDriver
        except ImportError:
            return None

        if isinstance(self._driver, SurrealDriver):
            return self._driver.entity_edge_ops
        return None

    def _surreal_episodic_edge_ops(self):
        ops = _declared_driver_attr(self._driver, "episodic_edge_ops")
        if ops is not None:
            return ops

        try:
            from sibyl_core.backends.surreal import SurrealDriver
        except ImportError:
            return None

        if isinstance(self._driver, SurrealDriver):
            return self._driver.episodic_edge_ops
        return None

    def _surreal_entity_node_ops(self):
        ops = _declared_driver_attr(self._driver, "entity_node_ops")
        if ops is not None:
            return ops

        try:
            from sibyl_core.backends.surreal import SurrealDriver
        except ImportError:
            return None

        if isinstance(self._driver, SurrealDriver):
            return self._driver.entity_node_ops
        return None

    def _is_surreal_driver(self) -> bool:
        try:
            from sibyl_core.backends.surreal import SurrealDriver
        except ImportError:
            return False

        return isinstance(self._driver, SurrealDriver)

    def _assert_legacy_fallback_allowed(self, operation: str) -> None:
        if self._is_surreal_driver():
            raise RuntimeError(
                f"SurrealDB relationship {operation} requires native edge operations"
            )

    def _raise_if_surreal_fallback_guard(self, error: Exception) -> None:
        if (
            self._is_surreal_driver()
            and isinstance(error, RuntimeError)
            and "requires native edge operations" in str(error)
        ):
            raise error

    def _to_graphiti_edge(self, relationship: Relationship) -> _RelationshipEdge:
        """Convert our Relationship model to an edge payload.

        Args:
            relationship: Our relationship model

        Returns:
            Edge payload accepted by the configured graph driver.
        """
        return _RelationshipEdge(
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

    async def _save_as_episode_mention(self, relationship: Relationship) -> str:
        episodic_edge_ops = self._surreal_episodic_edge_ops()
        if episodic_edge_ops is None:
            raise RuntimeError("SurrealDB episode mentions require native episodic edge operations")

        edge = _EpisodeMentionEdge(
            uuid=relationship.id or str(uuid4()),
            group_id=self._group_id,
            source_node_uuid=relationship.source_id,
            target_node_uuid=relationship.target_id,
            created_at=relationship.created_at,
        )
        await episodic_edge_ops.save(self._driver, edge)
        return edge.uuid

    async def _try_save_as_episode_mention(self, relationship: Relationship) -> bool:
        try:
            await self._save_as_episode_mention(relationship)
        except ValueError:
            return False
        return True

    async def _get_existing_edges_between_nodes(
        self,
        source_id: str,
        target_id: str,
    ) -> list[_RelationshipEdge]:
        query = """
            MATCH (source {uuid: $source_uuid})-[r]->(target {uuid: $target_uuid})
            WHERE r.group_id = $group_id
            RETURN r.uuid as uuid,
                   COALESCE(r.name, type(r)) as name,
                   COALESCE(r.source_node_uuid, r.source_uuid, source.uuid) as source_id,
                   COALESCE(r.target_node_uuid, r.target_uuid, target.uuid) as target_id,
                   COALESCE(r.weight, 1.0) as weight,
                   COALESCE(r.fact, r.name, type(r)) as fact,
                   properties(r) as properties
        """
        result = await self._driver.execute_query(
            query,
            source_uuid=source_id,
            target_uuid=target_id,
            group_id=self._group_id,
        )
        rows = self._client.normalize_result(result)
        edges: list[_RelationshipEdge] = []
        for row in rows:
            if isinstance(row, dict):
                edge_uuid = row.get("uuid")
                name = row.get("name")
                source_node_uuid = row.get("source_id")
                target_node_uuid = row.get("target_id")
                weight = row.get("weight", 1.0)
                fact = row.get("fact")
                properties = row.get("properties")
            else:
                edge_uuid = row[0] if len(row) > 0 else None
                name = row[1] if len(row) > 1 else None
                source_node_uuid = row[2] if len(row) > 2 else None
                target_node_uuid = row[3] if len(row) > 3 else None
                weight = row[4] if len(row) > 4 else 1.0
                fact = row[5] if len(row) > 5 else None
                properties = row[6] if len(row) > 6 else None

            if not edge_uuid or not name or not source_node_uuid or not target_node_uuid:
                continue

            attributes = (
                {
                    key: value
                    for key, value in properties.items()
                    if key
                    not in {
                        "uuid",
                        "name",
                        "group_id",
                        "source_node_uuid",
                        "source_uuid",
                        "target_node_uuid",
                        "target_uuid",
                        "created_at",
                        "fact",
                    }
                }
                if isinstance(properties, dict)
                else {}
            )
            attributes.setdefault("weight", weight)
            edges.append(
                _RelationshipEdge(
                    uuid=str(edge_uuid),
                    group_id=self._group_id,
                    source_node_uuid=str(source_node_uuid),
                    target_node_uuid=str(target_node_uuid),
                    created_at=datetime.now(UTC),
                    name=str(name),
                    fact=str(fact) if fact else f"{name} relationship",
                    valid_at=datetime.now(UTC),
                    attributes=attributes,
                )
            )
        return edges

    def _from_graphiti_edge(self, edge: Any) -> Relationship:
        """Convert an edge payload to our Relationship model.

        Args:
            edge: Edge payload from the configured graph driver.

        Returns:
            Our Relationship model
        """
        # Parse relationship type from edge name
        try:
            rel_type = RelationshipType(edge.name)
        except ValueError:
            rel_type = RelationshipType.RELATED_TO

        # Extract our metadata from attributes
        attributes = dict(edge.attributes or {})
        weight = float(attributes.pop("weight", 1.0))

        return Relationship(
            id=edge.uuid,
            relationship_type=rel_type,
            source_id=edge.source_node_uuid,
            target_id=edge.target_node_uuid,
            weight=weight,
            metadata=attributes,
        )

    def _from_episodic_edge(self, edge: Any) -> Relationship:
        return Relationship(
            id=edge.uuid,
            relationship_type=RelationshipType.RELATED_TO,
            source_id=edge.source_node_uuid,
            target_id=edge.target_node_uuid,
        )

    def _build_bulk_relationship_row(self, relationship: Relationship) -> dict[str, object]:
        """Build the property map used for batched relationship upserts."""
        edge = self._to_graphiti_edge(relationship)
        return {
            "uuid": edge.uuid,
            "source_uuid": relationship.source_id,
            "target_uuid": relationship.target_id,
            "name": relationship.relationship_type.value,
            "group_id": self._group_id,
            "created_at": edge.created_at.isoformat(),
            "weight": relationship.weight,
            "fact": edge.fact,
        }

    def _dedupe_relationships_for_create(
        self,
        relationships: list[Relationship],
    ) -> list[Relationship]:
        """Preserve the first edge for each source/type/target triplet.

        create() already treats duplicate triplets as idempotent in the active
        runtime, so batch paths need the same behavior before they bypass that
        duplicate check.
        """
        unique: dict[tuple[str, str, str], Relationship] = {}
        for relationship in relationships:
            key = (
                relationship.source_id,
                relationship.relationship_type.value,
                relationship.target_id,
            )
            unique.setdefault(key, relationship)
        return list(unique.values())

    def _dedupe_relationships_for_persistence(
        self,
        relationships: list[Relationship],
    ) -> list[Relationship]:
        """Match the active runtime's effective persistence semantics."""

        triplet_deduped = self._dedupe_relationships_for_create(relationships)
        deduped_by_id: dict[str, Relationship] = {}
        passthrough: list[Relationship] = []

        for relationship in triplet_deduped:
            if not relationship.id:
                passthrough.append(relationship)
                continue
            deduped_by_id[relationship.id] = relationship

        return [*passthrough, *deduped_by_id.values()]

    async def create(self, relationship: Relationship) -> str:
        """Create a new relationship between entities.

        Args:
            relationship: The relationship to create.

        Returns:
            The ID of the created relationship.

        Raises:
            GraphError: If relationship creation fails.
        """
        log.info(
            "Creating relationship",
            type=relationship.relationship_type.value,
            source=relationship.source_id,
            target=relationship.target_id,
        )

        try:
            surreal_edge_ops = self._surreal_entity_edge_ops()
            if surreal_edge_ops is not None:
                if _is_episode_id(relationship.source_id):
                    episodic_edge_ops = self._surreal_episodic_edge_ops()
                    if episodic_edge_ops is not None:
                        existing = await episodic_edge_ops.get_between_nodes(
                            self._driver,
                            relationship.source_id,
                            relationship.target_id,
                            group_ids=[self._group_id],
                            limit=1000,
                        )
                        if existing:
                            log.info(
                                "Episode mention already exists; skipping duplicate",
                                relationship_id=existing[0].uuid,
                            )
                            return existing[0].uuid

                    edge_id = await self._save_as_episode_mention(relationship)
                    log.info("Created episode mention", relationship_id=edge_id)
                    return edge_id

                existing = await surreal_edge_ops.get_between_nodes(
                    self._driver,
                    relationship.source_id,
                    relationship.target_id,
                    group_ids=[self._group_id],
                    limit=1000,
                )
                for edge in existing:
                    if edge.name == relationship.relationship_type.value:
                        log.info(
                            "Relationship already exists; skipping duplicate",
                            relationship_id=edge.uuid,
                        )
                        return edge.uuid

                edge = self._to_graphiti_edge(relationship)
                try:
                    await surreal_edge_ops.save(self._driver, edge)
                except ValueError as save_error:
                    if not await self._try_save_as_episode_mention(relationship):
                        raise save_error
                log.info("Created relationship", relationship_id=edge.uuid)
                return edge.uuid

            self._assert_legacy_fallback_allowed("create")

            existing = await self._get_existing_edges_between_nodes(
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

            # Create edge via direct Cypher; legacy edge saves miss episodic UUIDs.
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
            raise GraphError(
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
        relationships = self._dedupe_relationships_for_persistence(relationships)
        log.info("Creating relationships in bulk", count=len(relationships))

        created = 0
        failed = 0

        surreal_edge_ops = self._surreal_entity_edge_ops()
        if surreal_edge_ops is not None:
            try:
                node_ids = sorted(
                    {
                        node_id
                        for relationship in relationships
                        for node_id in (relationship.source_id, relationship.target_id)
                    }
                )
                existing_edges = await surreal_edge_ops.get_by_node_uuids(
                    self._driver,
                    node_ids,
                    group_ids=[self._group_id],
                )
                existing_keys = {
                    (edge.source_node_uuid, edge.name, edge.target_node_uuid)
                    for edge in existing_edges
                    if edge.group_id == self._group_id
                }
                edges = []
                skipped = 0
                for relationship in relationships:
                    key = (
                        relationship.source_id,
                        relationship.relationship_type.value,
                        relationship.target_id,
                    )
                    if key in existing_keys:
                        skipped += 1
                        continue
                    existing_keys.add(key)
                    edges.append(self._to_graphiti_edge(relationship))
                if edges:
                    await surreal_edge_ops.save_bulk(self._driver, edges)
                created += skipped + len(edges)
            except Exception as e:
                log.warning(
                    "bulk relationship save failed, falling back to per-relationship create",
                    batch_size=len(relationships),
                    error=str(e),
                )
                for relationship in relationships:
                    try:
                        await self.create(relationship)
                        created += 1
                    except Exception as item_error:
                        log.warning("Failed to create relationship", error=str(item_error))
                        failed += 1

            log.info("Bulk create complete", created=created, failed=failed)
            return created, failed

        self._assert_legacy_fallback_allowed("create_bulk")

        groups: dict[str, list[Relationship]] = defaultdict(list)
        for relationship in relationships:
            groups[relationship.relationship_type.value].append(relationship)

        for rel_type, group in groups.items():
            batch_query = f"""
                UNWIND $relationships AS rel
                MATCH (source {{uuid: rel.source_uuid}})
                MATCH (target {{uuid: rel.target_uuid}})
                MERGE (source)-[r:{rel_type}]->(target)
                ON CREATE SET
                    r.uuid = rel.uuid,
                    r.name = rel.name,
                    r.group_id = rel.group_id,
                    r.source_node_uuid = rel.source_uuid,
                    r.target_node_uuid = rel.target_uuid,
                    r.created_at = rel.created_at,
                    r.weight = rel.weight,
                    r.fact = rel.fact
                RETURN count(r) AS processed
            """

            try:
                await self._driver.execute_query(
                    batch_query,
                    relationships=[self._build_bulk_relationship_row(rel) for rel in group],
                )
                created += len(group)
            except Exception as e:
                log.warning(
                    "bulk relationship upsert failed, falling back to per-relationship create",
                    relationship_type=rel_type,
                    batch_size=len(group),
                    error=str(e),
                )
                for rel in group:
                    try:
                        await self.create(rel)
                        created += 1
                    except Exception as item_error:
                        log.warning("Failed to create relationship", error=str(item_error))
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
            surreal_edge_ops = self._surreal_entity_edge_ops()
            if surreal_edge_ops is not None:
                edges = await surreal_edge_ops.get_by_node_uuid(
                    self._driver,
                    entity_id,
                    group_ids=[self._group_id],
                    limit=1000,
                )
                relationships = []
                type_values = {t.value for t in relationship_types} if relationship_types else None

                for edge in edges:
                    if edge.group_id != self._group_id:
                        continue
                    if direction == "outgoing" and edge.source_node_uuid != entity_id:
                        continue
                    if direction == "incoming" and edge.target_node_uuid != entity_id:
                        continue
                    if type_values and edge.name not in type_values:
                        continue
                    relationships.append(self._from_graphiti_edge(edge))

                episodic_edge_ops = self._surreal_episodic_edge_ops()
                if episodic_edge_ops is not None and _is_episode_id(entity_id):
                    mention_edges = await episodic_edge_ops.get_by_node_uuid(
                        self._driver,
                        entity_id,
                        group_ids=[self._group_id],
                        limit=1000,
                    )
                    include_mentions = (
                        not type_values or RelationshipType.RELATED_TO.value in type_values
                    )
                    if direction != "incoming" and include_mentions:
                        for edge in mention_edges:
                            if edge.group_id == self._group_id:
                                relationships.append(self._from_episodic_edge(edge))

                log.debug(
                    "Retrieved relationships via Surreal edge ops",
                    entity_id=entity_id,
                    count=len(relationships),
                )
                return relationships

            self._assert_legacy_fallback_allowed("get_for_entity")

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
                       COALESCE(r.weight, 1.0) as weight,
                       properties(r) as properties
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
                    properties = row.get("properties")
                else:
                    rel_name = row[1]
                    rel_uuid = row[0]
                    source_id = row[2]
                    target_id = row[3]
                    weight = row[4] if len(row) > 4 else 1.0
                    properties = row[5] if len(row) > 5 else None

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

                metadata = (
                    {
                        key: value
                        for key, value in properties.items()
                        if key
                        not in {
                            "uuid",
                            "name",
                            "group_id",
                            "source_node_uuid",
                            "source_uuid",
                            "target_node_uuid",
                            "target_uuid",
                            "created_at",
                            "weight",
                            "fact",
                        }
                    }
                    if isinstance(properties, dict)
                    else {}
                )

                relationships.append(
                    Relationship(
                        id=str(rel_uuid) if rel_uuid else "",
                        relationship_type=rel_type,
                        source_id=str(source_id),
                        target_id=str(target_id),
                        weight=float(weight) if weight else 1.0,
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
            self._raise_if_surreal_fallback_guard(e)
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

            entity_manager = EntityManager(self._client, group_id=self._group_id)

            # Match entities back to relationships
            results: list[tuple[Entity, Relationship]] = []
            for rel, other_id in zip(relationships, other_ids, strict=False):
                try:
                    entity = await entity_manager.get(other_id)
                except Exception:
                    continue
                results.append((entity, rel))

            log.debug("Retrieved related entities", count=len(results), batch_size=len(other_ids))
            return results

        except Exception as e:
            self._raise_if_surreal_fallback_guard(e)
            log.warning(
                "Failed to get related entities, returning empty list",
                error=str(e),
                entity_id=entity_id,
            )
            return []

    async def get_related_entities_batch(
        self,
        entity_ids: list[str],
        relationship_types: list[RelationshipType] | None = None,
        limit_per_entity: int = 50,
    ) -> dict[str, list[tuple[Entity, Relationship]]]:
        """Get one-hop related entities for multiple seeds."""
        from sibyl_core.graph.entities import EntityManager

        seed_ids = list(dict.fromkeys(entity_id for entity_id in entity_ids if entity_id))
        if not seed_ids:
            return {}

        try:
            surreal_edge_ops = self._surreal_entity_edge_ops()
            if surreal_edge_ops is None:
                results: dict[str, list[tuple[Entity, Relationship]]] = {}
                for entity_id in seed_ids:
                    results[entity_id] = await self.get_related_entities(
                        entity_id,
                        relationship_types=relationship_types,
                        max_depth=1,
                        limit=limit_per_entity,
                    )
                return results

            batch_limit = max(limit_per_entity, 1) * len(seed_ids)
            edges = await surreal_edge_ops.get_by_node_uuids(
                self._driver,
                seed_ids,
                group_ids=[self._group_id],
                limit=batch_limit,
            )

            type_values = {t.value for t in relationship_types} if relationship_types else None
            seed_set = set(seed_ids)
            relationships_by_seed: dict[str, list[Relationship]] = {
                seed_id: [] for seed_id in seed_ids
            }

            for edge in edges:
                if edge.group_id != self._group_id:
                    continue
                if type_values and edge.name not in type_values:
                    continue

                relationship = self._from_graphiti_edge(edge)
                touched_seed_ids = (
                    endpoint
                    for endpoint in (relationship.source_id, relationship.target_id)
                    if endpoint in seed_set
                )
                for seed_id in touched_seed_ids:
                    if len(relationships_by_seed[seed_id]) < limit_per_entity:
                        relationships_by_seed[seed_id].append(relationship)

            related_ids = list(
                dict.fromkeys(
                    relationship.target_id
                    if relationship.source_id == seed_id
                    else relationship.source_id
                    for seed_id, relationships in relationships_by_seed.items()
                    for relationship in relationships
                )
            )
            if not related_ids:
                return {seed_id: [] for seed_id in seed_ids}

            entity_manager = EntityManager(self._client, group_id=self._group_id)
            surreal_node_ops = self._surreal_entity_node_ops()
            entities_by_id: dict[str, Entity] = {}
            if surreal_node_ops is not None:
                nodes = await surreal_node_ops.get_by_uuids(self._driver, related_ids)
                for node in nodes:
                    if node.group_id == self._group_id:
                        entities_by_id[node.uuid] = entity_manager.node_to_entity(node)
            else:
                fetched = await asyncio.gather(
                    *(entity_manager.get(entity_id) for entity_id in related_ids),
                    return_exceptions=True,
                )
                entities_by_id = {
                    entity_id: entity
                    for entity_id, entity in zip(related_ids, fetched, strict=True)
                    if isinstance(entity, Entity)
                }

            results: dict[str, list[tuple[Entity, Relationship]]] = {}
            for seed_id, relationships in relationships_by_seed.items():
                seed_results = []
                for relationship in relationships:
                    other_id = (
                        relationship.target_id
                        if relationship.source_id == seed_id
                        else relationship.source_id
                    )
                    if entity := entities_by_id.get(other_id):
                        seed_results.append((entity, relationship))
                results[seed_id] = seed_results

            log.debug(
                "Retrieved related entities batch",
                seeds=len(seed_ids),
                relationships=sum(len(v) for v in relationships_by_seed.values()),
                entities=len(entities_by_id),
            )
            return results

        except Exception as e:
            self._raise_if_surreal_fallback_guard(e)
            log.warning(
                "Failed to get related entities batch, returning empty lists",
                error=str(e),
                seeds=len(seed_ids),
            )
            return {seed_id: [] for seed_id in seed_ids}

    async def delete(self, relationship_id: str) -> bool:
        """Delete a relationship by ID.

        Args:
            relationship_id: The relationship UUID.

        Returns:
            True if deleted successfully.

        Raises:
            GraphError: If deletion fails.
        """
        log.info("Deleting relationship", relationship_id=relationship_id)

        try:
            surreal_edge_ops = self._surreal_entity_edge_ops()
            if surreal_edge_ops is not None:
                try:
                    edge = await surreal_edge_ops.get_by_uuid(self._driver, relationship_id)
                except Exception as edge_error:
                    if not _is_edge_not_found_error(edge_error):
                        raise
                    log.warning("Relationship not found", relationship_id=relationship_id)
                    return False

                await surreal_edge_ops.delete(self._driver, edge)
                log.info("Deleted relationship", relationship_id=relationship_id)
                return True

            self._assert_legacy_fallback_allowed("delete")

            # Use direct Cypher to delete by UUID (consistent with create/get)
            query = """
                MATCH ()-[r {uuid: $relationship_id}]-()
                WHERE r.group_id = $group_id
                DELETE r
                RETURN count(r) as deleted
            """

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
            raise GraphError(
                f"Failed to delete relationship: {e}",
                details={"relationship_id": relationship_id},
            ) from e

    async def delete_between(
        self,
        source_id: str,
        target_id: str,
        relationship_type: RelationshipType,
    ) -> int:
        """Delete relationships of a specific type between source and target.

        Args:
            source_id: Source entity UUID.
            target_id: Target entity UUID.
            relationship_type: Type of relationship to delete.

        Returns:
            Number of relationships deleted.
        """
        log.info(
            "Deleting relationship between entities",
            source_id=source_id,
            target_id=target_id,
            type=relationship_type.value,
        )

        try:
            rel_type = _validate_relationship_type(relationship_type.value)

            surreal_edge_ops = self._surreal_entity_edge_ops()
            if surreal_edge_ops is not None:
                edges = await surreal_edge_ops.get_between_nodes(
                    self._driver,
                    source_id,
                    target_id,
                    group_ids=[self._group_id],
                    limit=1000,
                )
                to_delete = [edge for edge in edges if edge.name == rel_type]
                for edge in to_delete:
                    await surreal_edge_ops.delete(self._driver, edge)

                log.info(
                    "Deleted relationships between entities",
                    source_id=source_id,
                    target_id=target_id,
                    count=len(to_delete),
                )
                return len(to_delete)

            self._assert_legacy_fallback_allowed("delete_between")

            query = f"""
                MATCH (s {{uuid: $source_id}})-[r:{rel_type}]->(t {{uuid: $target_id}})
                WHERE r.group_id = $group_id
                DELETE r
                RETURN count(r) as deleted
            """

            result = await self._driver.execute_query(
                query,
                source_id=source_id,
                target_id=target_id,
                group_id=self._group_id,
            )

            rows = self._client.normalize_result(result)
            deleted = (
                rows[0]["deleted"]
                if rows and isinstance(rows[0], dict)
                else (rows[0][0] if rows else 0)
            )

            log.info(
                "Deleted relationships between entities",
                source_id=source_id,
                target_id=target_id,
                count=deleted,
            )
            return deleted

        except Exception as e:
            self._raise_if_surreal_fallback_guard(e)
            log.warning(
                "Failed to delete relationships between entities",
                error=str(e),
                source_id=source_id,
                target_id=target_id,
            )
            return 0

    async def delete_for_entity(self, entity_id: str) -> int:
        """Delete all relationships for an entity.

        Args:
            entity_id: The entity UUID.

        Returns:
            Number of relationships deleted.
        """
        log.info("Deleting all relationships for entity", entity_id=entity_id)

        try:
            surreal_edge_ops = self._surreal_entity_edge_ops()
            if surreal_edge_ops is not None:
                edges = await surreal_edge_ops.get_by_node_uuid(
                    self._driver,
                    entity_id,
                    group_ids=[self._group_id],
                    limit=1000,
                )
                scoped_edges = [edge for edge in edges if edge.group_id == self._group_id]
                for edge in scoped_edges:
                    await surreal_edge_ops.delete(self._driver, edge)

                log.info(
                    "Deleted relationships for entity",
                    entity_id=entity_id,
                    count=len(scoped_edges),
                )
                return len(scoped_edges)

            self._assert_legacy_fallback_allowed("delete_for_entity")

            # Use direct Cypher to delete all relationships for entity
            query = """
                MATCH (n {uuid: $entity_id})-[r]-()
                WHERE r.group_id = $group_id
                DELETE r
                RETURN count(r) as deleted
            """

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
            self._raise_if_surreal_fallback_guard(e)
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
            surreal_edge_ops = self._surreal_entity_edge_ops()
            if surreal_edge_ops is not None:
                safe_offset = _sanitize_pagination(offset, max_value=100000)
                safe_limit = _sanitize_pagination(limit, max_value=100000)

                if relationship_types:
                    allowed = {relationship_type.value for relationship_type in relationship_types}
                    page_size = min(max(safe_limit, 100), 1000)
                    skipped = 0
                    selected_edges = []
                    cursor = None
                    while len(selected_edges) < safe_limit:
                        batch = await surreal_edge_ops.get_by_group_ids(
                            self._driver,
                            [self._group_id],
                            limit=page_size,
                            uuid_cursor=cursor,
                        )
                        if not batch:
                            break
                        for edge in batch:
                            if edge.name not in allowed:
                                continue
                            if skipped < safe_offset:
                                skipped += 1
                                continue
                            selected_edges.append(edge)
                            if len(selected_edges) >= safe_limit:
                                break
                        if len(batch) < page_size:
                            break
                        cursor = batch[-1].uuid
                    edges = selected_edges
                else:
                    edges = await surreal_edge_ops.get_by_group_ids(
                        self._driver,
                        [self._group_id],
                        limit=safe_limit,
                        offset=safe_offset,
                    )

                relationships = [self._from_graphiti_edge(edge) for edge in edges]
                log.debug("Listed relationships via Surreal edge ops", count=len(relationships))
                return relationships

            self._assert_legacy_fallback_allowed("list_all")

            # Sanitize pagination to prevent injection
            # Note: max_value for limit increased to support backup exports
            safe_offset = _sanitize_pagination(offset, max_value=100000)
            safe_limit = _sanitize_pagination(limit, max_value=100000)

            # Direct Cypher query to get edges across the native relation types.
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
            self._raise_if_surreal_fallback_guard(e)
            log.warning("Failed to list relationships", error=str(e))
            return []
