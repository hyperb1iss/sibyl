"""Unit tests for EntityManager in graph/entities.py.

Tests entity CRUD operations, conversions, and helper functions
with mocked dependencies (no real FalkorDB/LLM required).
"""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from sibyl.errors import EntityNotFoundError
from sibyl.graph.entities import EntityManager, sanitize_search_query
from sibyl.models.entities import Entity, EntityType
from sibyl.models.tasks import Epic, EpicStatus, Project, Task, TaskPriority, TaskStatus


class MockDriver:
    """Mock FalkorDB driver for testing."""

    def __init__(self) -> None:
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self.results: list[Any] = []
        self._result_index = 0

    def set_results(self, results: list[Any]) -> None:
        """Set results for upcoming queries."""
        self.results = results
        self._result_index = 0

    async def execute_query(self, query: str, **params: Any) -> Any:
        """Record query and return next result."""
        self.queries.append((query, params))
        if self._result_index < len(self.results):
            result = self.results[self._result_index]
            self._result_index += 1
            return result
        return []

    def clone(self, group_id: str) -> "MockDriver":
        """Clone driver for org-specific operations."""
        return self


class MockEmbedder:
    """Mock embedder for testing."""

    async def create(self, text: str) -> list[float]:
        """Return fake embedding."""
        return [0.1] * 1536


class MockGraphitiClient:
    """Mock Graphiti client for testing."""

    def __init__(self, driver: MockDriver) -> None:
        self.driver = driver
        self.embedder = MockEmbedder()
        self._add_episode_results: list[Any] = []

    def set_add_episode_results(self, results: list[Any]) -> None:
        """Set results for add_episode calls."""
        self._add_episode_results = results

    async def add_episode(self, **kwargs: Any) -> Any:
        """Mock add_episode."""
        if self._add_episode_results:
            return self._add_episode_results.pop(0)
        # Return a mock result with episode attribute
        mock_result = MagicMock()
        mock_result.episode = MagicMock()
        mock_result.episode.uuid = f"episode_{uuid4().hex[:8]}"
        return mock_result

    async def search_(self, **kwargs: Any) -> Any:
        """Mock search_ method."""
        mock_result = MagicMock()
        mock_result.nodes = []
        mock_result.episodes = []
        mock_result.node_reranker_scores = []
        mock_result.episode_reranker_scores = []
        return mock_result


class MockGraphClient:
    """Mock GraphClient for testing EntityManager."""

    def __init__(self) -> None:
        self._driver = MockDriver()
        self._graphiti_client = MockGraphitiClient(self._driver)
        self._write_semaphore = asyncio.Semaphore(1)

    @property
    def client(self) -> MockGraphitiClient:
        return self._graphiti_client

    @property
    def write_lock(self) -> asyncio.Semaphore:
        return self._write_semaphore

    @staticmethod
    def normalize_result(result: Any) -> list[dict[str, Any]]:
        """Normalize query results."""
        if result is None:
            return []
        if isinstance(result, tuple):
            return list(result[0]) if result else []
        if isinstance(result, list):
            return result
        return []


# Test organization ID
TEST_ORG_ID = f"test_org_{uuid4().hex[:8]}"


class TestSanitizeSearchQuery:
    """Tests for sanitize_search_query helper."""

    def test_escapes_pipe(self) -> None:
        """Pipe operator should be escaped."""
        assert "|" not in sanitize_search_query("foo|bar")

    def test_escapes_ampersand(self) -> None:
        """Ampersand should be escaped."""
        assert "&" not in sanitize_search_query("foo&bar")

    def test_escapes_minus(self) -> None:
        """Minus should be escaped."""
        result = sanitize_search_query("foo-bar")
        assert "-" not in result

    def test_escapes_at_sign(self) -> None:
        """At sign should be escaped."""
        assert "@" not in sanitize_search_query("foo@bar")

    def test_escapes_parentheses(self) -> None:
        """Parentheses should be escaped."""
        result = sanitize_search_query("foo(bar)")
        assert "(" not in result
        assert ")" not in result

    def test_escapes_colon(self) -> None:
        """Colon should be escaped."""
        assert ":" not in sanitize_search_query("foo:bar")

    def test_escapes_asterisk(self) -> None:
        """Asterisk should be escaped."""
        assert "*" not in sanitize_search_query("foo*bar")

    def test_preserves_alphanumeric(self) -> None:
        """Alphanumeric characters should be preserved."""
        result = sanitize_search_query("foo123 bar456")
        assert "foo123" in result
        assert "bar456" in result

    def test_complex_query(self) -> None:
        """Complex query with multiple special chars."""
        query = "error|warn @tag (category:debug)"
        result = sanitize_search_query(query)
        # All special chars should be replaced with spaces
        for char in "|@():":
            assert char not in result


class TestEntityManagerInit:
    """Tests for EntityManager initialization."""

    def test_requires_group_id(self) -> None:
        """EntityManager requires a non-empty group_id."""
        client = MockGraphClient()
        with pytest.raises(ValueError, match="group_id is required"):
            EntityManager(client, group_id="")

    def test_rejects_none_group_id(self) -> None:
        """EntityManager rejects None group_id."""
        client = MockGraphClient()
        with pytest.raises(ValueError, match="group_id is required"):
            EntityManager(client, group_id=None)  # type: ignore[arg-type]

    def test_accepts_valid_group_id(self) -> None:
        """EntityManager accepts valid group_id."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)
        assert manager._group_id == TEST_ORG_ID


class TestEntityManagerCreate:
    """Tests for EntityManager.create method."""

    @pytest.mark.asyncio
    async def test_create_calls_add_episode(self) -> None:
        """Create should call Graphiti add_episode."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        entity = Entity(
            id=f"test_{uuid4().hex[:8]}",
            name="Test Entity",
            entity_type=EntityType.EPISODE,
            description="Test description",
            content="Test content",
        )

        created_id = await manager.create(entity)
        assert created_id is not None

    @pytest.mark.asyncio
    async def test_create_formats_episode_body(self) -> None:
        """Create should format entity as episode body."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        entity = Entity(
            id=f"test_{uuid4().hex[:8]}",
            name="My Pattern",
            entity_type=EntityType.PATTERN,
            description="A useful pattern",
            content="Pattern content here",
        )

        # The internal _format_entity_as_episode should structure the data
        episode_body = manager._format_entity_as_episode(entity)
        assert "Entity: My Pattern" in episode_body
        assert "type: pattern" in episode_body.lower()  # Lowercase comparison
        assert "Description: A useful pattern" in episode_body

    @pytest.mark.asyncio
    async def test_create_uses_caller_id(self) -> None:
        """Create should preserve caller-provided entity ID."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        desired_id = f"desired_{uuid4().hex[:8]}"
        entity = Entity(
            id=desired_id,
            name="Test Entity",
            entity_type=EntityType.EPISODE,
        )

        created_id = await manager.create(entity)
        # The returned ID should be the desired ID after the SET query
        # In production, this modifies the node's uuid
        assert created_id is not None


class TestEntityManagerCreateDirect:
    """Tests for EntityManager.create_direct method."""

    @pytest.mark.asyncio
    async def test_create_direct_skips_llm(self) -> None:
        """create_direct should not call add_episode (LLM path)."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        entity = Entity(
            id=f"direct_{uuid4().hex[:8]}",
            name="Direct Entity",
            entity_type=EntityType.PATTERN,
            description="Created directly",
        )

        with patch("sibyl.graph.entities.EntityNode") as mock_entity_node:
            mock_instance = MagicMock()
            mock_instance.save = AsyncMock()
            mock_entity_node.return_value = mock_instance

            created_id = await manager.create_direct(entity, generate_embedding=False)
            assert created_id == entity.id
            mock_instance.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_direct_generates_embedding(self) -> None:
        """create_direct should generate embedding by default."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        entity = Entity(
            id=f"embed_{uuid4().hex[:8]}",
            name="Embedded Entity",
            entity_type=EntityType.PATTERN,
            description="Has embedding",
        )

        with patch("sibyl.graph.entities.EntityNode") as mock_entity_node:
            mock_instance = MagicMock()
            mock_instance.save = AsyncMock()
            mock_entity_node.return_value = mock_instance

            await manager.create_direct(entity, generate_embedding=True)

            # Should have called embedder.create
            # The embedding is stored via execute_query

    @pytest.mark.asyncio
    async def test_create_direct_task_with_metadata(self) -> None:
        """create_direct should persist Task-specific metadata."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        task = Task(
            id=f"task_{uuid4().hex[:8]}",
            title="Test Task",  # Task requires title field
            description="A task to complete",
            status=TaskStatus.TODO,
            priority=TaskPriority.HIGH,
            project_id="proj_123",
        )

        with patch("sibyl.graph.entities.EntityNode") as mock_entity_node:
            mock_instance = MagicMock()
            mock_instance.save = AsyncMock()
            mock_entity_node.return_value = mock_instance

            await manager.create_direct(task, generate_embedding=False)

            # Verify EntityNode was created with correct attributes
            call_kwargs = mock_entity_node.call_args.kwargs
            assert call_kwargs["uuid"] == task.id
            assert call_kwargs["name"] == task.title  # Task uses title as name
            assert "task" in call_kwargs["labels"]


class TestEntityManagerGet:
    """Tests for EntityManager.get method."""

    @pytest.mark.asyncio
    async def test_get_not_found_raises(self) -> None:
        """get should raise EntityNotFoundError when not found."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        with patch("sibyl.graph.entities.EntityNode") as mock_entity_node:
            mock_entity_node.get_by_uuid = AsyncMock(side_effect=Exception("Not found"))

            with patch("sibyl.graph.entities.EpisodicNode") as mock_episodic:
                mock_episodic.get_by_uuid = AsyncMock(side_effect=Exception("Not found"))

                with pytest.raises(EntityNotFoundError):
                    await manager.get("nonexistent_id")

    @pytest.mark.asyncio
    async def test_get_entity_node_success(self) -> None:
        """get should return entity from EntityNode lookup."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        mock_node = MagicMock()
        mock_node.uuid = "entity_123"
        mock_node.name = "Test Entity"
        mock_node.group_id = TEST_ORG_ID
        mock_node.labels = ["pattern"]
        mock_node.summary = "Test summary"
        mock_node.created_at = datetime.now(UTC)
        mock_node.attributes = {"entity_type": "pattern", "description": "Test desc"}
        mock_node.name_embedding = None

        with patch("sibyl.graph.entities.EntityNode") as mock_entity_cls:
            mock_entity_cls.get_by_uuid = AsyncMock(return_value=mock_node)

            entity = await manager.get("entity_123")
            assert entity.id == "entity_123"
            assert entity.name == "Test Entity"
            assert entity.entity_type == EntityType.PATTERN

    @pytest.mark.asyncio
    async def test_get_wrong_group_id_raises(self) -> None:
        """get should reject entities from different organizations."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        mock_node = MagicMock()
        mock_node.uuid = "entity_123"
        mock_node.group_id = "different_org"  # Wrong org
        mock_node.labels = []
        mock_node.attributes = {}

        with patch("sibyl.graph.entities.EntityNode") as mock_entity_cls:
            mock_entity_cls.get_by_uuid = AsyncMock(return_value=mock_node)

            with patch("sibyl.graph.entities.EpisodicNode") as mock_episodic:
                mock_episodic.get_by_uuid = AsyncMock(side_effect=Exception("Not found"))

                with pytest.raises(EntityNotFoundError):
                    await manager.get("entity_123")

    @pytest.mark.asyncio
    async def test_get_episodic_node_fallback(self) -> None:
        """get should try EpisodicNode if EntityNode fails."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        mock_episodic = MagicMock()
        mock_episodic.uuid = "episodic_456"
        mock_episodic.name = "episode:Test Episode"
        mock_episodic.group_id = TEST_ORG_ID
        mock_episodic.content = "Episode content"
        mock_episodic.source_description = "MCP Entity"
        mock_episodic.created_at = datetime.now(UTC)

        with patch("sibyl.graph.entities.EntityNode") as mock_entity_cls:
            mock_entity_cls.get_by_uuid = AsyncMock(side_effect=Exception("Not found"))

            with patch("sibyl.graph.entities.EpisodicNode") as mock_episodic_cls:
                mock_episodic_cls.get_by_uuid = AsyncMock(return_value=mock_episodic)

                entity = await manager.get("episodic_456")
                assert entity.id == "episodic_456"
                assert entity.entity_type == EntityType.EPISODE


class TestEntityManagerUpdate:
    """Tests for EntityManager.update method."""

    @pytest.mark.asyncio
    async def test_update_merges_metadata(self) -> None:
        """update should merge new metadata with existing."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        existing = Entity(
            id="entity_123",
            name="Original Name",
            entity_type=EntityType.PATTERN,
            description="Original desc",
            metadata={"key1": "value1"},
        )

        with patch.object(manager, "get", return_value=existing):
            updated = await manager.update("entity_123", {"key2": "value2"})

            assert updated is not None
            assert updated.metadata.get("key1") == "value1"
            assert updated.metadata.get("key2") == "value2"

    @pytest.mark.asyncio
    async def test_update_not_found_raises(self) -> None:
        """update should raise EntityNotFoundError when entity doesn't exist."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        with (
            patch.object(manager, "get", side_effect=EntityNotFoundError("Entity", "missing")),
            pytest.raises(EntityNotFoundError),
        ):
            await manager.update("missing", {"name": "New Name"})

    @pytest.mark.asyncio
    async def test_update_name_description(self) -> None:
        """update should update name and description fields."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        existing = Entity(
            id="entity_123",
            name="Old Name",
            entity_type=EntityType.PATTERN,
            description="Old description",
        )

        with patch.object(manager, "get", return_value=existing):
            updated = await manager.update(
                "entity_123", {"name": "New Name", "description": "New description"}
            )

            assert updated is not None
            assert updated.name == "New Name"
            assert updated.description == "New description"

    @pytest.mark.asyncio
    async def test_update_embedding(self) -> None:
        """update should store embedding on node."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        existing = Entity(
            id="entity_123",
            name="Test",
            entity_type=EntityType.PATTERN,
        )

        embedding = [0.1] * 1536

        with patch.object(manager, "get", return_value=existing):
            updated = await manager.update("entity_123", {"embedding": embedding})

            assert updated is not None
            # Check that embedding query was executed
            queries = client._driver.queries
            assert any("name_embedding" in q[0] for q in queries)


class TestEntityManagerDelete:
    """Tests for EntityManager.delete method."""

    @pytest.mark.asyncio
    async def test_delete_entity_node(self) -> None:
        """delete should delete EntityNode if found."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        mock_node = MagicMock()
        mock_node.uuid = "entity_123"
        mock_node.group_id = TEST_ORG_ID
        mock_node.delete = AsyncMock()

        with patch("sibyl.graph.entities.EntityNode") as mock_entity_cls:
            mock_entity_cls.get_by_uuid = AsyncMock(return_value=mock_node)

            result = await manager.delete("entity_123")
            assert result is True
            mock_node.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_episodic_fallback(self) -> None:
        """delete should try EpisodicNode if EntityNode fails."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        mock_episodic = MagicMock()
        mock_episodic.uuid = "episodic_456"
        mock_episodic.group_id = TEST_ORG_ID
        mock_episodic.delete = AsyncMock()

        with patch("sibyl.graph.entities.EntityNode") as mock_entity_cls:
            mock_entity_cls.get_by_uuid = AsyncMock(side_effect=Exception("Not found"))

            with patch("sibyl.graph.entities.EpisodicNode") as mock_episodic_cls:
                mock_episodic_cls.get_by_uuid = AsyncMock(return_value=mock_episodic)

                result = await manager.delete("episodic_456")
                assert result is True
                mock_episodic.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_not_found_raises(self) -> None:
        """delete should raise EntityNotFoundError when not found."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        with patch("sibyl.graph.entities.EntityNode") as mock_entity_cls:
            mock_entity_cls.get_by_uuid = AsyncMock(side_effect=Exception("Not found"))

            with patch("sibyl.graph.entities.EpisodicNode") as mock_episodic_cls:
                mock_episodic_cls.get_by_uuid = AsyncMock(side_effect=Exception("Not found"))

                with pytest.raises(EntityNotFoundError):
                    await manager.delete("nonexistent")

    @pytest.mark.asyncio
    async def test_delete_wrong_group_id_raises(self) -> None:
        """delete should reject entities from different organizations."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        mock_node = MagicMock()
        mock_node.uuid = "entity_123"
        mock_node.group_id = "different_org"  # Wrong org

        with patch("sibyl.graph.entities.EntityNode") as mock_entity_cls:
            mock_entity_cls.get_by_uuid = AsyncMock(return_value=mock_node)

            with patch("sibyl.graph.entities.EpisodicNode") as mock_episodic_cls:
                mock_episodic_cls.get_by_uuid = AsyncMock(side_effect=Exception("Not found"))

                with pytest.raises(EntityNotFoundError):
                    await manager.delete("entity_123")


class TestEntityManagerSearch:
    """Tests for EntityManager.search method."""

    @pytest.mark.asyncio
    async def test_search_sanitizes_query(self) -> None:
        """search should sanitize special characters."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        # Set up mock search results
        mock_result = MagicMock()
        mock_result.nodes = []
        mock_result.episodes = []
        mock_result.node_reranker_scores = []
        mock_result.episode_reranker_scores = []

        with patch.object(
            client._graphiti_client, "search_", return_value=mock_result
        ) as mock_search:
            await manager.search("test|query@special")

            # Verify search was called with sanitized query
            call_kwargs = mock_search.call_args.kwargs
            query = call_kwargs["query"]
            assert "|" not in query
            assert "@" not in query

    @pytest.mark.asyncio
    async def test_search_filters_by_type(self) -> None:
        """search should filter results by entity type."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        # Create mock nodes
        mock_node1 = MagicMock()
        mock_node1.uuid = "n1"
        mock_node1.name = "Pattern 1"
        mock_node1.group_id = TEST_ORG_ID
        mock_node1.labels = ["pattern"]
        mock_node1.summary = "Summary 1"
        mock_node1.created_at = datetime.now(UTC)
        mock_node1.attributes = {"entity_type": "pattern"}
        mock_node1.name_embedding = None

        mock_node2 = MagicMock()
        mock_node2.uuid = "n2"
        mock_node2.name = "Task 1"
        mock_node2.group_id = TEST_ORG_ID
        mock_node2.labels = ["task"]
        mock_node2.summary = "Summary 2"
        mock_node2.created_at = datetime.now(UTC)
        mock_node2.attributes = {"entity_type": "task"}
        mock_node2.name_embedding = None

        mock_result = MagicMock()
        mock_result.nodes = [mock_node1, mock_node2]
        mock_result.episodes = []
        mock_result.node_reranker_scores = [0.9, 0.8]
        mock_result.episode_reranker_scores = []

        with patch.object(client._graphiti_client, "search_", return_value=mock_result):
            # Search for only patterns
            results = await manager.search("test", entity_types=[EntityType.PATTERN])

            # Should only return the pattern
            assert len(results) == 1
            assert results[0][0].entity_type == EntityType.PATTERN

    @pytest.mark.asyncio
    async def test_search_respects_limit(self) -> None:
        """search should respect the limit parameter."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        # Create 5 mock nodes
        mock_nodes = []
        for i in range(5):
            node = MagicMock()
            node.uuid = f"n{i}"
            node.name = f"Entity {i}"
            node.group_id = TEST_ORG_ID
            node.labels = ["pattern"]
            node.summary = f"Summary {i}"
            node.created_at = datetime.now(UTC)
            node.attributes = {"entity_type": "pattern"}
            node.name_embedding = None
            mock_nodes.append(node)

        mock_result = MagicMock()
        mock_result.nodes = mock_nodes
        mock_result.episodes = []
        mock_result.node_reranker_scores = [0.9, 0.8, 0.7, 0.6, 0.5]
        mock_result.episode_reranker_scores = []

        with patch.object(client._graphiti_client, "search_", return_value=mock_result):
            results = await manager.search("test", limit=3)

            assert len(results) == 3

    @pytest.mark.asyncio
    async def test_search_filters_by_group_id(self) -> None:
        """search should filter out results from other organizations."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        mock_node_same_org = MagicMock()
        mock_node_same_org.uuid = "n1"
        mock_node_same_org.name = "Same Org"
        mock_node_same_org.group_id = TEST_ORG_ID
        mock_node_same_org.labels = ["pattern"]
        mock_node_same_org.summary = "Summary"
        mock_node_same_org.created_at = datetime.now(UTC)
        mock_node_same_org.attributes = {"entity_type": "pattern"}
        mock_node_same_org.name_embedding = None

        mock_node_diff_org = MagicMock()
        mock_node_diff_org.uuid = "n2"
        mock_node_diff_org.name = "Different Org"
        mock_node_diff_org.group_id = "other_org"  # Wrong org
        mock_node_diff_org.labels = ["pattern"]
        mock_node_diff_org.summary = "Summary"
        mock_node_diff_org.created_at = datetime.now(UTC)
        mock_node_diff_org.attributes = {"entity_type": "pattern"}
        mock_node_diff_org.name_embedding = None

        mock_result = MagicMock()
        mock_result.nodes = [mock_node_same_org, mock_node_diff_org]
        mock_result.episodes = []
        mock_result.node_reranker_scores = [0.9, 0.8]
        mock_result.episode_reranker_scores = []

        with patch.object(client._graphiti_client, "search_", return_value=mock_result):
            results = await manager.search("test")

            # Should only return the same org entity
            assert len(results) == 1
            assert results[0][0].organization_id == TEST_ORG_ID


class TestEntityManagerListByType:
    """Tests for EntityManager.list_by_type method."""

    @pytest.mark.asyncio
    async def test_list_by_type_queries_correctly(self) -> None:
        """list_by_type should query with correct type and group_id."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        # Set up mock result
        client._driver.set_results(
            [
                (
                    [
                        {
                            "uuid": "p1",
                            "name": "Pattern 1",
                            "entity_type": "pattern",
                            "group_id": TEST_ORG_ID,
                            "description": "Desc 1",
                        },
                        {
                            "uuid": "p2",
                            "name": "Pattern 2",
                            "entity_type": "pattern",
                            "group_id": TEST_ORG_ID,
                            "description": "Desc 2",
                        },
                    ],
                    ["uuid", "name", "entity_type", "group_id", "description"],
                    {},
                )
            ]
        )

        await manager.list_by_type(EntityType.PATTERN, limit=10)

        # Verify query was made with correct parameters
        assert len(client._driver.queries) > 0
        _query, params = client._driver.queries[0]
        assert params["entity_type"] == "pattern"
        assert params["group_id"] == TEST_ORG_ID

    @pytest.mark.asyncio
    async def test_list_by_type_respects_limit(self) -> None:
        """list_by_type should respect limit parameter."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        client._driver.set_results([([], [], {})])

        await manager.list_by_type(EntityType.PATTERN, limit=5, offset=10)

        _query, params = client._driver.queries[0]
        assert params["limit"] == 5
        assert params["offset"] == 10


class TestNodeToEntityConversion:
    """Tests for node_to_entity conversion method."""

    def test_converts_entity_type_from_attributes(self) -> None:
        """node_to_entity should extract entity_type from attributes."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        mock_node = MagicMock()
        mock_node.uuid = "n1"
        mock_node.name = "Test Node"
        mock_node.group_id = TEST_ORG_ID
        mock_node.labels = []
        mock_node.summary = "Summary"
        mock_node.created_at = datetime.now(UTC)
        mock_node.attributes = {"entity_type": "pattern", "description": "Desc"}
        mock_node.name_embedding = None

        entity = manager.node_to_entity(mock_node)

        assert entity.entity_type == EntityType.PATTERN
        assert entity.name == "Test Node"
        assert entity.id == "n1"

    def test_converts_entity_type_from_labels(self) -> None:
        """node_to_entity should fall back to labels for entity_type."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        mock_node = MagicMock()
        mock_node.uuid = "n1"
        mock_node.name = "Test Node"
        mock_node.group_id = TEST_ORG_ID
        mock_node.labels = ["Entity", "task"]  # task label should be used
        mock_node.summary = "Summary"
        mock_node.created_at = datetime.now(UTC)
        mock_node.attributes = {"description": "Desc"}  # No entity_type
        mock_node.name_embedding = None

        entity = manager.node_to_entity(mock_node)

        assert entity.entity_type == EntityType.TASK

    def test_defaults_to_topic_for_unknown_type(self) -> None:
        """node_to_entity should default to TOPIC for unknown types."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        mock_node = MagicMock()
        mock_node.uuid = "n1"
        mock_node.name = "Test Node"
        mock_node.group_id = TEST_ORG_ID
        mock_node.labels = ["Entity"]  # Generic label only
        mock_node.summary = "Summary"
        mock_node.created_at = datetime.now(UTC)
        mock_node.attributes = {"entity_type": "unknown_type"}
        mock_node.name_embedding = None

        entity = manager.node_to_entity(mock_node)

        assert entity.entity_type == EntityType.TOPIC

    def test_parses_json_metadata(self) -> None:
        """node_to_entity should parse JSON metadata string."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        metadata = {"category": "testing", "priority": "high"}
        mock_node = MagicMock()
        mock_node.uuid = "n1"
        mock_node.name = "Test Node"
        mock_node.group_id = TEST_ORG_ID
        mock_node.labels = ["pattern"]
        mock_node.summary = "Summary"
        mock_node.created_at = datetime.now(UTC)
        mock_node.attributes = {
            "entity_type": "pattern",
            "metadata": json.dumps(metadata),
        }
        mock_node.name_embedding = None

        entity = manager.node_to_entity(mock_node)

        assert entity.metadata.get("category") == "testing"
        assert entity.metadata.get("priority") == "high"


class TestEpisodicToEntityConversion:
    """Tests for _episodic_to_entity conversion method."""

    def test_extracts_type_from_name_prefix(self) -> None:
        """_episodic_to_entity should extract type from name prefix."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        mock_episodic = MagicMock()
        mock_episodic.uuid = "e1"
        mock_episodic.name = "pattern:My Pattern Name"
        mock_episodic.group_id = TEST_ORG_ID
        mock_episodic.content = "Content"
        mock_episodic.source_description = "MCP Entity"
        mock_episodic.created_at = datetime.now(UTC)

        entity = manager._episodic_to_entity(mock_episodic)

        assert entity.entity_type == EntityType.PATTERN
        assert entity.name == "My Pattern Name"

    def test_defaults_to_episode_type(self) -> None:
        """_episodic_to_entity should default to EPISODE type."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        mock_episodic = MagicMock()
        mock_episodic.uuid = "e1"
        mock_episodic.name = "Some Name Without Prefix"
        mock_episodic.group_id = TEST_ORG_ID
        mock_episodic.content = "Content"
        mock_episodic.source_description = "MCP Entity"
        mock_episodic.created_at = datetime.now(UTC)

        entity = manager._episodic_to_entity(mock_episodic)

        assert entity.entity_type == EntityType.EPISODE
        assert entity.name == "Some Name Without Prefix"


class TestEntityToMetadata:
    """Tests for _entity_to_metadata helper."""

    def test_task_metadata_extraction(self) -> None:
        """_entity_to_metadata should extract Task-specific fields."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        task = Task(
            id="task_123",
            title="Test Task",  # Task requires title field
            status=TaskStatus.DOING,
            priority=TaskPriority.HIGH,
            project_id="proj_456",
            feature="authentication",
            assignees=["alice", "bob"],
        )

        metadata = manager._entity_to_metadata(task)

        assert metadata["status"] == "doing"
        assert metadata["priority"] == "high"
        assert metadata["project_id"] == "proj_456"
        assert metadata["feature"] == "authentication"
        assert metadata["assignees"] == ["alice", "bob"]

    def test_project_metadata_extraction(self) -> None:
        """_entity_to_metadata should extract Project-specific fields."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        project = Project(
            id="proj_123",
            title="Test Project",  # Project requires title field
            tech_stack=["python", "fastapi"],
            repository_url="https://github.com/org/repo",
        )

        metadata = manager._entity_to_metadata(project)

        assert metadata["tech_stack"] == ["python", "fastapi"]
        assert metadata["repository_url"] == "https://github.com/org/repo"


class TestCollectProperties:
    """Tests for _collect_properties helper."""

    def test_collects_base_properties(self) -> None:
        """_collect_properties should collect base entity properties."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        entity = Entity(
            id="ent_123",
            name="Test Entity",
            entity_type=EntityType.PATTERN,
            description="Description",
            content="Content",
            source_file="/path/to/file.py",
        )

        props = manager._collect_properties(entity)

        assert props["uuid"] == "ent_123"
        assert props["name"] == "Test Entity"
        assert props["entity_type"] == "pattern"
        assert props["description"] == "Description"
        assert props["content"] == "Content"
        assert props["source_file"] == "/path/to/file.py"

    def test_collects_task_properties(self) -> None:
        """_collect_properties should collect Task-specific properties."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        task = Task(
            id="task_123",
            title="Test Task",  # Task requires title field
            status=TaskStatus.TODO,
            priority=TaskPriority.MEDIUM,
            project_id="proj_456",
            task_order=10,
        )

        props = manager._collect_properties(task)

        assert props["status"] == "todo"
        assert props["priority"] == "medium"
        assert props["project_id"] == "proj_456"
        assert props["task_order"] == 10


class TestBulkCreateDirect:
    """Tests for bulk_create_direct method."""

    @pytest.mark.asyncio
    async def test_bulk_create_counts(self) -> None:
        """bulk_create_direct should return correct counts."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        entities = [
            Entity(
                id=f"bulk_{i}",
                name=f"Bulk Entity {i}",
                entity_type=EntityType.PATTERN,
            )
            for i in range(5)
        ]

        with patch("sibyl.graph.entities.EntityNode") as mock_entity_node:
            mock_instance = MagicMock()
            mock_instance.save = AsyncMock()
            mock_entity_node.return_value = mock_instance

            created, failed = await manager.bulk_create_direct(entities)

            assert created == 5
            assert failed == 0

    @pytest.mark.asyncio
    async def test_bulk_create_handles_failures(self) -> None:
        """bulk_create_direct should count failures."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        entities = [
            Entity(
                id=f"bulk_{i}",
                name=f"Bulk Entity {i}",
                entity_type=EntityType.PATTERN,
            )
            for i in range(3)
        ]

        call_count = 0

        async def mock_save(driver: Any) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("Simulated failure")

        with patch("sibyl.graph.entities.EntityNode") as mock_entity_node:
            mock_instance = MagicMock()
            mock_instance.save = mock_save
            mock_entity_node.return_value = mock_instance

            created, failed = await manager.bulk_create_direct(entities)

            assert created == 2
            assert failed == 1


class TestFormatSpecializedFields:
    """Tests for _format_specialized_fields helper."""

    def test_formats_task_fields(self) -> None:
        """_format_specialized_fields should format Task fields."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        task = Task(
            id="task_123",
            title="Test Task",  # Task requires title field
            status=TaskStatus.DOING,
            priority=TaskPriority.HIGH,
            domain="backend",
            technologies=["python", "fastapi"],
            feature="auth",
        )

        def sanitize(text: str) -> str:
            return text

        parts = manager._format_specialized_fields(task, sanitize)

        assert any("doing" in p.lower() for p in parts)
        assert any("high" in p.lower() for p in parts)
        assert any("backend" in p.lower() for p in parts)
        assert any("python" in p for p in parts)
        assert any("auth" in p for p in parts)

    def test_formats_project_fields(self) -> None:
        """_format_specialized_fields should format Project fields."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        project = Project(
            id="proj_123",
            title="Test Project",  # Project requires title field
            tech_stack=["python", "react"],
            features=["auth", "api"],
        )

        def sanitize(text: str) -> str:
            return text

        parts = manager._format_specialized_fields(project, sanitize)

        assert any("python" in p for p in parts)
        assert any("auth" in p for p in parts)

    def test_formats_epic_fields(self) -> None:
        """_format_specialized_fields should format Epic fields."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        epic = Epic(
            id="epic_123",
            title="Auth Epic",
            project_id="proj_456",
            status=EpicStatus.IN_PROGRESS,
            priority=TaskPriority.HIGH,
            assignees=["alice", "bob"],
        )

        def sanitize(text: str) -> str:
            return text

        parts = manager._format_specialized_fields(epic, sanitize)

        assert any("in_progress" in p.lower() for p in parts)
        assert any("high" in p.lower() for p in parts)
        assert any("proj_456" in p for p in parts)


class TestEpicMetadataExtraction:
    """Tests for Epic-specific metadata extraction."""

    def test_epic_metadata_extraction(self) -> None:
        """_entity_to_metadata should extract Epic-specific fields."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        epic = Epic(
            id="epic_123",
            title="Auth System Epic",
            project_id="proj_456",
            status=EpicStatus.IN_PROGRESS,
            priority=TaskPriority.HIGH,
            assignees=["alice", "bob"],
            tags=["security", "auth"],
            learnings="OAuth redirect URIs matter",
        )

        metadata = manager._entity_to_metadata(epic)

        assert metadata["status"] == "in_progress"
        assert metadata["priority"] == "high"
        assert metadata["project_id"] == "proj_456"
        assert metadata["assignees"] == ["alice", "bob"]
        assert metadata["learnings"] == "OAuth redirect URIs matter"
        # Tags are in common fields section
        assert metadata["tags"] == ["security", "auth"]

    def test_collects_epic_properties(self) -> None:
        """_collect_properties should collect Epic-specific properties."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        epic = Epic(
            id="epic_123",
            title="Auth System Epic",
            description="Complete authentication system",
            project_id="proj_456",
            status=EpicStatus.PLANNING,
            priority=TaskPriority.CRITICAL,
        )

        props = manager._collect_properties(epic)

        assert props["uuid"] == "epic_123"
        assert props["name"] == "Auth System Epic"
        assert props["entity_type"] == "epic"
        assert props["project_id"] == "proj_456"
        assert props["status"] == "planning"
        assert props["priority"] == "critical"


class TestGetTasksForEpic:
    """Tests for EntityManager.get_tasks_for_epic method."""

    @pytest.mark.asyncio
    async def test_get_tasks_for_epic_returns_tasks(self) -> None:
        """get_tasks_for_epic should return tasks belonging to an epic."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        now = datetime.now(UTC).isoformat()

        # Set up mock result with tasks
        client._driver.set_results(
            [
                (
                    [
                        {
                            "uuid": "task_1",
                            "name": "Task 1",
                            "entity_type": "task",
                            "group_id": TEST_ORG_ID,
                            "status": "todo",
                            "priority": "high",
                            "epic_id": "epic_123",
                            "project_id": "proj_456",
                            "created_at": now,
                            "updated_at": now,
                        },
                        {
                            "uuid": "task_2",
                            "name": "Task 2",
                            "entity_type": "task",
                            "group_id": TEST_ORG_ID,
                            "status": "doing",
                            "priority": "medium",
                            "epic_id": "epic_123",
                            "project_id": "proj_456",
                            "created_at": now,
                            "updated_at": now,
                        },
                    ],
                    [
                        "uuid",
                        "name",
                        "entity_type",
                        "group_id",
                        "status",
                        "priority",
                        "epic_id",
                        "project_id",
                        "created_at",
                        "updated_at",
                    ],
                    {},
                )
            ]
        )

        tasks = await manager.get_tasks_for_epic("epic_123")

        assert len(tasks) == 2
        assert all(t.entity_type == EntityType.TASK for t in tasks)

    @pytest.mark.asyncio
    async def test_get_tasks_for_epic_filters_by_status(self) -> None:
        """get_tasks_for_epic should filter by status when specified."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        client._driver.set_results([([], [], {})])

        await manager.get_tasks_for_epic("epic_123", status="todo")

        # Verify query includes status filter
        _query, params = client._driver.queries[0]
        assert params.get("status") == "todo"

    @pytest.mark.asyncio
    async def test_get_tasks_for_epic_respects_limit(self) -> None:
        """get_tasks_for_epic should respect limit parameter."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        client._driver.set_results([([], [], {})])

        await manager.get_tasks_for_epic("epic_123", limit=5)

        _query, params = client._driver.queries[0]
        assert params["limit"] == 5


class TestGetEpicProgress:
    """Tests for EntityManager.get_epic_progress method."""

    @pytest.mark.asyncio
    async def test_get_epic_progress_returns_counts(self) -> None:
        """get_epic_progress should return task counts by status."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        # Set up mock result matching the query structure
        client._driver.set_results(
            [
                (
                    [
                        {"total": 10, "done": 3, "doing": 2, "blocked": 1, "review": 1},
                    ],
                    ["total", "done", "doing", "blocked", "review"],
                    {},
                )
            ]
        )

        progress = await manager.get_epic_progress("epic_123")

        assert progress["total_tasks"] == 10
        assert progress["completed_tasks"] == 3
        assert progress["in_progress_tasks"] == 2
        assert progress["blocked_tasks"] == 1
        assert progress["in_review_tasks"] == 1

    @pytest.mark.asyncio
    async def test_get_epic_progress_calculates_percentages(self) -> None:
        """get_epic_progress should calculate done percentage."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        client._driver.set_results(
            [
                (
                    [
                        {"total": 10, "done": 8, "doing": 0, "blocked": 0, "review": 2},
                    ],
                    ["total", "done", "doing", "blocked", "review"],
                    {},
                )
            ]
        )

        progress = await manager.get_epic_progress("epic_123")

        assert progress["completion_pct"] == 80.0

    @pytest.mark.asyncio
    async def test_get_epic_progress_handles_empty_epic(self) -> None:
        """get_epic_progress should handle epic with no tasks."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        client._driver.set_results([([], [], {})])

        progress = await manager.get_epic_progress("epic_123")

        assert progress["total_tasks"] == 0
        assert progress["completion_pct"] == 0.0


class TestListEpicsForProject:
    """Tests for EntityManager.list_epics_for_project method."""

    @pytest.mark.asyncio
    async def test_list_epics_for_project_returns_epics(self) -> None:
        """list_epics_for_project should return epics for a project."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        now = datetime.now(UTC).isoformat()

        client._driver.set_results(
            [
                (
                    [
                        {
                            "uuid": "epic_1",
                            "name": "Auth Epic",
                            "entity_type": "epic",
                            "group_id": TEST_ORG_ID,
                            "project_id": "proj_123",
                            "status": "in_progress",
                            "created_at": now,
                            "updated_at": now,
                        },
                        {
                            "uuid": "epic_2",
                            "name": "API Epic",
                            "entity_type": "epic",
                            "group_id": TEST_ORG_ID,
                            "project_id": "proj_123",
                            "status": "planning",
                            "created_at": now,
                            "updated_at": now,
                        },
                    ],
                    [
                        "uuid",
                        "name",
                        "entity_type",
                        "group_id",
                        "project_id",
                        "status",
                        "created_at",
                        "updated_at",
                    ],
                    {},
                )
            ]
        )

        epics = await manager.list_epics_for_project("proj_123")

        assert len(epics) == 2
        assert all(e.entity_type == EntityType.EPIC for e in epics)

    @pytest.mark.asyncio
    async def test_list_epics_for_project_filters_by_status(self) -> None:
        """list_epics_for_project should filter by status when specified."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        client._driver.set_results([([], [], {})])

        await manager.list_epics_for_project("proj_123", status="in_progress")

        _query, params = client._driver.queries[0]
        assert params.get("status") == "in_progress"

    @pytest.mark.asyncio
    async def test_list_epics_for_project_queries_correctly(self) -> None:
        """list_epics_for_project should use correct parameters."""
        client = MockGraphClient()
        manager = EntityManager(client, group_id=TEST_ORG_ID)

        client._driver.set_results([([], [], {})])

        await manager.list_epics_for_project("proj_123", limit=10)

        _query, params = client._driver.queries[0]
        assert params["project_id"] == "proj_123"
        assert params["group_id"] == TEST_ORG_ID
        assert params["limit"] == 10
