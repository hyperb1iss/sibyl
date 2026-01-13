"""Tests for conftest.py fixtures and mocks.

Ensures the test infrastructure itself works correctly.
"""

import pytest

from sibyl_core.errors import EntityNotFoundError
from sibyl_core.models.entities import EntityType, RelationshipType
from sibyl_core.models.tasks import TaskPriority, TaskStatus


class TestMockGraphClient:
    """Test MockGraphClient functionality."""

    def test_connection_state(self, mock_graph_client) -> None:
        """Client tracks connection state."""
        assert mock_graph_client.is_connected

    @pytest.mark.asyncio
    async def test_execute_read_org(self, mock_graph_client) -> None:
        """Execute read tracks query history."""
        await mock_graph_client.execute_read_org(
            "MATCH (n) WHERE n.uuid = $uuid RETURN n",
            "test-org",
            uuid="test-123",
        )
        assert len(mock_graph_client.query_history) == 1
        assert mock_graph_client.query_history[0].operation == "read"

    @pytest.mark.asyncio
    async def test_execute_write_org(self, mock_graph_client) -> None:
        """Execute write tracks query history."""
        await mock_graph_client.execute_write_org(
            "CREATE (n:Entity {uuid: $uuid})",
            "test-org",
            uuid="test-456",
        )
        assert len(mock_graph_client.query_history) == 1
        assert mock_graph_client.query_history[0].operation == "write"

    def test_query_assertion_helper(self, mock_graph_client) -> None:
        """Query assertion helper works correctly."""
        from tests.conftest import QueryRecord

        mock_graph_client.query_history.append(
            QueryRecord(query="MATCH (n:Task) RETURN n", params={}, operation="read")
        )

        mock_graph_client.assert_query_executed("MATCH.*Task")
        with pytest.raises(AssertionError):
            mock_graph_client.assert_query_executed("nonexistent")

    def test_normalize_result(self, mock_graph_client) -> None:
        """Result normalization handles various formats."""
        # None
        assert mock_graph_client.normalize_result(None) == []

        # List
        assert mock_graph_client.normalize_result([{"a": 1}]) == [{"a": 1}]

        # Tuple (FalkorDB format)
        assert mock_graph_client.normalize_result(([{"a": 1}], ["header"], {})) == [{"a": 1}]


class TestMockEntityManager:
    """Test MockEntityManager functionality."""

    @pytest.mark.asyncio
    async def test_create_and_get(self, mock_entity_manager, entity_factory) -> None:
        """Create and retrieve entity."""
        entity = entity_factory(entity_id="test-001", name="Test Entity")
        await mock_entity_manager.create(entity)

        retrieved = await mock_entity_manager.get("test-001")
        assert retrieved.name == "Test Entity"

    @pytest.mark.asyncio
    async def test_get_not_found(self, mock_entity_manager) -> None:
        """Get raises EntityNotFoundError for missing entity."""
        with pytest.raises(EntityNotFoundError):
            await mock_entity_manager.get("nonexistent")

    @pytest.mark.asyncio
    async def test_update(self, mock_entity_manager, entity_factory) -> None:
        """Update entity with partial updates."""
        entity = entity_factory(entity_id="test-002", name="Original")
        await mock_entity_manager.create(entity)

        updated = await mock_entity_manager.update(
            "test-002", {"name": "Updated", "metadata": {"foo": "bar"}}
        )
        assert updated.name == "Updated"
        assert updated.metadata.get("foo") == "bar"

    @pytest.mark.asyncio
    async def test_delete(self, mock_entity_manager, entity_factory) -> None:
        """Delete removes entity."""
        entity = entity_factory(entity_id="test-003")
        await mock_entity_manager.create(entity)

        assert await mock_entity_manager.delete("test-003")
        with pytest.raises(EntityNotFoundError):
            await mock_entity_manager.get("test-003")

    @pytest.mark.asyncio
    async def test_search_with_preconfigured_results(
        self, mock_entity_manager, entity_factory
    ) -> None:
        """Search returns preconfigured results."""
        entity = entity_factory(name="Searchable")
        mock_entity_manager.search_results = [(entity, 0.95)]

        results = await mock_entity_manager.search("query")
        assert len(results) == 1
        assert results[0][0].name == "Searchable"
        assert results[0][1] == 0.95

    @pytest.mark.asyncio
    async def test_search_fallback_text_matching(self, mock_entity_manager, entity_factory) -> None:
        """Search falls back to text matching when no preconfigured results."""
        entity = entity_factory(
            entity_id="search-test", name="Authentication handler", description=""
        )
        mock_entity_manager.add_entity(entity)

        results = await mock_entity_manager.search("authentication")
        assert len(results) == 1
        assert results[0][0].id == "search-test"

    @pytest.mark.asyncio
    async def test_list_by_type(self, mock_entity_manager, task_factory) -> None:
        """List entities by type with filters."""
        task1 = task_factory(task_id="t1", status=TaskStatus.TODO, project_id="proj-1")
        task2 = task_factory(task_id="t2", status=TaskStatus.DOING, project_id="proj-1")
        task3 = task_factory(task_id="t3", status=TaskStatus.TODO, project_id="proj-2")

        # Update metadata to match filter expectations
        task1.metadata = {"status": "todo", "project_id": "proj-1"}
        task2.metadata = {"status": "doing", "project_id": "proj-1"}
        task3.metadata = {"status": "todo", "project_id": "proj-2"}

        mock_entity_manager.add_entity(task1)
        mock_entity_manager.add_entity(task2)
        mock_entity_manager.add_entity(task3)

        # Filter by project
        results = await mock_entity_manager.list_by_type(EntityType.TASK, project_id="proj-1")
        assert len(results) == 2

        # Filter by status
        results = await mock_entity_manager.list_by_type(EntityType.TASK, status="todo")
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_operation_tracking(self, mock_entity_manager, entity_factory) -> None:
        """Operations are tracked in history."""
        entity = entity_factory()
        mock_entity_manager.add_entity(entity)

        # Check get operations are tracked
        await mock_entity_manager.get(entity.id)
        assert any(op["op"] == "get" for op in mock_entity_manager.operation_history)


class TestMockRelationshipManager:
    """Test MockRelationshipManager functionality."""

    @pytest.mark.asyncio
    async def test_create_relationship(
        self, mock_relationship_manager, relationship_factory
    ) -> None:
        """Create relationship."""
        rel = relationship_factory(
            source_id="entity-1",
            target_id="entity-2",
            relationship_type=RelationshipType.BELONGS_TO,
        )
        rel_id = await mock_relationship_manager.create(rel)
        assert rel_id is not None
        assert rel_id in mock_relationship_manager.relationships

    @pytest.mark.asyncio
    async def test_get_for_entity(self, mock_relationship_manager, relationship_factory) -> None:
        """Get relationships for an entity."""
        rel1 = relationship_factory(
            source_id="entity-1",
            target_id="entity-2",
            relationship_type=RelationshipType.BELONGS_TO,
        )
        rel2 = relationship_factory(
            source_id="entity-3",
            target_id="entity-1",
            relationship_type=RelationshipType.REFERENCES,
        )
        await mock_relationship_manager.create(rel1)
        await mock_relationship_manager.create(rel2)

        # Both directions
        results = await mock_relationship_manager.get_for_entity("entity-1")
        assert len(results) == 2

        # Outgoing only
        results = await mock_relationship_manager.get_for_entity("entity-1", direction="outgoing")
        assert len(results) == 1
        assert results[0].target_id == "entity-2"

        # Incoming only
        results = await mock_relationship_manager.get_for_entity("entity-1", direction="incoming")
        assert len(results) == 1
        assert results[0].source_id == "entity-3"


class TestFactoryFunctions:
    """Test factory functions for creating test data."""

    def test_make_task_defaults(self, task_factory) -> None:
        """make_task creates task with sensible defaults."""
        task = task_factory()
        assert task.id.startswith("task_")
        assert task.title == "Test task"
        assert task.status == TaskStatus.TODO
        assert task.priority == TaskPriority.MEDIUM
        assert task.entity_type == EntityType.TASK

    def test_make_task_custom(self, task_factory) -> None:
        """make_task accepts custom values."""
        task = task_factory(
            task_id="custom-task",
            title="Custom Title",
            status=TaskStatus.DOING,
            priority=TaskPriority.HIGH,
            project_id="proj-123",
            feature="auth",
        )
        assert task.id == "custom-task"
        assert task.title == "Custom Title"
        assert task.status == TaskStatus.DOING
        assert task.priority == TaskPriority.HIGH
        assert task.project_id == "proj-123"
        assert task.feature == "auth"

    def test_make_entity_defaults(self, entity_factory) -> None:
        """make_entity creates entity with sensible defaults."""
        entity = entity_factory()
        assert entity.id.startswith("entity_")
        assert entity.name == "Test entity"
        assert entity.entity_type == EntityType.TOPIC

    def test_make_project_defaults(self, project_factory) -> None:
        """make_project creates project with sensible defaults."""
        project = project_factory()
        assert project.id.startswith("project_")
        assert project.title == "Test project"
        assert project.entity_type == EntityType.PROJECT

    def test_make_epic_defaults(self, epic_factory) -> None:
        """make_epic creates epic with sensible defaults."""
        epic = epic_factory()
        assert epic.id.startswith("epic_")
        assert epic.title == "Test epic"
        assert epic.entity_type == EntityType.EPIC
        assert epic.project_id == "test-project-id"

    def test_make_note_defaults(self, note_factory) -> None:
        """make_note creates note with sensible defaults."""
        note = note_factory()
        assert note.id.startswith("note_")
        assert note.entity_type == EntityType.NOTE
        assert note.task_id == "test-task-id"


class TestDataGenerators:
    """Test bulk data generators."""

    def test_generate_tasks(self) -> None:
        """generate_tasks creates multiple tasks."""
        from tests.conftest import generate_tasks

        tasks = generate_tasks(10, project_id="test-proj")
        assert len(tasks) == 10
        assert all(t.project_id == "test-proj" for t in tasks)
        # Should cycle through statuses
        statuses = {t.status for t in tasks}
        assert len(statuses) > 1

    def test_generate_project_hierarchy(self) -> None:
        """generate_project_with_epics_and_tasks creates full hierarchy."""
        from tests.conftest import generate_project_with_epics_and_tasks

        project, epics, tasks = generate_project_with_epics_and_tasks(num_epics=2, tasks_per_epic=3)
        assert project.entity_type == EntityType.PROJECT
        assert len(epics) == 2
        assert len(tasks) == 6  # 2 * 3
        assert all(e.project_id == project.id for e in epics)
        assert all(t.project_id == project.id for t in tasks)


class TestPrePopulatedFixture:
    """Test pre-populated fixture."""

    def test_populated_entity_manager(self, populated_entity_manager) -> None:
        """Populated manager has sample entities."""
        assert "sample-project-001" in populated_entity_manager.entities
        assert "sample-epic-001" in populated_entity_manager.entities
        assert "sample-task-001" in populated_entity_manager.entities
