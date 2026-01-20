"""Entity management for the knowledge graph.

This module provides entity CRUD operations using Graphiti's native node APIs.
All graph operations go through EntityNode/EpisodicNode rather than raw Cypher.
"""

import asyncio
import contextlib
import json
import random
import re
from datetime import UTC, datetime
from enum import Enum
from typing import Any, TypeVar

import structlog
from graphiti_core.nodes import EntityNode, EpisodicNode
from graphiti_core.search.search_config_recipes import NODE_HYBRID_SEARCH_RRF
from pydantic import BaseModel

from sibyl_core.errors import EntityNotFoundError, SearchError
from sibyl_core.graph.client import GraphClient
from sibyl_core.models.agents import (
    AgentCheckpoint,
    AgentRecord,
    ApprovalRecord,
    ApprovalStatus,
    ApprovalType,
    MetaOrchestratorRecord,
    TaskOrchestratorRecord,
    WorktreeRecord,
    WorktreeStatus,
)
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.models.sources import Community, Document, Source
from sibyl_core.models.tasks import (
    Epic,
    ErrorPattern,
    Milestone,
    Note,
    Project,
    Task,
    TaskComplexity,
    TaskPriority,
    TaskStatus,
    Team,
)
from sibyl_core.utils.resilience import GRAPHITI_RETRY

log = structlog.get_logger()

# Generic enum type for coercion
TEnum = TypeVar("TEnum", bound=Enum)
# RediSearch special characters that need escaping in fulltext queries
# Includes / which appears in paths like "create/cleanup" or "~/.sibyl-worktrees/"
_REDISEARCH_SPECIAL_CHARS = re.compile(r"[|&\-@()~$:*\\/]")


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

    async def _add_episode_with_retry(
        self,
        name: str,
        episode_body: str,
        source_description: str,
        reference_time: datetime,
        entity_types: dict[str, type[BaseModel]],
    ) -> Any:
        """Call Graphiti add_episode with retry on transient failures.

        This is separated out to apply retry logic - add_episode can take 60-90s
        under load and may fail with Redis timeouts during edge_fulltext_search.
        """
        # Import here to get RedisTimeoutError for type annotation
        try:
            from redis.exceptions import TimeoutError as RedisTimeoutError
        except ImportError:
            RedisTimeoutError = TimeoutError  # type: ignore[misc,assignment]

        # Retry wrapper - applied inline since @retry decorator doesn't work well with methods
        last_error: Exception | None = None
        for attempt in range(GRAPHITI_RETRY.max_attempts):
            try:
                return await self._client.client.add_episode(
                    name=name,
                    episode_body=episode_body,
                    source_description=source_description,
                    reference_time=reference_time,
                    group_id=self._group_id,
                    entity_types=entity_types,
                )
            except (ConnectionError, TimeoutError, OSError, RedisTimeoutError) as e:
                last_error = e
                if attempt < GRAPHITI_RETRY.max_attempts - 1:
                    delay = min(
                        GRAPHITI_RETRY.base_delay * (2**attempt),
                        GRAPHITI_RETRY.max_delay,
                    )
                    if GRAPHITI_RETRY.jitter:
                        delay += random.uniform(-delay * 0.25, delay * 0.25)
                    log.warning(
                        "add_episode failed, retrying",
                        attempt=attempt + 1,
                        max_attempts=GRAPHITI_RETRY.max_attempts,
                        delay=f"{delay:.1f}s",
                        error=str(e),
                    )
                    await asyncio.sleep(delay)
                else:
                    log.error(
                        "add_episode exhausted retries",
                        attempts=GRAPHITI_RETRY.max_attempts,
                        error=str(e),
                    )
        if last_error:
            raise last_error
        raise RuntimeError("Retry logic error in add_episode")

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

            # Call add_episode with retry logic for transient failures
            result = await self._add_episode_with_retry(
                name=f"{entity.entity_type}:{safe_name}",
                episode_body=episode_body,
                source_description=f"MCP Entity: {entity.entity_type}",
                reference_time=entity.created_at or datetime.now(UTC),
                entity_types=entity_types,
            )

            created_uuid = result.episode.uuid
            desired_id = entity.id or created_uuid

            # Use write lock to serialize FalkorDB writes and prevent connection corruption
            async with self._client.write_lock:
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

    async def create_direct(self, entity: Entity, *, generate_embedding: bool = True) -> str:
        """Create an entity directly using Graphiti's EntityNode, bypassing LLM.

        This is faster than create() as it skips LLM-based entity extraction.
        Use this for structured entities (tasks, projects) where LLM extraction
        isn't needed. Generates embeddings inline for semantic search support.

        Uses EntityNode.save() which handles idempotent creation (MERGE pattern).

        Args:
            entity: The entity to create.
            generate_embedding: If True (default), generate and store a name_embedding
                for semantic search. Set to False for bulk inserts where embeddings
                will be generated separately.

        Returns:
            The ID of the created entity.

        Raises:
            EntityCreationError: If creation fails.
        """
        import json

        from sibyl_core.errors import EntityCreationError

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
                await node.save(self._driver)

                # Persist structured properties (project_id, status, etc.) for graph filtering
                # This ensures create_direct() nodes are queryable the same as create() nodes
                await self._persist_entity_attributes(entity.id, entity)

            # Generate embedding for semantic search (name + summary combined)
            if generate_embedding:
                try:
                    embed_text = f"{entity.name}. {entity.description or ''}"[:2000]
                    embedding = await self._client.client.embedder.create(embed_text)

                    # Store embedding on node using vecf32() for FalkorDB vector ops
                    async with self._client.write_lock:
                        await self._driver.execute_query(
                            "MATCH (n {uuid: $entity_id}) SET n.name_embedding = vecf32($embedding)",
                            entity_id=entity.id,
                            embedding=embedding,
                        )
                    log.debug("Generated embedding for entity", entity_id=entity.id)
                except Exception as e:
                    # Don't fail entity creation if embedding fails - search will still work via BM25
                    log.warning(
                        "Failed to generate embedding, entity still created",
                        entity_id=entity.id,
                        error=str(e),
                    )

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
                node = await EntityNode.get_by_uuid(self._driver, entity_id)
                if node and node.group_id == self._group_id:
                    entity = self.node_to_entity(node)
                    log.debug(
                        "Entity retrieved via EntityNode",
                        entity_id=entity_id,
                        entity_type=entity.entity_type,
                    )
                    return self._coerce_entity(entity)
            except Exception as e:
                log.debug(
                    "EntityNode lookup failed, trying EpisodicNode",
                    entity_id=entity_id,
                    error=str(e),
                )

            # Try EpisodicNode (nodes created via add_episode)
            try:
                episodic = await EpisodicNode.get_by_uuid(self._driver, entity_id)
                if episodic and episodic.group_id == self._group_id:
                    # Query for entity_type property (not hydrated by Graphiti's dataclass)
                    entity_type_override = await self._get_node_entity_type(entity_id)
                    entity = self._episodic_to_entity(episodic, entity_type_override)
                    log.debug(
                        "Entity retrieved via EpisodicNode",
                        entity_id=entity_id,
                        entity_type=entity.entity_type,
                    )
                    return self._coerce_entity(entity)
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
        """Semantic search for entities using Graphiti's node-based hybrid search.

        Uses NODE_HYBRID_SEARCH which combines:
        - BM25 keyword search on node text
        - Cosine similarity on name_embedding vectors
        - RRF (Reciprocal Rank Fusion) for combining results

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
            # Use search_() with NODE_HYBRID_SEARCH for direct node search
            # This searches node embeddings directly instead of going through edges
            # CRITICAL: Pass self._driver (org-specific driver) - otherwise Graphiti
            # uses the default driver which points to "default" graph, not our org graph
            search_results = await self._client.client.search_(
                query=safe_query,
                config=NODE_HYBRID_SEARCH_RRF,
                group_ids=[self._group_id],
                driver=self._driver,
            )

            results: list[tuple[Entity, float]] = []

            # Process EntityNodes with their reranker scores
            for i, node in enumerate(search_results.nodes):
                try:
                    # Filter by group_id (multi-tenancy)
                    if node.group_id != self._group_id:
                        continue

                    entity = self.node_to_entity(node)

                    # Filter by entity types if specified
                    if entity_types and entity.entity_type not in entity_types:
                        continue

                    # Use reranker score if available, otherwise position-based
                    if i < len(search_results.node_reranker_scores):
                        score = search_results.node_reranker_scores[i]
                    else:
                        score = 1.0 / (i + 1)

                    results.append((entity, score))
                except Exception as e:
                    log.debug("Failed to convert EntityNode", error=str(e), node=node.uuid)

            # Also check episodes (for nodes created via add_episode)
            for i, node in enumerate(search_results.episodes):
                try:
                    if node.group_id != self._group_id:
                        continue

                    entity = self._episodic_to_entity(node)

                    if entity_types and entity.entity_type not in entity_types:
                        continue

                    if i < len(search_results.episode_reranker_scores):
                        score = search_results.episode_reranker_scores[i]
                    else:
                        score = 1.0 / (i + 1)

                    results.append((entity, score))
                except Exception as e:
                    log.debug("Failed to convert EpisodicNode", error=str(e))

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
            # Use write lock to serialize FalkorDB writes and prevent connection corruption
            async with self._client.write_lock:
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
            # Use write lock to serialize FalkorDB writes and prevent connection corruption
            async with self._client.write_lock:
                # Try to delete as EntityNode first
                try:
                    node = await EntityNode.get_by_uuid(self._driver, entity_id)
                    if node and node.group_id == self._group_id:
                        await node.delete(self._driver)
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
                    episodic = await EpisodicNode.get_by_uuid(self._driver, entity_id)
                    if episodic and episodic.group_id == self._group_id:
                        await episodic.delete(self._driver)
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
        *,
        project_id: str | None = None,
        epic_id: str | None = None,
        no_epic: bool = False,
        status: str | None = None,
        priority: str | None = None,
        complexity: str | None = None,
        feature: str | None = None,
        tags: list[str] | None = None,
        include_archived: bool = False,
    ) -> list[Entity]:
        """List all entities of a specific type using direct Cypher query.

        Note: Filters like status, priority, epic_id are stored in metadata JSON,
        not as top-level node properties. We use BELONGS_TO relationship for epic
        filtering and parse metadata for other filters in Python.

        Args:
            entity_type: The type of entities to list.
            limit: Maximum results to return.
            offset: Pagination offset.
            project_id: Filter by project ID.
            epic_id: Filter by epic ID (uses BELONGS_TO relationship).
            no_epic: Filter for entities without an epic (mutually exclusive with epic_id).
            status: Filter by status (for tasks, parsed from metadata).
            priority: Filter by priority (for tasks, parsed from metadata).
            complexity: Filter by complexity (for tasks, parsed from metadata).
            feature: Filter by feature area (for tasks, parsed from metadata).
            tags: Filter by tags (matches if ANY tag present, parsed from metadata).
            include_archived: Include archived entities.

        Returns:
            List of entities.
        """
        log.debug(
            "Listing entities",
            entity_type=entity_type,
            limit=limit,
            offset=offset,
            project_id=project_id,
            epic_id=epic_id,
            status=status,
            priority=priority,
        )

        # Build base query - filters are applied in Python from metadata
        params: dict[str, Any] = {
            "entity_type": entity_type.value,
            "group_id": self._group_id,
        }

        # Use BELONGS_TO relationship for epic filtering (most reliable)
        if epic_id:
            query = """
                MATCH (n)-[:BELONGS_TO]->(e)
                WHERE n.entity_type = $entity_type
                  AND n.group_id = $group_id
                  AND e.uuid = $epic_id
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
                       labels(n) AS labels
                ORDER BY n.created_at DESC
            """
            params["epic_id"] = epic_id
        else:
            query = """
                MATCH (n)
                WHERE n.entity_type = $entity_type
                  AND n.group_id = $group_id
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
                       labels(n) AS labels
                ORDER BY n.created_at DESC
            """

        try:
            result = await self._driver.execute_query(query, **params)

            entities: list[Entity] = []
            skipped = 0

            # Handle FalkorDB result format using normalize helper
            records = GraphClient.normalize_result(result)
            for record in records:
                try:
                    entity = self._record_to_entity(record)
                    entity = self._coerce_entity(entity)

                    # Parse metadata for filtering (stored as JSON string)
                    metadata = entity.metadata or {}

                    # Filter by project_id from metadata
                    if project_id and metadata.get("project_id") != project_id:
                        continue

                    # Filter by status from metadata (supports comma-separated)
                    if status:
                        entity_status = metadata.get("status", "").lower()
                        status_list = [s.strip().lower() for s in status.split(",")]
                        if entity_status not in status_list:
                            continue

                    # Filter by priority from metadata (supports comma-separated)
                    if priority:
                        entity_priority = metadata.get("priority", "").lower()
                        priority_list = [p.strip().lower() for p in priority.split(",")]
                        if entity_priority not in priority_list:
                            continue

                    # Filter by complexity from metadata (supports comma-separated)
                    if complexity:
                        entity_complexity = metadata.get("complexity", "").lower()
                        complexity_list = [c.strip().lower() for c in complexity.split(",")]
                        if entity_complexity not in complexity_list:
                            continue

                    # Filter by feature from metadata
                    if feature:
                        entity_feature = metadata.get("feature")
                        if entity_feature != feature:
                            continue

                    # Filter by tags from metadata (match if ANY tag present)
                    if tags:
                        entity_tags = metadata.get("tags", [])
                        if not any(t in entity_tags for t in tags):
                            continue

                    # Filter for entities without an epic
                    if no_epic:
                        entity_epic = metadata.get("epic_id")
                        if entity_epic:  # Has an epic, skip it
                            continue

                    # Filter archived unless include_archived is True
                    if not include_archived:
                        entity_status = metadata.get("status")
                        if entity_status == "archived":
                            continue

                    # Apply pagination
                    if skipped < offset:
                        skipped += 1
                        continue

                    entities.append(entity)

                    # Check limit
                    if len(entities) >= limit:
                        break

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

    async def list_all(
        self,
        limit: int = 1000,
        offset: int = 0,
        *,
        include_archived: bool = False,
    ) -> list[Entity]:
        """List all entities regardless of type using a single query.

        Args:
            limit: Maximum results to return.
            offset: Pagination offset.
            include_archived: Include archived entities.

        Returns:
            List of entities.
        """
        log.debug("Listing all entities", limit=limit, offset=offset)

        try:
            query = """
                MATCH (n)
                WHERE n.group_id = $group_id
                  AND n.entity_type IS NOT NULL
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
                       labels(n) AS labels
                ORDER BY n.updated_at DESC
                SKIP $offset
                LIMIT $limit
            """

            params: dict[str, Any] = {
                "group_id": self._group_id,
                "limit": limit,
                "offset": offset,
            }

            result = await self._client.execute_read_org(query, self._group_id, **params)

            entities: list[Entity] = []
            for record in result:
                try:
                    metadata = record.get("metadata") or {}
                    if isinstance(metadata, str):
                        import json

                        metadata = json.loads(metadata)

                    # Skip archived unless requested
                    if not include_archived and metadata.get("archived"):
                        continue

                    entity = Entity(
                        id=record.get("uuid", ""),
                        entity_type=record.get("entity_type", ""),
                        name=record.get("name", ""),
                        description=record.get("description") or record.get("summary") or "",
                        content=record.get("content") or "",
                        metadata=metadata,
                        **(
                            {"created_at": record["created_at"]} if record.get("created_at") else {}
                        ),
                        **(
                            {"updated_at": record["updated_at"]} if record.get("updated_at") else {}
                        ),
                    )
                    entities.append(entity)

                except Exception as e:
                    log.debug("Failed to convert record to entity", error=str(e))

            log.debug("Listed all entities", returned=len(entities))
            return entities

        except Exception as e:
            log.exception("Failed to list all entities", error=str(e))
            return []

    async def get_tasks_for_epic(
        self,
        epic_id: str,
        status: str | None = None,
        limit: int = 100,
    ) -> list[Entity]:
        """Get all tasks belonging to an epic.

        Args:
            epic_id: The epic's unique identifier.
            status: Optional status filter (todo, doing, done, etc.).
            limit: Maximum results to return.

        Returns:
            List of Task entities belonging to the epic.
        """
        log.debug("Fetching tasks for epic", epic_id=epic_id, status=status)

        try:
            # Use BELONGS_TO relationship to find tasks in epic
            # Status is stored in metadata JSON, so we filter in Python if needed
            query = """
                MATCH (n)-[:BELONGS_TO]->(e)
                WHERE n.entity_type = 'task'
                  AND n.group_id = $group_id
                  AND e.uuid = $epic_id
                RETURN n.uuid AS uuid,
                       n.name AS name,
                       n.entity_type AS entity_type,
                       n.group_id AS group_id,
                       n.content AS content,
                       n.description AS description,
                       n.summary AS summary,
                       n.metadata AS metadata,
                       n.created_at AS created_at
                ORDER BY n.created_at DESC
                LIMIT $limit
            """

            params: dict[str, Any] = {
                "group_id": self._group_id,
                "epic_id": epic_id,
                "limit": limit,
            }

            result = await self._driver.execute_query(query, **params)

            entities: list[Entity] = []
            records = GraphClient.normalize_result(result)
            for record in records:
                try:
                    entity = self._record_to_entity(record)
                    # Filter by status in Python since it's in metadata
                    if status:
                        entity_status = entity.metadata.get("status") if entity.metadata else None
                        if entity_status != status:
                            continue
                    entities.append(entity)
                except Exception as e:
                    log.debug("Failed to convert record", error=str(e))

            log.debug("Fetched tasks for epic", epic_id=epic_id, count=len(entities))
            return entities

        except Exception as e:
            log.exception("Failed to get tasks for epic", epic_id=epic_id, error=str(e))
            return []

    async def get_epic_progress(self, epic_id: str) -> dict[str, Any]:
        """Get progress statistics for an epic.

        Args:
            epic_id: The epic's unique identifier.

        Returns:
            Dict with total_tasks, completed_tasks, in_progress_tasks, and completion_pct.
        """
        log.debug("Getting epic progress", epic_id=epic_id)

        try:
            # Use BELONGS_TO relationship to find tasks, then count by status in Python
            # since status is stored in metadata JSON
            result = await self._driver.execute_query(
                """
                MATCH (n)-[:BELONGS_TO]->(e)
                WHERE n.entity_type = 'task'
                  AND n.group_id = $group_id
                  AND e.uuid = $epic_id
                RETURN n.metadata AS metadata
                """,
                group_id=self._group_id,
                epic_id=epic_id,
            )

            records = GraphClient.normalize_result(result)

            # Count statuses from metadata
            total = len(records)
            done = 0
            doing = 0
            blocked = 0
            review = 0

            for record in records:
                metadata = record.get("metadata")
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except json.JSONDecodeError:
                        metadata = {}
                status = metadata.get("status") if metadata else None
                if status == "done":
                    done += 1
                elif status == "doing":
                    doing += 1
                elif status == "blocked":
                    blocked += 1
                elif status == "review":
                    review += 1

            return {
                "total_tasks": total,
                "completed_tasks": done,
                "in_progress_tasks": doing,
                "blocked_tasks": blocked,
                "in_review_tasks": review,
                "completion_pct": round((done / total * 100) if total > 0 else 0, 1),
            }

        except Exception as e:
            log.exception("Failed to get epic progress", epic_id=epic_id, error=str(e))
            return {"total_tasks": 0, "completed_tasks": 0, "completion_pct": 0.0}

    async def get_project_summary(
        self,
        project_id: str,
        *,
        actionable_limit: int = 5,
        critical_limit: int = 3,
        epic_limit: int = 3,
    ) -> dict[str, Any]:
        """Get a rich summary of a project with actionable task highlights.

        Returns task counts by status and curated lists of tasks that need attention,
        prioritized by urgency: doing > blocked > review > recent.

        Args:
            project_id: The project's unique identifier.
            actionable_limit: Max number of actionable tasks to return.
            critical_limit: Max number of critical tasks to return.
            epic_limit: Max number of epics to return.

        Returns:
            Dict with:
                - status_counts: Dict of status -> count
                - total_tasks: Total task count
                - progress_pct: Completion percentage
                - actionable_tasks: List of tasks needing attention (dicts with id, name, status)
                - critical_tasks: List of critical/high priority tasks
                - epics: List of active epics with progress
        """
        log.debug("Getting project summary", project_id=project_id)

        try:
            # Fetch all tasks for the project
            result = await self._driver.execute_query(
                """
                MATCH (n)
                WHERE n.entity_type = 'task'
                  AND n.group_id = $group_id
                  AND n.project_id = $project_id
                RETURN n.uuid AS uuid,
                       n.name AS name,
                       n.metadata AS metadata,
                       n.updated_at AS updated_at
                ORDER BY n.updated_at DESC
                """,
                group_id=self._group_id,
                project_id=project_id,
            )

            records = GraphClient.normalize_result(result)

            # Count by status and collect actionable/critical tasks
            status_counts: dict[str, int] = {}
            doing_tasks: list[dict[str, Any]] = []
            blocked_tasks: list[dict[str, Any]] = []
            review_tasks: list[dict[str, Any]] = []
            critical_tasks: list[dict[str, Any]] = []
            recent_tasks: list[dict[str, Any]] = []

            for record in records:
                metadata = record.get("metadata")
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except json.JSONDecodeError:
                        metadata = {}

                status = (metadata.get("status") if metadata else None) or "todo"
                priority = (metadata.get("priority") if metadata else None) or ""
                name = record.get("name") or ""

                status_counts[status] = status_counts.get(status, 0) + 1

                task_info = {
                    "id": record.get("uuid"),
                    "name": name,
                    "status": status,
                    "priority": priority,
                }

                # Check if task is critical (not done/archived)
                is_critical = (
                    priority.lower() in ("critical", "high") or "CRITICAL" in name.upper()
                ) and status not in ("done", "archived")

                if is_critical and len(critical_tasks) < critical_limit:
                    critical_tasks.append(task_info)

                # Collect actionable tasks by priority
                if status == "doing" and len(doing_tasks) < actionable_limit:
                    doing_tasks.append(task_info)
                elif status == "blocked" and len(blocked_tasks) < actionable_limit:
                    blocked_tasks.append(task_info)
                elif status == "review" and len(review_tasks) < actionable_limit:
                    review_tasks.append(task_info)
                elif len(recent_tasks) < actionable_limit:
                    recent_tasks.append(task_info)

            # Build prioritized actionable list: doing > blocked > review > recent
            actionable: list[dict[str, Any]] = []
            for pool in [doing_tasks, blocked_tasks, review_tasks, recent_tasks]:
                for task in pool:
                    if len(actionable) >= actionable_limit:
                        break
                    # Dedupe by ID
                    if task["id"] not in [t["id"] for t in actionable]:
                        actionable.append(task)
                if len(actionable) >= actionable_limit:
                    break

            total = sum(status_counts.values())
            done = status_counts.get("done", 0)

            # Fetch epics for project
            epics: list[dict[str, Any]] = []
            try:
                epic_result = await self._driver.execute_query(
                    """
                    MATCH (n)
                    WHERE n.entity_type = 'epic'
                      AND n.group_id = $group_id
                      AND n.project_id = $project_id
                      AND (n.status IS NULL OR n.status <> 'archived')
                    RETURN n.uuid AS uuid,
                           n.name AS name,
                           n.status AS status,
                           n.priority AS priority
                    ORDER BY n.priority ASC, n.created_at DESC
                    LIMIT $limit
                    """,
                    group_id=self._group_id,
                    project_id=project_id,
                    limit=epic_limit,
                )
                epic_records = GraphClient.normalize_result(epic_result)
                for rec in epic_records:
                    epic_info = {
                        "id": rec.get("uuid"),
                        "name": rec.get("name"),
                        "status": rec.get("status") or "planning",
                    }
                    # Get epic progress
                    try:
                        progress = await self.get_epic_progress(epic_info["id"])
                        epic_info["progress_pct"] = progress.get("completion_pct", 0.0)
                        epic_info["total_tasks"] = progress.get("total_tasks", 0)
                    except Exception:
                        epic_info["progress_pct"] = 0.0
                        epic_info["total_tasks"] = 0
                    epics.append(epic_info)
            except Exception as epic_err:
                log.debug("Failed to fetch epics", error=str(epic_err))

            return {
                "status_counts": status_counts,
                "total_tasks": total,
                "progress_pct": round((done / total * 100) if total > 0 else 0, 1),
                "actionable_tasks": actionable,
                "critical_tasks": critical_tasks,
                "epics": epics,
            }

        except Exception as e:
            log.exception("Failed to get project summary", project_id=project_id, error=str(e))
            return {
                "status_counts": {},
                "total_tasks": 0,
                "progress_pct": 0.0,
                "actionable_tasks": [],
                "critical_tasks": [],
                "epics": [],
            }

    async def list_epics_for_project(
        self,
        project_id: str,
        status: str | None = None,
        limit: int = 50,
    ) -> list[Entity]:
        """Get all epics belonging to a project.

        Args:
            project_id: The project's unique identifier.
            status: Optional status filter (planning, in_progress, completed, etc.).
            limit: Maximum results to return.

        Returns:
            List of Epic entities belonging to the project.
        """
        log.debug("Fetching epics for project", project_id=project_id, status=status)

        try:
            status_clause = "AND n.status = $status" if status else ""
            query = f"""
                MATCH (n)
                WHERE n.entity_type = 'epic'
                  AND n.group_id = $group_id
                  AND n.project_id = $project_id
                  {status_clause}
                RETURN n.uuid AS uuid,
                       n.name AS name,
                       n.entity_type AS entity_type,
                       n.group_id AS group_id,
                       n.content AS content,
                       n.description AS description,
                       n.metadata AS metadata,
                       n.created_at AS created_at,
                       n.status AS status,
                       n.priority AS priority,
                       n.project_id AS project_id,
                       n.total_tasks AS total_tasks,
                       n.completed_tasks AS completed_tasks
                ORDER BY n.priority ASC, n.created_at DESC
                LIMIT $limit
            """

            params: dict[str, Any] = {
                "group_id": self._group_id,
                "project_id": project_id,
                "limit": limit,
            }
            if status:
                params["status"] = status

            result = await self._driver.execute_query(query, **params)

            entities: list[Entity] = []
            records = GraphClient.normalize_result(result)
            for record in records:
                try:
                    entity = self._record_to_entity(record)
                    entities.append(entity)
                except Exception as e:
                    log.debug("Failed to convert record", error=str(e))

            log.debug("Fetched epics for project", project_id=project_id, count=len(entities))
            return entities

        except Exception as e:
            log.exception("Failed to list epics for project", project_id=project_id, error=str(e))
            return []

    async def get_notes_for_task(
        self,
        task_id: str,
        limit: int = 50,
    ) -> list[Entity]:
        """Get all notes belonging to a task, ordered by creation time (newest first).

        Args:
            task_id: The task's unique identifier.
            limit: Maximum results to return.

        Returns:
            List of Note entities belonging to the task.
        """
        log.debug("Fetching notes for task", task_id=task_id, limit=limit)

        try:
            # Use BELONGS_TO relationship to find notes
            query = """
                MATCH (n)-[:BELONGS_TO]->(t)
                WHERE n.entity_type = 'note'
                  AND n.group_id = $group_id
                  AND t.uuid = $task_id
                RETURN n.uuid AS uuid,
                       n.name AS name,
                       n.entity_type AS entity_type,
                       n.group_id AS group_id,
                       n.content AS content,
                       n.description AS description,
                       n.metadata AS metadata,
                       n.created_at AS created_at
                ORDER BY n.created_at DESC
                LIMIT $limit
            """

            params: dict[str, Any] = {
                "group_id": self._group_id,
                "task_id": task_id,
                "limit": limit,
            }

            result = await self._driver.execute_query(query, **params)

            entities: list[Entity] = []
            records = GraphClient.normalize_result(result)
            for record in records:
                try:
                    entity = self._record_to_entity(record)
                    entities.append(entity)
                except Exception as e:
                    log.debug("Failed to convert note record", error=str(e))

            log.debug("Fetched notes for task", task_id=task_id, count=len(entities))
            return entities

        except Exception as e:
            log.exception("Failed to get notes for task", task_id=task_id, error=str(e))
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

        # Build entity kwargs, only including datetime fields if present
        # Use `or ""` to convert None to empty string for required string fields
        entity_kwargs: dict[str, Any] = {
            "id": node_data.get("uuid") or "",
            "name": node_data.get("name") or "",
            "entity_type": entity_type,
            "description": node_data.get("description") or node_data.get("summary") or "",
            "content": node_data.get("content") or "",
            "organization_id": node_data.get("group_id") or metadata.get("organization_id"),
            "created_by": metadata.get("created_by"),
            "modified_by": metadata.get("modified_by"),
            "metadata": metadata,
        }
        if created_at := self._parse_datetime(node_data.get("created_at")):
            entity_kwargs["created_at"] = created_at
        if updated_at := self._parse_datetime(node_data.get("updated_at")):
            entity_kwargs["updated_at"] = updated_at

        return Entity(**entity_kwargs)

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

        def add_fields(fields: tuple[str, ...]) -> None:
            for field in fields:
                value = getattr(entity, field, None)
                if value is None:
                    value = entity.metadata.get(field)
                if value is not None:
                    if isinstance(value, datetime):
                        props[field] = value.isoformat()
                    elif hasattr(value, "value"):
                        props[field] = value.value
                    else:
                        props[field] = value

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

        # Task/Epic-specific fields (if present)
        task_fields = (
            "status",
            "priority",
            "task_order",
            "project_id",
            "epic_id",
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
        add_fields(task_fields)

        if isinstance(entity, AgentRecord):
            add_fields(
                (
                    "agent_type",
                    "spawn_source",
                    "status",
                    "project_id",
                    "task_id",
                    "worktree_id",
                    "worktree_path",
                    "worktree_branch",
                    "task_orchestrator_id",
                    "standalone",
                    "started_at",
                    "last_heartbeat",
                    "completed_at",
                )
            )
        elif isinstance(entity, TaskOrchestratorRecord):
            add_fields(
                (
                    "project_id",
                    "meta_orchestrator_id",
                    "task_id",
                    "worker_id",
                    "worktree_id",
                    "status",
                    "current_phase",
                    "pending_approval_id",
                )
            )
        elif isinstance(entity, MetaOrchestratorRecord):
            add_fields(
                (
                    "project_id",
                    "current_epic_id",
                    "status",
                    "strategy",
                    "max_concurrent",
                )
            )
        elif isinstance(entity, WorktreeRecord):
            add_fields(
                (
                    "task_id",
                    "agent_id",
                    "path",
                    "branch",
                    "base_commit",
                    "status",
                    "last_used",
                    "has_uncommitted",
                    "last_commit",
                )
            )
        elif isinstance(entity, AgentCheckpoint):
            add_fields(
                (
                    "agent_id",
                    "session_id",
                    "pending_approval_id",
                    "waiting_for_task_id",
                    "current_step",
                )
            )

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
        # Start with explicit metadata
        metadata = dict(entity.metadata or {})

        # Add Task-specific fields
        if isinstance(entity, Task):
            metadata["status"] = entity.status.value if entity.status else "todo"
            metadata["priority"] = entity.priority.value if entity.priority else "medium"
            metadata["project_id"] = entity.project_id
            metadata["epic_id"] = entity.epic_id
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

        # Add Epic-specific fields
        elif isinstance(entity, Epic):
            metadata["status"] = entity.status.value if entity.status else "planning"
            metadata["priority"] = entity.priority.value if entity.priority else "medium"
            metadata["project_id"] = entity.project_id
            if entity.assignees:
                metadata["assignees"] = entity.assignees
            if entity.target_date:
                metadata["target_date"] = entity.target_date.isoformat()
            if entity.learnings:
                metadata["learnings"] = entity.learnings

        # Add Note-specific fields
        elif isinstance(entity, Note):
            metadata["task_id"] = entity.task_id
            metadata["author_type"] = entity.author_type.value if entity.author_type else "user"
            metadata["author_name"] = entity.author_name

        # Add AgentRecord-specific fields
        elif isinstance(entity, AgentRecord):
            metadata["agent_type"] = entity.agent_type.value if entity.agent_type else "general"
            metadata["spawn_source"] = entity.spawn_source.value if entity.spawn_source else "user"
            metadata["status"] = entity.status.value if entity.status else "initializing"
            metadata["project_id"] = entity.project_id
            metadata["task_id"] = entity.task_id
            metadata["worktree_id"] = entity.worktree_id
            metadata["created_by"] = entity.created_by
            metadata["worktree_path"] = entity.worktree_path
            metadata["worktree_branch"] = entity.worktree_branch
            metadata["task_orchestrator_id"] = entity.task_orchestrator_id
            metadata["standalone"] = entity.standalone
            metadata["session_id"] = entity.session_id
            metadata["checkpoint_id"] = entity.checkpoint_id
            metadata["conversation_turns"] = entity.conversation_turns
            metadata["tokens_used"] = entity.tokens_used
            metadata["cost_usd"] = entity.cost_usd
            if entity.initial_prompt:
                metadata["initial_prompt"] = entity.initial_prompt
            if entity.system_prompt_hash:
                metadata["system_prompt_hash"] = entity.system_prompt_hash
            if entity.started_at:
                metadata["started_at"] = entity.started_at.isoformat()
            if entity.last_heartbeat:
                metadata["last_heartbeat"] = entity.last_heartbeat.isoformat()
            if entity.completed_at:
                metadata["completed_at"] = entity.completed_at.isoformat()
            if entity.paused_reason:
                metadata["paused_reason"] = entity.paused_reason

        # Add TaskOrchestrator-specific fields
        elif isinstance(entity, TaskOrchestratorRecord):
            metadata["project_id"] = entity.project_id
            metadata["meta_orchestrator_id"] = entity.meta_orchestrator_id
            metadata["task_id"] = entity.task_id
            metadata["worker_id"] = entity.worker_id
            metadata["worktree_id"] = entity.worktree_id
            metadata["status"] = entity.status.value if entity.status else "initializing"
            metadata["current_phase"] = (
                entity.current_phase.value if entity.current_phase else "implement"
            )
            metadata["rework_count"] = entity.rework_count
            metadata["max_rework_attempts"] = entity.max_rework_attempts
            metadata["gate_config"] = [gate.value for gate in entity.gate_config or []]
            metadata["gate_results"] = entity.gate_results
            metadata["pending_approval_id"] = entity.pending_approval_id
            metadata["total_tokens"] = entity.total_tokens
            metadata["total_cost_usd"] = entity.total_cost_usd

        # Add MetaOrchestrator-specific fields
        elif isinstance(entity, MetaOrchestratorRecord):
            metadata["project_id"] = entity.project_id
            metadata["current_epic_id"] = entity.current_epic_id
            metadata["task_queue"] = entity.task_queue
            metadata["active_orchestrators"] = entity.active_orchestrators
            metadata["status"] = entity.status.value if entity.status else "idle"
            metadata["strategy"] = entity.strategy.value if entity.strategy else "sequential"
            metadata["max_concurrent"] = entity.max_concurrent
            metadata["budget_usd"] = entity.budget_usd
            metadata["spent_usd"] = entity.spent_usd
            metadata["cost_alert_threshold"] = entity.cost_alert_threshold
            if entity.sprint_started_at:
                metadata["sprint_started_at"] = entity.sprint_started_at.isoformat()
            if entity.sprint_ends_at:
                metadata["sprint_ends_at"] = entity.sprint_ends_at.isoformat()
            metadata["tasks_completed"] = entity.tasks_completed
            metadata["tasks_failed"] = entity.tasks_failed
            metadata["total_rework_cycles"] = entity.total_rework_cycles

        # Add WorktreeRecord-specific fields
        elif isinstance(entity, WorktreeRecord):
            metadata["task_id"] = entity.task_id
            metadata["agent_id"] = entity.agent_id
            metadata["path"] = entity.path
            metadata["branch"] = entity.branch
            metadata["base_commit"] = entity.base_commit
            metadata["status"] = entity.status.value if entity.status else "active"
            metadata["last_used"] = entity.last_used.isoformat()
            metadata["has_uncommitted"] = entity.has_uncommitted
            metadata["last_commit"] = entity.last_commit

        # Add ApprovalRecord-specific fields
        elif isinstance(entity, ApprovalRecord):
            metadata["project_id"] = entity.project_id
            metadata["agent_id"] = entity.agent_id
            metadata["task_id"] = entity.task_id
            metadata["approval_type"] = (
                entity.approval_type.value if entity.approval_type else "dangerous_operation"
            )
            metadata["status"] = entity.status.value if entity.status else "pending"
            metadata["priority"] = entity.priority
            metadata["title"] = entity.title
            metadata["summary"] = entity.summary
            metadata["actions"] = entity.actions
            if entity.response_by:
                metadata["response_by"] = entity.response_by
            if entity.responded_at:
                metadata["responded_at"] = entity.responded_at.isoformat()
            if entity.response_message:
                metadata["response_message"] = entity.response_message

        # Add AgentCheckpoint-specific fields
        elif isinstance(entity, AgentCheckpoint):
            metadata["agent_id"] = entity.agent_id
            metadata["session_id"] = entity.session_id
            metadata["conversation_history"] = entity.conversation_history or []
            metadata["pending_tool_calls"] = entity.pending_tool_calls or []
            metadata["files_modified"] = entity.files_modified or []
            metadata["uncommitted_changes"] = entity.uncommitted_changes
            if entity.current_step:
                metadata["current_step"] = entity.current_step
            if entity.completed_steps:
                metadata["completed_steps"] = entity.completed_steps
            if entity.pending_approval_id:
                metadata["pending_approval_id"] = entity.pending_approval_id
            if entity.waiting_for_task_id:
                metadata["waiting_for_task_id"] = entity.waiting_for_task_id

        # Common fields (use getattr since not all entity types have these)
        if languages := getattr(entity, "languages", None):
            metadata["languages"] = languages
        if tags := getattr(entity, "tags", None):
            metadata["tags"] = tags
        if category := getattr(entity, "category", None):
            metadata["category"] = category

        return self._serialize_metadata(metadata)

    def _coerce_enum(self, enum_type: type[TEnum], value: Any, default: TEnum) -> TEnum:
        """Coerce a value into an enum, falling back to default on mismatch."""
        if value is None:
            return default
        if isinstance(value, enum_type):
            return value
        try:
            return enum_type(value)
        except (ValueError, TypeError):
            return default

    def _coerce_entity(self, entity: Entity) -> Entity:
        """Convert generic entities into their typed models when possible."""
        if isinstance(
            entity,
            (
                Task,
                AgentRecord,
                TaskOrchestratorRecord,
                MetaOrchestratorRecord,
                WorktreeRecord,
                AgentCheckpoint,
                ApprovalRecord,
            ),
        ):
            return entity

        try:
            if entity.entity_type == EntityType.TASK:
                return self._entity_to_task(entity)
            if entity.entity_type == EntityType.AGENT:
                return AgentRecord.from_entity(entity, self._group_id)
            if entity.entity_type == EntityType.TASK_ORCHESTRATOR:
                return TaskOrchestratorRecord.from_entity(entity, self._group_id)
            if entity.entity_type == EntityType.META_ORCHESTRATOR:
                return MetaOrchestratorRecord.from_entity(entity, self._group_id)
            if entity.entity_type == EntityType.WORKTREE:
                return self._entity_to_worktree(entity)
            if entity.entity_type == EntityType.CHECKPOINT:
                return AgentCheckpoint.from_entity(entity)
            if entity.entity_type == EntityType.APPROVAL:
                return self._entity_to_approval(entity)
        except Exception as exc:
            log.debug(
                "Failed to coerce entity",
                entity_id=entity.id,
                entity_type=entity.entity_type,
                error=str(exc),
            )

        return entity

    def _entity_to_task(self, entity: Entity) -> Task:
        """Hydrate a Task from a generic entity + metadata."""
        meta = entity.metadata or {}
        return Task(
            id=entity.id,
            entity_type=EntityType.TASK,
            name=entity.name,
            title=meta.get("title") or entity.name,
            description=entity.description or meta.get("description") or "",
            content=entity.content or meta.get("content") or "",
            organization_id=entity.organization_id,
            created_by=entity.created_by,
            modified_by=entity.modified_by,
            metadata=meta,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
            source_file=entity.source_file,
            embedding=entity.embedding,
            status=self._coerce_enum(TaskStatus, meta.get("status"), TaskStatus.TODO),
            priority=self._coerce_enum(TaskPriority, meta.get("priority"), TaskPriority.MEDIUM),
            task_order=meta.get("task_order", 0),
            project_id=meta.get("project_id"),
            epic_id=meta.get("epic_id"),
            feature=meta.get("feature"),
            sprint=meta.get("sprint"),
            assignees=meta.get("assignees", []),
            due_date=meta.get("due_date"),
            estimated_hours=meta.get("estimated_hours"),
            actual_hours=meta.get("actual_hours"),
            domain=meta.get("domain"),
            technologies=meta.get("technologies", []),
            complexity=self._coerce_enum(
                TaskComplexity,
                meta.get("complexity"),
                TaskComplexity.MEDIUM,
            ),
            tags=meta.get("tags", []),
            branch_name=meta.get("branch_name"),
            commit_shas=meta.get("commit_shas", []),
            pr_url=meta.get("pr_url"),
            learnings=meta.get("learnings", ""),
            blockers_encountered=meta.get("blockers_encountered", []),
            started_at=meta.get("started_at"),
            completed_at=meta.get("completed_at"),
            reviewed_at=meta.get("reviewed_at"),
            assigned_agent=meta.get("assigned_agent"),
            claimed_at=meta.get("claimed_at"),
            heartbeat_at=meta.get("heartbeat_at"),
            worktree_path=meta.get("worktree_path"),
            worktree_branch=meta.get("worktree_branch"),
            collaborators=meta.get("collaborators", []),
            handoff_history=meta.get("handoff_history", []),
            last_checkpoint=meta.get("last_checkpoint"),
        )

    def _entity_to_worktree(self, entity: Entity) -> Entity:
        """Hydrate a WorktreeRecord when required fields are present."""
        meta = entity.metadata or {}
        path = meta.get("path") or meta.get("worktree_path")
        branch = meta.get("branch") or meta.get("worktree_branch") or entity.name
        base_commit = meta.get("base_commit")
        task_id = meta.get("task_id")

        if not path or not branch or not base_commit or not task_id:
            return entity

        return WorktreeRecord(
            id=entity.id,
            entity_type=EntityType.WORKTREE,
            name=entity.name,
            description=entity.description,
            content=entity.content,
            organization_id=entity.organization_id or self._group_id,
            created_by=entity.created_by,
            modified_by=entity.modified_by,
            metadata=meta,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
            source_file=entity.source_file,
            task_id=task_id,
            agent_id=meta.get("agent_id"),
            path=path,
            branch=branch,
            base_commit=base_commit,
            status=self._coerce_enum(
                WorktreeStatus,
                meta.get("status"),
                WorktreeStatus.ACTIVE,
            ),
            last_used=meta.get("last_used") or datetime.now(UTC),
            has_uncommitted=meta.get("has_uncommitted", False),
            last_commit=meta.get("last_commit"),
        )

    def _entity_to_approval(self, entity: Entity) -> Entity:
        """Hydrate an ApprovalRecord when required fields are present."""
        meta = entity.metadata or {}
        project_id = meta.get("project_id")
        agent_id = meta.get("agent_id")
        if not project_id or not agent_id:
            return entity

        approval_metadata = meta.get("metadata")
        if not isinstance(approval_metadata, dict):
            excluded_keys = {
                "project_id",
                "agent_id",
                "task_id",
                "approval_type",
                "priority",
                "title",
                "summary",
                "actions",
                "status",
                "expires_at",
                "responded_at",
                "response_by",
                "response_message",
                "created_by",
                "modified_by",
            }
            approval_metadata = {k: v for k, v in meta.items() if k not in excluded_keys}

        return ApprovalRecord(
            id=entity.id,
            entity_type=EntityType.APPROVAL,
            name=entity.name,
            description=entity.description,
            content=entity.content,
            organization_id=entity.organization_id or self._group_id,
            created_by=entity.created_by,
            modified_by=entity.modified_by,
            metadata=approval_metadata,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
            source_file=entity.source_file,
            project_id=project_id,
            agent_id=agent_id,
            task_id=meta.get("task_id"),
            approval_type=self._coerce_enum(
                ApprovalType,
                meta.get("approval_type"),
                ApprovalType.DESTRUCTIVE_COMMAND,
            ),
            priority=meta.get("priority") or "medium",
            title=meta.get("title") or entity.name,
            summary=meta.get("summary") or entity.description or "",
            actions=meta.get("actions", ["approve", "deny"]),
            status=self._coerce_enum(
                ApprovalStatus,
                meta.get("status"),
                ApprovalStatus.PENDING,
            ),
            expires_at=meta.get("expires_at"),
            responded_at=meta.get("responded_at"),
            response_by=meta.get("response_by"),
            response_message=meta.get("response_message"),
        )

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
                        await node.save(self._driver)

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

        elif isinstance(entity, Epic):
            if entity.status:
                parts.append(f"Status: {entity.status}")
            if entity.priority:
                parts.append(f"Priority: {entity.priority}")
            if entity.project_id:
                parts.append(f"Project ID: {entity.project_id}")
            if entity.assignees:
                parts.append(f"Assignees: {', '.join(entity.assignees[:5])}")

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

        elif isinstance(entity, Note):
            if entity.task_id:
                parts.append(f"Task ID: {entity.task_id}")
            if entity.author_type:
                parts.append(f"Author Type: {entity.author_type}")
            if entity.author_name:
                parts.append(f"Author: {sanitize(entity.author_name)}")

        return parts

    def node_to_entity(self, node: EntityNode) -> Entity:
        """Convert a Graphiti EntityNode to our Entity model.

        Args:
            node: The EntityNode to convert.

        Returns:
            Converted Entity.
        """
        # Extract entity type from attributes first, then fall back to node labels
        entity_type_str = node.attributes.get("entity_type") or ""

        # If no entity_type attribute, check node labels (e.g., ["Entity", "task"])
        if not entity_type_str and node.labels:
            for label in node.labels:
                label_lower = label.lower()
                if label_lower != "entity":  # Skip the generic "Entity" label
                    try:
                        EntityType(label_lower)
                        entity_type_str = label_lower
                        break
                    except ValueError:
                        continue

        # Default to topic if still not found
        entity_type_str = entity_type_str or "topic"

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

    async def _get_node_entity_type(self, entity_id: str) -> EntityType | None:
        """Query for entity_type property directly from graph node.

        Graphiti's dataclass hydration doesn't include custom properties like
        entity_type that we persist via _persist_entity_attributes. This method
        directly queries the graph to retrieve it.

        Args:
            entity_id: The node's UUID.

        Returns:
            EntityType if found and valid, None otherwise.
        """
        try:
            result = await self._driver.execute_query(
                "MATCH (n {uuid: $id}) RETURN n.entity_type AS entity_type",
                id=entity_id,
            )
            # FalkorDB returns (rows, columns, stats) where rows is list of dicts
            if result and result[0]:
                rows = result[0]
                if rows and isinstance(rows[0], dict):
                    raw_type = rows[0].get("entity_type")
                    if raw_type:
                        return EntityType(raw_type)
        except (ValueError, IndexError, TypeError, KeyError):
            pass
        return None

    def _episodic_to_entity(
        self, node: EpisodicNode, entity_type_override: EntityType | None = None
    ) -> Entity:
        """Convert a Graphiti EpisodicNode to our Entity model.

        EpisodicNodes are created via add_episode() and have different structure
        than EntityNodes.

        Args:
            node: The EpisodicNode to convert.
            entity_type_override: Optional entity type from graph property lookup.
                Used when the node's Python object doesn't have the entity_type
                attribute (Graphiti dataclass doesn't hydrate custom properties).

        Returns:
            Converted Entity.
        """

        # EpisodicNode has: uuid, name, group_id, content, created_at, valid_at, source_description

        # Priority for entity_type:
        # 1. entity_type_override (from direct graph property query)
        # 2. node.entity_type attribute (if Graphiti ever hydrates it)
        # 3. Parse from name prefix (format: "type:name")
        # 4. Default to EPISODE
        entity_type: EntityType = entity_type_override or EntityType.EPISODE
        name = node.name

        # Try node attribute as fallback (may not be hydrated by Graphiti)
        if entity_type == EntityType.EPISODE and (
            node_entity_type := getattr(node, "entity_type", None)
        ):
            with contextlib.suppress(ValueError):
                entity_type = EntityType(node_entity_type)

        # Fallback: try to extract entity_type from the name (format: "type:name")
        if entity_type == EntityType.EPISODE and ":" in name:
            parts = name.split(":", 1)
            potential_type = parts[0].strip().lower()
            # Check if it's a valid entity type
            try:
                entity_type = EntityType(potential_type)
                name = parts[1].strip() if len(parts) > 1 else name
            except ValueError:
                pass  # Not a valid type prefix, use full name

        # Extract content and description from node (use getattr for type safety)
        content = getattr(node, "content", "") or ""
        description = getattr(node, "source_description", "") or ""

        # Build metadata dict
        metadata: dict[str, Any] = {}

        # Build entity kwargs, only including datetime fields if present
        entity_kwargs: dict[str, Any] = {
            "id": node.uuid,
            "entity_type": entity_type,
            "name": name,
            "description": description,
            "content": content,
            "organization_id": getattr(node, "group_id", None),
            "metadata": metadata,
        }
        if created_at := getattr(node, "created_at", None):
            entity_kwargs["created_at"] = created_at
            # Use created_at for updated_at if no explicit updated_at
            entity_kwargs["updated_at"] = created_at

        return Entity(**entity_kwargs)
