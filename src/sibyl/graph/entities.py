"""Entity management for the knowledge graph."""

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from graphiti_core.nodes import EntityNode
from pydantic import BaseModel

from sibyl.errors import EntityNotFoundError, SearchError
from sibyl.models.entities import Entity, EntityType

if TYPE_CHECKING:
    from sibyl.graph.client import GraphClient

log = structlog.get_logger()


class EntityManager:
    """Manages entity CRUD operations in the knowledge graph."""

    def __init__(self, client: "GraphClient") -> None:
        """Initialize entity manager with graph client."""
        self._client = client

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
            entity_types: dict[str, type[BaseModel]] = {
                entity.entity_type.value: BaseModel
            }

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
                group_id="conventions",
                entity_types=entity_types,
            )

            # Graphiti generates its own UUID - return the episode UUID
            created_uuid = result.episode.uuid
            log.info(
                "Entity created successfully",
                entity_id=entity.id,
                episode_uuid=created_uuid,
            )
            return created_uuid

        except Exception as e:
            log.exception("Failed to create entity", entity_id=entity.id, error=str(e))
            raise

    async def get(self, entity_id: str) -> Entity:
        """Get an entity by ID.

        Args:
            entity_id: The entity's unique identifier.

        Returns:
            The requested entity.

        Raises:
            EntityNotFoundError: If entity doesn't exist.
        """
        log.debug("Fetching entity", entity_id=entity_id)

        try:
            # Retrieve the node by UUID
            nodes = await EntityNode.get_by_uuids(
                self._client.client.driver,
                [entity_id]
            )

            if not nodes:
                raise EntityNotFoundError("Entity", entity_id)

            node = nodes[0]
            entity = self._node_to_entity(node)

            log.debug("Entity retrieved", entity_id=entity_id, entity_type=entity.entity_type)
            return entity

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
        """Semantic search for entities.

        Args:
            query: Natural language search query.
            entity_types: Optional filter by entity types.
            limit: Maximum results to return.

        Returns:
            List of (entity, score) tuples ordered by relevance.
        """
        log.info("Searching entities", query=query, types=entity_types, limit=limit)

        try:
            # Perform hybrid search using Graphiti
            edges = await self._client.client.search(
                query=query,
                group_ids=["conventions"],
                num_results=limit * 3,  # Get more results for filtering
            )

            # Extract unique nodes from edges
            node_uuids = set()
            for edge in edges:
                node_uuids.add(edge.source_node_uuid)
                node_uuids.add(edge.target_node_uuid)

            if not node_uuids:
                log.info("No search results found", query=query)
                return []

            # Retrieve full node details
            nodes = await EntityNode.get_by_uuids(
                self._client.client.driver,
                list(node_uuids)
            )

            # Convert nodes to entities and filter by type
            results: list[tuple[Entity, float]] = []
            for node in nodes:
                try:
                    entity = self._node_to_entity(node)

                    # Filter by entity types if specified
                    if entity_types and entity.entity_type not in entity_types:
                        continue

                    # Calculate relevance score (simple approach using node position)
                    # In a real implementation, you'd use embedding similarity
                    score = 1.0 / (len(results) + 1)

                    results.append((entity, score))

                    if len(results) >= limit:
                        break

                except Exception as e:
                    log.warning("Failed to convert node to entity", node_uuid=node.uuid, error=str(e))
                    continue

            log.info("Search completed", query=query, results_count=len(results))
            return results

        except Exception as e:
            log.exception("Search failed", query=query, error=str(e))
            raise SearchError(f"Search failed: {e}") from e

    async def update(self, entity: Entity) -> None:
        """Update an existing entity.

        Args:
            entity: The entity with updated values.
        """
        log.info("Updating entity", entity_id=entity.id, entity_type=entity.entity_type)

        try:
            # Graphiti doesn't have direct update - we need to:
            # 1. Retrieve the existing node
            # 2. Delete the old episode
            # 3. Create a new episode with updated data

            # First verify the entity exists
            nodes = await EntityNode.get_by_uuids(
                self._client.client.driver,
                [entity.id]
            )

            if not nodes:
                raise EntityNotFoundError("Entity", entity.id)

            # Update timestamp
            entity.updated_at = datetime.now(UTC)

            # Remove old episode (if it exists as an episode)
            try:
                await self._client.client.remove_episode(entity.id)
            except Exception as e:
                log.debug("No episode to remove or removal failed", entity_id=entity.id, error=str(e))

            # Create new episode with updated data
            await self.create(entity)

            log.info("Entity updated successfully", entity_id=entity.id)

        except EntityNotFoundError:
            raise
        except Exception as e:
            log.exception("Failed to update entity", entity_id=entity.id, error=str(e))
            raise

    async def delete(self, entity_id: str) -> None:
        """Delete an entity from the graph.

        Args:
            entity_id: The entity's unique identifier.
        """
        log.info("Deleting entity", entity_id=entity_id)

        try:
            # Verify entity exists
            nodes = await EntityNode.get_by_uuids(
                self._client.client.driver,
                [entity_id]
            )

            if not nodes:
                raise EntityNotFoundError("Entity", entity_id)

            # Remove the episode (this will cascade to nodes/edges if they're only in this episode)
            await self._client.client.remove_episode(entity_id)

            log.info("Entity deleted successfully", entity_id=entity_id)

        except EntityNotFoundError:
            raise
        except Exception as e:
            log.exception("Failed to delete entity", entity_id=entity_id, error=str(e))
            raise

    async def list_by_type(
        self,
        entity_type: EntityType,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Entity]:
        """List all entities of a specific type.

        Args:
            entity_type: The type of entities to list.
            limit: Maximum results to return.
            offset: Pagination offset.

        Returns:
            List of entities.
        """
        log.debug("Listing entities", entity_type=entity_type, limit=limit, offset=offset)

        try:
            # Get all nodes in the conventions group
            # Note: Graphiti doesn't have built-in pagination/filtering by attributes
            # So we retrieve more than needed and filter in memory
            nodes = await EntityNode.get_by_group_ids(
                self._client.client.driver,
                ["conventions"],
                limit=limit * 5,  # Get extra for filtering
            )

            # Convert nodes to entities and filter by type
            entities: list[Entity] = []
            skipped = 0

            for node in nodes:
                try:
                    entity = self._node_to_entity(node)

                    # Filter by entity type
                    if entity.entity_type != entity_type:
                        continue

                    # Handle offset
                    if skipped < offset:
                        skipped += 1
                        continue

                    entities.append(entity)

                    if len(entities) >= limit:
                        break

                except Exception as e:
                    log.warning("Failed to convert node to entity", node_uuid=node.uuid, error=str(e))
                    continue

            log.debug(
                "Listed entities",
                entity_type=entity_type,
                count=len(entities),
                limit=limit,
            )
            return entities

        except Exception as e:
            log.exception("Failed to list entities", entity_type=entity_type, error=str(e))
            return []

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

        return "\n".join(parts)

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
            k: v for k, v in node.attributes.items()
            if k not in {"entity_type", "description", "content", "source_file"}
        }

        return Entity(
            id=node.uuid,
            entity_type=entity_type,
            name=node.name,
            description=description,
            content=content,
            metadata=metadata,
            created_at=node.created_at,
            updated_at=node.created_at,  # Graphiti doesn't track updated_at
            source_file=source_file,
            embedding=node.name_embedding if node.name_embedding else None,
        )
