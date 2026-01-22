"""Tests for sibyl-core graph/entities.py EntityManager."""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from graphiti_core.nodes import EntityNode, EpisodicNode

from sibyl_core.errors import EntityCreationError, EntityNotFoundError, SearchError
from sibyl_core.graph.entities import EntityManager, sanitize_search_query
from sibyl_core.models.agents import (
    AgentCheckpoint,
    AgentRecord,
    AgentStatus,
    AgentType,
    WorktreeRecord,
    WorktreeStatus,
)
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.models.tasks import (
    Epic,
    EpicStatus,
    Project,
    ProjectStatus,
    Task,
    TaskPriority,
    TaskStatus,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_driver() -> MagicMock:
    """Create a mock FalkorDB driver."""
    driver = MagicMock()
    driver.execute_query = AsyncMock(return_value=([], None, None))
    return driver


@pytest.fixture
def mock_graphiti_client(mock_driver: MagicMock) -> MagicMock:
    """Create a mock Graphiti client."""
    client = MagicMock()
    client.driver = mock_driver
    client.driver.clone = MagicMock(return_value=mock_driver)
    client.add_episode = AsyncMock()
    client.embedder = MagicMock()
    client.embedder.create = AsyncMock(return_value=[0.1] * 1536)
    client.search_ = AsyncMock()
    return client


@pytest.fixture
def mock_graph_client(mock_graphiti_client: MagicMock) -> MagicMock:
    """Create a mock GraphClient wrapper."""
    graph_client = MagicMock()
    graph_client.client = mock_graphiti_client
    return graph_client


@pytest.fixture
def entity_manager(mock_graph_client: MagicMock) -> EntityManager:
    """Create EntityManager with mocked dependencies."""
    return EntityManager(mock_graph_client, group_id="test-org-123")


@pytest.fixture
def sample_task() -> Task:
    """Create a sample task for testing."""
    return Task(
        id="task-001",
        name="Implement auth flow",
        title="Implement auth flow",
        description="Add OAuth2 authentication",
        status=TaskStatus.TODO,
        priority=TaskPriority.HIGH,
        project_id="project-001",
        feature="authentication",
        tags=["backend", "security"],
        technologies=["python", "oauth2"],
    )


@pytest.fixture
def sample_project() -> Project:
    """Create a sample project for testing."""
    return Project(
        id="project-001",
        name="Sibyl API",
        title="Sibyl API",
        description="Knowledge graph API server",
        status=ProjectStatus.ACTIVE,
        tech_stack=["python", "fastapi", "graphiti"],
        features=["task-management", "knowledge-graph"],
    )


@pytest.fixture
def sample_epic() -> Epic:
    """Create a sample epic for testing."""
    return Epic(
        id="epic-001",
        name="Authentication System",
        title="Authentication System",
        description="Complete auth implementation",
        status=EpicStatus.IN_PROGRESS,
        priority=TaskPriority.HIGH,
        project_id="project-001",
    )


@pytest.fixture
def sample_entity() -> Entity:
    """Create a generic entity for testing."""
    return Entity(
        id="entity-001",
        entity_type=EntityType.PATTERN,
        name="Repository Pattern",
        description="Data access abstraction layer",
        content="Use repositories for data access...",
        metadata={"category": "architecture"},
    )


@pytest.fixture
def sample_entity_node() -> EntityNode:
    """Create a mock EntityNode from Graphiti."""
    return EntityNode(
        uuid="entity-001",
        name="Test Entity",
        group_id="test-org-123",
        labels=["Entity", "pattern"],
        created_at=datetime.now(UTC),
        summary="A test entity summary",
        attributes={
            "entity_type": "pattern",
            "description": "Test description",
            "content": "Test content",
            "metadata": json.dumps({"category": "testing"}),
        },
    )


@pytest.fixture
def sample_episodic_node() -> EpisodicNode:
    """Create a mock EpisodicNode from Graphiti."""
    node = MagicMock(spec=EpisodicNode)
    node.uuid = "episode-001"
    node.name = "pattern:Test Episode"
    node.group_id = "test-org-123"
    node.content = "Episode content"
    node.source_description = "MCP Entity: pattern"
    node.created_at = datetime.now(UTC)
    return node


# =============================================================================
# EntityManager Initialization Tests
# =============================================================================


class TestEntityManagerInit:
    """Test EntityManager initialization and configuration."""

    def test_init_with_valid_group_id(self, mock_graph_client: MagicMock) -> None:
        """EntityManager initializes with valid group_id."""
        manager = EntityManager(mock_graph_client, group_id="org-123")
        assert manager._group_id == "org-123"
        assert manager._client == mock_graph_client

    def test_init_requires_group_id(self, mock_graph_client: MagicMock) -> None:
        """EntityManager requires non-empty group_id."""
        with pytest.raises(ValueError, match="group_id is required"):
            EntityManager(mock_graph_client, group_id="")

    def test_init_clones_driver_for_org(self, mock_graph_client: MagicMock) -> None:
        """EntityManager clones driver with org-specific graph."""
        EntityManager(mock_graph_client, group_id="my-org")
        mock_graph_client.client.driver.clone.assert_called_once_with("my-org")


# =============================================================================
# Entity Creation Tests
# =============================================================================


class TestEntityCreate:
    """Test entity creation via add_episode."""

    @pytest.mark.asyncio
    async def test_create_entity_success(
        self,
        entity_manager: EntityManager,
        sample_entity: Entity,
        mock_graph_client: MagicMock,
    ) -> None:
        """create() stores entity via add_episode and returns ID."""
        # Setup mock episode result
        mock_episode = MagicMock()
        mock_episode.uuid = "generated-uuid"
        mock_result = MagicMock()
        mock_result.episode = mock_episode
        mock_graph_client.client.add_episode.return_value = mock_result

        result = await entity_manager.create(sample_entity)

        # Should use provided entity ID
        assert result == sample_entity.id
        mock_graph_client.client.add_episode.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_entity_sanitizes_name(
        self,
        entity_manager: EntityManager,
        mock_graph_client: MagicMock,
    ) -> None:
        """create() sanitizes special characters in entity name."""
        entity = Entity(
            id="entity-special",
            entity_type=EntityType.PATTERN,
            name="**Bold** _italic_ [link](url)",
            description="Test",
        )

        mock_episode = MagicMock()
        mock_episode.uuid = "uuid-123"
        mock_result = MagicMock()
        mock_result.episode = mock_episode
        mock_graph_client.client.add_episode.return_value = mock_result

        await entity_manager.create(entity)

        # Verify add_episode was called with sanitized name
        call_args = mock_graph_client.client.add_episode.call_args
        assert call_args is not None
        # Name should not contain markdown special chars
        name = call_args.kwargs.get("name", "")
        assert "**" not in name
        assert "_" not in name
        assert "[" not in name

    @pytest.mark.asyncio
    async def test_create_task_entity(
        self,
        entity_manager: EntityManager,
        sample_task: Task,
        mock_graph_client: MagicMock,
    ) -> None:
        """create() handles Task entities with all fields."""
        mock_episode = MagicMock()
        mock_episode.uuid = "task-uuid"
        mock_result = MagicMock()
        mock_result.episode = mock_episode
        mock_graph_client.client.add_episode.return_value = mock_result

        result = await entity_manager.create(sample_task)

        assert result == sample_task.id
        # Episode body should contain task-specific fields
        call_args = mock_graph_client.client.add_episode.call_args
        episode_body = call_args.kwargs.get("episode_body", "")
        assert "Status:" in episode_body
        assert "Priority:" in episode_body


class TestEntityCreateDirect:
    """Test direct entity creation bypassing LLM."""

    @pytest.mark.asyncio
    async def test_create_direct_success(
        self,
        entity_manager: EntityManager,
        sample_entity: Entity,
    ) -> None:
        """create_direct() creates entity via EntityNode.save()."""
        with patch.object(EntityNode, "save", new_callable=AsyncMock) as mock_save:
            result = await entity_manager.create_direct(sample_entity)

            assert result == sample_entity.id
            mock_save.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_direct_generates_embedding(
        self,
        entity_manager: EntityManager,
        sample_entity: Entity,
        mock_graph_client: MagicMock,
    ) -> None:
        """create_direct() generates embeddings by default."""
        with patch.object(EntityNode, "save", new_callable=AsyncMock):
            await entity_manager.create_direct(sample_entity, generate_embedding=True)

            mock_graph_client.client.embedder.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_direct_skip_embedding(
        self,
        entity_manager: EntityManager,
        sample_entity: Entity,
        mock_graph_client: MagicMock,
    ) -> None:
        """create_direct() can skip embedding generation."""
        with patch.object(EntityNode, "save", new_callable=AsyncMock):
            await entity_manager.create_direct(sample_entity, generate_embedding=False)

            mock_graph_client.client.embedder.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_direct_failure_raises_error(
        self,
        entity_manager: EntityManager,
        sample_entity: Entity,
        mock_graph_client: MagicMock,
    ) -> None:
        """create_direct() raises EntityCreationError on failure."""
        # Mock EntityNode.save to raise an exception
        with (
            patch.object(
                EntityNode,
                "save",
                new_callable=AsyncMock,
                side_effect=Exception("DB error"),
            ),
            pytest.raises(EntityCreationError, match="Failed to create entity"),
        ):
            await entity_manager.create_direct(sample_entity)


# =============================================================================
# Entity Retrieval Tests
# =============================================================================


class TestEntityGet:
    """Test entity retrieval by ID."""

    @pytest.mark.asyncio
    async def test_get_entity_node_success(
        self,
        entity_manager: EntityManager,
        sample_entity_node: EntityNode,
    ) -> None:
        """get() retrieves entity from EntityNode."""
        with patch.object(
            EntityNode,
            "get_by_uuid",
            new_callable=AsyncMock,
            return_value=sample_entity_node,
        ):
            result = await entity_manager.get("entity-001")

            assert result.id == "entity-001"
            assert result.name == "Test Entity"
            assert result.entity_type == EntityType.PATTERN

    @pytest.mark.asyncio
    async def test_get_episodic_node_fallback(
        self,
        entity_manager: EntityManager,
        sample_episodic_node: EpisodicNode,
    ) -> None:
        """get() falls back to EpisodicNode if EntityNode not found."""
        with (
            patch.object(
                EntityNode,
                "get_by_uuid",
                new_callable=AsyncMock,
                side_effect=Exception("Not found"),
            ),
            patch.object(
                EpisodicNode,
                "get_by_uuid",
                new_callable=AsyncMock,
                return_value=sample_episodic_node,
            ),
        ):
            result = await entity_manager.get("episode-001")

            assert result.id == "episode-001"
            assert result.entity_type == EntityType.PATTERN

    @pytest.mark.asyncio
    async def test_get_not_found_raises_error(
        self,
        entity_manager: EntityManager,
    ) -> None:
        """get() raises EntityNotFoundError when entity doesn't exist."""
        with (
            patch.object(
                EntityNode,
                "get_by_uuid",
                new_callable=AsyncMock,
                side_effect=Exception("Not found"),
            ),
            patch.object(
                EpisodicNode,
                "get_by_uuid",
                new_callable=AsyncMock,
                side_effect=Exception("Not found"),
            ),
            pytest.raises(EntityNotFoundError, match="Entity not found"),
        ):
            await entity_manager.get("nonexistent-id")

    @pytest.mark.asyncio
    async def test_get_filters_by_group_id(
        self,
        entity_manager: EntityManager,
    ) -> None:
        """get() only returns entities from the correct group."""
        # Create node with different group_id
        wrong_group_node = EntityNode(
            uuid="entity-001",
            name="Test",
            group_id="other-org",  # Different org
            labels=["Entity"],
            created_at=datetime.now(UTC),
            summary="Test",
            attributes={},
        )

        with (
            patch.object(
                EntityNode,
                "get_by_uuid",
                new_callable=AsyncMock,
                return_value=wrong_group_node,
            ),
            patch.object(
                EpisodicNode,
                "get_by_uuid",
                new_callable=AsyncMock,
                side_effect=Exception("Not found"),
            ),
            pytest.raises(EntityNotFoundError),
        ):
            await entity_manager.get("entity-001")


# =============================================================================
# Typed Hydration Tests
# =============================================================================


class TestTypedHydration:
    """Test typed hydration for EntityManager get/list_by_type."""

    @pytest.mark.asyncio
    async def test_get_hydrates_task_record(
        self,
        entity_manager: EntityManager,
    ) -> None:
        """get() returns Task when entity_type is task."""
        task_node = EntityNode(
            uuid="task-typed-001",
            name="Typed Task",
            group_id="test-org-123",
            labels=["Entity", "task"],
            created_at=datetime.now(UTC),
            summary="",
            attributes={
                "entity_type": "task",
                "description": "Task description",
                "content": "Task description",
                "metadata": json.dumps(
                    {
                        "status": "doing",
                        "priority": "high",
                        "project_id": "project-typed-001",
                    }
                ),
            },
        )

        with patch.object(
            EntityNode,
            "get_by_uuid",
            new_callable=AsyncMock,
            return_value=task_node,
        ):
            result = await entity_manager.get("task-typed-001")

        assert isinstance(result, Task)
        assert result.title == "Typed Task"
        assert result.status == TaskStatus.DOING
        assert result.priority == TaskPriority.HIGH
        assert result.project_id == "project-typed-001"

    @pytest.mark.asyncio
    async def test_get_hydrates_agent_record(
        self,
        entity_manager: EntityManager,
    ) -> None:
        """get() returns AgentRecord when entity_type is agent."""
        agent_node = EntityNode(
            uuid="agent-typed-001",
            name="Planner Agent",
            group_id="test-org-123",
            labels=["Entity", "agent"],
            created_at=datetime.now(UTC),
            summary="",
            attributes={
                "entity_type": "agent",
                "description": "Agent description",
                "content": "Agent description",
                "metadata": json.dumps(
                    {
                        "project_id": "project-typed-001",
                        "agent_type": "planner",
                        "status": "working",
                        "task_id": "task-typed-001",
                        "worktree_id": "worktree-typed-001",
                        "last_heartbeat": datetime.now(UTC).isoformat(),
                    }
                ),
            },
        )

        with patch.object(
            EntityNode,
            "get_by_uuid",
            new_callable=AsyncMock,
            return_value=agent_node,
        ):
            result = await entity_manager.get("agent-typed-001")

        assert isinstance(result, AgentRecord)
        assert result.agent_type == AgentType.PLANNER
        assert result.status == AgentStatus.WORKING
        assert result.task_id == "task-typed-001"
        assert result.worktree_id == "worktree-typed-001"

    @pytest.mark.asyncio
    async def test_list_by_type_hydrates_worktree_record(
        self,
        entity_manager: EntityManager,
        mock_driver: MagicMock,
    ) -> None:
        """list_by_type() returns WorktreeRecord instances."""
        mock_driver.execute_query.return_value = (
            [
                {
                    "uuid": "worktree-typed-001",
                    "name": "feature/typed",
                    "entity_type": "worktree",
                    "group_id": "test-org-123",
                    "metadata": json.dumps(
                        {
                            "task_id": "task-typed-001",
                            "agent_id": "agent-typed-001",
                            "path": "/tmp/worktrees/feature-typed",
                            "branch": "feature/typed",
                            "base_commit": "abc123",
                            "status": "active",
                            "last_used": datetime.now(UTC).isoformat(),
                        }
                    ),
                }
            ],
            None,
            None,
        )

        results = await entity_manager.list_by_type(EntityType.WORKTREE)

        assert len(results) == 1
        assert isinstance(results[0], WorktreeRecord)
        assert results[0].branch == "feature/typed"
        assert results[0].status == WorktreeStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_list_by_type_hydrates_checkpoint(
        self,
        entity_manager: EntityManager,
        mock_driver: MagicMock,
    ) -> None:
        """list_by_type() returns AgentCheckpoint instances."""
        mock_driver.execute_query.return_value = (
            [
                {
                    "uuid": "checkpoint-typed-001",
                    "name": "checkpoint-agent",
                    "entity_type": "checkpoint",
                    "group_id": "test-org-123",
                    "metadata": json.dumps(
                        {
                            "agent_id": "agent-typed-001",
                            "session_id": "session-typed-001",
                            "current_step": "review",
                            "pending_approval_id": "approval-typed-001",
                        }
                    ),
                }
            ],
            None,
            None,
        )

        results = await entity_manager.list_by_type(EntityType.CHECKPOINT)

        assert len(results) == 1
        assert isinstance(results[0], AgentCheckpoint)
        assert results[0].current_step == "review"
        assert results[0].pending_approval_id == "approval-typed-001"


# =============================================================================
# Entity Update Tests
# =============================================================================


class TestEntityUpdate:
    """Test entity update operations."""

    @pytest.mark.asyncio
    async def test_update_partial(
        self,
        entity_manager: EntityManager,
        sample_entity_node: EntityNode,
        mock_driver: MagicMock,
    ) -> None:
        """update() applies partial updates preserving other fields."""
        with patch.object(
            EntityNode,
            "get_by_uuid",
            new_callable=AsyncMock,
            return_value=sample_entity_node,
        ):
            result = await entity_manager.update(
                "entity-001",
                {"description": "Updated description"},
            )

            assert result is not None
            assert result.description == "Updated description"
            # Name should be preserved
            assert result.name == "Test Entity"

    @pytest.mark.asyncio
    async def test_update_metadata_merge(
        self,
        entity_manager: EntityManager,
        sample_entity_node: EntityNode,
    ) -> None:
        """update() merges metadata rather than replacing."""
        with patch.object(
            EntityNode,
            "get_by_uuid",
            new_callable=AsyncMock,
            return_value=sample_entity_node,
        ):
            result = await entity_manager.update(
                "entity-001",
                {"metadata": {"new_key": "new_value"}},
            )

            assert result is not None
            # Original metadata should be preserved
            assert "category" in result.metadata
            # New metadata should be added
            assert result.metadata.get("new_key") == "new_value"

    @pytest.mark.asyncio
    async def test_update_not_found_raises_error(
        self,
        entity_manager: EntityManager,
    ) -> None:
        """update() raises EntityNotFoundError if entity doesn't exist."""
        with (
            patch.object(
                EntityNode,
                "get_by_uuid",
                new_callable=AsyncMock,
                side_effect=Exception("Not found"),
            ),
            patch.object(
                EpisodicNode,
                "get_by_uuid",
                new_callable=AsyncMock,
                side_effect=Exception("Not found"),
            ),
            pytest.raises(EntityNotFoundError),
        ):
            await entity_manager.update("nonexistent", {"name": "New Name"})

    @pytest.mark.asyncio
    async def test_update_embedding(
        self,
        entity_manager: EntityManager,
        sample_entity_node: EntityNode,
        mock_driver: MagicMock,
    ) -> None:
        """update() can store new embedding on node."""
        with patch.object(
            EntityNode,
            "get_by_uuid",
            new_callable=AsyncMock,
            return_value=sample_entity_node,
        ):
            embedding = [0.1] * 1536
            result = await entity_manager.update(
                "entity-001",
                {"embedding": embedding},
            )

            assert result is not None
            # Verify execute_query was called to set embedding
            mock_driver.execute_query.assert_called()


# =============================================================================
# Entity Delete Tests
# =============================================================================


class TestEntityDelete:
    """Test entity deletion operations."""

    @pytest.mark.asyncio
    async def test_delete_entity_node(
        self,
        entity_manager: EntityManager,
    ) -> None:
        """delete() removes entity via EntityNode.delete()."""
        # Create a mock entity node with the delete method as a MagicMock
        mock_entity = MagicMock(spec=EntityNode)
        mock_entity.uuid = "entity-001"
        mock_entity.group_id = "test-org-123"
        mock_entity.delete = AsyncMock()

        with patch.object(
            EntityNode,
            "get_by_uuid",
            new_callable=AsyncMock,
            return_value=mock_entity,
        ):
            result = await entity_manager.delete("entity-001")

            assert result is True
            mock_entity.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_episodic_node_fallback(
        self,
        entity_manager: EntityManager,
    ) -> None:
        """delete() falls back to EpisodicNode if EntityNode not found."""
        # Create a mock episodic node with the delete method as a MagicMock
        mock_episodic = MagicMock(spec=EpisodicNode)
        mock_episodic.uuid = "episode-001"
        mock_episodic.group_id = "test-org-123"
        mock_episodic.delete = AsyncMock()

        with (
            patch.object(
                EntityNode,
                "get_by_uuid",
                new_callable=AsyncMock,
                side_effect=Exception("Not found"),
            ),
            patch.object(
                EpisodicNode,
                "get_by_uuid",
                new_callable=AsyncMock,
                return_value=mock_episodic,
            ),
        ):
            result = await entity_manager.delete("episode-001")

            assert result is True
            mock_episodic.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_not_found_raises_error(
        self,
        entity_manager: EntityManager,
    ) -> None:
        """delete() raises EntityNotFoundError if entity doesn't exist."""
        with (
            patch.object(
                EntityNode,
                "get_by_uuid",
                new_callable=AsyncMock,
                side_effect=Exception("Not found"),
            ),
            patch.object(
                EpisodicNode,
                "get_by_uuid",
                new_callable=AsyncMock,
                side_effect=Exception("Not found"),
            ),
            pytest.raises(EntityNotFoundError),
        ):
            await entity_manager.delete("nonexistent")


# =============================================================================
# Entity List/Query Tests
# =============================================================================


class TestEntityListByType:
    """Test listing entities by type with filters."""

    @pytest.mark.asyncio
    async def test_list_by_type_basic(
        self,
        entity_manager: EntityManager,
        mock_driver: MagicMock,
    ) -> None:
        """list_by_type() returns entities of specified type."""
        mock_driver.execute_query.return_value = (
            [
                {
                    "uuid": "task-001",
                    "name": "Task 1",
                    "entity_type": "task",
                    "group_id": "test-org-123",
                    "metadata": json.dumps({"status": "todo"}),
                }
            ],
            None,
            None,
        )

        results = await entity_manager.list_by_type(EntityType.TASK)

        assert len(results) == 1
        assert results[0].id == "task-001"
        assert results[0].entity_type == EntityType.TASK

    @pytest.mark.asyncio
    async def test_list_by_type_with_status_filter(
        self,
        entity_manager: EntityManager,
        mock_driver: MagicMock,
    ) -> None:
        """list_by_type() filters by status from metadata."""
        mock_driver.execute_query.return_value = (
            [
                {
                    "uuid": "task-001",
                    "name": "Task 1",
                    "entity_type": "task",
                    "group_id": "test-org-123",
                    "metadata": json.dumps({"status": "doing"}),
                },
                {
                    "uuid": "task-002",
                    "name": "Task 2",
                    "entity_type": "task",
                    "group_id": "test-org-123",
                    "metadata": json.dumps({"status": "todo"}),
                },
            ],
            None,
            None,
        )

        results = await entity_manager.list_by_type(EntityType.TASK, status="doing")

        assert len(results) == 1
        assert results[0].id == "task-001"

    @pytest.mark.asyncio
    async def test_list_by_type_multiple_statuses(
        self,
        entity_manager: EntityManager,
        mock_driver: MagicMock,
    ) -> None:
        """list_by_type() supports comma-separated status values."""
        mock_driver.execute_query.return_value = (
            [
                {
                    "uuid": "task-001",
                    "name": "Task 1",
                    "entity_type": "task",
                    "group_id": "test-org-123",
                    "metadata": json.dumps({"status": "doing"}),
                },
                {
                    "uuid": "task-002",
                    "name": "Task 2",
                    "entity_type": "task",
                    "group_id": "test-org-123",
                    "metadata": json.dumps({"status": "blocked"}),
                },
                {
                    "uuid": "task-003",
                    "name": "Task 3",
                    "entity_type": "task",
                    "group_id": "test-org-123",
                    "metadata": json.dumps({"status": "done"}),
                },
            ],
            None,
            None,
        )

        results = await entity_manager.list_by_type(EntityType.TASK, status="doing,blocked")

        assert len(results) == 2
        ids = {r.id for r in results}
        assert ids == {"task-001", "task-002"}

    @pytest.mark.asyncio
    async def test_list_by_type_with_priority_filter(
        self,
        entity_manager: EntityManager,
        mock_driver: MagicMock,
    ) -> None:
        """list_by_type() filters by priority from metadata."""
        mock_driver.execute_query.return_value = (
            [
                {
                    "uuid": "task-001",
                    "name": "Critical Task",
                    "entity_type": "task",
                    "group_id": "test-org-123",
                    "metadata": json.dumps({"status": "todo", "priority": "critical"}),
                },
                {
                    "uuid": "task-002",
                    "name": "Low Task",
                    "entity_type": "task",
                    "group_id": "test-org-123",
                    "metadata": json.dumps({"status": "todo", "priority": "low"}),
                },
            ],
            None,
            None,
        )

        results = await entity_manager.list_by_type(EntityType.TASK, priority="critical")

        assert len(results) == 1
        assert results[0].id == "task-001"

    @pytest.mark.asyncio
    async def test_list_by_type_with_project_filter(
        self,
        entity_manager: EntityManager,
        mock_driver: MagicMock,
    ) -> None:
        """list_by_type() filters by project_id."""
        mock_driver.execute_query.return_value = (
            [
                {
                    "uuid": "task-001",
                    "name": "Task 1",
                    "entity_type": "task",
                    "group_id": "test-org-123",
                    "metadata": json.dumps({"project_id": "project-001", "status": "todo"}),
                },
                {
                    "uuid": "task-002",
                    "name": "Task 2",
                    "entity_type": "task",
                    "group_id": "test-org-123",
                    "metadata": json.dumps({"project_id": "project-002", "status": "todo"}),
                },
            ],
            None,
            None,
        )

        results = await entity_manager.list_by_type(EntityType.TASK, project_id="project-001")

        assert len(results) == 1
        assert results[0].id == "task-001"

    @pytest.mark.asyncio
    async def test_list_by_type_with_tags_filter(
        self,
        entity_manager: EntityManager,
        mock_driver: MagicMock,
    ) -> None:
        """list_by_type() filters by tags (any match)."""
        mock_driver.execute_query.return_value = (
            [
                {
                    "uuid": "task-001",
                    "name": "Task 1",
                    "entity_type": "task",
                    "group_id": "test-org-123",
                    "metadata": json.dumps({"tags": ["backend", "api"], "status": "todo"}),
                },
                {
                    "uuid": "task-002",
                    "name": "Task 2",
                    "entity_type": "task",
                    "group_id": "test-org-123",
                    "metadata": json.dumps({"tags": ["frontend"], "status": "todo"}),
                },
            ],
            None,
            None,
        )

        results = await entity_manager.list_by_type(EntityType.TASK, tags=["backend"])

        assert len(results) == 1
        assert results[0].id == "task-001"

    @pytest.mark.asyncio
    async def test_list_by_type_excludes_archived_by_default(
        self,
        entity_manager: EntityManager,
        mock_driver: MagicMock,
    ) -> None:
        """list_by_type() excludes archived entities by default."""
        mock_driver.execute_query.return_value = (
            [
                {
                    "uuid": "task-001",
                    "name": "Active Task",
                    "entity_type": "task",
                    "group_id": "test-org-123",
                    "metadata": json.dumps({"status": "todo"}),
                },
                {
                    "uuid": "task-002",
                    "name": "Archived Task",
                    "entity_type": "task",
                    "group_id": "test-org-123",
                    "metadata": json.dumps({"status": "archived"}),
                },
            ],
            None,
            None,
        )

        results = await entity_manager.list_by_type(EntityType.TASK)

        assert len(results) == 1
        assert results[0].id == "task-001"

    @pytest.mark.asyncio
    async def test_list_by_type_include_archived(
        self,
        entity_manager: EntityManager,
        mock_driver: MagicMock,
    ) -> None:
        """list_by_type() can include archived entities."""
        mock_driver.execute_query.return_value = (
            [
                {
                    "uuid": "task-001",
                    "name": "Active Task",
                    "entity_type": "task",
                    "group_id": "test-org-123",
                    "metadata": json.dumps({"status": "todo"}),
                },
                {
                    "uuid": "task-002",
                    "name": "Archived Task",
                    "entity_type": "task",
                    "group_id": "test-org-123",
                    "metadata": json.dumps({"status": "archived"}),
                },
            ],
            None,
            None,
        )

        results = await entity_manager.list_by_type(EntityType.TASK, include_archived=True)

        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_list_by_type_pagination(
        self,
        entity_manager: EntityManager,
        mock_driver: MagicMock,
    ) -> None:
        """list_by_type() respects limit and offset."""
        mock_driver.execute_query.return_value = (
            [
                {
                    "uuid": f"task-{i:03d}",
                    "name": f"Task {i}",
                    "entity_type": "task",
                    "group_id": "test-org-123",
                    "metadata": json.dumps({"status": "todo"}),
                }
                for i in range(10)
            ],
            None,
            None,
        )

        results = await entity_manager.list_by_type(EntityType.TASK, limit=3, offset=2)

        assert len(results) == 3
        # Should skip first 2 and return next 3
        assert results[0].id == "task-002"
        assert results[1].id == "task-003"
        assert results[2].id == "task-004"

    @pytest.mark.asyncio
    async def test_list_by_type_empty_results(
        self,
        entity_manager: EntityManager,
        mock_driver: MagicMock,
    ) -> None:
        """list_by_type() returns empty list when no matches."""
        mock_driver.execute_query.return_value = ([], None, None)

        results = await entity_manager.list_by_type(EntityType.TASK)

        assert results == []

    @pytest.mark.asyncio
    async def test_list_by_type_with_epic_id(
        self,
        entity_manager: EntityManager,
        mock_driver: MagicMock,
    ) -> None:
        """list_by_type() filters by epic_id using BELONGS_TO relationship."""
        mock_driver.execute_query.return_value = (
            [
                {
                    "uuid": "task-001",
                    "name": "Epic Task",
                    "entity_type": "task",
                    "group_id": "test-org-123",
                    "metadata": json.dumps({"status": "todo", "epic_id": "epic-001"}),
                }
            ],
            None,
            None,
        )

        await entity_manager.list_by_type(EntityType.TASK, epic_id="epic-001")

        # Verify query uses BELONGS_TO pattern
        call_args = mock_driver.execute_query.call_args
        query = call_args[0][0] if call_args[0] else ""
        assert "BELONGS_TO" in query

    @pytest.mark.asyncio
    async def test_list_by_type_no_epic(
        self,
        entity_manager: EntityManager,
        mock_driver: MagicMock,
    ) -> None:
        """list_by_type() can filter for entities without an epic."""
        mock_driver.execute_query.return_value = (
            [
                {
                    "uuid": "task-001",
                    "name": "Task with Epic",
                    "entity_type": "task",
                    "group_id": "test-org-123",
                    "metadata": json.dumps({"status": "todo", "epic_id": "epic-001"}),
                },
                {
                    "uuid": "task-002",
                    "name": "Task without Epic",
                    "entity_type": "task",
                    "group_id": "test-org-123",
                    "metadata": json.dumps({"status": "todo"}),
                },
            ],
            None,
            None,
        )

        results = await entity_manager.list_by_type(EntityType.TASK, no_epic=True)

        assert len(results) == 1
        assert results[0].id == "task-002"


class TestEntityListAll:
    """Test listing all entities."""

    @pytest.mark.asyncio
    async def test_list_all_basic(
        self,
        entity_manager: EntityManager,
        mock_graph_client: MagicMock,
    ) -> None:
        """list_all() returns entities of all types."""
        mock_graph_client.execute_read_org = AsyncMock(
            return_value=[
                {
                    "uuid": "task-001",
                    "name": "Task 1",
                    "entity_type": "task",
                    "group_id": "test-org-123",
                    "metadata": json.dumps({}),
                },
                {
                    "uuid": "pattern-001",
                    "name": "Pattern 1",
                    "entity_type": "pattern",
                    "group_id": "test-org-123",
                    "metadata": json.dumps({}),
                },
            ]
        )

        results = await entity_manager.list_all()

        assert len(results) == 2
        types = {r.entity_type for r in results}
        assert EntityType.TASK in types
        assert EntityType.PATTERN in types


# =============================================================================
# Search Tests
# =============================================================================


class TestEntitySearch:
    """Test semantic search operations."""

    @pytest.mark.asyncio
    async def test_search_basic(
        self,
        entity_manager: EntityManager,
        sample_entity_node: EntityNode,
        mock_graph_client: MagicMock,
    ) -> None:
        """search() returns entities with relevance scores."""
        mock_search_result = MagicMock()
        mock_search_result.nodes = [sample_entity_node]
        mock_search_result.node_reranker_scores = [0.95]
        mock_search_result.episodes = []
        mock_search_result.episode_reranker_scores = []
        mock_graph_client.client.search_.return_value = mock_search_result

        results = await entity_manager.search("test query")

        assert len(results) == 1
        entity, score = results[0]
        assert entity.id == "entity-001"
        assert score == 0.95

    @pytest.mark.asyncio
    async def test_search_filters_by_type(
        self,
        entity_manager: EntityManager,
        mock_graph_client: MagicMock,
    ) -> None:
        """search() filters results by entity type."""
        pattern_node = EntityNode(
            uuid="pattern-001",
            name="Pattern",
            group_id="test-org-123",
            labels=["Entity", "pattern"],
            created_at=datetime.now(UTC),
            summary="A pattern",
            attributes={"entity_type": "pattern"},
        )
        task_node = EntityNode(
            uuid="task-001",
            name="Task",
            group_id="test-org-123",
            labels=["Entity", "task"],
            created_at=datetime.now(UTC),
            summary="A task",
            attributes={"entity_type": "task"},
        )

        mock_search_result = MagicMock()
        mock_search_result.nodes = [pattern_node, task_node]
        mock_search_result.node_reranker_scores = [0.9, 0.8]
        mock_search_result.episodes = []
        mock_search_result.episode_reranker_scores = []
        mock_graph_client.client.search_.return_value = mock_search_result

        results = await entity_manager.search("test", entity_types=[EntityType.PATTERN])

        assert len(results) == 1
        assert results[0][0].entity_type == EntityType.PATTERN

    @pytest.mark.asyncio
    async def test_search_sanitizes_query(
        self,
        entity_manager: EntityManager,
        mock_graph_client: MagicMock,
    ) -> None:
        """search() sanitizes special characters in query."""
        mock_search_result = MagicMock()
        mock_search_result.nodes = []
        mock_search_result.node_reranker_scores = []
        mock_search_result.episodes = []
        mock_search_result.episode_reranker_scores = []
        mock_graph_client.client.search_.return_value = mock_search_result

        # Query with RediSearch special characters
        await entity_manager.search("create/cleanup @user ~fuzzy")

        # Verify search was called (query gets sanitized internally)
        mock_graph_client.client.search_.assert_called_once()

    @pytest.mark.asyncio
    async def test_search_respects_limit(
        self,
        entity_manager: EntityManager,
        mock_graph_client: MagicMock,
    ) -> None:
        """search() limits number of results."""
        nodes = [
            EntityNode(
                uuid=f"entity-{i:03d}",
                name=f"Entity {i}",
                group_id="test-org-123",
                labels=["Entity"],
                created_at=datetime.now(UTC),
                summary=f"Entity {i}",
                attributes={"entity_type": "pattern"},
            )
            for i in range(10)
        ]

        mock_search_result = MagicMock()
        mock_search_result.nodes = nodes
        mock_search_result.node_reranker_scores = [0.9 - i * 0.05 for i in range(10)]
        mock_search_result.episodes = []
        mock_search_result.episode_reranker_scores = []
        mock_graph_client.client.search_.return_value = mock_search_result

        results = await entity_manager.search("test", limit=3)

        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_search_filters_by_group(
        self,
        entity_manager: EntityManager,
        mock_graph_client: MagicMock,
    ) -> None:
        """search() only returns results from correct group."""
        own_group_node = EntityNode(
            uuid="own-entity",
            name="Own Entity",
            group_id="test-org-123",
            labels=["Entity"],
            created_at=datetime.now(UTC),
            summary="Own",
            attributes={"entity_type": "pattern"},
        )
        other_group_node = EntityNode(
            uuid="other-entity",
            name="Other Entity",
            group_id="other-org",
            labels=["Entity"],
            created_at=datetime.now(UTC),
            summary="Other",
            attributes={"entity_type": "pattern"},
        )

        mock_search_result = MagicMock()
        mock_search_result.nodes = [own_group_node, other_group_node]
        mock_search_result.node_reranker_scores = [0.9, 0.85]
        mock_search_result.episodes = []
        mock_search_result.episode_reranker_scores = []
        mock_graph_client.client.search_.return_value = mock_search_result

        results = await entity_manager.search("test")

        assert len(results) == 1
        assert results[0][0].id == "own-entity"

    @pytest.mark.asyncio
    async def test_search_failure_raises_error(
        self,
        entity_manager: EntityManager,
        mock_graph_client: MagicMock,
    ) -> None:
        """search() raises SearchError on failure."""
        mock_graph_client.client.search_.side_effect = Exception("Search failed")

        with pytest.raises(SearchError, match="Search failed"):
            await entity_manager.search("test")


# =============================================================================
# Epic/Project Relationship Tests
# =============================================================================


class TestGetTasksForEpic:
    """Test retrieving tasks for an epic."""

    @pytest.mark.asyncio
    async def test_get_tasks_for_epic(
        self,
        entity_manager: EntityManager,
        mock_driver: MagicMock,
    ) -> None:
        """get_tasks_for_epic() returns tasks via BELONGS_TO relationship."""
        mock_driver.execute_query.return_value = (
            [
                {
                    "uuid": "task-001",
                    "name": "Epic Task 1",
                    "entity_type": "task",
                    "group_id": "test-org-123",
                    "metadata": json.dumps({"status": "doing"}),
                },
                {
                    "uuid": "task-002",
                    "name": "Epic Task 2",
                    "entity_type": "task",
                    "group_id": "test-org-123",
                    "metadata": json.dumps({"status": "todo"}),
                },
            ],
            None,
            None,
        )

        results = await entity_manager.get_tasks_for_epic("epic-001")

        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_get_tasks_for_epic_with_status_filter(
        self,
        entity_manager: EntityManager,
        mock_driver: MagicMock,
    ) -> None:
        """get_tasks_for_epic() filters by status."""
        mock_driver.execute_query.return_value = (
            [
                {
                    "uuid": "task-001",
                    "name": "Task Doing",
                    "entity_type": "task",
                    "group_id": "test-org-123",
                    "metadata": json.dumps({"status": "doing"}),
                },
                {
                    "uuid": "task-002",
                    "name": "Task Todo",
                    "entity_type": "task",
                    "group_id": "test-org-123",
                    "metadata": json.dumps({"status": "todo"}),
                },
            ],
            None,
            None,
        )

        results = await entity_manager.get_tasks_for_epic("epic-001", status="doing")

        assert len(results) == 1
        assert results[0].id == "task-001"


class TestGetEpicProgress:
    """Test epic progress calculation."""

    @pytest.mark.asyncio
    async def test_get_epic_progress(
        self,
        entity_manager: EntityManager,
        mock_driver: MagicMock,
    ) -> None:
        """get_epic_progress() calculates completion percentage."""
        mock_driver.execute_query.return_value = (
            [
                {"metadata": json.dumps({"status": "done"})},
                {"metadata": json.dumps({"status": "done"})},
                {"metadata": json.dumps({"status": "doing"})},
                {"metadata": json.dumps({"status": "todo"})},
                {"metadata": json.dumps({"status": "blocked"})},
            ],
            None,
            None,
        )

        progress = await entity_manager.get_epic_progress("epic-001")

        assert progress["total_tasks"] == 5
        assert progress["completed_tasks"] == 2
        assert progress["in_progress_tasks"] == 1
        assert progress["blocked_tasks"] == 1
        assert progress["completion_pct"] == 40.0

    @pytest.mark.asyncio
    async def test_get_epic_progress_empty(
        self,
        entity_manager: EntityManager,
        mock_driver: MagicMock,
    ) -> None:
        """get_epic_progress() handles epic with no tasks."""
        mock_driver.execute_query.return_value = ([], None, None)

        progress = await entity_manager.get_epic_progress("epic-001")

        assert progress["total_tasks"] == 0
        assert progress["completed_tasks"] == 0
        assert progress["completion_pct"] == 0.0


class TestListEpicsForProject:
    """Test listing epics for a project."""

    @pytest.mark.asyncio
    async def test_list_epics_for_project(
        self,
        entity_manager: EntityManager,
        mock_driver: MagicMock,
    ) -> None:
        """list_epics_for_project() returns epics for project."""
        mock_driver.execute_query.return_value = (
            [
                {
                    "uuid": "epic-001",
                    "name": "Epic 1",
                    "entity_type": "epic",
                    "group_id": "test-org-123",
                    "metadata": json.dumps({}),
                    "status": "in_progress",
                    "priority": "high",
                },
                {
                    "uuid": "epic-002",
                    "name": "Epic 2",
                    "entity_type": "epic",
                    "group_id": "test-org-123",
                    "metadata": json.dumps({}),
                    "status": "planning",
                    "priority": "medium",
                },
            ],
            None,
            None,
        )

        results = await entity_manager.list_epics_for_project("project-001")

        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_list_epics_for_project_with_status_filter(
        self,
        entity_manager: EntityManager,
        mock_driver: MagicMock,
    ) -> None:
        """list_epics_for_project() filters by status."""
        mock_driver.execute_query.return_value = (
            [
                {
                    "uuid": "epic-001",
                    "name": "Active Epic",
                    "entity_type": "epic",
                    "group_id": "test-org-123",
                    "metadata": json.dumps({}),
                    "status": "in_progress",
                }
            ],
            None,
            None,
        )

        await entity_manager.list_epics_for_project("project-001", status="in_progress")

        # Verify query includes status filter
        call_args = mock_driver.execute_query.call_args
        assert "status" in call_args.kwargs


# =============================================================================
# Notes Tests
# =============================================================================


class TestGetNotesForTask:
    """Test retrieving notes for a task."""

    @pytest.mark.asyncio
    async def test_get_notes_for_task(
        self,
        entity_manager: EntityManager,
        mock_driver: MagicMock,
    ) -> None:
        """get_notes_for_task() returns notes via BELONGS_TO relationship."""
        mock_driver.execute_query.return_value = (
            [
                {
                    "uuid": "note-001",
                    "name": "First note",
                    "entity_type": "note",
                    "group_id": "test-org-123",
                    "content": "Note content",
                    "metadata": json.dumps({"task_id": "task-001"}),
                }
            ],
            None,
            None,
        )

        results = await entity_manager.get_notes_for_task("task-001")

        assert len(results) == 1
        assert results[0].id == "note-001"


# =============================================================================
# Bulk Operations Tests
# =============================================================================


class TestBulkCreateDirect:
    """Test bulk entity creation."""

    @pytest.mark.asyncio
    async def test_bulk_create_direct(
        self,
        entity_manager: EntityManager,
    ) -> None:
        """bulk_create_direct() creates multiple entities."""
        entities = [
            Entity(
                id=f"entity-{i:03d}",
                entity_type=EntityType.PATTERN,
                name=f"Pattern {i}",
                description=f"Description {i}",
            )
            for i in range(5)
        ]

        with patch.object(EntityNode, "save", new_callable=AsyncMock):
            created, failed = await entity_manager.bulk_create_direct(entities)

            assert created == 5
            assert failed == 0

    @pytest.mark.asyncio
    async def test_bulk_create_direct_partial_failure(
        self,
        entity_manager: EntityManager,
        mock_graph_client: MagicMock,
    ) -> None:
        """bulk_create_direct() tracks failed creations."""
        entities = [
            Entity(
                id=f"entity-{i:03d}",
                entity_type=EntityType.PATTERN,
                name=f"Pattern {i}",
                description=f"Description {i}",
            )
            for i in range(3)
        ]

        call_count = 0

        async def flaky_save(*args: object, **kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            # Fail on second entity
            if call_count == 2:
                raise Exception("Random failure")
            return None

        # Make EntityNode.save fail on second call
        with patch.object(EntityNode, "save", new_callable=AsyncMock, side_effect=flaky_save):
            created, failed = await entity_manager.bulk_create_direct(entities)

        assert created == 2
        assert failed == 1


# =============================================================================
# Helper Function Tests
# =============================================================================


class TestSanitizeSearchQuery:
    """Test query sanitization helper."""

    def test_sanitize_special_chars(self) -> None:
        """sanitize_search_query() removes RediSearch special characters."""
        query = "create/cleanup @user ~fuzzy | (group) $var -exclude"
        result = sanitize_search_query(query)

        # All special chars should be replaced with spaces
        assert "/" not in result
        assert "@" not in result
        assert "~" not in result
        assert "|" not in result
        assert "(" not in result
        assert ")" not in result
        assert "$" not in result
        assert "-" not in result

    def test_sanitize_preserves_words(self) -> None:
        """sanitize_search_query() preserves normal text."""
        query = "simple search query"
        result = sanitize_search_query(query)

        assert result == "simple search query"


class TestNodeToEntity:
    """Test node conversion helpers."""

    def test_node_to_entity_basic(
        self,
        entity_manager: EntityManager,
        sample_entity_node: EntityNode,
    ) -> None:
        """node_to_entity() converts EntityNode to Entity."""
        result = entity_manager.node_to_entity(sample_entity_node)

        assert result.id == "entity-001"
        assert result.name == "Test Entity"
        assert result.entity_type == EntityType.PATTERN

    def test_node_to_entity_with_labels(
        self,
        entity_manager: EntityManager,
    ) -> None:
        """node_to_entity() extracts type from labels if not in attributes."""
        node = EntityNode(
            uuid="node-001",
            name="Task Node",
            group_id="test-org-123",
            labels=["Entity", "task"],
            created_at=datetime.now(UTC),
            summary="A task",
            attributes={},  # No entity_type attribute
        )

        result = entity_manager.node_to_entity(node)

        assert result.entity_type == EntityType.TASK

    def test_node_to_entity_unknown_type_defaults_to_topic(
        self,
        entity_manager: EntityManager,
    ) -> None:
        """node_to_entity() defaults to TOPIC for unknown types."""
        node = EntityNode(
            uuid="node-001",
            name="Unknown Node",
            group_id="test-org-123",
            labels=["Entity", "unknown_type"],
            created_at=datetime.now(UTC),
            summary="Unknown",
            attributes={"entity_type": "not_a_real_type"},
        )

        result = entity_manager.node_to_entity(node)

        assert result.entity_type == EntityType.TOPIC


# =============================================================================
# Edge Cases and Error Handling
# =============================================================================


class TestEdgeCases:
    """Test edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_list_by_type_handles_malformed_metadata(
        self,
        entity_manager: EntityManager,
        mock_driver: MagicMock,
    ) -> None:
        """list_by_type() handles invalid JSON in metadata."""
        mock_driver.execute_query.return_value = (
            [
                {
                    "uuid": "task-001",
                    "name": "Task with bad metadata",
                    "entity_type": "task",
                    "group_id": "test-org-123",
                    "metadata": "not valid json{{{",
                }
            ],
            None,
            None,
        )

        # Should not raise, but may skip the malformed record
        results = await entity_manager.list_by_type(EntityType.TASK)

        # The record should be skipped or handled gracefully
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_search_handles_empty_results(
        self,
        entity_manager: EntityManager,
        mock_graph_client: MagicMock,
    ) -> None:
        """search() returns empty list when no matches."""
        mock_search_result = MagicMock()
        mock_search_result.nodes = []
        mock_search_result.node_reranker_scores = []
        mock_search_result.episodes = []
        mock_search_result.episode_reranker_scores = []
        mock_graph_client.client.search_.return_value = mock_search_result

        results = await entity_manager.search("nonexistent query xyz")

        assert results == []

    @pytest.mark.asyncio
    async def test_list_by_type_handles_db_error(
        self,
        entity_manager: EntityManager,
        mock_driver: MagicMock,
    ) -> None:
        """list_by_type() returns empty list on DB error."""
        mock_driver.execute_query.side_effect = Exception("DB connection lost")

        results = await entity_manager.list_by_type(EntityType.TASK)

        assert results == []

    def test_record_to_entity_handles_missing_fields(
        self,
        entity_manager: EntityManager,
    ) -> None:
        """_record_to_entity() handles records with missing fields."""
        minimal_record = {
            "uuid": "min-001",
            "name": "Minimal",
            "entity_type": "pattern",
        }

        result = entity_manager._record_to_entity(minimal_record)

        assert result.id == "min-001"
        assert result.name == "Minimal"
        assert result.description == ""
        assert result.content == ""

    def test_record_to_entity_handles_unknown_entity_type(
        self,
        entity_manager: EntityManager,
    ) -> None:
        """_record_to_entity() defaults to EPISODE for unknown types."""
        record = {
            "uuid": "unknown-001",
            "name": "Unknown Type Entity",
            "entity_type": "not_a_real_type",
        }

        result = entity_manager._record_to_entity(record)

        assert result.entity_type == EntityType.EPISODE

    @pytest.mark.asyncio
    async def test_update_handles_db_error_during_persist(
        self,
        mock_graph_client: MagicMock,
    ) -> None:
        """update() propagates errors from persistence layer."""
        # Create a fresh manager with proper mock setup
        manager = EntityManager(mock_graph_client, group_id="test-org-123")

        # Create a mock entity node
        mock_entity = MagicMock(spec=EntityNode)
        mock_entity.uuid = "entity-001"
        mock_entity.name = "Test Entity"
        mock_entity.group_id = "test-org-123"
        mock_entity.labels = ["Entity", "pattern"]
        mock_entity.created_at = datetime.now(UTC)
        mock_entity.summary = "Test"
        mock_entity.attributes = {"entity_type": "pattern", "metadata": "{}"}
        mock_entity.name_embedding = None

        with (
            patch.object(
                EntityNode,
                "get_by_uuid",
                new_callable=AsyncMock,
                return_value=mock_entity,
            ),
            patch.object(
                manager,
                "_persist_entity_attributes",
                new_callable=AsyncMock,
                side_effect=Exception("Write failed"),
            ),
            pytest.raises(Exception, match="Write failed"),
        ):
            await manager.update("entity-001", {"name": "New Name"})


# =============================================================================
# Project Summary Tests
# =============================================================================


class TestGetProjectSummary:
    """Test project summary generation."""

    @pytest.mark.asyncio
    async def test_get_project_summary(
        self,
        entity_manager: EntityManager,
        mock_driver: MagicMock,
    ) -> None:
        """get_project_summary() returns task counts and actionable tasks."""
        # Mock task query results
        mock_driver.execute_query.return_value = (
            [
                {
                    "uuid": "task-001",
                    "name": "CRITICAL Bug",
                    "metadata": json.dumps({"status": "doing", "priority": "critical"}),
                },
                {
                    "uuid": "task-002",
                    "name": "Regular task",
                    "metadata": json.dumps({"status": "todo", "priority": "medium"}),
                },
                {
                    "uuid": "task-003",
                    "name": "Blocked task",
                    "metadata": json.dumps({"status": "blocked", "priority": "high"}),
                },
                {
                    "uuid": "task-004",
                    "name": "Done task",
                    "metadata": json.dumps({"status": "done", "priority": "medium"}),
                },
            ],
            None,
            None,
        )

        result = await entity_manager.get_project_summary("project-001")

        assert result["total_tasks"] == 4
        assert result["status_counts"]["doing"] == 1
        assert result["status_counts"]["todo"] == 1
        assert result["status_counts"]["blocked"] == 1
        assert result["status_counts"]["done"] == 1
        assert result["progress_pct"] == 25.0
        # Should have actionable tasks prioritized: doing > blocked
        assert len(result["actionable_tasks"]) > 0
        # Critical task should be in critical_tasks
        assert len(result["critical_tasks"]) > 0
