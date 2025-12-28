"""Mock implementations for testing MCP tools.

Provides mock versions of GraphClient, EntityManager, and RelationshipManager
that can be used to test tools without requiring a real FalkorDB connection.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sibyl.models.entities import Entity, EntityType, Relationship, RelationshipType


@dataclass
class MockGraphClient:
    """Mock GraphClient for testing.

    Simulates a connected graph client without actual FalkorDB connection.
    """

    _connected: bool = True

    @property
    def connected(self) -> bool:
        """Return connection status."""
        return self._connected

    @property
    def client(self) -> "MockGraphitiClient":
        """Return mock Graphiti client."""
        return MockGraphitiClient()

    @property
    def driver(self) -> "MockDriver":
        """Return mock driver (shortcut to client.driver)."""
        return self.client.driver

    async def connect(self) -> None:
        """Simulate connection."""
        self._connected = True

    async def disconnect(self) -> None:
        """Simulate disconnection."""
        self._connected = False

    @staticmethod
    def normalize_result(result: object) -> list[dict[str, Any]]:
        """Normalize query results to a consistent format."""
        if result is None:
            return []
        if isinstance(result, tuple):
            records = result[0] if len(result) > 0 else []
            return records if records else []
        if isinstance(result, list):
            return result
        return []

    async def execute_read(self, query: str, **params: object) -> list[dict[str, Any]]:
        """Execute a read query and normalize results."""
        result = await self.client.driver.execute_query(query, **params)
        return self.normalize_result(result)

    async def execute_write(self, query: str, **params: object) -> list[dict[str, Any]]:
        """Execute a write query and normalize results."""
        result = await self.client.driver.execute_query(query, **params)
        return self.normalize_result(result)


@dataclass
class MockGraphitiClient:
    """Mock Graphiti client with driver property."""

    @property
    def driver(self) -> "MockDriver":
        """Return mock driver."""
        return MockDriver()


@dataclass
class MockDriver:
    """Mock FalkorDB driver for query execution."""

    async def execute_query(self, query: str, **params: Any) -> list[Any]:
        """Execute a mock query - returns empty list by default."""
        return []


@dataclass
class MockEntityManager:
    """Mock EntityManager for testing.

    Stores entities in memory and provides basic CRUD operations.
    Can be pre-populated with test data.
    """

    _entities: dict[str, Entity] = field(default_factory=dict)
    _search_results: list[tuple[Entity, float]] = field(default_factory=list)

    def add_entity(self, entity: Entity) -> None:
        """Add an entity to the mock store."""
        self._entities[entity.id] = entity

    def set_search_results(self, results: list[tuple[Entity, float]]) -> None:
        """Set results for the next search call."""
        self._search_results = results

    async def create(self, entity: Entity) -> str:
        """Create entity and return ID."""
        entity_id = entity.id or f"entity-{uuid4().hex[:8]}"
        entity.id = entity_id
        self._entities[entity_id] = entity
        return entity_id

    async def create_direct(self, entity: Entity) -> str:
        """Create entity directly (same as create for mock)."""
        return await self.create(entity)

    async def get(self, entity_id: str) -> Entity:
        """Get entity by ID."""
        if entity_id not in self._entities:
            from sibyl.errors import EntityNotFoundError

            raise EntityNotFoundError("Entity", entity_id)
        return self._entities[entity_id]

    async def search(
        self,
        query: str,
        entity_types: list[EntityType] | None = None,
        limit: int = 10,
        **kwargs: Any,
    ) -> list[tuple[Entity, float]]:
        """Search entities - returns pre-configured results."""
        results = self._search_results[:limit]

        # Filter by entity type if specified
        if entity_types:
            results = [(e, s) for e, s in results if e.entity_type in entity_types]

        return results

    async def update(self, entity_id: str, updates: dict[str, Any]) -> Entity | None:
        """Update entity fields."""
        if entity_id not in self._entities:
            return None

        entity = self._entities[entity_id]
        for key, value in updates.items():
            if hasattr(entity, key):
                setattr(entity, key, value)
            elif entity.metadata is not None:
                entity.metadata[key] = value

        return entity

    async def delete(self, entity_id: str) -> bool:
        """Delete entity."""
        if entity_id in self._entities:
            del self._entities[entity_id]
            return True
        return False

    async def list_by_type(
        self,
        entity_type: EntityType,
        limit: int = 50,
        **kwargs: Any,
    ) -> list[Entity]:
        """List entities by type."""
        return [e for e in self._entities.values() if e.entity_type == entity_type][:limit]


@dataclass
class MockRelationshipManager:
    """Mock RelationshipManager for testing.

    Stores relationships in memory and provides basic operations.
    """

    _relationships: dict[str, Relationship] = field(default_factory=dict)

    def add_relationship(self, relationship: Relationship) -> None:
        """Add a relationship to the mock store."""
        rel_id = relationship.id or f"rel-{uuid4().hex[:8]}"
        relationship.id = rel_id
        self._relationships[rel_id] = relationship

    async def create(self, relationship: Relationship) -> str:
        """Create relationship and return ID."""
        rel_id = relationship.id or f"rel-{uuid4().hex[:8]}"
        relationship.id = rel_id
        self._relationships[rel_id] = relationship
        return rel_id

    async def get_for_entity(
        self,
        entity_id: str,
        direction: str = "both",
        relationship_types: list[RelationshipType] | None = None,
    ) -> list[Relationship]:
        """Get relationships for an entity."""
        results = []
        for rel in self._relationships.values():
            match = False
            if direction in ("outgoing", "both") and rel.source_id == entity_id:
                match = True
            if direction in ("incoming", "both") and rel.target_id == entity_id:
                match = True

            if match:
                if relationship_types is None or rel.relationship_type in relationship_types:
                    results.append(rel)

        return results

    async def get_related_entities(
        self,
        entity_id: str,
        relationship_types: list[RelationshipType] | None = None,
        max_depth: int = 1,
        limit: int = 50,
    ) -> list[tuple[Entity, Relationship]]:
        """Get entities related to a given entity.

        Returns list of (Entity, Relationship) tuples matching the real implementation.
        """
        results: list[tuple[Entity, Relationship]] = []
        for rel in self._relationships.values():
            # Check source -> target direction
            if rel.source_id == entity_id:
                if relationship_types is None or rel.relationship_type in relationship_types:
                    # Create a minimal Entity for the target
                    entity = Entity(
                        id=rel.target_id,
                        name=f"Entity-{rel.target_id[:8]}",
                        entity_type=EntityType.EPISODE,
                    )
                    results.append((entity, rel))
            # Check target -> source direction
            elif rel.target_id == entity_id:
                if relationship_types is None or rel.relationship_type in relationship_types:
                    entity = Entity(
                        id=rel.source_id,
                        name=f"Entity-{rel.source_id[:8]}",
                        entity_type=EntityType.EPISODE,
                    )
                    results.append((entity, rel))

        return results[:limit]

    async def delete(self, relationship_id: str) -> None:
        """Delete a relationship."""
        if relationship_id in self._relationships:
            del self._relationships[relationship_id]

    async def list_all(
        self,
        relationship_types: list[RelationshipType] | None = None,
        limit: int = 100,
    ) -> list[Relationship]:
        """List all relationships."""
        results = list(self._relationships.values())
        if relationship_types:
            results = [r for r in results if r.relationship_type in relationship_types]
        return results[:limit]


def create_test_entity(
    entity_type: EntityType = EntityType.EPISODE,
    name: str = "Test Entity",
    description: str = "Test description",
    entity_id: str | None = None,
    **kwargs: Any,
) -> Entity:
    """Create a test entity with sensible defaults."""
    return Entity(
        id=entity_id or f"{entity_type.value}-{uuid4().hex[:8]}",
        name=name,
        entity_type=entity_type,
        description=description,
        metadata=kwargs.get("metadata", {}),
        created_at=kwargs.get("created_at", datetime.now(UTC)),
    )


def create_test_relationship(
    source_id: str,
    target_id: str,
    relationship_type: RelationshipType = RelationshipType.RELATED_TO,
    **kwargs: Any,
) -> Relationship:
    """Create a test relationship with sensible defaults."""
    return Relationship(
        id=kwargs.get("id", f"rel-{uuid4().hex[:8]}"),
        source_id=source_id,
        target_id=target_id,
        relationship_type=relationship_type,
        properties=kwargs.get("properties", {}),
        created_at=kwargs.get("created_at", datetime.now(UTC)),
    )
