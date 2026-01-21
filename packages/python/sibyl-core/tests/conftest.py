"""Pytest fixtures and mock infrastructure for sibyl-core tests.

This module provides:
- MockGraphClient: Simulates FalkorDB operations with in-memory storage
- MockEntityManager: In-memory CRUD for entities with query tracking
- MockRelationshipManager: In-memory relationship management
- Factory functions: make_task, make_entity, make_project, make_epic, make_note
- Shared fixtures for common test setups

Usage:
    def test_something(mock_entity_manager, make_task):
        task = make_task(title="Test task", status=TaskStatus.DOING)
        mock_entity_manager.entities[task.id] = task
        # ... test logic
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.models.tasks import (
    AuthorType,
    Epic,
    EpicStatus,
    Milestone,
    Note,
    Project,
    ProjectStatus,
    Task,
    TaskComplexity,
    TaskPriority,
    TaskStatus,
)

# =============================================================================
# Query Tracking for Test Assertions
# =============================================================================


@dataclass
class QueryRecord:
    """Record of an executed query for test assertions."""

    query: str
    params: dict[str, Any]
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    operation: str = "read"  # "read" or "write"

    def matches_pattern(self, pattern: str) -> bool:
        """Check if query matches a regex pattern."""
        return bool(re.search(pattern, self.query, re.IGNORECASE))


# =============================================================================
# Mock FalkorDB/Graph Client
# =============================================================================


@dataclass
class MockGraphClient:
    """Mock GraphClient simulating FalkorDB operations.

    Provides in-memory storage and query tracking for testing graph operations
    without requiring a real FalkorDB connection.

    Attributes:
        nodes: In-memory node storage keyed by UUID.
        edges: In-memory edge storage keyed by UUID.
        query_history: List of executed queries for assertions.
        default_results: Default results to return from queries.
        custom_handlers: Dict of query pattern -> handler functions.
    """

    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: dict[str, dict[str, Any]] = field(default_factory=dict)
    query_history: list[QueryRecord] = field(default_factory=list)
    default_results: list[dict[str, Any]] = field(default_factory=list)
    custom_handlers: dict[str, Any] = field(default_factory=dict)
    _connected: bool = False

    # -------------------------------------------------------------------------
    # Connection Management
    # -------------------------------------------------------------------------

    async def connect(self) -> None:
        """Simulate connection to FalkorDB."""
        self._connected = True

    async def disconnect(self) -> None:
        """Simulate disconnection from FalkorDB."""
        self._connected = False

    @property
    def is_connected(self) -> bool:
        """Check if client is connected."""
        return self._connected

    # -------------------------------------------------------------------------
    # Query Execution
    # -------------------------------------------------------------------------

    async def execute_read(self, query: str, **params: Any) -> list[dict[str, Any]]:
        """Execute a read query (deprecated, use execute_read_org)."""
        self.query_history.append(QueryRecord(query=query, params=params, operation="read"))
        return self._process_query(query, params)

    async def execute_write(self, query: str, **params: Any) -> list[dict[str, Any]]:
        """Execute a write query (deprecated, use execute_write_org)."""
        self.query_history.append(QueryRecord(query=query, params=params, operation="write"))
        return self._process_query(query, params)

    async def execute_read_org(
        self, query: str, organization_id: str, **params: Any
    ) -> list[dict[str, Any]]:
        """Execute a read query scoped to an organization."""
        self.query_history.append(
            QueryRecord(
                query=query,
                params={"organization_id": organization_id, **params},
                operation="read",
            )
        )
        return self._process_query(query, params, org_id=organization_id)

    async def execute_write_org(
        self, query: str, organization_id: str, **params: Any
    ) -> list[dict[str, Any]]:
        """Execute a write query scoped to an organization."""
        self.query_history.append(
            QueryRecord(
                query=query,
                params={"organization_id": organization_id, **params},
                operation="write",
            )
        )
        return self._process_query(query, params, org_id=organization_id)

    def _process_query(
        self, query: str, params: dict[str, Any], org_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Process a query against in-memory storage.

        Supports basic MATCH/CREATE/SET patterns for common test scenarios.
        Falls back to default_results for unrecognized queries.
        """
        # Check custom handlers first
        for pattern, handler in self.custom_handlers.items():
            if re.search(pattern, query, re.IGNORECASE):
                return handler(query, params, org_id)

        # Handle MATCH by uuid
        if "MATCH" in query.upper() and "uuid" in params:
            node_id = params.get("uuid") or params.get("entity_id")
            if node_id and node_id in self.nodes:
                node = self.nodes[node_id]
                # Filter by org if specified
                if org_id and node.get("group_id") != org_id:
                    return []
                return [node]

        # Handle MATCH by entity_type
        if "entity_type" in params:
            entity_type = params["entity_type"]
            group_id = params.get("group_id") or org_id
            results = []
            for node in self.nodes.values():
                if node.get("entity_type") == entity_type and (
                    group_id is None or node.get("group_id") == group_id
                ):
                    results.append(node)
            return results

        # Return default results for unhandled queries
        return self.default_results

    # -------------------------------------------------------------------------
    # Org-scoped Driver
    # -------------------------------------------------------------------------

    def get_org_driver(self, organization_id: str) -> MockOrgDriver:
        """Get a driver scoped to a specific organization."""
        if not organization_id:
            raise ValueError("organization_id is required")
        return MockOrgDriver(client=self, organization_id=organization_id)

    # -------------------------------------------------------------------------
    # Static Helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def normalize_result(result: Any) -> list[dict[str, Any]]:
        """Normalize query results to consistent format."""
        if result is None:
            return []
        if isinstance(result, tuple):
            records = result[0] if len(result) > 0 else []
            return records if records else []
        if isinstance(result, list):
            return result
        return []

    # -------------------------------------------------------------------------
    # Test Assertion Helpers
    # -------------------------------------------------------------------------

    def get_queries_matching(self, pattern: str) -> list[QueryRecord]:
        """Get all queries matching a regex pattern."""
        return [q for q in self.query_history if q.matches_pattern(pattern)]

    def assert_query_executed(self, pattern: str, msg: str = "") -> None:
        """Assert that at least one query matching pattern was executed."""
        matches = self.get_queries_matching(pattern)
        if not matches:
            query_list = "\n".join(f"  - {q.query[:100]}" for q in self.query_history)
            raise AssertionError(
                f"No query matching '{pattern}' was executed. {msg}\n"
                f"Executed queries:\n{query_list or '  (none)'}"
            )

    def reset_query_history(self) -> None:
        """Clear query history for fresh assertions."""
        self.query_history.clear()


@dataclass
class MockOrgDriver:
    """Mock driver scoped to a specific organization's graph."""

    client: MockGraphClient
    organization_id: str

    async def execute_query(self, query: str, **params: Any) -> list[dict[str, Any]]:
        """Execute query against org-specific graph."""
        return await self.client.execute_read_org(query, self.organization_id, **params)

    def clone(self, new_org_id: str) -> MockOrgDriver:
        """Clone driver for a different org."""
        return MockOrgDriver(client=self.client, organization_id=new_org_id)


# =============================================================================
# Mock Entity Manager
# =============================================================================


@dataclass
class MockEntityManager:
    """Mock EntityManager with in-memory storage.

    Simulates EntityManager operations for testing without FalkorDB.
    Tracks all operations for test assertions.

    Attributes:
        entities: In-memory entity storage keyed by ID.
        search_results: Preconfigured search results.
        operation_history: List of operations for assertions.
        group_id: Organization ID for multi-tenancy.
    """

    entities: dict[str, Entity] = field(default_factory=dict)
    search_results: list[tuple[Entity, float]] = field(default_factory=list)
    operation_history: list[dict[str, Any]] = field(default_factory=list)
    group_id: str = "test-org-id"

    # -------------------------------------------------------------------------
    # CRUD Operations
    # -------------------------------------------------------------------------

    async def create(self, entity: Entity) -> str:
        """Create a new entity."""
        self.entities[entity.id] = entity
        self.operation_history.append(
            {"op": "create", "entity_id": entity.id, "entity_type": entity.entity_type}
        )
        return entity.id

    async def create_direct(self, entity: Entity, *, generate_embedding: bool = True) -> str:
        """Create entity directly (bypassing LLM)."""
        return await self.create(entity)

    async def get(self, entity_id: str) -> Entity:
        """Get entity by ID."""
        if entity_id not in self.entities:
            from sibyl_core.errors import EntityNotFoundError

            raise EntityNotFoundError("Entity", entity_id)
        self.operation_history.append({"op": "get", "entity_id": entity_id})
        return self.entities[entity_id]

    async def update(self, entity_id: str, updates: dict[str, Any]) -> Entity:
        """Update entity with partial updates."""
        if entity_id not in self.entities:
            from sibyl_core.errors import EntityNotFoundError

            raise EntityNotFoundError("Entity", entity_id)

        existing = self.entities[entity_id]
        merged_metadata = {**(existing.metadata or {}), **(updates.get("metadata") or {})}

        # Apply updates
        updated = Entity(
            id=existing.id,
            name=updates.get("name", existing.name),
            entity_type=existing.entity_type,
            description=updates.get("description", existing.description),
            content=updates.get("content", existing.content),
            metadata=merged_metadata,
            created_at=existing.created_at,
            updated_at=datetime.now(UTC),
            source_file=updates.get("source_file", existing.source_file),
        )
        self.entities[entity_id] = updated
        self.operation_history.append({"op": "update", "entity_id": entity_id, "updates": updates})
        return updated

    async def delete(self, entity_id: str) -> bool:
        """Delete entity by ID."""
        if entity_id not in self.entities:
            return False
        del self.entities[entity_id]
        self.operation_history.append({"op": "delete", "entity_id": entity_id})
        return True

    # -------------------------------------------------------------------------
    # Query Operations
    # -------------------------------------------------------------------------

    async def search(
        self,
        query: str,
        entity_types: list[EntityType] | None = None,
        limit: int = 10,
    ) -> list[tuple[Entity, float]]:
        """Semantic search for entities."""
        self.operation_history.append(
            {"op": "search", "query": query, "types": entity_types, "limit": limit}
        )

        # If preconfigured results exist, filter and return them
        if self.search_results:
            results = self.search_results
            if entity_types:
                results = [(e, s) for e, s in results if e.entity_type in entity_types]
            return results[:limit]

        # Fallback: simple text matching on name/description
        results = []
        query_lower = query.lower()
        for entity in self.entities.values():
            if entity_types and entity.entity_type not in entity_types:
                continue
            # Simple relevance scoring
            score = 0.0
            if query_lower in entity.name.lower():
                score = 0.9
            elif query_lower in (entity.description or "").lower():
                score = 0.7
            elif query_lower in (entity.content or "").lower():
                score = 0.5
            if score > 0:
                results.append((entity, score))

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:limit]

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
        """List entities by type with optional filters."""
        self.operation_history.append(
            {
                "op": "list_by_type",
                "entity_type": entity_type,
                "project_id": project_id,
                "status": status,
            }
        )

        results = []
        for entity in self.entities.values():
            if entity.entity_type != entity_type:
                continue

            metadata = entity.metadata or {}

            # Apply filters
            if project_id and metadata.get("project_id") != project_id:
                continue
            if epic_id and metadata.get("epic_id") != epic_id:
                continue
            if no_epic and metadata.get("epic_id"):
                continue
            if status:
                status_list = [s.strip().lower() for s in status.split(",")]
                if metadata.get("status", "").lower() not in status_list:
                    continue
            if priority:
                priority_list = [p.strip().lower() for p in priority.split(",")]
                if metadata.get("priority", "").lower() not in priority_list:
                    continue
            if not include_archived and metadata.get("status") == "archived":
                continue

            results.append(entity)

        return results[offset : offset + limit]

    async def list_all(
        self,
        limit: int = 1000,
        offset: int = 0,
        *,
        include_archived: bool = False,
    ) -> list[Entity]:
        """List all entities."""
        results = list(self.entities.values())
        if not include_archived:
            results = [e for e in results if (e.metadata or {}).get("status") != "archived"]
        return results[offset : offset + limit]

    async def get_tasks_for_epic(
        self, epic_id: str, status: str | None = None, limit: int = 100
    ) -> list[Entity]:
        """Get tasks belonging to an epic."""
        return await self.list_by_type(EntityType.TASK, epic_id=epic_id, status=status, limit=limit)

    async def get_epic_progress(self, epic_id: str) -> dict[str, Any]:
        """Get progress statistics for an epic."""
        tasks = await self.get_tasks_for_epic(epic_id)
        total = len(tasks)
        done = sum(1 for t in tasks if (t.metadata or {}).get("status") == "done")
        doing = sum(1 for t in tasks if (t.metadata or {}).get("status") == "doing")

        return {
            "total_tasks": total,
            "completed_tasks": done,
            "in_progress_tasks": doing,
            "completion_pct": round((done / total * 100) if total > 0 else 0, 1),
        }

    async def get_notes_for_task(self, task_id: str, limit: int = 50) -> list[Entity]:
        """Get notes for a task."""
        results = []
        for entity in self.entities.values():
            if (
                entity.entity_type == EntityType.NOTE
                and (entity.metadata or {}).get("task_id") == task_id
            ):
                results.append(entity)
        return results[:limit]

    # -------------------------------------------------------------------------
    # Test Helpers
    # -------------------------------------------------------------------------

    def add_entity(self, entity: Entity) -> None:
        """Synchronously add entity (for test setup)."""
        self.entities[entity.id] = entity

    def clear(self) -> None:
        """Clear all entities and history."""
        self.entities.clear()
        self.operation_history.clear()

    def get_operations(self, op_type: str) -> list[dict[str, Any]]:
        """Get operations of a specific type."""
        return [op for op in self.operation_history if op["op"] == op_type]


# =============================================================================
# Mock Relationship Manager
# =============================================================================


@dataclass
class MockRelationshipManager:
    """Mock RelationshipManager with in-memory storage."""

    relationships: dict[str, Relationship] = field(default_factory=dict)
    operation_history: list[dict[str, Any]] = field(default_factory=list)
    group_id: str = "test-org-id"

    async def create(self, relationship: Relationship) -> str:
        """Create a new relationship."""
        rel_id = relationship.id or str(uuid.uuid4())
        relationship.id = rel_id
        self.relationships[rel_id] = relationship
        self.operation_history.append(
            {
                "op": "create",
                "id": rel_id,
                "type": relationship.relationship_type,
                "source": relationship.source_id,
                "target": relationship.target_id,
            }
        )
        return rel_id

    async def get(self, relationship_id: str) -> Relationship:
        """Get relationship by ID."""
        if relationship_id not in self.relationships:
            raise KeyError(f"Relationship not found: {relationship_id}")
        return self.relationships[relationship_id]

    async def delete(self, relationship_id: str) -> bool:
        """Delete relationship by ID."""
        if relationship_id not in self.relationships:
            return False
        del self.relationships[relationship_id]
        return True

    async def get_for_entity(
        self,
        entity_id: str,
        relationship_types: list[RelationshipType] | None = None,
        direction: str = "both",
    ) -> list[Relationship]:
        """Get relationships for an entity."""
        results = []
        for rel in self.relationships.values():
            is_source = rel.source_id == entity_id
            is_target = rel.target_id == entity_id

            if direction == "outgoing" and not is_source:
                continue
            if direction == "incoming" and not is_target:
                continue
            if direction == "both" and not (is_source or is_target):
                continue

            if relationship_types and rel.relationship_type not in relationship_types:
                continue

            results.append(rel)
        return results

    async def get_between(self, source_id: str, target_id: str) -> list[Relationship]:
        """Get relationships between two entities."""
        return [
            rel
            for rel in self.relationships.values()
            if rel.source_id == source_id and rel.target_id == target_id
        ]


# =============================================================================
# Entity Factory Functions
# =============================================================================


def make_task(
    task_id: str | None = None,
    title: str = "Test task",
    status: TaskStatus = TaskStatus.TODO,
    priority: TaskPriority = TaskPriority.MEDIUM,
    complexity: TaskComplexity = TaskComplexity.MEDIUM,
    project_id: str | None = None,
    epic_id: str | None = None,
    feature: str | None = None,
    description: str = "",
    tags: list[str] | None = None,
    technologies: list[str] | None = None,
    assignees: list[str] | None = None,
    domain: str | None = None,
    organization_id: str = "test-org-id",
    **kwargs: Any,
) -> Task:
    """Factory for creating test Task instances.

    Args:
        task_id: Unique ID (auto-generated if not provided).
        title: Task title.
        status: Task status.
        priority: Task priority.
        complexity: Task complexity.
        project_id: Parent project ID.
        epic_id: Parent epic ID.
        feature: Feature area.
        description: Task description.
        tags: Task tags.
        technologies: Technologies involved.
        assignees: Assigned team members.
        domain: Knowledge domain.
        organization_id: Organization scope.
        **kwargs: Additional Task fields.

    Returns:
        Configured Task instance.
    """
    return Task(
        id=task_id or f"task_{uuid.uuid4().hex[:8]}",
        name=title,
        title=title,
        description=description,
        status=status,
        priority=priority,
        complexity=complexity,
        project_id=project_id,
        epic_id=epic_id,
        feature=feature,
        tags=tags or [],
        technologies=technologies or [],
        assignees=assignees or [],
        domain=domain,
        organization_id=organization_id,
        **kwargs,
    )


def make_entity(
    entity_id: str | None = None,
    name: str = "Test entity",
    entity_type: EntityType = EntityType.TOPIC,
    description: str = "",
    content: str = "",
    metadata: dict[str, Any] | None = None,
    organization_id: str = "test-org-id",
    **kwargs: Any,
) -> Entity:
    """Factory for creating test Entity instances.

    Args:
        entity_id: Unique ID (auto-generated if not provided).
        name: Entity name.
        entity_type: Type of entity.
        description: Entity description.
        content: Entity content.
        metadata: Additional metadata.
        organization_id: Organization scope.
        **kwargs: Additional Entity fields.

    Returns:
        Configured Entity instance.
    """
    return Entity(
        id=entity_id or f"entity_{uuid.uuid4().hex[:8]}",
        name=name,
        entity_type=entity_type,
        description=description,
        content=content,
        metadata=metadata or {},
        organization_id=organization_id,
        **kwargs,
    )


def make_project(
    project_id: str | None = None,
    title: str = "Test project",
    description: str = "",
    status: ProjectStatus = ProjectStatus.ACTIVE,
    repository_url: str | None = None,
    tech_stack: list[str] | None = None,
    features: list[str] | None = None,
    organization_id: str = "test-org-id",
    **kwargs: Any,
) -> Project:
    """Factory for creating test Project instances.

    Args:
        project_id: Unique ID (auto-generated if not provided).
        title: Project title.
        description: Project description.
        status: Project status.
        repository_url: GitHub repository URL.
        tech_stack: Technologies used.
        features: Major feature areas.
        organization_id: Organization scope.
        **kwargs: Additional Project fields.

    Returns:
        Configured Project instance.
    """
    return Project(
        id=project_id or f"project_{uuid.uuid4().hex[:8]}",
        name=title,
        title=title,
        description=description,
        status=status,
        repository_url=repository_url,
        tech_stack=tech_stack or [],
        features=features or [],
        organization_id=organization_id,
        **kwargs,
    )


def make_epic(
    epic_id: str | None = None,
    title: str = "Test epic",
    description: str = "",
    project_id: str = "test-project-id",
    status: EpicStatus = EpicStatus.PLANNING,
    priority: TaskPriority = TaskPriority.MEDIUM,
    assignees: list[str] | None = None,
    organization_id: str = "test-org-id",
    **kwargs: Any,
) -> Epic:
    """Factory for creating test Epic instances.

    Args:
        epic_id: Unique ID (auto-generated if not provided).
        title: Epic title.
        description: Epic description.
        project_id: Parent project ID.
        status: Epic status.
        priority: Epic priority.
        assignees: Epic leads/owners.
        organization_id: Organization scope.
        **kwargs: Additional Epic fields.

    Returns:
        Configured Epic instance.
    """
    return Epic(
        id=epic_id or f"epic_{uuid.uuid4().hex[:8]}",
        name=title,
        title=title,
        description=description,
        project_id=project_id,
        status=status,
        priority=priority,
        assignees=assignees or [],
        organization_id=organization_id,
        **kwargs,
    )


def make_note(
    note_id: str | None = None,
    content: str = "Test note content",
    task_id: str = "test-task-id",
    author_type: AuthorType = AuthorType.USER,
    author_name: str = "test-user",
    organization_id: str = "test-org-id",
    **kwargs: Any,
) -> Note:
    """Factory for creating test Note instances.

    Args:
        note_id: Unique ID (auto-generated if not provided).
        content: Note content.
        task_id: Parent task ID.
        author_type: Agent or user.
        author_name: Author identifier.
        organization_id: Organization scope.
        **kwargs: Additional Note fields.

    Returns:
        Configured Note instance.
    """
    return Note(
        id=note_id or f"note_{uuid.uuid4().hex[:8]}",
        name=content[:50] + ("..." if len(content) > 50 else ""),
        content=content,
        task_id=task_id,
        author_type=author_type,
        author_name=author_name,
        organization_id=organization_id,
        **kwargs,
    )


def make_milestone(
    milestone_id: str | None = None,
    name: str = "Test milestone",
    description: str = "",
    project_id: str = "test-project-id",
    organization_id: str = "test-org-id",
    **kwargs: Any,
) -> Milestone:
    """Factory for creating test Milestone instances."""
    return Milestone(
        id=milestone_id or f"milestone_{uuid.uuid4().hex[:8]}",
        name=name,
        description=description,
        project_id=project_id,
        organization_id=organization_id,
        **kwargs,
    )


def make_relationship(
    source_id: str,
    target_id: str,
    relationship_type: RelationshipType = RelationshipType.RELATED_TO,
    relationship_id: str | None = None,
    weight: float = 1.0,
    metadata: dict[str, Any] | None = None,
) -> Relationship:
    """Factory for creating test Relationship instances."""
    return Relationship(
        id=relationship_id or f"rel_{uuid.uuid4().hex[:8]}",
        relationship_type=relationship_type,
        source_id=source_id,
        target_id=target_id,
        weight=weight,
        metadata=metadata or {},
    )


# =============================================================================
# Pytest Fixtures
# =============================================================================


@pytest.fixture
def mock_graph_client() -> MockGraphClient:
    """Create a fresh MockGraphClient for testing."""
    client = MockGraphClient()
    client._connected = True
    return client


@pytest.fixture
def mock_entity_manager() -> MockEntityManager:
    """Create a fresh MockEntityManager for testing."""
    return MockEntityManager()


@pytest.fixture
def mock_relationship_manager() -> MockRelationshipManager:
    """Create a fresh MockRelationshipManager for testing."""
    return MockRelationshipManager()


@pytest.fixture
def test_org_id() -> str:
    """Standard test organization ID."""
    return "test-org-id"


@pytest.fixture
def sample_task() -> Task:
    """A pre-configured sample task for quick tests."""
    return make_task(
        task_id="sample-task-001",
        title="Sample task for testing",
        description="A pre-configured task for testing purposes",
        status=TaskStatus.TODO,
        priority=TaskPriority.MEDIUM,
        project_id="sample-project-001",
    )


@pytest.fixture
def sample_project() -> Project:
    """A pre-configured sample project for quick tests."""
    return make_project(
        project_id="sample-project-001",
        title="Sample Project",
        description="A pre-configured project for testing",
        tech_stack=["python", "fastapi"],
    )


@pytest.fixture
def sample_epic() -> Epic:
    """A pre-configured sample epic for quick tests."""
    return make_epic(
        epic_id="sample-epic-001",
        title="Sample Epic",
        description="A pre-configured epic for testing",
        project_id="sample-project-001",
    )


@pytest.fixture
def populated_entity_manager(
    mock_entity_manager: MockEntityManager,
    sample_project: Project,
    sample_epic: Epic,
    sample_task: Task,
) -> MockEntityManager:
    """EntityManager pre-populated with sample entities."""
    mock_entity_manager.add_entity(sample_project)
    mock_entity_manager.add_entity(sample_epic)
    mock_entity_manager.add_entity(sample_task)
    return mock_entity_manager


@pytest.fixture
def task_factory():
    """Fixture providing make_task factory function."""
    return make_task


@pytest.fixture
def entity_factory():
    """Fixture providing make_entity factory function."""
    return make_entity


@pytest.fixture
def project_factory():
    """Fixture providing make_project factory function."""
    return make_project


@pytest.fixture
def epic_factory():
    """Fixture providing make_epic factory function."""
    return make_epic


@pytest.fixture
def note_factory():
    """Fixture providing make_note factory function."""
    return make_note


@pytest.fixture
def relationship_factory():
    """Fixture providing make_relationship factory function."""
    return make_relationship


# =============================================================================
# Async Test Helpers
# =============================================================================


@pytest.fixture
def event_loop():
    """Create event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# =============================================================================
# Test Data Generators
# =============================================================================


def generate_tasks(
    count: int,
    project_id: str = "test-project",
    statuses: list[TaskStatus] | None = None,
) -> list[Task]:
    """Generate multiple tasks for bulk testing.

    Args:
        count: Number of tasks to generate.
        project_id: Parent project ID.
        statuses: List of statuses to cycle through (default: all statuses).

    Returns:
        List of generated tasks.
    """
    statuses = statuses or list(TaskStatus)
    return [
        make_task(
            task_id=f"task-{i:04d}",
            title=f"Generated task {i}",
            project_id=project_id,
            status=statuses[i % len(statuses)],
        )
        for i in range(count)
    ]


def generate_project_with_epics_and_tasks(
    num_epics: int = 3,
    tasks_per_epic: int = 5,
) -> tuple[Project, list[Epic], list[Task]]:
    """Generate a complete project hierarchy for integration testing.

    Args:
        num_epics: Number of epics to create.
        tasks_per_epic: Number of tasks per epic.

    Returns:
        Tuple of (project, epics, tasks).
    """
    project = make_project(
        project_id="gen-project-001",
        title="Generated Project",
        description="Auto-generated project for testing",
    )

    epics = []
    tasks = []

    for e_idx in range(num_epics):
        epic = make_epic(
            epic_id=f"gen-epic-{e_idx:03d}",
            title=f"Epic {e_idx}",
            project_id=project.id,
        )
        epics.append(epic)

        for t_idx in range(tasks_per_epic):
            task = make_task(
                task_id=f"gen-task-{e_idx:03d}-{t_idx:03d}",
                title=f"Task {t_idx} of Epic {e_idx}",
                project_id=project.id,
                epic_id=epic.id,
            )
            tasks.append(task)

    return project, epics, tasks
