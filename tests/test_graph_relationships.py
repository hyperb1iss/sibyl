"""Tests for RelationshipManager class."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from graphiti_core.edges import EntityEdge

from sibyl.graph.relationships import RelationshipManager
from sibyl.models.entities import Relationship, RelationshipType


@pytest.fixture
def mock_graph_client() -> MagicMock:
    """Create a mock GraphClient."""
    client = MagicMock()
    client.driver = MagicMock()
    client.client = MagicMock()
    client.client.driver = MagicMock()
    client.client.driver.clone = MagicMock(return_value=MagicMock())
    client.write_lock = MagicMock()
    client.write_lock.__aenter__ = AsyncMock()
    client.write_lock.__aexit__ = AsyncMock()
    client.normalize_result = MagicMock(return_value=[])
    return client


@pytest.fixture
def relationship_manager(mock_graph_client: MagicMock) -> RelationshipManager:
    """Create a RelationshipManager with mocked client."""
    return RelationshipManager(mock_graph_client, group_id="org_test_123")


@pytest.fixture
def sample_relationship() -> Relationship:
    """Create a sample relationship for testing."""
    return Relationship(
        id="rel_123",
        source_id="source_abc",
        target_id="target_xyz",
        relationship_type=RelationshipType.DEPENDS_ON,
        weight=0.9,
        metadata={"auto_created": True},
    )


class TestRelationshipManagerInit:
    """Tests for RelationshipManager initialization."""

    def test_requires_group_id(self, mock_graph_client: MagicMock) -> None:
        """Should raise ValueError if group_id is empty."""
        with pytest.raises(ValueError, match="group_id is required"):
            RelationshipManager(mock_graph_client, group_id="")

    def test_stores_client_and_group_id(self, mock_graph_client: MagicMock) -> None:
        """Should store client and group_id."""
        manager = RelationshipManager(mock_graph_client, group_id="org_123")
        assert manager._client is mock_graph_client
        assert manager._group_id == "org_123"

    def test_clones_driver_for_org(self, mock_graph_client: MagicMock) -> None:
        """Should clone driver with group_id for multi-tenancy."""
        RelationshipManager(mock_graph_client, group_id="org_456")
        mock_graph_client.client.driver.clone.assert_called_once_with("org_456")


class TestToGraphitiEdge:
    """Tests for _to_graphiti_edge method."""

    def test_converts_relationship_to_edge(
        self, relationship_manager: RelationshipManager, sample_relationship: Relationship
    ) -> None:
        """Should convert Relationship to EntityEdge."""
        edge = relationship_manager._to_graphiti_edge(sample_relationship)

        assert edge.uuid == "rel_123"
        assert edge.source_node_uuid == "source_abc"
        assert edge.target_node_uuid == "target_xyz"
        assert edge.name == "DEPENDS_ON"
        assert edge.group_id == "org_test_123"

    def test_includes_weight_in_attributes(
        self, relationship_manager: RelationshipManager, sample_relationship: Relationship
    ) -> None:
        """Should include weight in attributes."""
        edge = relationship_manager._to_graphiti_edge(sample_relationship)
        assert edge.attributes["weight"] == 0.9

    def test_includes_metadata_in_attributes(
        self, relationship_manager: RelationshipManager, sample_relationship: Relationship
    ) -> None:
        """Should include metadata in attributes."""
        edge = relationship_manager._to_graphiti_edge(sample_relationship)
        assert edge.attributes["auto_created"] is True

    def test_generates_uuid_if_empty(self, relationship_manager: RelationshipManager) -> None:
        """Should generate UUID if relationship.id is empty."""
        rel = Relationship(
            id="",  # Empty string triggers UUID generation
            source_id="src",
            target_id="tgt",
            relationship_type=RelationshipType.RELATED_TO,
        )
        edge = relationship_manager._to_graphiti_edge(rel)
        # Empty string is falsy, so uuid4() is called
        assert edge.uuid is not None
        assert len(edge.uuid) > 0


class TestFromGraphitiEdge:
    """Tests for _from_graphiti_edge method."""

    def test_converts_edge_to_relationship(self, relationship_manager: RelationshipManager) -> None:
        """Should convert EntityEdge to Relationship."""
        edge = EntityEdge(
            uuid="edge_123",
            group_id="org_test_123",
            source_node_uuid="source_1",
            target_node_uuid="target_2",
            created_at=datetime.now(UTC),
            name="DEPENDS_ON",
            fact="test",
            fact_embedding=None,
            episodes=[],
            expired_at=None,
            valid_at=datetime.now(UTC),
            invalid_at=None,
            attributes={"weight": 0.8, "custom": "value"},
        )

        rel = relationship_manager._from_graphiti_edge(edge)

        assert rel.id == "edge_123"
        assert rel.source_id == "source_1"
        assert rel.target_id == "target_2"
        assert rel.relationship_type == RelationshipType.DEPENDS_ON
        assert rel.weight == 0.8
        assert rel.metadata == {"custom": "value"}

    def test_handles_unknown_relationship_type(
        self, relationship_manager: RelationshipManager
    ) -> None:
        """Should default to RELATED_TO for unknown types."""
        edge = EntityEdge(
            uuid="edge_123",
            group_id="org_test_123",
            source_node_uuid="source_1",
            target_node_uuid="target_2",
            created_at=datetime.now(UTC),
            name="UNKNOWN_TYPE",  # Not in RelationshipType enum
            fact="test",
            fact_embedding=None,
            episodes=[],
            expired_at=None,
            valid_at=datetime.now(UTC),
            invalid_at=None,
            attributes={},
        )

        rel = relationship_manager._from_graphiti_edge(edge)
        assert rel.relationship_type == RelationshipType.RELATED_TO

    def test_defaults_weight_to_one(self, relationship_manager: RelationshipManager) -> None:
        """Should default weight to 1.0 if not in attributes."""
        edge = EntityEdge(
            uuid="edge_123",
            group_id="org_test_123",
            source_node_uuid="source_1",
            target_node_uuid="target_2",
            created_at=datetime.now(UTC),
            name="RELATED_TO",
            fact="test",
            fact_embedding=None,
            episodes=[],
            expired_at=None,
            valid_at=datetime.now(UTC),
            invalid_at=None,
            attributes={},  # Empty dict, no weight key
        )

        rel = relationship_manager._from_graphiti_edge(edge)
        assert rel.weight == 1.0


class TestCreate:
    """Tests for create method."""

    @pytest.mark.asyncio
    async def test_creates_relationship(
        self,
        relationship_manager: RelationshipManager,
        sample_relationship: Relationship,
    ) -> None:
        """Should create relationship and return ID."""
        with patch.object(EntityEdge, "get_between_nodes", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = []
            relationship_manager._driver.execute_query = AsyncMock()

            result = await relationship_manager.create(sample_relationship)

            assert result == "rel_123"
            relationship_manager._driver.execute_query.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_duplicate(
        self,
        relationship_manager: RelationshipManager,
        sample_relationship: Relationship,
    ) -> None:
        """Should skip if relationship already exists."""
        existing_edge = MagicMock()
        existing_edge.name = "DEPENDS_ON"
        existing_edge.uuid = "existing_123"

        with patch.object(EntityEdge, "get_between_nodes", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = [existing_edge]

            result = await relationship_manager.create(sample_relationship)

            assert result == "existing_123"
            # Should not execute query since duplicate exists
            relationship_manager._driver.execute_query.assert_not_called()

    @pytest.mark.asyncio
    async def test_allows_different_type(
        self,
        relationship_manager: RelationshipManager,
        sample_relationship: Relationship,
    ) -> None:
        """Should create if existing relationship has different type."""
        existing_edge = MagicMock()
        existing_edge.name = "RELATED_TO"  # Different from DEPENDS_ON
        existing_edge.uuid = "existing_123"

        with patch.object(EntityEdge, "get_between_nodes", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = [existing_edge]
            relationship_manager._driver.execute_query = AsyncMock()

            await relationship_manager.create(sample_relationship)

            # Should create new relationship since type is different
            relationship_manager._driver.execute_query.assert_called_once()

    @pytest.mark.asyncio
    async def test_raises_on_failure(
        self,
        relationship_manager: RelationshipManager,
        sample_relationship: Relationship,
    ) -> None:
        """Should raise ConventionsMCPError on failure."""
        from sibyl.errors import ConventionsMCPError

        with patch.object(EntityEdge, "get_between_nodes", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = RuntimeError("Connection failed")

            with pytest.raises(ConventionsMCPError):
                await relationship_manager.create(sample_relationship)


class TestCreateBulk:
    """Tests for create_bulk method."""

    @pytest.mark.asyncio
    async def test_creates_all_relationships(
        self,
        relationship_manager: RelationshipManager,
    ) -> None:
        """Should create all relationships and return counts."""
        rels = [
            Relationship(
                id=f"rel_{i}",
                source_id="src",
                target_id="tgt",
                relationship_type=RelationshipType.RELATED_TO,
            )
            for i in range(3)
        ]

        with patch.object(relationship_manager, "create", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = "created_id"

            created, failed = await relationship_manager.create_bulk(rels)

            assert created == 3
            assert failed == 0
            assert mock_create.call_count == 3

    @pytest.mark.asyncio
    async def test_counts_failures(
        self,
        relationship_manager: RelationshipManager,
    ) -> None:
        """Should count failed creates."""
        rels = [
            Relationship(
                id=f"rel_{i}",
                source_id="src",
                target_id="tgt",
                relationship_type=RelationshipType.RELATED_TO,
            )
            for i in range(3)
        ]

        with patch.object(relationship_manager, "create", new_callable=AsyncMock) as mock_create:
            # First succeeds, second fails, third succeeds
            mock_create.side_effect = ["id_1", RuntimeError("Failed"), "id_3"]

            created, failed = await relationship_manager.create_bulk(rels)

            assert created == 2
            assert failed == 1


class TestGetForEntity:
    """Tests for get_for_entity method."""

    @pytest.mark.asyncio
    async def test_queries_both_directions(
        self,
        relationship_manager: RelationshipManager,
        mock_graph_client: MagicMock,
    ) -> None:
        """Should query both directions by default."""
        relationship_manager._driver.execute_query = AsyncMock(return_value=MagicMock())
        mock_graph_client.normalize_result.return_value = []

        await relationship_manager.get_for_entity("entity_123")

        call_args = relationship_manager._driver.execute_query.call_args
        query = call_args[0][0]
        assert "-[r]-" in query  # Both directions pattern

    @pytest.mark.asyncio
    async def test_queries_outgoing_direction(
        self,
        relationship_manager: RelationshipManager,
        mock_graph_client: MagicMock,
    ) -> None:
        """Should query outgoing direction when specified."""
        relationship_manager._driver.execute_query = AsyncMock(return_value=MagicMock())
        mock_graph_client.normalize_result.return_value = []

        await relationship_manager.get_for_entity("entity_123", direction="outgoing")

        call_args = relationship_manager._driver.execute_query.call_args
        query = call_args[0][0]
        assert "-[r]->" in query

    @pytest.mark.asyncio
    async def test_queries_incoming_direction(
        self,
        relationship_manager: RelationshipManager,
        mock_graph_client: MagicMock,
    ) -> None:
        """Should query incoming direction when specified."""
        relationship_manager._driver.execute_query = AsyncMock(return_value=MagicMock())
        mock_graph_client.normalize_result.return_value = []

        await relationship_manager.get_for_entity("entity_123", direction="incoming")

        call_args = relationship_manager._driver.execute_query.call_args
        query = call_args[0][0]
        assert "<-[r]-" in query

    @pytest.mark.asyncio
    async def test_parses_dict_results(
        self,
        relationship_manager: RelationshipManager,
        mock_graph_client: MagicMock,
    ) -> None:
        """Should parse dict-style results."""
        relationship_manager._driver.execute_query = AsyncMock(return_value=MagicMock())
        mock_graph_client.normalize_result.return_value = [
            {
                "uuid": "rel_1",
                "name": "DEPENDS_ON",
                "source_id": "src",
                "target_id": "tgt",
                "weight": 0.8,
            }
        ]

        result = await relationship_manager.get_for_entity("entity_123")

        assert len(result) == 1
        assert result[0].id == "rel_1"
        assert result[0].relationship_type == RelationshipType.DEPENDS_ON
        assert result[0].weight == 0.8

    @pytest.mark.asyncio
    async def test_parses_list_results(
        self,
        relationship_manager: RelationshipManager,
        mock_graph_client: MagicMock,
    ) -> None:
        """Should parse list-style results."""
        relationship_manager._driver.execute_query = AsyncMock(return_value=MagicMock())
        # List format: [uuid, name, source_id, target_id, weight]
        mock_graph_client.normalize_result.return_value = [
            ["rel_2", "REQUIRES", "src2", "tgt2", 0.9]
        ]

        result = await relationship_manager.get_for_entity("entity_123")

        assert len(result) == 1
        assert result[0].id == "rel_2"
        assert result[0].relationship_type == RelationshipType.REQUIRES
        assert result[0].weight == 0.9

    @pytest.mark.asyncio
    async def test_filters_by_type(
        self,
        relationship_manager: RelationshipManager,
        mock_graph_client: MagicMock,
    ) -> None:
        """Should filter results by relationship type."""
        relationship_manager._driver.execute_query = AsyncMock(return_value=MagicMock())
        mock_graph_client.normalize_result.return_value = [
            {
                "uuid": "rel_1",
                "name": "DEPENDS_ON",
                "source_id": "s",
                "target_id": "t",
                "weight": 1.0,
            },
            {
                "uuid": "rel_2",
                "name": "REQUIRES",
                "source_id": "s",
                "target_id": "t",
                "weight": 1.0,
            },
        ]

        result = await relationship_manager.get_for_entity(
            "entity_123",
            relationship_types=[RelationshipType.DEPENDS_ON],
        )

        assert len(result) == 1
        assert result[0].relationship_type == RelationshipType.DEPENDS_ON

    @pytest.mark.asyncio
    async def test_returns_empty_on_error(
        self,
        relationship_manager: RelationshipManager,
    ) -> None:
        """Should return empty list on error."""
        relationship_manager._driver.execute_query = AsyncMock(
            side_effect=RuntimeError("Connection failed")
        )

        result = await relationship_manager.get_for_entity("entity_123")

        assert result == []


class TestDelete:
    """Tests for delete method."""

    @pytest.mark.asyncio
    async def test_deletes_relationship(
        self,
        relationship_manager: RelationshipManager,
        mock_graph_client: MagicMock,
    ) -> None:
        """Should delete relationship and return True."""
        relationship_manager._driver.execute_query = AsyncMock(return_value=MagicMock())
        mock_graph_client.normalize_result.return_value = [{"deleted": 1}]

        result = await relationship_manager.delete("rel_123")

        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_if_not_found(
        self,
        relationship_manager: RelationshipManager,
        mock_graph_client: MagicMock,
    ) -> None:
        """Should return False if relationship not found."""
        relationship_manager._driver.execute_query = AsyncMock(return_value=MagicMock())
        mock_graph_client.normalize_result.return_value = [{"deleted": 0}]

        result = await relationship_manager.delete("rel_nonexistent")

        assert result is False

    @pytest.mark.asyncio
    async def test_handles_list_result(
        self,
        relationship_manager: RelationshipManager,
        mock_graph_client: MagicMock,
    ) -> None:
        """Should handle list-style result format."""
        relationship_manager._driver.execute_query = AsyncMock(return_value=MagicMock())
        mock_graph_client.normalize_result.return_value = [[1]]  # List format

        result = await relationship_manager.delete("rel_123")

        assert result is True

    @pytest.mark.asyncio
    async def test_raises_on_error(
        self,
        relationship_manager: RelationshipManager,
    ) -> None:
        """Should raise ConventionsMCPError on failure."""
        from sibyl.errors import ConventionsMCPError

        relationship_manager._driver.execute_query = AsyncMock(
            side_effect=RuntimeError("Connection failed")
        )

        with pytest.raises(ConventionsMCPError):
            await relationship_manager.delete("rel_123")


class TestDeleteForEntity:
    """Tests for delete_for_entity method."""

    @pytest.mark.asyncio
    async def test_deletes_all_entity_relationships(
        self,
        relationship_manager: RelationshipManager,
        mock_graph_client: MagicMock,
    ) -> None:
        """Should delete all relationships for entity."""
        relationship_manager._driver.execute_query = AsyncMock(return_value=MagicMock())
        mock_graph_client.normalize_result.return_value = [{"deleted": 5}]

        result = await relationship_manager.delete_for_entity("entity_123")

        assert result == 5

    @pytest.mark.asyncio
    async def test_returns_zero_on_error(
        self,
        relationship_manager: RelationshipManager,
    ) -> None:
        """Should return 0 on error."""
        relationship_manager._driver.execute_query = AsyncMock(
            side_effect=RuntimeError("Connection failed")
        )

        result = await relationship_manager.delete_for_entity("entity_123")

        assert result == 0


class TestListAll:
    """Tests for list_all method."""

    @pytest.mark.asyncio
    async def test_lists_relationships(
        self,
        relationship_manager: RelationshipManager,
    ) -> None:
        """Should list all relationships."""
        from sibyl.graph.client import GraphClient

        relationship_manager._driver.execute_query = AsyncMock(return_value=MagicMock())

        with patch.object(
            GraphClient,
            "normalize_result",
            return_value=[
                {
                    "id": "rel_1",
                    "source_id": "src",
                    "target_id": "tgt",
                    "rel_type": "DEPENDS_ON",
                    "created_at": "2024-01-01",
                }
            ],
        ):
            result = await relationship_manager.list_all()

        assert len(result) == 1
        assert result[0].id == "rel_1"
        assert result[0].relationship_type == RelationshipType.DEPENDS_ON

    @pytest.mark.asyncio
    async def test_filters_by_type(
        self,
        relationship_manager: RelationshipManager,
    ) -> None:
        """Should filter by relationship types in query."""
        from sibyl.graph.client import GraphClient

        relationship_manager._driver.execute_query = AsyncMock(return_value=MagicMock())

        with patch.object(
            GraphClient,
            "normalize_result",
            return_value=[],
        ):
            await relationship_manager.list_all(
                relationship_types=[RelationshipType.DEPENDS_ON, RelationshipType.REQUIRES]
            )

        call_args = relationship_manager._driver.execute_query.call_args
        query = call_args[0][0]
        assert "DEPENDS_ON" in query
        assert "REQUIRES" in query

    @pytest.mark.asyncio
    async def test_handles_unknown_type(
        self,
        relationship_manager: RelationshipManager,
    ) -> None:
        """Should default to RELATED_TO for unknown types."""
        from sibyl.graph.client import GraphClient

        relationship_manager._driver.execute_query = AsyncMock(return_value=MagicMock())

        with patch.object(
            GraphClient,
            "normalize_result",
            return_value=[
                {
                    "id": "rel_1",
                    "source_id": "src",
                    "target_id": "tgt",
                    "rel_type": "UNKNOWN_TYPE",
                    "created_at": "2024-01-01",
                }
            ],
        ):
            result = await relationship_manager.list_all()

        assert result[0].relationship_type == RelationshipType.RELATED_TO

    @pytest.mark.asyncio
    async def test_returns_empty_on_error(
        self,
        relationship_manager: RelationshipManager,
    ) -> None:
        """Should return empty list on error."""
        relationship_manager._driver.execute_query = AsyncMock(
            side_effect=RuntimeError("Connection failed")
        )

        result = await relationship_manager.list_all()

        assert result == []


class TestGetRelatedEntities:
    """Tests for RelationshipManager.get_related_entities method."""

    @pytest.mark.asyncio
    async def test_queries_for_related_entities(
        self,
        relationship_manager: RelationshipManager,
    ) -> None:
        """Should query for related entities when relationships exist."""
        # Mock get_for_entity to return relationships
        with patch.object(
            relationship_manager,
            "get_for_entity",
            new_callable=AsyncMock,
            return_value=[
                Relationship(
                    id="rel_1",
                    source_id="entity_1",
                    target_id="entity_2",
                    relationship_type=RelationshipType.DEPENDS_ON,
                ),
            ],
        ):
            # Mock execute_read_org to return empty (we just verify query happens)
            relationship_manager._client.execute_read_org = AsyncMock(return_value=[])

            await relationship_manager.get_related_entities("entity_1")

        # Verify the query was made with correct entity IDs
        relationship_manager._client.execute_read_org.assert_called_once()
        call_args = relationship_manager._client.execute_read_org.call_args
        assert "entity_2" in call_args.kwargs["ids"]

    @pytest.mark.asyncio
    async def test_returns_empty_for_no_relationships(
        self,
        relationship_manager: RelationshipManager,
    ) -> None:
        """Should return empty list when no relationships found."""
        with patch.object(
            relationship_manager,
            "get_for_entity",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await relationship_manager.get_related_entities("entity_1")

        assert result == []

    @pytest.mark.asyncio
    async def test_handles_error_gracefully(
        self,
        relationship_manager: RelationshipManager,
    ) -> None:
        """Should return empty list on error."""
        with patch.object(
            relationship_manager,
            "get_for_entity",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Query failed"),
        ):
            result = await relationship_manager.get_related_entities("entity_1")

        assert result == []

    @pytest.mark.asyncio
    async def test_respects_limit(
        self,
        relationship_manager: RelationshipManager,
    ) -> None:
        """Should respect limit parameter."""
        # Return more relationships than limit
        many_relationships = [
            Relationship(
                id=f"rel_{i}",
                source_id="entity_1",
                target_id=f"entity_{i}",
                relationship_type=RelationshipType.RELATED_TO,
            )
            for i in range(10)
        ]

        with (
            patch.object(
                relationship_manager,
                "get_for_entity",
                new_callable=AsyncMock,
                return_value=many_relationships,
            ),
            patch("sibyl.graph.entities.EntityManager"),
        ):
            # Mock execute_read_org
            relationship_manager._client.execute_read_org = AsyncMock(return_value=[])

            await relationship_manager.get_related_entities("entity_1", limit=3)

            # Should only query for first 3 entities
            call_args = relationship_manager._client.execute_read_org.call_args
            ids_queried = call_args.kwargs["ids"]
            assert len(ids_queried) == 3

    @pytest.mark.asyncio
    async def test_skips_entities_without_properties(
        self,
        relationship_manager: RelationshipManager,
    ) -> None:
        """Should skip nodes that don't have properties attribute."""
        with (
            patch.object(
                relationship_manager,
                "get_for_entity",
                new_callable=AsyncMock,
                return_value=[
                    Relationship(
                        id="rel_1",
                        source_id="entity_1",
                        target_id="entity_2",
                        relationship_type=RelationshipType.DEPENDS_ON,
                    ),
                ],
            ),
            patch("sibyl.graph.entities.EntityManager"),
        ):
            # Return node without properties attribute
            mock_node = MagicMock(spec=[])  # No properties
            del mock_node.properties  # Ensure no properties attr
            relationship_manager._client.execute_read_org = AsyncMock(
                return_value=[{"n": mock_node}]
            )

            result = await relationship_manager.get_related_entities("entity_1")

        # Should return empty because node couldn't be processed
        assert result == []
