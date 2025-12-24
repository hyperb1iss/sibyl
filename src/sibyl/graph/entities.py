"""Entity management for the knowledge graph.

This module provides entity CRUD operations using Graphiti's native node APIs.
All graph operations go through EntityNode/EpisodicNode rather than raw Cypher.
"""

import re
from datetime import UTC, datetime
from typing import Any

import structlog
from graphiti_core.nodes import EntityNode, EpisodicNode
from pydantic import BaseModel

from sibyl.errors import EntityNotFoundError, SearchError
from sibyl.graph.client import GraphClient
from sibyl.models.entities import Entity, EntityType
from sibyl.models.sources import Community, Document, Source
from sibyl.models.tasks import ErrorPattern, Milestone, Project, Task, Team

log = structlog.get_logger()

# RediSearch special characters that need escaping in fulltext queries
_REDISEARCH_SPECIAL_CHARS = re.compile(r"[|&\-@()~$:*\\]")


def sanitize_search_query(query: str) -> str:
    """Escape RediSearch special characters in a query string.

    RediSearch treats |, &, -, @, (), ~, $, :, * as special operators.
    When these appear in document titles or content, they cause syntax errors.
    """
    return _REDISEARCH_SPECIAL_CHARS.sub(r" ", query)


class EntityManager:
    """Manages entity CRUD operations in the knowledge graph."""

    def __init__(self, client: "GraphClient", *, group_id: str) -> None:
        """Initialize entity manager with graph client.

        Creates a cloned driver targeting the org-specific graph for multi-tenancy.
        FalkorDB supports multiple isolated graphs within a single database instance.

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
        # Clone the driver to use the org-specific graph (group_id as database name)
        # This enables multi-tenancy: each org has its own isolated graph
        self._driver = client.client.driver.clone(group_id)

    async def create(self, entity: Entity) -> str:
        """Create a new entity in the graph.

        Args:
            entity: The entity to create.

        Returns:
            The ID of the created entity.
        """
        log.info("Creating entity", entity_type=entity.entity_type, name=entity.name)

        try:
            # Use add_episode to store the entity in Graphiti
            # Graphiti extracts entities from episode content, so we format it as natural language
            episode_body = self._format_entity_as_episode(entity)

            # Store the entity metadata in custom entity_types for extraction
            # Cast to dict[str, type[BaseModel]] for type safety
            entity_types: dict[str, type[BaseModel]] = {entity.entity_type.value: BaseModel}

            # Sanitize the episode name for RediSearch compatibility
            # First: remove markdown formatting (bold/italic)
            safe_name = re.sub(r"\*{1,3}", "", entity.name)
            safe_name = re.sub(r"_{1,3}", "", safe_name)
            # Second: remove special characters that break RediSearch
            safe_name = re.sub(r"[`\[\]{}()|@#$%^&+=<>/:\"']", "", safe_name)
            safe_name = re.sub(r"\s+", " ", safe_name).strip()

            result = await self._client.client.add_episode(
                name=f"{entity.entity_type}:{safe_name}",
                episode_body=episode_body,
                source_description=f"MCP Entity: {entity.entity_type}",
                reference_time=entity.created_at or datetime.now(UTC),
                group_id=self._group_id,
                entity_types=entity_types,
            )

            created_uuid = result.episode.uuid
            desired_id = entity.id or created_uuid

            # Force deterministic UUID when caller provides one
            await self._driver.execute_query(
                """
                MATCH (n {uuid: $created_uuid})
                SET n.uuid = $desired_id
                RETURN n.uuid
                """,
                created_uuid=created_uuid,
                desired_id=desired_id,
            )

            # Persist attributes and metadata on the created node so downstream filters work
            await self._persist_entity_attributes(desired_id, entity)

            log.info(
                "Entity created successfully",
                entity_id=desired_id,
                episode_uuid=created_uuid,
            )
            return desired_id

        except Exception as e:
            log.exception("Failed to create entity", entity_id=entity.id, error=str(e))
            raise

    async def create_direct(self, entity: Entity) -> str:
        """Create an entity directly using Graphiti's EntityNode, bypassing LLM.

        This is faster than create() as it skips LLM-based entity extraction.
        Use this for structured entities (tasks, projects) where LLM extraction
        isn't needed. Embeddings can be generated asynchronously via background queue.

        Uses EntityNode.save() which handles idempotent creation (MERGE pattern).

        Args:
            entity: The entity to create.

        Returns:
            The ID of the created entity.

        Raises:
            EntityCreationError: If creation fails.
        """
        import json

        from sibyl.errors import EntityCreationError

        log.info(
            "Creating entity directly via EntityNode",
            entity_type=entity.entity_type,
            name=entity.name,
        )

        try:
            # Build attributes dict - all values must be primitives (FalkorDB limitation)
            # Serialize nested dicts to JSON strings
            metadata = self._entity_to_metadata(entity)
            attributes = {
                "entity_type": entity.entity_type.value,
                "description": entity.description or "",
                "content": entity.content or "",
                "source_file": entity.source_file or "",
                "updated_at": datetime.now(UTC).isoformat(),
                "_direct_insert": True,
                "metadata": json.dumps(metadata),  # Serialize to JSON string
            }

            # Create EntityNode instance
            node = EntityNode(
                uuid=entity.id,
                name=entity.name,
                group_id=self._group_id,
                labels=[entity.entity_type.value],
                created_at=entity.created_at or datetime.now(UTC),
                summary=entity.description[:500] if entity.description else entity.name,
                attributes=attributes,
            )

            # Save using Graphiti's serialized write
            async with self._client.write_lock:
                await node.save(self._client.driver)

            log.info(
                "Entity created via EntityNode.save",
                entity_id=entity.id,
                entity_type=entity.entity_type,
            )
            return entity.id

        except Exception as e:
            log.exception(
                "Failed to create entity directly",
                entity_id=entity.id,
                error=str(e),
            )
            raise EntityCreationError(
                f"Failed to create entity: {e}",
                entity_id=entity.id,
            ) from e

    async def get(self, entity_id: str) -> Entity:
        """Get an entity by ID using Graphiti's node APIs.

        Tries EntityNode first, then EpisodicNode, since nodes can be either type.

        Args:
            entity_id: The entity's unique identifier.

        Returns:
            The requested entity.

        Raises:
            EntityNotFoundError: If entity doesn't exist.
        """
        log.debug("Fetching entity", entity_id=entity_id)

        try:
            # Try EntityNode first (nodes created via create_direct or extracted)
            try:
                node = await EntityNode.get_by_uuid(self._client.driver, entity_id)
                if node and node.group_id == self._group_id:
                    entity = self._node_to_entity(node)
                    log.debug(
                        "Entity retrieved via EntityNode",
                        entity_id=entity_id,
                        entity_type=entity.entity_type,
                    )
                    return entity
            except Exception as e:
                log.debug(
                    "EntityNode lookup failed, trying EpisodicNode",
                    entity_id=entity_id,
                    error=str(e),
                )

            # Try EpisodicNode (nodes created via add_episode)
            try:
                episodic = await EpisodicNode.get_by_uuid(self._client.driver, entity_id)
                if episodic and episodic.group_id == self._group_id:
                    entity = self._episodic_to_entity(episodic)
                    log.debug(
                        "Entity retrieved via EpisodicNode",
                        entity_id=entity_id,
                        entity_type=entity.entity_type,
                    )
                    return entity
            except Exception as e:
                log.debug("EpisodicNode lookup failed", entity_id=entity_id, error=str(e))

            raise EntityNotFoundError("Entity", entity_id)

        except EntityNotFoundError:
            raise
        except Exception as e:
            log.exception("Failed to retrieve entity", entity_id=entity_id, error=str(e))
            raise EntityNotFoundError("Entity", entity_id) from e

    async def search(
        self,
        query: str,
        entity_types: list[EntityType] | None = None,
        limit: int = 10,
    ) -> list[tuple[Entity, float]]:
        """Semantic search for entities using Graphiti's native search API.

        Args:
            query: Natural language search query.
            entity_types: Optional filter by entity types.
            limit: Maximum results to return.

        Returns:
            List of (entity, score) tuples ordered by relevance.
        """
        # Sanitize query to escape RediSearch special characters
        safe_query = sanitize_search_query(query)
        log.info("Searching entities", query=safe_query, types=entity_types, limit=limit)

        try:
            # Perform hybrid search using Graphiti
            edges = await self._client.client.search(
                query=safe_query,
                group_ids=[self._group_id],
                num_results=limit * 3,  # Get more results for filtering
            )

            # Extract unique nodes from edges
            node_uuids = list(
                {edge.source_node_uuid for edge in edges}
                | {edge.target_node_uuid for edge in edges}
            )

            if not node_uuids:
                log.info("No search results found", query=query)
                return []

            # Retrieve full node details using Graphiti's node APIs
            results: list[tuple[Entity, float]] = []
            uuid_to_position = {uuid: i for i, uuid in enumerate(node_uuids)}

            # Get EntityNodes by UUIDs
            try:
                entity_nodes = await EntityNode.get_by_uuids(self._client.driver, node_uuids)
                for node in entity_nodes:
                    try:
                        entity = self._node_to_entity(node)

                        # Filter by entity types if specified
                        if entity_types and entity.entity_type not in entity_types:
                            continue

                        # Score based on position in search results
                        position = uuid_to_position.get(node.uuid, len(node_uuids))
                        score = 1.0 / (position + 1)

                        results.append((entity, score))
                    except Exception as e:
                        log.debug("Failed to convert EntityNode", error=str(e))
            except Exception as e:
                log.debug("EntityNode.get_by_uuids failed", error=str(e))

            # Get EpisodicNodes by UUIDs
            try:
                episodic_nodes = await EpisodicNode.get_by_uuids(self._client.driver, node_uuids)
                for node in episodic_nodes:
                    try:
                        entity = self._episodic_to_entity(node)

                        # Filter by entity types if specified
                        if entity_types and entity.entity_type not in entity_types:
                            continue

                        # Score based on position in search results
                        position = uuid_to_position.get(node.uuid, len(node_uuids))
                        score = 1.0 / (position + 1)

                        results.append((entity, score))
                    except Exception as e:
                        log.debug("Failed to convert EpisodicNode", error=str(e))
            except Exception as e:
                log.debug("EpisodicNode.get_by_uuids failed", error=str(e))

            # Sort by score and limit results
            results.sort(key=lambda x: x[1], reverse=True)
            results = results[:limit]

            log.info("Search completed", query=query, results_count=len(results))
            return results

        except Exception as e:
            log.exception("Search failed", query=query, error=str(e))
            raise SearchError(f"Search failed: {e}") from e

    async def update(self, entity_id: str, updates: dict[str, Any]) -> Entity | None:
        """Update an existing entity with partial updates.

        Args:
            entity_id: The entity's unique identifier.
            updates: Dictionary of fields to update.

        Returns:
            The updated entity, or None if update failed.

        Raises:
            EntityNotFoundError: If entity doesn't exist.
        """
        log.info("Updating entity", entity_id=entity_id, fields=list(updates.keys()))

        try:
            # Retrieve the existing entity
            existing = await self.get(entity_id)
            if not existing:
                raise EntityNotFoundError("Entity", entity_id)

            merged_metadata = {**(existing.metadata or {}), **(updates.get("metadata") or {})}

            # Any non-core fields should be preserved in metadata so filters can read them
            # Exclude embedding - it's stored as a direct node property, not in metadata
            # (embeddings in metadata bloat Graphiti's LLM context ~30KB per entity)
            excluded_keys = {
                "name",
                "description",
                "content",
                "metadata",
                "source_file",
                "embedding",
            }
            merged_metadata.update(
                {key: value for key, value in updates.items() if key not in excluded_keys}
            )

            # Collect all properties, preserving existing values when not updated
            updated_entity = Entity(
                id=existing.id,
                entity_type=existing.entity_type,
                name=updates.get("name", existing.name),
                description=updates.get("description", existing.description),
                content=updates.get("content", existing.content),
                metadata=merged_metadata,
                created_at=existing.created_at,
                updated_at=datetime.now(UTC),
                source_file=updates.get("source_file", existing.source_file),
            )

            # Persist updates in-place to avoid changing UUIDs
            await self._persist_entity_attributes(entity_id, updated_entity)

            # Store embedding as direct node property (not in metadata to avoid bloating LLM context)
            if "embedding" in updates:
                embedding = updates.get("embedding")

                # FalkorDB expects Vectorf32 for vector ops. Casting via vecf32() avoids
                # "expected Null or Vectorf32 but was List" type mismatches.
                if embedding and isinstance(embedding, list):
                    await self._driver.execute_query(
                        "MATCH (n {uuid: $entity_id}) SET n.name_embedding = vecf32($embedding)",
                        entity_id=entity_id,
                        embedding=embedding,
                    )
                    log.debug("Stored embedding on node", entity_id=entity_id)
                else:
                    # Allow clearing embeddings by passing null/empty.
                    await self._driver.execute_query(
                        "MATCH (n {uuid: $entity_id}) SET n.name_embedding = NULL",
                        entity_id=entity_id,
                    )
                    log.debug("Cleared embedding on node", entity_id=entity_id)

            log.info("Entity updated successfully", entity_id=entity_id)
            return updated_entity

        except EntityNotFoundError:
            raise
        except Exception as e:
            log.exception("Failed to update entity", entity_id=entity_id, error=str(e))
            raise

    async def delete(self, entity_id: str) -> bool:
        """Delete an entity from the graph using Graphiti's node APIs.

        Tries EntityNode first, then EpisodicNode deletion.

        Args:
            entity_id: The entity's unique identifier.

        Returns:
            True if deletion succeeded, False otherwise.
        """
        log.info("Deleting entity", entity_id=entity_id)

        try:
            # Try to delete as EntityNode first
            try:
                node = await EntityNode.get_by_uuid(self._client.driver, entity_id)
                if node and node.group_id == self._group_id:
                    await node.delete(self._client.driver)
                    log.info("Entity deleted via EntityNode", entity_id=entity_id)
                    return True
            except Exception as e:
                log.debug(
                    "EntityNode delete failed, trying EpisodicNode",
                    entity_id=entity_id,
                    error=str(e),
                )

            # Try to delete as EpisodicNode
            try:
                episodic = await EpisodicNode.get_by_uuid(self._client.driver, entity_id)
                if episodic and episodic.group_id == self._group_id:
                    await episodic.delete(self._client.driver)
                    log.info("Entity deleted via EpisodicNode", entity_id=entity_id)
                    return True
            except Exception as e:
                log.debug("EpisodicNode delete failed", entity_id=entity_id, error=str(e))

            raise EntityNotFoundError("Entity", entity_id)

        except EntityNotFoundError:
            raise
        except Exception as e:
            log.exception("Failed to delete entity", entity_id=entity_id, error=str(e))
            return False

    async def list_by_type(
        self,
        entity_type: EntityType,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Entity]:
        """List all entities of a specific type using direct Cypher query.

        Note: Graphiti's get_by_group_ids() doesn't return custom properties like
        entity_type, so we use a direct Cypher query instead.

        Args:
            entity_type: The type of entities to list.
            limit: Maximum results to return.
            offset: Pagination offset.

        Returns:
            List of entities.
        """
        log.debug("Listing entities", entity_type=entity_type, limit=limit, offset=offset)

        try:
            # Direct Cypher query to get entities with all their properties
            # This is more reliable than Graphiti's get_by_group_ids which doesn't
            # return custom properties like entity_type
            result = await self._driver.execute_query(
                """
                MATCH (n)
                WHERE n.entity_type = $entity_type AND n.group_id = $group_id
                RETURN n.uuid AS uuid,
                       n.name AS name,
                       n.entity_type AS entity_type,
                       n.group_id AS group_id,
                       n.content AS content,
                       n.description AS description,
                       n.summary AS summary,
                       n.metadata AS metadata,
                       n.created_at AS created_at,
                       n.updated_at AS updated_at,
                       n.status AS status,
                       n.priority AS priority,
                       n.project_id AS project_id,
                       n.task_order AS task_order,
                       n.feature AS feature,
                       n.complexity AS complexity,
                       n.due_date AS due_date,
                       n.tags AS tags,
                       n.assignees AS assignees,
                       n.learnings AS learnings,
                       labels(n) AS labels
                ORDER BY n.created_at DESC
                SKIP $offset
                LIMIT $limit
                """,
                entity_type=entity_type.value,
                group_id=self._group_id,
                offset=offset,
                limit=limit,
            )

            entities: list[Entity] = []

            # Handle FalkorDB result format using normalize helper
            records = GraphClient.normalize_result(result)
            for record in records:
                try:
                    entity = self._record_to_entity(record)
                    entities.append(entity)
                except Exception as e:
                    log.debug("Failed to convert record to entity", error=str(e))

            log.debug(
                "Listed entities",
                entity_type=entity_type,
                returned=len(entities),
            )
            return entities

        except Exception as e:
            log.exception("Failed to list entities", entity_type=entity_type, error=str(e))
            return []

    def _record_to_entity(self, node_data: dict[str, Any]) -> Entity:
        """Convert a raw database record to an Entity.

        Args:
            node_data: Raw node data from Cypher query.

        Returns:
            Entity instance.
        """
        import json

        # Parse metadata if it's a string
        metadata = node_data.get("metadata", {})
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (json.JSONDecodeError, TypeError):
                metadata = {}

        # Get entity type
        entity_type_str = node_data.get("entity_type", "episode")
        try:
            entity_type = EntityType(entity_type_str)
        except ValueError:
            entity_type = EntityType.EPISODE

        return Entity(
            id=node_data.get("uuid", ""),
            name=node_data.get("name", ""),
            entity_type=entity_type,
            description=node_data.get("description") or node_data.get("summary", ""),
            content=node_data.get("content", ""),
            organization_id=node_data.get("group_id") or metadata.get("organization_id"),
            created_by=metadata.get("created_by"),
            modified_by=metadata.get("modified_by"),
            metadata=metadata,
            created_at=self._parse_datetime(node_data.get("created_at")),
            updated_at=self._parse_datetime(node_data.get("updated_at")),
        )

    def _parse_datetime(self, value: Any) -> datetime | None:
        """Parse datetime from various formats."""
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                return None
        return None

    async def _persist_entity_attributes(self, entity_id: str, entity: Entity) -> None:
        """Persist normalized attributes/metadata on a node for reliable querying."""
        props = self._collect_properties(entity)
        # Use _entity_to_metadata to include model-specific fields (Task.status, etc.)
        metadata = self._entity_to_metadata(entity)

        # Remove None values to appease FalkorDB property constraints
        props = {k: v for k, v in props.items() if v is not None}

        props["updated_at"] = datetime.now(UTC).isoformat()
        if entity.created_at:
            props["created_at"] = entity.created_at.isoformat()

        import json

        metadata_json = json.dumps(metadata) if metadata else "{}"

        await self._driver.execute_query(
            """
            MATCH (n {uuid: $entity_id})
            SET n += $props,
                n.metadata = $metadata
            """,
            entity_id=entity_id,
            props=props,
            metadata=metadata_json,
        )

    def _collect_properties(self, entity: Entity) -> dict[str, Any]:
        """Collect structured properties for storage and filtering."""
        props: dict[str, Any] = {
            "uuid": entity.id,
            "entity_type": entity.entity_type.value,
            "name": entity.name,
            "description": entity.description,
            "content": entity.content,
            "source_file": entity.source_file,
        }

        # Common optional fields
        for field in (
            "category",
            "languages",
            "tags",
            "organization_id",
            "created_by",
            "modified_by",
            "severity",
            "template_type",
            "file_extension",
        ):
            value = getattr(entity, field, None)
            if value is None:
                value = entity.metadata.get(field)
            if value is not None:
                props[field] = value

        # Task-specific fields (if present)
        task_fields = (
            "status",
            "priority",
            "task_order",
            "project_id",
            "feature",
            "sprint",
            "assignees",
            "due_date",
            "estimated_hours",
            "actual_hours",
            "domain",
            "technologies",
            "complexity",
            "branch_name",
            "commit_shas",
            "pr_url",
            "learnings",
            "blockers_encountered",
            "started_at",
            "completed_at",
            "reviewed_at",
        )
        for field in task_fields:
            value = getattr(entity, field, None)
            if value is None:
                value = entity.metadata.get(field)
            if value is not None:
                # Serialize datetimes to isoformat for storage
                if isinstance(value, datetime):
                    props[field] = value.isoformat()
                # Serialize enums to their string value
                elif hasattr(value, "value"):
                    props[field] = value.value
                else:
                    props[field] = value

        return props

    def _serialize_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """Convert metadata values to JSON-serializable forms."""
        serialized: dict[str, Any] = {}
        for key, value in metadata.items():
            if isinstance(value, datetime):
                serialized[key] = value.isoformat()
            elif hasattr(value, "value"):  # Enum
                serialized[key] = value.value
            elif value is not None:
                serialized[key] = value
        return serialized

    def _entity_to_metadata(self, entity: Entity) -> dict[str, Any]:
        """Extract all entity fields as metadata for storage.

        This ensures model-specific fields (Task.status, Project.tech_stack, etc.)
        are persisted in the metadata JSON, not just the generic metadata dict.
        """
        from sibyl.models.tasks import Project, Task

        # Start with explicit metadata
        metadata = dict(entity.metadata or {})

        # Add Task-specific fields
        if isinstance(entity, Task):
            metadata["status"] = entity.status.value if entity.status else "todo"
            metadata["priority"] = entity.priority.value if entity.priority else "medium"
            metadata["project_id"] = entity.project_id
            metadata["task_order"] = entity.task_order
            if entity.assignees:
                metadata["assignees"] = entity.assignees
            if entity.technologies:
                metadata["technologies"] = entity.technologies
            if entity.feature:
                metadata["feature"] = entity.feature
            if entity.domain:
                metadata["domain"] = entity.domain
            if entity.due_date:
                metadata["due_date"] = entity.due_date.isoformat()
            if entity.estimated_hours:
                metadata["estimated_hours"] = entity.estimated_hours
            if entity.branch_name:
                metadata["branch_name"] = entity.branch_name
            if entity.pr_url:
                metadata["pr_url"] = entity.pr_url

        # Add Project-specific fields
        elif isinstance(entity, Project):
            metadata["status"] = entity.status.value if entity.status else "active"
            if entity.tech_stack:
                metadata["tech_stack"] = entity.tech_stack
            if entity.repository_url:
                metadata["repository_url"] = entity.repository_url

        # Common fields (check hasattr since not all entities have these)
        if hasattr(entity, "languages") and entity.languages:
            metadata["languages"] = entity.languages
        if hasattr(entity, "tags") and entity.tags:
            metadata["tags"] = entity.tags
        if hasattr(entity, "category") and entity.category:
            metadata["category"] = entity.category

        return self._serialize_metadata(metadata)

    async def bulk_create_direct(
        self,
        entities: list[Entity],
        batch_size: int = 100,
    ) -> tuple[int, int]:
        """Bulk create entities using Graphiti's EntityNode.save(), bypassing LLM.

        This is faster than create() as it skips LLM-based entity extraction.
        Use this for stress testing or bulk imports where LLM processing isn't needed.

        Args:
            entities: List of entities to create.
            batch_size: Number of entities per batch.

        Returns:
            Tuple of (created_count, failed_count).
        """
        import json

        created = 0
        failed = 0

        for i in range(0, len(entities), batch_size):
            batch = entities[i : i + batch_size]

            for entity in batch:
                try:
                    # Build attributes dict - serialize nested dicts to JSON strings
                    metadata = self._entity_to_metadata(entity)
                    attributes = {
                        "entity_type": entity.entity_type.value,
                        "description": entity.description or "",
                        "content": entity.content or "",
                        "source_file": entity.source_file or "",
                        "updated_at": datetime.now(UTC).isoformat(),
                        "_generated": True,
                        "metadata": json.dumps(metadata),  # Serialize to JSON string
                    }

                    # Create EntityNode instance
                    node = EntityNode(
                        uuid=entity.id,
                        name=entity.name,
                        group_id=self._group_id,
                        labels=[entity.entity_type.value],
                        created_at=entity.created_at or datetime.now(UTC),
                        summary=entity.description[:500] if entity.description else entity.name,
                        attributes=attributes,
                    )

                    # Save using Graphiti's API with write lock
                    async with self._client.write_lock:
                        await node.save(self._client.driver)

                    created += 1
                except Exception as e:
                    log.debug("Failed to create entity", entity_id=entity.id, error=str(e))
                    failed += 1

        log.info("Bulk create complete", created=created, failed=failed)
        return created, failed

    def _format_entity_as_episode(self, entity: Entity) -> str:
        """Format an entity as natural language for episode storage.

        Args:
            entity: The entity to format.

        Returns:
            Formatted episode body.
        """

        # Sanitize text for RediSearch compatibility
        def sanitize(text: str) -> str:
            # Remove markdown formatting (bold/italic markers)
            result = re.sub(r"\*{1,3}", "", text)
            result = re.sub(r"_{1,3}", "", result)
            # Remove special characters that break RediSearch
            result = re.sub(r"[`\[\]{}()|@#$%^&+=<>\"']", "", result)
            result = result.replace(":", " ").replace("/", " ")
            return re.sub(r"\s+", " ", result).strip()

        parts = [
            f"Entity: {sanitize(entity.name)}",
            f"Type: {entity.entity_type}",
        ]

        if entity.description:
            parts.append(f"Description: {sanitize(entity.description)}")

        if entity.content:
            # Truncate content to avoid excessive episode size
            content = entity.content[:500] if len(entity.content) > 500 else entity.content
            parts.append(f"Content: {sanitize(content)}")

        # Add type-specific fields
        parts.extend(self._format_specialized_fields(entity, sanitize))

        return "\n".join(parts)

    def _format_specialized_fields(
        self,
        entity: Entity,
        sanitize: Any,
    ) -> list[str]:
        """Format specialized fields for different entity types.

        Args:
            entity: The entity to format.
            sanitize: Function to sanitize text.

        Returns:
            List of formatted field strings.
        """
        parts: list[str] = []

        if isinstance(entity, Task):
            if entity.status:
                parts.append(f"Status: {entity.status}")
            if entity.priority:
                parts.append(f"Priority: {entity.priority}")
            if entity.domain:
                parts.append(f"Domain: {sanitize(entity.domain)}")
            if entity.technologies:
                parts.append(f"Technologies: {', '.join(entity.technologies)}")
            if entity.feature:
                parts.append(f"Feature: {sanitize(entity.feature)}")

        elif isinstance(entity, Project):
            if entity.status:
                parts.append(f"Status: {entity.status}")
            if entity.tech_stack:
                parts.append(f"Tech Stack: {', '.join(entity.tech_stack)}")
            if entity.features:
                parts.append(f"Features: {', '.join(entity.features[:5])}")

        elif isinstance(entity, Source):
            parts.append(f"URL: {sanitize(entity.url)}")
            parts.append(f"Source Type: {entity.source_type}")
            if entity.crawl_status:
                parts.append(f"Crawl Status: {entity.crawl_status}")
            if entity.document_count:
                parts.append(f"Documents: {entity.document_count}")

        elif isinstance(entity, Document):
            parts.append(f"URL: {sanitize(entity.url)}")
            if entity.title:
                parts.append(f"Title: {sanitize(entity.title)}")
            if entity.headings:
                parts.append(f"Headings: {', '.join(entity.headings[:5])}")
            if entity.has_code:
                parts.append("Has Code: yes")
            if entity.language:
                parts.append(f"Language: {entity.language}")

        elif isinstance(entity, Community):
            if entity.key_concepts:
                parts.append(f"Concepts: {', '.join(entity.key_concepts)}")
            if entity.member_count:
                parts.append(f"Members: {entity.member_count}")
            if entity.level is not None:
                parts.append(f"Level: {entity.level}")

        elif isinstance(entity, ErrorPattern):
            parts.append(f"Error: {sanitize(entity.error_message)}")
            parts.append(f"Root Cause: {sanitize(entity.root_cause)}")
            parts.append(f"Solution: {sanitize(entity.solution)}")
            if entity.technologies:
                parts.append(f"Technologies: {', '.join(entity.technologies)}")

        elif isinstance(entity, Team):
            if entity.members:
                parts.append(f"Members: {', '.join(entity.members[:5])}")
            if entity.focus_areas:
                parts.append(f"Focus Areas: {', '.join(entity.focus_areas)}")

        elif isinstance(entity, Milestone):
            if entity.total_tasks:
                parts.append(f"Tasks: {entity.completed_tasks}/{entity.total_tasks}")

        return parts

    def _node_to_entity(self, node: EntityNode) -> Entity:
        """Convert a Graphiti EntityNode to our Entity model.

        Args:
            node: The EntityNode to convert.

        Returns:
            Converted Entity.
        """
        # Extract entity type from attributes or labels
        entity_type_str = node.attributes.get("entity_type", "topic")
        try:
            entity_type = EntityType(entity_type_str)
        except ValueError:
            # Default to TOPIC if unknown type
            entity_type = EntityType.TOPIC
            log.warning(
                "Unknown entity type, defaulting to TOPIC",
                node_uuid=node.uuid,
                entity_type_str=entity_type_str,
            )

        # Extract other attributes
        description = node.attributes.get("description", node.summary or "")
        content = node.attributes.get("content", "")
        source_file = node.attributes.get("source_file")

        # Remove known fields from attributes to get clean metadata
        metadata = {
            k: v
            for k, v in node.attributes.items()
            if k not in {"entity_type", "description", "content", "source_file", "metadata"}
        }

        # Parse metadata - may be JSON string (from create_direct) or dict
        raw_metadata = node.attributes.get("metadata")
        if raw_metadata:
            if isinstance(raw_metadata, str):
                import json

                try:
                    parsed = json.loads(raw_metadata)
                    if isinstance(parsed, dict):
                        metadata.update(parsed)
                except json.JSONDecodeError:
                    pass  # Not valid JSON, skip
            elif isinstance(raw_metadata, dict):
                metadata.update(raw_metadata)

        return Entity(
            id=node.uuid,
            entity_type=entity_type,
            name=node.name,
            description=description,
            content=content,
            organization_id=node.group_id,
            created_by=metadata.get("created_by"),
            modified_by=metadata.get("modified_by"),
            metadata=metadata,
            created_at=node.created_at,
            updated_at=node.created_at,  # Graphiti doesn't track updated_at
            source_file=source_file,
            embedding=node.name_embedding if node.name_embedding else None,
        )

    def _episodic_to_entity(self, node: EpisodicNode) -> Entity:
        """Convert a Graphiti EpisodicNode to our Entity model.

        EpisodicNodes are created via add_episode() and have different structure
        than EntityNodes.

        Args:
            node: The EpisodicNode to convert.

        Returns:
            Converted Entity.
        """

        # EpisodicNode has: uuid, name, group_id, content, created_at, valid_at, source_description

        # Try to extract entity_type from the name (format: "type:name")
        entity_type_str = "episode"
        name = node.name

        if ":" in name:
            parts = name.split(":", 1)
            potential_type = parts[0].strip().lower()
            # Check if it's a valid entity type
            try:
                entity_type = EntityType(potential_type)
                entity_type_str = potential_type
                name = parts[1].strip() if len(parts) > 1 else name
            except ValueError:
                pass  # Not a valid type prefix, use full name

        try:
            entity_type = EntityType(entity_type_str)
        except ValueError:
            entity_type = EntityType.EPISODE

        # Extract content and description from node
        content = node.content if hasattr(node, "content") else ""
        description = node.source_description if hasattr(node, "source_description") else ""

        # Try to parse metadata if stored in content as structured text
        metadata: dict[str, Any] = {}
        if hasattr(node, "entity_type"):
            metadata["entity_type"] = node.entity_type

        return Entity(
            id=node.uuid,
            entity_type=entity_type,
            name=name,
            description=description,
            content=content,
            organization_id=getattr(node, "group_id", None),
            metadata=metadata,
            created_at=node.created_at if hasattr(node, "created_at") else None,
            updated_at=node.created_at if hasattr(node, "created_at") else None,
        )
