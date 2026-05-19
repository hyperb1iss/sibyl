"""Tests for sibyl-core graph/relationships.py RelationshipManager."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sibyl_core.backends.surreal import SurrealDriver
from sibyl_core.errors import GraphError
from sibyl_core.graph.relationships import (
    VALID_RELATIONSHIP_TYPES,
    RelationshipManager,
    _sanitize_pagination,
    _validate_relationship_type,
)
from sibyl_core.graph.surreal.compat.models import EntityEdge, EntityNode, EpisodicEdge
from sibyl_core.models.entities import Relationship, RelationshipType
from tests.conftest import (
    MockRelationshipManager,
    make_relationship,
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
    return client


@pytest.fixture
def mock_graph_client(mock_graphiti_client: MagicMock) -> MagicMock:
    """Create a mock GraphClient wrapper."""
    graph_client = MagicMock()
    graph_client.client = mock_graphiti_client
    graph_client.driver = mock_graphiti_client.driver
    graph_client.get_org_driver = MagicMock(return_value=mock_graphiti_client.driver)
    graph_client.normalize_result = MagicMock(side_effect=lambda x: x[0] if x else [])
    return graph_client


@pytest.fixture
def relationship_manager(mock_graph_client: MagicMock) -> RelationshipManager:
    """Create RelationshipManager with mocked dependencies."""
    return RelationshipManager(mock_graph_client, group_id="test-org-123")


@pytest.fixture
def surreal_relationship_manager() -> RelationshipManager:
    """Create RelationshipManager backed by a Surreal driver clone."""
    driver = SurrealDriver("memory://")
    org_driver = driver.clone("test-org-123")
    graph_client = MagicMock()
    graph_client.client = MagicMock(driver=driver)
    graph_client.driver = driver
    graph_client.get_org_driver = MagicMock(return_value=org_driver)
    graph_client.normalize_result = MagicMock(side_effect=lambda x: x[0] if x else [])
    return RelationshipManager(graph_client, group_id="test-org-123")


@pytest.fixture
def sample_relationship() -> Relationship:
    """Create a sample relationship for testing."""
    return Relationship(
        id="rel-001",
        relationship_type=RelationshipType.RELATED_TO,
        source_id="entity-001",
        target_id="entity-002",
        weight=1.0,
        metadata={},
    )


@pytest.fixture
def sample_entity_edge() -> EntityEdge:
    """Create a sample EntityEdge from Graphiti."""
    return EntityEdge(
        uuid="edge-001",
        group_id="test-org-123",
        source_node_uuid="entity-001",
        target_node_uuid="entity-002",
        name="RELATED_TO",
        fact="RELATED_TO relationship",
        created_at=datetime.now(UTC),
        valid_at=datetime.now(UTC),
        fact_embedding=None,
        episodes=[],
        expired_at=None,
        invalid_at=None,
        attributes={"weight": 1.0},
    )


# =============================================================================
# RelationshipManager Initialization Tests
# =============================================================================


class TestRelationshipManagerInit:
    """Test RelationshipManager initialization and configuration."""

    def test_init_with_valid_group_id(self, mock_graph_client: MagicMock) -> None:
        """RelationshipManager initializes with valid group_id."""
        manager = RelationshipManager(mock_graph_client, group_id="org-123")
        assert manager._group_id == "org-123"
        assert manager._client == mock_graph_client

    def test_init_requires_group_id(self, mock_graph_client: MagicMock) -> None:
        """RelationshipManager requires non-empty group_id."""
        with pytest.raises(ValueError, match="group_id is required"):
            RelationshipManager(mock_graph_client, group_id="")

    def test_init_gets_org_driver_for_org(self, mock_graph_client: MagicMock) -> None:
        """RelationshipManager scopes itself through the graph runtime helper."""
        RelationshipManager(mock_graph_client, group_id="my-org")
        mock_graph_client.get_org_driver.assert_called_once_with("my-org")


# =============================================================================
# Relationship Creation Tests
# =============================================================================


class TestRelationshipCreate:
    """Test relationship creation operations."""

    @pytest.mark.asyncio
    async def test_create_relationship_uses_surreal_edge_ops(
        self,
        surreal_relationship_manager: RelationshipManager,
        sample_relationship: Relationship,
    ) -> None:
        ops = surreal_relationship_manager._driver.entity_edge_ops
        ops.get_between_nodes = AsyncMock(return_value=[])
        ops.save = AsyncMock()

        result = await surreal_relationship_manager.create(sample_relationship)

        assert result == sample_relationship.id
        ops.get_between_nodes.assert_awaited_once_with(
            surreal_relationship_manager._driver,
            sample_relationship.source_id,
            sample_relationship.target_id,
            group_ids=[surreal_relationship_manager._group_id],
            limit=1000,
        )
        ops.save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_create_relationship_routes_episode_sources_directly_to_mentions(
        self,
        surreal_relationship_manager: RelationshipManager,
    ) -> None:
        relationship = Relationship(
            id="rel-episode-task",
            relationship_type=RelationshipType.DERIVED_FROM,
            source_id="episode_task-123",
            target_id="task-123",
        )
        entity_ops = surreal_relationship_manager._driver.entity_edge_ops
        entity_ops.get_between_nodes = AsyncMock(return_value=[])
        entity_ops.save = AsyncMock()
        episodic_ops = surreal_relationship_manager._driver.episodic_edge_ops
        episodic_ops.get_between_nodes = AsyncMock(return_value=[])
        episodic_ops.save = AsyncMock()

        result = await surreal_relationship_manager.create(relationship)

        assert result == relationship.id
        entity_ops.get_between_nodes.assert_not_awaited()
        entity_ops.save.assert_not_awaited()
        episodic_ops.save.assert_awaited_once()
        edge = episodic_ops.save.await_args.args[1]
        assert edge.uuid == "rel-episode-task"
        assert edge.source_node_uuid == "episode_task-123"
        assert edge.target_node_uuid == "task-123"

    @pytest.mark.asyncio
    async def test_surreal_like_driver_routes_episode_sources_to_mentions(self) -> None:
        relationship = Relationship(
            id="rel-episode-task",
            relationship_type=RelationshipType.DERIVED_FROM,
            source_id="episode_task-123",
            target_id="task-123",
        )
        entity_ops = SimpleNamespace(get_between_nodes=AsyncMock(), save=AsyncMock())
        episodic_ops = SimpleNamespace(
            get_between_nodes=AsyncMock(return_value=[]),
            save=AsyncMock(),
        )
        driver = SimpleNamespace(
            entity_edge_ops=entity_ops,
            episodic_edge_ops=episodic_ops,
        )
        graph_client = MagicMock()
        graph_client.get_org_driver.return_value = driver
        manager = RelationshipManager(graph_client, group_id="org-123")

        result = await manager.create(relationship)

        assert result == relationship.id
        entity_ops.get_between_nodes.assert_not_awaited()
        entity_ops.save.assert_not_awaited()
        episodic_ops.save.assert_awaited_once()
        edge = episodic_ops.save.await_args.args[1]
        assert edge.uuid == "rel-episode-task"
        assert edge.source_node_uuid == "episode_task-123"
        assert edge.target_node_uuid == "task-123"

    @pytest.mark.asyncio
    async def test_create_relationship_reuses_existing_episode_mentions(
        self,
        surreal_relationship_manager: RelationshipManager,
    ) -> None:
        relationship = Relationship(
            id="rel-new",
            relationship_type=RelationshipType.RELATED_TO,
            source_id="episode_task-123",
            target_id="task-123",
        )
        entity_ops = surreal_relationship_manager._driver.entity_edge_ops
        entity_ops.save = AsyncMock()
        episodic_ops = surreal_relationship_manager._driver.episodic_edge_ops
        existing = EpisodicEdge(
            uuid="rel-existing",
            group_id=surreal_relationship_manager._group_id,
            source_node_uuid="episode_task-123",
            target_node_uuid="task-123",
            created_at=datetime.now(UTC),
        )
        episodic_ops.get_between_nodes = AsyncMock(return_value=[existing])
        episodic_ops.save = AsyncMock()

        result = await surreal_relationship_manager.create(relationship)

        assert result == "rel-existing"
        entity_ops.save.assert_not_awaited()
        episodic_ops.save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_relationship_refuses_surreal_cypher_fallback(
        self,
        surreal_relationship_manager: RelationshipManager,
        sample_relationship: Relationship,
    ) -> None:
        surreal_relationship_manager._driver.execute_query = AsyncMock()

        with (
            patch.object(
                surreal_relationship_manager, "_surreal_entity_edge_ops", return_value=None
            ),
            pytest.raises(GraphError, match="native edge operations"),
        ):
            await surreal_relationship_manager.create(sample_relationship)

        surreal_relationship_manager._driver.execute_query.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_relationship_success(
        self,
        relationship_manager: RelationshipManager,
        sample_relationship: Relationship,
        mock_driver: MagicMock,
    ) -> None:
        """create() stores relationship and returns ID."""
        with patch.object(
            relationship_manager,
            "_get_existing_edges_between_nodes",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await relationship_manager.create(sample_relationship)

            assert result is not None
            assert isinstance(result, str)
            mock_driver.execute_query.assert_called()

    @pytest.mark.asyncio
    async def test_create_relationship_with_all_types(
        self,
        relationship_manager: RelationshipManager,
        mock_driver: MagicMock,
    ) -> None:
        """create() works with all valid relationship types."""
        # Test several relationship types to ensure the whitelist works
        test_types = [
            RelationshipType.RELATED_TO,
            RelationshipType.DEPENDS_ON,
            RelationshipType.BELONGS_TO,
            RelationshipType.BLOCKS,
        ]

        for rel_type in test_types:
            relationship = Relationship(
                id=f"rel-{rel_type.value}",
                relationship_type=rel_type,
                source_id="entity-001",
                target_id="entity-002",
                weight=1.0,
            )

            with patch.object(
                relationship_manager,
                "_get_existing_edges_between_nodes",
                new_callable=AsyncMock,
                return_value=[],
            ):
                result = await relationship_manager.create(relationship)
                assert result is not None

    @pytest.mark.asyncio
    async def test_create_skips_duplicate_relationship(
        self,
        relationship_manager: RelationshipManager,
        sample_relationship: Relationship,
        sample_entity_edge: EntityEdge,
        mock_driver: MagicMock,
    ) -> None:
        """create() returns existing ID if duplicate relationship exists."""
        with patch.object(
            relationship_manager,
            "_get_existing_edges_between_nodes",
            new_callable=AsyncMock,
            return_value=[sample_entity_edge],
        ):
            result = await relationship_manager.create(sample_relationship)

            # Should return existing edge UUID without creating new one
            assert result == sample_entity_edge.uuid
            # Should not have written anything
            mock_driver.execute_query.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_allows_different_type_between_same_nodes(
        self,
        relationship_manager: RelationshipManager,
        sample_entity_edge: EntityEdge,
        mock_driver: MagicMock,
    ) -> None:
        """create() allows different relationship types between same entities."""
        # Existing edge is RELATED_TO
        existing_edge = sample_entity_edge

        # New relationship is DEPENDS_ON (different type)
        new_relationship = Relationship(
            id="rel-new",
            relationship_type=RelationshipType.DEPENDS_ON,
            source_id="entity-001",
            target_id="entity-002",
            weight=1.0,
        )

        with patch.object(
            relationship_manager,
            "_get_existing_edges_between_nodes",
            new_callable=AsyncMock,
            return_value=[existing_edge],
        ):
            result = await relationship_manager.create(new_relationship)

            # Should create new relationship (different type)
            assert result != existing_edge.uuid
            mock_driver.execute_query.assert_called()

    @pytest.mark.asyncio
    async def test_create_with_metadata(
        self,
        relationship_manager: RelationshipManager,
        mock_driver: MagicMock,
    ) -> None:
        """create() preserves relationship metadata."""
        relationship = Relationship(
            id="rel-meta",
            relationship_type=RelationshipType.RELATED_TO,
            source_id="entity-001",
            target_id="entity-002",
            weight=0.8,
            metadata={"reason": "semantic similarity", "confidence": 0.95},
        )

        with patch.object(
            relationship_manager,
            "_get_existing_edges_between_nodes",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await relationship_manager.create(relationship)
            assert result is not None

    @pytest.mark.asyncio
    async def test_create_failure_raises_error(
        self,
        relationship_manager: RelationshipManager,
        sample_relationship: Relationship,
        mock_driver: MagicMock,
    ) -> None:
        """create() raises GraphError on failure."""
        with (
            patch.object(
                relationship_manager,
                "_get_existing_edges_between_nodes",
                new_callable=AsyncMock,
                side_effect=Exception("DB connection failed"),
            ),
            pytest.raises(GraphError, match="Failed to create relationship"),
        ):
            await relationship_manager.create(sample_relationship)


# =============================================================================
# Bulk Creation Tests
# =============================================================================


class TestRelationshipBulkCreate:
    """Test bulk relationship creation."""

    @pytest.mark.asyncio
    async def test_bulk_create_uses_surreal_edge_ops_and_dedupes(
        self,
        surreal_relationship_manager: RelationshipManager,
    ) -> None:
        ops = surreal_relationship_manager._driver.entity_edge_ops
        ops.get_between_nodes = AsyncMock(return_value=[])
        ops.save_bulk = AsyncMock()
        relationships = [
            Relationship(
                id="rel-1",
                relationship_type=RelationshipType.RELATED_TO,
                source_id="entity-1",
                target_id="entity-2",
                weight=1.0,
            ),
            Relationship(
                id="rel-2",
                relationship_type=RelationshipType.RELATED_TO,
                source_id="entity-1",
                target_id="entity-2",
                weight=1.0,
            ),
            Relationship(
                id="rel-3",
                relationship_type=RelationshipType.DEPENDS_ON,
                source_id="entity-2",
                target_id="entity-3",
                weight=1.0,
            ),
        ]

        created, failed = await surreal_relationship_manager.create_bulk(relationships)

        assert created == 2
        assert failed == 0
        ops.save_bulk.assert_awaited_once()
        saved_edges = ops.save_bulk.await_args.args[1]
        assert len(saved_edges) == 2
        assert {
            (edge.source_node_uuid, edge.name, edge.target_node_uuid) for edge in saved_edges
        } == {
            ("entity-1", "RELATED_TO", "entity-2"),
            ("entity-2", "DEPENDS_ON", "entity-3"),
        }
        assert ops.get_between_nodes.await_count == 2

    @pytest.mark.asyncio
    async def test_bulk_create_is_idempotent_against_existing_surreal_edges(
        self,
        surreal_relationship_manager: RelationshipManager,
        sample_entity_edge: EntityEdge,
    ) -> None:
        ops = surreal_relationship_manager._driver.entity_edge_ops
        ops.get_between_nodes = AsyncMock(side_effect=[[sample_entity_edge], []])
        ops.save_bulk = AsyncMock()
        relationships = [
            Relationship(
                id="rel-existing",
                relationship_type=RelationshipType.RELATED_TO,
                source_id="entity-001",
                target_id="entity-002",
                weight=1.0,
            ),
            Relationship(
                id="rel-new",
                relationship_type=RelationshipType.BELONGS_TO,
                source_id="entity-003",
                target_id="entity-004",
                weight=1.0,
            ),
        ]

        created, failed = await surreal_relationship_manager.create_bulk(relationships)

        assert created == 2
        assert failed == 0
        first_call = ops.get_between_nodes.await_args_list[0]
        assert first_call.args == (
            surreal_relationship_manager._driver,
            "entity-001",
            "entity-002",
        )
        assert first_call.kwargs == {
            "group_ids": [surreal_relationship_manager._group_id],
            "limit": 1000,
        }
        ops.save_bulk.assert_awaited_once()
        saved_edges = ops.save_bulk.await_args.args[1]
        assert [
            (edge.source_node_uuid, edge.name, edge.target_node_uuid) for edge in saved_edges
        ] == [("entity-003", "BELONGS_TO", "entity-004")]

    @pytest.mark.asyncio
    async def test_bulk_create_does_not_duplicate_existing_surreal_triplets_live(
        self,
        surreal_relationship_manager: RelationshipManager,
    ) -> None:
        driver = surreal_relationship_manager._driver
        await driver.build_indices_and_constraints()
        for uuid in ("entity-001", "entity-002", "entity-003", "entity-004"):
            await driver.entity_node_ops.save(
                driver,
                EntityNode(
                    uuid=uuid,
                    name=uuid,
                    group_id=surreal_relationship_manager._group_id,
                    labels=["Entity"],
                    summary="",
                    attributes={},
                    name_embedding=None,
                    created_at=datetime.now(UTC),
                ),
            )

        await surreal_relationship_manager.create(
            Relationship(
                id="rel-existing",
                relationship_type=RelationshipType.RELATED_TO,
                source_id="entity-001",
                target_id="entity-002",
                weight=1.0,
            )
        )

        created, failed = await surreal_relationship_manager.create_bulk(
            [
                Relationship(
                    id="rel-duplicate",
                    relationship_type=RelationshipType.RELATED_TO,
                    source_id="entity-001",
                    target_id="entity-002",
                    weight=1.0,
                ),
                Relationship(
                    id="rel-new",
                    relationship_type=RelationshipType.BELONGS_TO,
                    source_id="entity-003",
                    target_id="entity-004",
                    weight=1.0,
                ),
            ]
        )

        rows = await driver.execute_query(
            "SELECT uuid, name, in.uuid AS source, out.uuid AS target FROM relates_to ORDER BY uuid;"
        )

        assert created == 2
        assert failed == 0
        assert [(row["uuid"], row["name"], row["source"], row["target"]) for row in rows] == [
            ("rel-existing", "RELATED_TO", "entity-001", "entity-002"),
            ("rel-new", "BELONGS_TO", "entity-003", "entity-004"),
        ]

    @pytest.mark.asyncio
    async def test_bulk_create_success(
        self,
        relationship_manager: RelationshipManager,
        mock_driver: MagicMock,
    ) -> None:
        """create_bulk() creates multiple relationships."""
        relationships = [
            Relationship(
                id=f"rel-{i}",
                relationship_type=RelationshipType.RELATED_TO,
                source_id=f"entity-{i}",
                target_id=f"entity-{i + 1}",
                weight=1.0,
            )
            for i in range(5)
        ]

        mock_driver.execute_query.return_value = ([{"processed": 5}], None, None)

        created, failed = await relationship_manager.create_bulk(relationships)

        assert created == 5
        assert failed == 0
        query = mock_driver.execute_query.await_args.args[0]
        assert "UNWIND $relationships AS rel" in query

    @pytest.mark.asyncio
    async def test_bulk_create_partial_failure(
        self,
        relationship_manager: RelationshipManager,
        mock_driver: MagicMock,
    ) -> None:
        """create_bulk() tracks partial failures."""
        relationships = [
            Relationship(
                id=f"rel-{i}",
                relationship_type=RelationshipType.RELATED_TO,
                source_id=f"entity-{i}",
                target_id=f"entity-{i + 1}",
                weight=1.0,
            )
            for i in range(3)
        ]

        mock_driver.execute_query.side_effect = Exception("Batch failed")
        with patch.object(
            relationship_manager,
            "create",
            new_callable=AsyncMock,
            side_effect=["rel-0", Exception("Random failure"), "rel-2"],
        ):
            created, failed = await relationship_manager.create_bulk(relationships)

        assert created == 2
        assert failed == 1
        assert mock_driver.execute_query.await_count == 1


# =============================================================================
# Relationship Retrieval Tests (get_for_entity)
# =============================================================================


class TestGetForEntity:
    """Test retrieving relationships for an entity."""

    @pytest.mark.asyncio
    async def test_get_for_entity_uses_surreal_edge_ops(
        self,
        surreal_relationship_manager: RelationshipManager,
        sample_entity_edge: EntityEdge,
    ) -> None:
        ops = surreal_relationship_manager._driver.entity_edge_ops
        ops.get_by_node_uuid = AsyncMock(return_value=[sample_entity_edge])

        results = await surreal_relationship_manager.get_for_entity(
            "entity-001", direction="outgoing"
        )

        assert len(results) == 1
        assert results[0].source_id == "entity-001"
        ops.get_by_node_uuid.assert_awaited_once_with(
            surreal_relationship_manager._driver,
            "entity-001",
            group_ids=[surreal_relationship_manager._group_id],
            limit=1000,
        )

    @pytest.mark.asyncio
    async def test_get_for_entity_includes_surreal_episode_mentions(
        self,
        surreal_relationship_manager: RelationshipManager,
    ) -> None:
        entity_ops = surreal_relationship_manager._driver.entity_edge_ops
        entity_ops.get_by_node_uuid = AsyncMock(return_value=[])
        episodic_ops = surreal_relationship_manager._driver.episodic_edge_ops
        episodic_ops.get_by_node_uuid = AsyncMock(
            return_value=[
                EpisodicEdge(
                    uuid="rel-episode-task",
                    group_id=surreal_relationship_manager._group_id,
                    source_node_uuid="episode_task-123",
                    target_node_uuid="task-123",
                    created_at=datetime.now(UTC),
                )
            ]
        )

        results = await surreal_relationship_manager.get_for_entity(
            "episode_task-123",
            direction="outgoing",
        )

        assert len(results) == 1
        assert results[0].id == "rel-episode-task"
        assert results[0].relationship_type == RelationshipType.RELATED_TO
        assert results[0].source_id == "episode_task-123"
        assert results[0].target_id == "task-123"
        episodic_ops.get_by_node_uuid.assert_awaited_once_with(
            surreal_relationship_manager._driver,
            "episode_task-123",
            group_ids=[surreal_relationship_manager._group_id],
            limit=1000,
        )

    @pytest.mark.asyncio
    async def test_get_for_entity_refuses_surreal_cypher_fallback(
        self,
        surreal_relationship_manager: RelationshipManager,
    ) -> None:
        surreal_relationship_manager._driver.execute_query = AsyncMock()

        with (
            patch.object(
                surreal_relationship_manager, "_surreal_entity_edge_ops", return_value=None
            ),
            pytest.raises(RuntimeError, match="native edge operations"),
        ):
            await surreal_relationship_manager.get_for_entity("entity-001")

        surreal_relationship_manager._driver.execute_query.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_get_for_entity_outgoing(
        self,
        relationship_manager: RelationshipManager,
        mock_driver: MagicMock,
        mock_graph_client: MagicMock,
    ) -> None:
        """get_for_entity() returns outgoing relationships."""
        mock_driver.execute_query.return_value = (
            [
                {
                    "uuid": "rel-001",
                    "name": "DEPENDS_ON",
                    "source_id": "entity-001",
                    "target_id": "entity-002",
                    "weight": 1.0,
                }
            ],
            None,
            None,
        )
        mock_graph_client.normalize_result.return_value = [
            {
                "uuid": "rel-001",
                "name": "DEPENDS_ON",
                "source_id": "entity-001",
                "target_id": "entity-002",
                "weight": 1.0,
            }
        ]

        results = await relationship_manager.get_for_entity("entity-001", direction="outgoing")

        assert len(results) == 1
        assert results[0].source_id == "entity-001"
        assert results[0].relationship_type == RelationshipType.DEPENDS_ON

    @pytest.mark.asyncio
    async def test_get_for_entity_incoming(
        self,
        relationship_manager: RelationshipManager,
        mock_driver: MagicMock,
        mock_graph_client: MagicMock,
    ) -> None:
        """get_for_entity() returns incoming relationships."""
        mock_driver.execute_query.return_value = (
            [
                {
                    "uuid": "rel-001",
                    "name": "BLOCKS",
                    "source_id": "entity-002",
                    "target_id": "entity-001",
                    "weight": 1.0,
                }
            ],
            None,
            None,
        )
        mock_graph_client.normalize_result.return_value = [
            {
                "uuid": "rel-001",
                "name": "BLOCKS",
                "source_id": "entity-002",
                "target_id": "entity-001",
                "weight": 1.0,
            }
        ]

        results = await relationship_manager.get_for_entity("entity-001", direction="incoming")

        assert len(results) == 1
        assert results[0].target_id == "entity-001"

    @pytest.mark.asyncio
    async def test_get_for_entity_both_directions(
        self,
        relationship_manager: RelationshipManager,
        mock_driver: MagicMock,
        mock_graph_client: MagicMock,
    ) -> None:
        """get_for_entity() returns both incoming and outgoing by default."""
        mock_driver.execute_query.return_value = (
            [
                {
                    "uuid": "rel-001",
                    "name": "DEPENDS_ON",
                    "source_id": "entity-001",
                    "target_id": "entity-002",
                    "weight": 1.0,
                },
                {
                    "uuid": "rel-002",
                    "name": "BLOCKS",
                    "source_id": "entity-003",
                    "target_id": "entity-001",
                    "weight": 1.0,
                },
            ],
            None,
            None,
        )
        mock_graph_client.normalize_result.return_value = [
            {
                "uuid": "rel-001",
                "name": "DEPENDS_ON",
                "source_id": "entity-001",
                "target_id": "entity-002",
                "weight": 1.0,
            },
            {
                "uuid": "rel-002",
                "name": "BLOCKS",
                "source_id": "entity-003",
                "target_id": "entity-001",
                "weight": 1.0,
            },
        ]

        results = await relationship_manager.get_for_entity("entity-001", direction="both")

        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_get_for_entity_filters_by_type(
        self,
        relationship_manager: RelationshipManager,
        mock_driver: MagicMock,
        mock_graph_client: MagicMock,
    ) -> None:
        """get_for_entity() filters by relationship type."""
        mock_driver.execute_query.return_value = (
            [
                {
                    "uuid": "rel-001",
                    "name": "DEPENDS_ON",
                    "source_id": "entity-001",
                    "target_id": "entity-002",
                    "weight": 1.0,
                },
                {
                    "uuid": "rel-002",
                    "name": "RELATED_TO",
                    "source_id": "entity-001",
                    "target_id": "entity-003",
                    "weight": 1.0,
                },
            ],
            None,
            None,
        )
        mock_graph_client.normalize_result.return_value = [
            {
                "uuid": "rel-001",
                "name": "DEPENDS_ON",
                "source_id": "entity-001",
                "target_id": "entity-002",
                "weight": 1.0,
            },
            {
                "uuid": "rel-002",
                "name": "RELATED_TO",
                "source_id": "entity-001",
                "target_id": "entity-003",
                "weight": 1.0,
            },
        ]

        results = await relationship_manager.get_for_entity(
            "entity-001",
            relationship_types=[RelationshipType.DEPENDS_ON],
        )

        assert len(results) == 1
        assert results[0].relationship_type == RelationshipType.DEPENDS_ON

    @pytest.mark.asyncio
    async def test_get_for_entity_preserves_metadata(
        self,
        relationship_manager: RelationshipManager,
        mock_driver: MagicMock,
        mock_graph_client: MagicMock,
    ) -> None:
        """get_for_entity() keeps custom edge metadata."""
        mock_driver.execute_query.return_value = (
            [
                {
                    "uuid": "rel-001",
                    "name": "RELATED_TO",
                    "source_id": "entity-001",
                    "target_id": "entity-002",
                    "weight": 0.7,
                    "properties": {
                        "uuid": "rel-001",
                        "group_id": "org-123",
                        "weight": 0.7,
                        "reason": "semantic similarity",
                        "confidence": 0.95,
                    },
                }
            ],
            None,
            None,
        )
        mock_graph_client.normalize_result.return_value = [
            {
                "uuid": "rel-001",
                "name": "RELATED_TO",
                "source_id": "entity-001",
                "target_id": "entity-002",
                "weight": 0.7,
                "properties": {
                    "uuid": "rel-001",
                    "group_id": "org-123",
                    "weight": 0.7,
                    "reason": "semantic similarity",
                    "confidence": 0.95,
                },
            }
        ]

        results = await relationship_manager.get_for_entity("entity-001", direction="outgoing")

        assert len(results) == 1
        assert results[0].metadata == {
            "reason": "semantic similarity",
            "confidence": 0.95,
        }

    @pytest.mark.asyncio
    async def test_get_for_entity_handles_empty_results(
        self,
        relationship_manager: RelationshipManager,
        mock_driver: MagicMock,
        mock_graph_client: MagicMock,
    ) -> None:
        """get_for_entity() returns empty list when no relationships."""
        mock_driver.execute_query.return_value = ([], None, None)
        mock_graph_client.normalize_result.return_value = []

        results = await relationship_manager.get_for_entity("entity-orphan")

        assert results == []

    @pytest.mark.asyncio
    async def test_get_for_entity_handles_error(
        self,
        relationship_manager: RelationshipManager,
        mock_driver: MagicMock,
    ) -> None:
        """get_for_entity() returns empty list on error (graceful degradation)."""
        mock_driver.execute_query.side_effect = Exception("DB error")

        results = await relationship_manager.get_for_entity("entity-001")

        # Should return empty list, not raise
        assert results == []

    @pytest.mark.asyncio
    async def test_get_for_entity_handles_unknown_type(
        self,
        relationship_manager: RelationshipManager,
        mock_driver: MagicMock,
        mock_graph_client: MagicMock,
    ) -> None:
        """get_for_entity() defaults unknown types to RELATED_TO."""
        mock_driver.execute_query.return_value = (
            [
                {
                    "uuid": "rel-001",
                    "name": "UNKNOWN_TYPE",
                    "source_id": "entity-001",
                    "target_id": "entity-002",
                    "weight": 1.0,
                }
            ],
            None,
            None,
        )
        mock_graph_client.normalize_result.return_value = [
            {
                "uuid": "rel-001",
                "name": "UNKNOWN_TYPE",
                "source_id": "entity-001",
                "target_id": "entity-002",
                "weight": 1.0,
            }
        ]

        results = await relationship_manager.get_for_entity("entity-001")

        assert len(results) == 1
        assert results[0].relationship_type == RelationshipType.RELATED_TO

    @pytest.mark.asyncio
    async def test_get_for_entity_skips_invalid_records(
        self,
        relationship_manager: RelationshipManager,
        mock_driver: MagicMock,
        mock_graph_client: MagicMock,
    ) -> None:
        """get_for_entity() skips records with missing source/target."""
        mock_driver.execute_query.return_value = (
            [
                {
                    "uuid": "rel-001",
                    "name": "RELATED_TO",
                    "source_id": "entity-001",
                    "target_id": "entity-002",
                    "weight": 1.0,
                },
                {
                    "uuid": "rel-002",
                    "name": "RELATED_TO",
                    "source_id": None,  # Invalid - missing source
                    "target_id": "entity-003",
                    "weight": 1.0,
                },
                {
                    "uuid": "rel-003",
                    "name": "RELATED_TO",
                    "source_id": "entity-001",
                    "target_id": None,  # Invalid - missing target
                    "weight": 1.0,
                },
            ],
            None,
            None,
        )
        mock_graph_client.normalize_result.return_value = [
            {
                "uuid": "rel-001",
                "name": "RELATED_TO",
                "source_id": "entity-001",
                "target_id": "entity-002",
                "weight": 1.0,
            },
            {
                "uuid": "rel-002",
                "name": "RELATED_TO",
                "source_id": None,
                "target_id": "entity-003",
                "weight": 1.0,
            },
            {
                "uuid": "rel-003",
                "name": "RELATED_TO",
                "source_id": "entity-001",
                "target_id": None,
                "weight": 1.0,
            },
        ]

        results = await relationship_manager.get_for_entity("entity-001")

        # Only the valid record should be returned
        assert len(results) == 1
        assert results[0].id == "rel-001"


# =============================================================================
# Relationship Deletion Tests
# =============================================================================


class TestRelationshipDelete:
    """Test relationship deletion operations."""

    @pytest.mark.asyncio
    async def test_delete_uses_surreal_edge_ops(
        self,
        surreal_relationship_manager: RelationshipManager,
        sample_entity_edge: EntityEdge,
    ) -> None:
        ops = surreal_relationship_manager._driver.entity_edge_ops
        ops.get_by_uuid = AsyncMock(return_value=sample_entity_edge)
        ops.delete = AsyncMock()

        result = await surreal_relationship_manager.delete(sample_entity_edge.uuid)

        assert result is True
        ops.get_by_uuid.assert_awaited_once_with(
            surreal_relationship_manager._driver,
            sample_entity_edge.uuid,
        )
        ops.delete.assert_awaited_once_with(
            surreal_relationship_manager._driver,
            sample_entity_edge,
        )

    @pytest.mark.asyncio
    async def test_delete_success(
        self,
        relationship_manager: RelationshipManager,
        mock_driver: MagicMock,
        mock_graph_client: MagicMock,
    ) -> None:
        """delete() removes relationship by ID."""
        mock_driver.execute_query.return_value = ([{"deleted": 1}], None, None)
        mock_graph_client.normalize_result.return_value = [{"deleted": 1}]

        result = await relationship_manager.delete("rel-001")

        assert result is True
        mock_driver.execute_query.assert_called()

    @pytest.mark.asyncio
    async def test_delete_not_found(
        self,
        relationship_manager: RelationshipManager,
        mock_driver: MagicMock,
        mock_graph_client: MagicMock,
    ) -> None:
        """delete() returns False when relationship not found."""
        mock_driver.execute_query.return_value = ([{"deleted": 0}], None, None)
        mock_graph_client.normalize_result.return_value = [{"deleted": 0}]

        result = await relationship_manager.delete("nonexistent-rel")

        assert result is False

    @pytest.mark.asyncio
    async def test_delete_failure_raises_error(
        self,
        relationship_manager: RelationshipManager,
        mock_driver: MagicMock,
    ) -> None:
        """delete() raises GraphError on failure."""
        mock_driver.execute_query.side_effect = Exception("DB error")

        with pytest.raises(GraphError, match="Failed to delete relationship"):
            await relationship_manager.delete("rel-001")


class TestDeleteForEntity:
    """Test deleting all relationships for an entity."""

    @pytest.mark.asyncio
    async def test_delete_for_entity_success(
        self,
        relationship_manager: RelationshipManager,
        mock_driver: MagicMock,
        mock_graph_client: MagicMock,
    ) -> None:
        """delete_for_entity() removes all relationships for entity."""
        mock_driver.execute_query.return_value = ([{"deleted": 5}], None, None)
        mock_graph_client.normalize_result.return_value = [{"deleted": 5}]

        result = await relationship_manager.delete_for_entity("entity-001")

        assert result == 5

    @pytest.mark.asyncio
    async def test_delete_for_entity_none_found(
        self,
        relationship_manager: RelationshipManager,
        mock_driver: MagicMock,
        mock_graph_client: MagicMock,
    ) -> None:
        """delete_for_entity() returns 0 when no relationships exist."""
        mock_driver.execute_query.return_value = ([{"deleted": 0}], None, None)
        mock_graph_client.normalize_result.return_value = [{"deleted": 0}]

        result = await relationship_manager.delete_for_entity("orphan-entity")

        assert result == 0

    @pytest.mark.asyncio
    async def test_delete_for_entity_handles_error(
        self,
        relationship_manager: RelationshipManager,
        mock_driver: MagicMock,
    ) -> None:
        """delete_for_entity() returns 0 on error (graceful degradation)."""
        mock_driver.execute_query.side_effect = Exception("DB error")

        result = await relationship_manager.delete_for_entity("entity-001")

        # Should return 0, not raise
        assert result == 0

    @pytest.mark.asyncio
    async def test_delete_for_entity_refuses_surreal_cypher_fallback(
        self,
        surreal_relationship_manager: RelationshipManager,
    ) -> None:
        surreal_relationship_manager._driver.execute_query = AsyncMock()

        with (
            patch.object(
                surreal_relationship_manager, "_surreal_entity_edge_ops", return_value=None
            ),
            pytest.raises(RuntimeError, match="native edge operations"),
        ):
            await surreal_relationship_manager.delete_for_entity("entity-001")

        surreal_relationship_manager._driver.execute_query.assert_not_awaited()


# =============================================================================
# List All Relationships Tests
# =============================================================================


class TestListAll:
    """Test listing all relationships."""

    @pytest.mark.asyncio
    async def test_list_all_uses_surreal_edge_ops(
        self,
        surreal_relationship_manager: RelationshipManager,
        sample_entity_edge: EntityEdge,
    ) -> None:
        ops = surreal_relationship_manager._driver.entity_edge_ops
        ops.get_by_group_ids = AsyncMock(return_value=[sample_entity_edge])

        results = await surreal_relationship_manager.list_all()

        assert len(results) == 1
        ops.get_by_group_ids.assert_awaited_once_with(
            surreal_relationship_manager._driver,
            [surreal_relationship_manager._group_id],
            limit=100,
            offset=0,
        )

    @pytest.mark.asyncio
    async def test_list_all_uses_surreal_edge_ops_with_offset(
        self,
        surreal_relationship_manager: RelationshipManager,
        sample_entity_edge: EntityEdge,
    ) -> None:
        ops = surreal_relationship_manager._driver.entity_edge_ops
        ops.get_by_group_ids = AsyncMock(return_value=[sample_entity_edge])

        results = await surreal_relationship_manager.list_all(limit=5, offset=10)

        assert len(results) == 1
        ops.get_by_group_ids.assert_awaited_once_with(
            surreal_relationship_manager._driver,
            [surreal_relationship_manager._group_id],
            limit=5,
            offset=10,
        )

    @pytest.mark.asyncio
    async def test_list_all_surreal_type_filter_pages_until_enough_matches(
        self,
        surreal_relationship_manager: RelationshipManager,
        sample_entity_edge: EntityEdge,
    ) -> None:
        skipped_edge = sample_entity_edge.model_copy(
            update={"uuid": "edge-200", "name": RelationshipType.BLOCKS.value}
        )
        matching_edge = sample_entity_edge.model_copy(
            update={"uuid": "edge-199", "name": RelationshipType.DEPENDS_ON.value}
        )
        first_page = [skipped_edge, matching_edge]
        first_page.extend(
            sample_entity_edge.model_copy(
                update={"uuid": f"edge-{198 - index:03d}", "name": RelationshipType.BLOCKS.value}
            )
            for index in range(98)
        )
        second_matching_edge = sample_entity_edge.model_copy(
            update={"uuid": "edge-001", "name": RelationshipType.DEPENDS_ON.value}
        )
        ops = surreal_relationship_manager._driver.entity_edge_ops
        ops.get_by_group_ids = AsyncMock(
            side_effect=[
                first_page,
                [second_matching_edge],
            ]
        )

        results = await surreal_relationship_manager.list_all(
            relationship_types=[RelationshipType.DEPENDS_ON],
            limit=1,
            offset=1,
        )

        assert [relationship.id for relationship in results] == ["edge-001"]
        assert ops.get_by_group_ids.await_args_list[0].kwargs == {
            "limit": 100,
            "uuid_cursor": None,
        }
        assert ops.get_by_group_ids.await_args_list[1].kwargs == {
            "limit": 100,
            "uuid_cursor": "edge-101",
        }

    @pytest.mark.asyncio
    async def test_list_all_refuses_surreal_cypher_fallback(
        self,
        surreal_relationship_manager: RelationshipManager,
    ) -> None:
        surreal_relationship_manager._driver.execute_query = AsyncMock()

        with (
            patch.object(
                surreal_relationship_manager, "_surreal_entity_edge_ops", return_value=None
            ),
            pytest.raises(RuntimeError, match="native edge operations"),
        ):
            await surreal_relationship_manager.list_all()

        surreal_relationship_manager._driver.execute_query.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_list_all_basic(
        self,
        relationship_manager: RelationshipManager,
        mock_driver: MagicMock,
    ) -> None:
        """list_all() returns all relationships."""
        mock_driver.execute_query.return_value = (
            [
                {
                    "id": "rel-001",
                    "source_id": "entity-001",
                    "target_id": "entity-002",
                    "rel_type": "RELATED_TO",
                    "created_at": datetime.now(UTC).isoformat(),
                },
                {
                    "id": "rel-002",
                    "source_id": "entity-002",
                    "target_id": "entity-003",
                    "rel_type": "DEPENDS_ON",
                    "created_at": datetime.now(UTC).isoformat(),
                },
            ],
            None,
            None,
        )

        # Patch the static method on the GraphClient class
        with patch(
            "sibyl_core.graph.relationships.GraphClient.normalize_result",
            return_value=[
                {
                    "id": "rel-001",
                    "source_id": "entity-001",
                    "target_id": "entity-002",
                    "rel_type": "RELATED_TO",
                },
                {
                    "id": "rel-002",
                    "source_id": "entity-002",
                    "target_id": "entity-003",
                    "rel_type": "DEPENDS_ON",
                },
            ],
        ):
            results = await relationship_manager.list_all()

            assert len(results) == 2

    @pytest.mark.asyncio
    async def test_list_all_with_type_filter(
        self,
        relationship_manager: RelationshipManager,
        mock_driver: MagicMock,
    ) -> None:
        """list_all() filters by relationship type."""
        mock_driver.execute_query.return_value = (
            [
                {
                    "id": "rel-001",
                    "source_id": "entity-001",
                    "target_id": "entity-002",
                    "rel_type": "DEPENDS_ON",
                }
            ],
            None,
            None,
        )

        with patch(
            "sibyl_core.graph.relationships.GraphClient.normalize_result",
            return_value=[
                {
                    "id": "rel-001",
                    "source_id": "entity-001",
                    "target_id": "entity-002",
                    "rel_type": "DEPENDS_ON",
                }
            ],
        ):
            await relationship_manager.list_all(relationship_types=[RelationshipType.DEPENDS_ON])

            # Verify query includes type filter
            call_args = mock_driver.execute_query.call_args
            query = call_args[0][0] if call_args[0] else ""
            assert "DEPENDS_ON" in query

    @pytest.mark.asyncio
    async def test_list_all_pagination(
        self,
        relationship_manager: RelationshipManager,
        mock_driver: MagicMock,
    ) -> None:
        """list_all() respects limit and offset."""
        mock_driver.execute_query.return_value = (
            [
                {
                    "id": f"rel-{i:03d}",
                    "source_id": f"entity-{i}",
                    "target_id": f"entity-{i + 1}",
                    "rel_type": "RELATED_TO",
                }
                for i in range(5)
            ],
            None,
            None,
        )

        with patch(
            "sibyl_core.graph.relationships.GraphClient.normalize_result",
            return_value=[
                {
                    "id": f"rel-{i:03d}",
                    "source_id": f"entity-{i}",
                    "target_id": f"entity-{i + 1}",
                    "rel_type": "RELATED_TO",
                }
                for i in range(5)
            ],
        ):
            await relationship_manager.list_all(limit=5, offset=10)

            # Verify query includes pagination
            call_args = mock_driver.execute_query.call_args
            query = call_args[0][0] if call_args[0] else ""
            assert "SKIP 10" in query
            assert "LIMIT 5" in query

    @pytest.mark.asyncio
    async def test_list_all_handles_error(
        self,
        relationship_manager: RelationshipManager,
        mock_driver: MagicMock,
    ) -> None:
        """list_all() returns empty list on error."""
        mock_driver.execute_query.side_effect = Exception("DB error")

        results = await relationship_manager.list_all()

        assert results == []


# =============================================================================
# Related Entities Tests
# =============================================================================


class TestGetRelatedEntities:
    """Test retrieving related entities."""

    @pytest.mark.asyncio
    async def test_get_related_entities_empty(
        self,
        relationship_manager: RelationshipManager,
        mock_driver: MagicMock,
        mock_graph_client: MagicMock,
    ) -> None:
        """get_related_entities() returns empty list when no relationships."""
        mock_driver.execute_query.return_value = ([], None, None)
        mock_graph_client.normalize_result.return_value = []

        results = await relationship_manager.get_related_entities("orphan-entity")

        assert results == []

    @pytest.mark.asyncio
    async def test_get_related_entities_respects_limit(
        self,
        relationship_manager: RelationshipManager,
        mock_driver: MagicMock,
        mock_graph_client: MagicMock,
    ) -> None:
        """get_related_entities() respects limit parameter."""
        # Create many relationships
        mock_driver.execute_query.return_value = (
            [
                {
                    "uuid": f"rel-{i:03d}",
                    "name": "RELATED_TO",
                    "source_id": "entity-001",
                    "target_id": f"entity-{i:03d}",
                    "weight": 1.0,
                }
                for i in range(100)
            ],
            None,
            None,
        )
        mock_graph_client.normalize_result.return_value = [
            {
                "uuid": f"rel-{i:03d}",
                "name": "RELATED_TO",
                "source_id": "entity-001",
                "target_id": f"entity-{i:03d}",
                "weight": 1.0,
            }
            for i in range(100)
        ]

        # The method internally limits before fetching entities
        results = await relationship_manager.get_related_entities("entity-001", limit=5)

        # Result should be limited (or empty if entity batch fetch returns empty)
        assert len(results) <= 5

    @pytest.mark.asyncio
    async def test_get_related_entities_batch_uses_surreal_batch_ops(
        self,
        surreal_relationship_manager: RelationshipManager,
        sample_entity_edge: EntityEdge,
    ) -> None:
        """get_related_entities_batch() collapses Surreal edge and node reads."""
        edge_ops = surreal_relationship_manager._driver.entity_edge_ops
        node_ops = surreal_relationship_manager._driver.entity_node_ops
        incoming = EntityEdge(
            uuid="edge-002",
            group_id="test-org-123",
            source_node_uuid="entity-004",
            target_node_uuid="entity-003",
            name="RELATED_TO",
            fact="RELATED_TO relationship",
            created_at=datetime.now(UTC),
            valid_at=datetime.now(UTC),
            fact_embedding=None,
            episodes=[],
            expired_at=None,
            invalid_at=None,
            attributes={"weight": 1.0},
        )
        edge_ops.get_by_node_uuids = AsyncMock(return_value=[sample_entity_edge, incoming])
        node_ops.get_by_uuids = AsyncMock(
            return_value=[
                EntityNode(
                    uuid="entity-002",
                    name="Entity Two",
                    group_id="test-org-123",
                    summary="",
                    labels=["Entity"],
                    attributes={"entity_type": "topic"},
                    name_embedding=None,
                    created_at=datetime.now(UTC),
                ),
                EntityNode(
                    uuid="entity-004",
                    name="Entity Four",
                    group_id="test-org-123",
                    summary="",
                    labels=["Entity"],
                    attributes={"entity_type": "topic"},
                    name_embedding=None,
                    created_at=datetime.now(UTC),
                ),
            ]
        )

        results = await surreal_relationship_manager.get_related_entities_batch(
            ["entity-001", "entity-003"],
            limit_per_entity=5,
        )

        edge_ops.get_by_node_uuids.assert_awaited_once_with(
            surreal_relationship_manager._driver,
            ["entity-001", "entity-003"],
            group_ids=["test-org-123"],
            limit=10,
        )
        node_ops.get_by_uuids.assert_awaited_once_with(
            surreal_relationship_manager._driver,
            ["entity-002", "entity-004"],
        )
        assert [entity.id for entity, _ in results["entity-001"]] == ["entity-002"]
        assert [entity.id for entity, _ in results["entity-003"]] == ["entity-004"]


# =============================================================================
# Validation Tests
# =============================================================================


class TestValidation:
    """Test input validation."""

    def test_validate_relationship_type_valid(self) -> None:
        """_validate_relationship_type() accepts valid types."""
        for rel_type in RelationshipType:
            result = _validate_relationship_type(rel_type.value)
            assert result == rel_type.value

    def test_validate_relationship_type_invalid(self) -> None:
        """_validate_relationship_type() rejects invalid types."""
        with pytest.raises(ValueError, match="Invalid relationship type"):
            _validate_relationship_type("INVALID_TYPE")

    def test_validate_relationship_type_injection_attempt(self) -> None:
        """_validate_relationship_type() prevents Cypher injection."""
        injection_attempts = [
            "RELATED_TO]->(n) DELETE n//",
            "test'; DROP TABLE users;--",
            "RELATED_TO{malicious: true}",
        ]
        for attempt in injection_attempts:
            with pytest.raises(ValueError, match="Invalid relationship type"):
                _validate_relationship_type(attempt)

    def test_valid_relationship_types_constant(self) -> None:
        """VALID_RELATIONSHIP_TYPES contains all enum values."""
        for rel_type in RelationshipType:
            assert rel_type.value in VALID_RELATIONSHIP_TYPES


class TestSanitizePagination:
    """Test pagination sanitization."""

    def test_sanitize_pagination_valid_values(self) -> None:
        """_sanitize_pagination() accepts valid integers."""
        assert _sanitize_pagination(0) == 0
        assert _sanitize_pagination(50) == 50
        assert _sanitize_pagination(100) == 100

    def test_sanitize_pagination_negative_value(self) -> None:
        """_sanitize_pagination() clamps negative values to 0."""
        assert _sanitize_pagination(-10) == 0
        assert _sanitize_pagination(-1) == 0

    def test_sanitize_pagination_exceeds_max(self) -> None:
        """_sanitize_pagination() clamps values exceeding max."""
        assert _sanitize_pagination(50000) == 10000  # default max
        assert _sanitize_pagination(500, max_value=100) == 100

    def test_sanitize_pagination_invalid_type(self) -> None:
        """_sanitize_pagination() raises TypeError for non-int."""
        with pytest.raises(TypeError, match="Pagination value must be int"):
            _sanitize_pagination("100")  # type: ignore
        with pytest.raises(TypeError, match="Pagination value must be int"):
            _sanitize_pagination(100.5)  # type: ignore


# =============================================================================
# Edge Conversion Tests
# =============================================================================


class TestEdgeConversion:
    """Test Relationship <-> EntityEdge conversion."""

    def test_to_graphiti_edge(
        self,
        relationship_manager: RelationshipManager,
        sample_relationship: Relationship,
    ) -> None:
        """_to_graphiti_edge() converts Relationship to EntityEdge."""
        edge = relationship_manager._to_graphiti_edge(sample_relationship)

        assert edge.source_node_uuid == sample_relationship.source_id
        assert edge.target_node_uuid == sample_relationship.target_id
        assert edge.name == sample_relationship.relationship_type.value
        assert edge.group_id == "test-org-123"
        assert edge.attributes["weight"] == sample_relationship.weight

    def test_from_graphiti_edge(
        self,
        relationship_manager: RelationshipManager,
        sample_entity_edge: EntityEdge,
    ) -> None:
        """_from_graphiti_edge() converts EntityEdge to Relationship."""
        relationship = relationship_manager._from_graphiti_edge(sample_entity_edge)

        assert relationship.id == sample_entity_edge.uuid
        assert relationship.source_id == sample_entity_edge.source_node_uuid
        assert relationship.target_id == sample_entity_edge.target_node_uuid
        assert relationship.relationship_type == RelationshipType.RELATED_TO
        assert relationship.weight == 1.0

    def test_from_graphiti_edge_preserves_source_attributes(
        self,
        relationship_manager: RelationshipManager,
        sample_entity_edge: EntityEdge,
    ) -> None:
        """_from_graphiti_edge() does not mutate the Graphiti edge payload."""
        sample_entity_edge.attributes["confidence"] = 0.9

        relationship = relationship_manager._from_graphiti_edge(sample_entity_edge)

        assert relationship.weight == 1.0
        assert relationship.metadata == {"confidence": 0.9}
        assert sample_entity_edge.attributes == {"weight": 1.0, "confidence": 0.9}

    def test_from_graphiti_edge_unknown_type(
        self,
        relationship_manager: RelationshipManager,
    ) -> None:
        """_from_graphiti_edge() defaults unknown types to RELATED_TO."""
        edge = EntityEdge(
            uuid="edge-unknown",
            group_id="test-org-123",
            source_node_uuid="entity-001",
            target_node_uuid="entity-002",
            name="UNKNOWN_CUSTOM_TYPE",
            fact="Unknown relationship",
            created_at=datetime.now(UTC),
            valid_at=datetime.now(UTC),
            fact_embedding=None,
            episodes=[],
            expired_at=None,
            invalid_at=None,
            attributes={},
        )

        relationship = relationship_manager._from_graphiti_edge(edge)

        assert relationship.relationship_type == RelationshipType.RELATED_TO


# =============================================================================
# MockRelationshipManager Tests (conftest.py validation)
# =============================================================================


class TestMockRelationshipManager:
    """Test the MockRelationshipManager from conftest.py."""

    @pytest.mark.asyncio
    async def test_mock_create(
        self,
        mock_relationship_manager: MockRelationshipManager,
    ) -> None:
        """MockRelationshipManager.create() stores relationships."""
        rel = make_relationship(
            source_id="entity-001",
            target_id="entity-002",
            relationship_type=RelationshipType.DEPENDS_ON,
        )

        rel_id = await mock_relationship_manager.create(rel)

        assert rel_id == rel.id
        assert rel.id in mock_relationship_manager.relationships
        assert len(mock_relationship_manager.operation_history) == 1

    @pytest.mark.asyncio
    async def test_mock_get(
        self,
        mock_relationship_manager: MockRelationshipManager,
    ) -> None:
        """MockRelationshipManager.get() retrieves relationships."""
        rel = make_relationship(
            source_id="entity-001",
            target_id="entity-002",
        )
        await mock_relationship_manager.create(rel)

        result = await mock_relationship_manager.get(rel.id)

        assert result.id == rel.id
        assert result.source_id == rel.source_id
        assert result.target_id == rel.target_id

    @pytest.mark.asyncio
    async def test_mock_get_not_found(
        self,
        mock_relationship_manager: MockRelationshipManager,
    ) -> None:
        """MockRelationshipManager.get() raises KeyError when not found."""
        with pytest.raises(KeyError, match="Relationship not found"):
            await mock_relationship_manager.get("nonexistent")

    @pytest.mark.asyncio
    async def test_mock_delete(
        self,
        mock_relationship_manager: MockRelationshipManager,
    ) -> None:
        """MockRelationshipManager.delete() removes relationships."""
        rel = make_relationship(
            source_id="entity-001",
            target_id="entity-002",
        )
        await mock_relationship_manager.create(rel)

        result = await mock_relationship_manager.delete(rel.id)

        assert result is True
        assert rel.id not in mock_relationship_manager.relationships

    @pytest.mark.asyncio
    async def test_mock_delete_not_found(
        self,
        mock_relationship_manager: MockRelationshipManager,
    ) -> None:
        """MockRelationshipManager.delete() returns False when not found."""
        result = await mock_relationship_manager.delete("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_mock_get_for_entity_outgoing(
        self,
        mock_relationship_manager: MockRelationshipManager,
    ) -> None:
        """MockRelationshipManager.get_for_entity() filters by direction."""
        # Create outgoing relationship
        rel_out = make_relationship(
            source_id="entity-001",
            target_id="entity-002",
            relationship_id="rel-out",
        )
        await mock_relationship_manager.create(rel_out)

        # Create incoming relationship
        rel_in = make_relationship(
            source_id="entity-003",
            target_id="entity-001",
            relationship_id="rel-in",
        )
        await mock_relationship_manager.create(rel_in)

        results = await mock_relationship_manager.get_for_entity("entity-001", direction="outgoing")

        assert len(results) == 1
        assert results[0].id == "rel-out"

    @pytest.mark.asyncio
    async def test_mock_get_for_entity_incoming(
        self,
        mock_relationship_manager: MockRelationshipManager,
    ) -> None:
        """MockRelationshipManager.get_for_entity() filters incoming."""
        rel_out = make_relationship(
            source_id="entity-001",
            target_id="entity-002",
            relationship_id="rel-out",
        )
        await mock_relationship_manager.create(rel_out)

        rel_in = make_relationship(
            source_id="entity-003",
            target_id="entity-001",
            relationship_id="rel-in",
        )
        await mock_relationship_manager.create(rel_in)

        results = await mock_relationship_manager.get_for_entity("entity-001", direction="incoming")

        assert len(results) == 1
        assert results[0].id == "rel-in"

    @pytest.mark.asyncio
    async def test_mock_get_for_entity_both(
        self,
        mock_relationship_manager: MockRelationshipManager,
    ) -> None:
        """MockRelationshipManager.get_for_entity() returns both directions."""
        rel_out = make_relationship(
            source_id="entity-001",
            target_id="entity-002",
            relationship_id="rel-out",
        )
        await mock_relationship_manager.create(rel_out)

        rel_in = make_relationship(
            source_id="entity-003",
            target_id="entity-001",
            relationship_id="rel-in",
        )
        await mock_relationship_manager.create(rel_in)

        results = await mock_relationship_manager.get_for_entity("entity-001", direction="both")

        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_mock_get_for_entity_type_filter(
        self,
        mock_relationship_manager: MockRelationshipManager,
    ) -> None:
        """MockRelationshipManager.get_for_entity() filters by type."""
        rel_depends = make_relationship(
            source_id="entity-001",
            target_id="entity-002",
            relationship_type=RelationshipType.DEPENDS_ON,
            relationship_id="rel-depends",
        )
        await mock_relationship_manager.create(rel_depends)

        rel_related = make_relationship(
            source_id="entity-001",
            target_id="entity-003",
            relationship_type=RelationshipType.RELATED_TO,
            relationship_id="rel-related",
        )
        await mock_relationship_manager.create(rel_related)

        results = await mock_relationship_manager.get_for_entity(
            "entity-001",
            relationship_types=[RelationshipType.DEPENDS_ON],
        )

        assert len(results) == 1
        assert results[0].relationship_type == RelationshipType.DEPENDS_ON

    @pytest.mark.asyncio
    async def test_mock_get_between(
        self,
        mock_relationship_manager: MockRelationshipManager,
    ) -> None:
        """MockRelationshipManager.get_between() returns relationships between entities."""
        rel1 = make_relationship(
            source_id="entity-001",
            target_id="entity-002",
            relationship_id="rel-1",
        )
        await mock_relationship_manager.create(rel1)

        rel2 = make_relationship(
            source_id="entity-001",
            target_id="entity-003",
            relationship_id="rel-2",
        )
        await mock_relationship_manager.create(rel2)

        results = await mock_relationship_manager.get_between("entity-001", "entity-002")

        assert len(results) == 1
        assert results[0].id == "rel-1"

    @pytest.mark.asyncio
    async def test_mock_get_between_no_match(
        self,
        mock_relationship_manager: MockRelationshipManager,
    ) -> None:
        """MockRelationshipManager.get_between() returns empty when no match."""
        results = await mock_relationship_manager.get_between("entity-001", "entity-002")

        assert results == []
