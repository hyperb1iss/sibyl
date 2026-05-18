"""Tests for RelationshipManager class."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sibyl_core.graph.relationships import RelationshipManager
from sibyl_core.graph.surreal.compat.models import EntityEdge
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType


@pytest.fixture
def mock_graph_client() -> MagicMock:
    """Create a mock GraphClient."""
    client = MagicMock()
    org_driver = MagicMock()
    client.driver = org_driver
    client.client = MagicMock()
    client.client.driver = org_driver
    client.get_org_driver = MagicMock(return_value=org_driver)
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
        """Should resolve the org-scoped driver."""
        RelationshipManager(mock_graph_client, group_id="org_456")
        mock_graph_client.get_org_driver.assert_called_once_with("org_456")


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
        """Should raise GraphError on failure."""
        from sibyl_core.errors import GraphError

        with patch.object(EntityEdge, "get_between_nodes", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = RuntimeError("Connection failed")

            with pytest.raises(GraphError):
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
                source_id=f"src_{i}",
                target_id=f"tgt_{i}",
                relationship_type=RelationshipType.RELATED_TO,
            )
            for i in range(3)
        ]

        relationship_manager._driver.execute_query = AsyncMock(
            return_value=([{"processed": 3}], None, None)
        )

        created, failed = await relationship_manager.create_bulk(rels)

        assert created == 3
        assert failed == 0
        query = relationship_manager._driver.execute_query.await_args.args[0]
        assert "UNWIND $relationships AS rel" in query

    @pytest.mark.asyncio
    async def test_counts_failures(
        self,
        relationship_manager: RelationshipManager,
    ) -> None:
        """Should count failed creates."""
        rels = [
            Relationship(
                id=f"rel_{i}",
                source_id=f"src_{i}",
                target_id=f"tgt_{i}",
                relationship_type=RelationshipType.RELATED_TO,
            )
            for i in range(3)
        ]

        relationship_manager._driver.execute_query = AsyncMock(
            side_effect=RuntimeError("Batch failed")
        )
        with patch.object(relationship_manager, "create", new_callable=AsyncMock) as mock_create:
            # First succeeds, second fails, third succeeds
            mock_create.side_effect = ["id_1", RuntimeError("Failed"), "id_3"]

            created, failed = await relationship_manager.create_bulk(rels)

            assert created == 2
            assert failed == 1
            assert relationship_manager._driver.execute_query.await_count == 1


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
        """Should raise GraphError on failure."""
        from sibyl_core.errors import GraphError

        relationship_manager._driver.execute_query = AsyncMock(
            side_effect=RuntimeError("Connection failed")
        )

        with pytest.raises(GraphError):
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
        from sibyl_core.graph.client import GraphClient

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
        from sibyl_core.graph.client import GraphClient

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
        from sibyl_core.graph.client import GraphClient

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
        """Should load related entities through EntityManager."""
        entity_manager = MagicMock()
        entity_manager.get = AsyncMock(
            return_value=Entity(
                id="entity_2",
                name="Entity Two",
                entity_type=EntityType.TOPIC,
            )
        )

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
            patch("sibyl_core.graph.entities.EntityManager", return_value=entity_manager),
        ):
            result = await relationship_manager.get_related_entities("entity_1")

        entity_manager.get.assert_awaited_once_with("entity_2")
        assert len(result) == 1
        assert result[0][0].id == "entity_2"
        assert result[0][1].id == "rel_1"

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

        entity_manager = MagicMock()
        entity_manager.get = AsyncMock(
            side_effect=[
                Entity(id=f"entity_{i}", name=f"Entity {i}", entity_type=EntityType.TOPIC)
                for i in range(3)
            ]
        )

        with (
            patch.object(
                relationship_manager,
                "get_for_entity",
                new_callable=AsyncMock,
                return_value=many_relationships,
            ),
            patch("sibyl_core.graph.entities.EntityManager", return_value=entity_manager),
        ):
            await relationship_manager.get_related_entities("entity_1", limit=3)

        assert entity_manager.get.await_count == 3
        assert [call.args[0] for call in entity_manager.get.await_args_list] == [
            "entity_0",
            "entity_1",
            "entity_2",
        ]

    @pytest.mark.asyncio
    async def test_skips_entities_without_properties(
        self,
        relationship_manager: RelationshipManager,
    ) -> None:
        """Should skip entities that fail to load."""
        entity_manager = MagicMock()
        entity_manager.get = AsyncMock(side_effect=RuntimeError("Entity missing"))

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
            patch("sibyl_core.graph.entities.EntityManager", return_value=entity_manager),
        ):
            result = await relationship_manager.get_related_entities("entity_1")

        assert result == []
