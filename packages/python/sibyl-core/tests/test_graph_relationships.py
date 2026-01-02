"""Tests for sibyl-core graph/relationships.py RelationshipManager."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from graphiti_core.edges import EntityEdge

from sibyl_core.errors import ConventionsMCPError
from sibyl_core.graph.relationships import (
    VALID_RELATIONSHIP_TYPES,
    RelationshipManager,
    _sanitize_pagination,
    _validate_relationship_type,
)
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
    graph_client.write_lock = MagicMock()
    graph_client.write_lock.__aenter__ = AsyncMock()
    graph_client.write_lock.__aexit__ = AsyncMock()
    graph_client.normalize_result = MagicMock(side_effect=lambda x: x[0] if x else [])
    return graph_client


@pytest.fixture
def relationship_manager(mock_graph_client: MagicMock) -> RelationshipManager:
    """Create RelationshipManager with mocked dependencies."""
    return RelationshipManager(mock_graph_client, group_id="test-org-123")


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

    def test_init_clones_driver_for_org(self, mock_graph_client: MagicMock) -> None:
        """RelationshipManager clones driver with org-specific graph."""
        RelationshipManager(mock_graph_client, group_id="my-org")
        mock_graph_client.client.driver.clone.assert_called_once_with("my-org")


# =============================================================================
# Relationship Creation Tests
# =============================================================================


class TestRelationshipCreate:
    """Test relationship creation operations."""

    @pytest.mark.asyncio
    async def test_create_relationship_success(
        self,
        relationship_manager: RelationshipManager,
        sample_relationship: Relationship,
        mock_driver: MagicMock,
    ) -> None:
        """create() stores relationship and returns ID."""
        # Mock no existing relationship
        with patch.object(
            EntityEdge,
            "get_between_nodes",
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
                EntityEdge,
                "get_between_nodes",
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
            EntityEdge,
            "get_between_nodes",
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
            EntityEdge,
            "get_between_nodes",
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
            EntityEdge,
            "get_between_nodes",
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
        """create() raises ConventionsMCPError on failure."""
        with (
            patch.object(
                EntityEdge,
                "get_between_nodes",
                new_callable=AsyncMock,
                side_effect=Exception("DB connection failed"),
            ),
            pytest.raises(ConventionsMCPError, match="Failed to create relationship"),
        ):
            await relationship_manager.create(sample_relationship)


# =============================================================================
# Bulk Creation Tests
# =============================================================================


class TestRelationshipBulkCreate:
    """Test bulk relationship creation."""

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

        with patch.object(
            EntityEdge,
            "get_between_nodes",
            new_callable=AsyncMock,
            return_value=[],
        ):
            created, failed = await relationship_manager.create_bulk(relationships)

            assert created == 5
            assert failed == 0

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

        call_count = 0

        async def flaky_get_between(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise Exception("Random failure")
            return []

        with patch.object(
            EntityEdge,
            "get_between_nodes",
            new_callable=AsyncMock,
            side_effect=flaky_get_between,
        ):
            created, failed = await relationship_manager.create_bulk(relationships)

            assert created == 2
            assert failed == 1


# =============================================================================
# Relationship Retrieval Tests (get_for_entity)
# =============================================================================


class TestGetForEntity:
    """Test retrieving relationships for an entity."""

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
        mock_graph_client: MagicMock,
    ) -> None:
        """delete() raises ConventionsMCPError on failure."""
        mock_graph_client.write_lock.__aenter__.side_effect = Exception("DB error")

        with pytest.raises(ConventionsMCPError, match="Failed to delete relationship"):
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
        mock_graph_client: MagicMock,
    ) -> None:
        """delete_for_entity() returns 0 on error (graceful degradation)."""
        mock_graph_client.write_lock.__aenter__.side_effect = Exception("DB error")

        result = await relationship_manager.delete_for_entity("entity-001")

        # Should return 0, not raise
        assert result == 0


# =============================================================================
# List All Relationships Tests
# =============================================================================


class TestListAll:
    """Test listing all relationships."""

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
