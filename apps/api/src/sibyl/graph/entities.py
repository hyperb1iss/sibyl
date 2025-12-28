"""Entity management for the knowledge graph.

This module provides entity CRUD operations using Graphiti's native node APIs.
All graph operations go through EntityNode/EpisodicNode rather than raw Cypher.
"""

import json
import re
from datetime import UTC, datetime
from typing import Any

import structlog
from graphiti_core.nodes import EntityNode, EpisodicNode
from graphiti_core.search.search_config_recipes import NODE_HYBRID_SEARCH_RRF
from pydantic import BaseModel

from sibyl.errors import EntityNotFoundError, SearchError
from sibyl.graph.client import GraphClient
from sibyl.models.entities import Entity, EntityType
from sibyl.models.sources import Community, Document, Source
from sibyl.models.tasks import Epic, ErrorPattern, Milestone, Project, Task, Team

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
                await node.save(self._driver)

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
                    return entity
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

                    # Parse metadata for filtering (stored as JSON string)
                    metadata = entity.metadata or {}

                    # Filter by project_id from metadata
                    if project_id and metadata.get("project_id") != project_id:
                        continue

                    # Filter by status from metadata
                    if status:
                        entity_status = metadata.get("status")
                        if entity_status != status:
                            continue

                    # Filter by priority from metadata
                    if priority:
                        entity_priority = metadata.get("priority")
                        if entity_priority != priority:
                            continue

                    # Filter by complexity from metadata
                    if complexity:
                        entity_complexity = metadata.get("complexity")
                        if entity_complexity != complexity:
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

    def _format_specialized_fields(  # noqa: PLR0915
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
