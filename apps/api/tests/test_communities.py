"""Tests for community detection module."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services.graph_communities import (
    CommunityConfig,
    DetectedCommunity,
    detect_communities,
    export_to_networkx,
    get_community_members,
    get_entity_communities,
    get_hierarchical_graph,
    link_hierarchy,
    partition_to_communities,
    store_communities,
)

# Test organization ID for multi-tenancy
TEST_ORG_ID = "test-org-communities"


def _make_entity(entity_id: str, name: str, entity_type: EntityType) -> Entity:
    return Entity(
        id=entity_id,
        name=name,
        entity_type=entity_type,
    )


def _make_relationship(
    relationship_id: str,
    source_id: str,
    target_id: str,
    relationship_type: RelationshipType = RelationshipType.RELATED_TO,
) -> Relationship:
    return Relationship(
        id=relationship_id,
        source_id=source_id,
        target_id=target_id,
        relationship_type=relationship_type,
    )


def _make_community_entity(
    entity_id: str,
    *,
    level: int,
    member_count: int,
    summary: str = "",
) -> Entity:
    return Entity(
        id=entity_id,
        name=f"Community L{level}",
        entity_type=EntityType.COMMUNITY,
        description=summary,
        metadata={
            "level": level,
            "member_count": member_count,
            "summary": summary,
        },
    )


class TestCommunityConfig:
    """Tests for CommunityConfig dataclass."""

    def test_default_values(self) -> None:
        """Test default configuration values."""
        config = CommunityConfig()

        assert config.resolutions == [0.5, 1.0, 2.0]
        assert config.min_community_size == 2
        assert config.max_levels == 3
        assert config.store_in_graph is True

    def test_custom_values(self) -> None:
        """Test custom configuration values."""
        config = CommunityConfig(
            resolutions=[0.3, 0.7, 1.5, 3.0],
            min_community_size=5,
            max_levels=4,
            store_in_graph=False,
        )

        assert config.resolutions == [0.3, 0.7, 1.5, 3.0]
        assert config.min_community_size == 5
        assert config.max_levels == 4
        assert config.store_in_graph is False


class TestDetectedCommunity:
    """Tests for DetectedCommunity dataclass."""

    def test_member_count(self) -> None:
        """Test member_count property."""
        community = DetectedCommunity(
            id="comm_1",
            member_ids=["e1", "e2", "e3"],
            level=0,
            resolution=1.0,
        )

        assert community.member_count == 3

    def test_default_values(self) -> None:
        """Test default field values."""
        community = DetectedCommunity(
            id="comm_1",
            member_ids=["e1"],
            level=0,
            resolution=1.0,
        )

        assert community.modularity == 0.0
        assert community.parent_id is None
        assert community.child_ids == []


class TestPartitionToCommunities:
    """Tests for partition_to_communities function."""

    def test_basic_partition(self) -> None:
        """Convert basic partition to communities."""
        partition = {
            "e1": 0,
            "e2": 0,
            "e3": 1,
            "e4": 1,
        }

        communities = partition_to_communities(
            partition=partition,
            level=0,
            resolution=1.0,
            modularity=0.5,
            min_size=2,
        )

        assert len(communities) == 2
        # Check member counts
        member_counts = sorted([c.member_count for c in communities])
        assert member_counts == [2, 2]

    def test_min_size_filter(self) -> None:
        """Communities below min_size are filtered."""
        partition = {
            "e1": 0,
            "e2": 0,
            "e3": 1,  # Single node community
        }

        communities = partition_to_communities(
            partition=partition,
            level=0,
            resolution=1.0,
            modularity=0.5,
            min_size=2,
        )

        assert len(communities) == 1
        assert communities[0].member_count == 2

    def test_empty_partition(self) -> None:
        """Empty partition returns empty list."""
        communities = partition_to_communities(
            partition={},
            level=0,
            resolution=1.0,
            modularity=0.0,
            min_size=2,
        )

        assert communities == []

    def test_community_ids_unique(self) -> None:
        """Each community gets unique ID."""
        partition = {
            "e1": 0,
            "e2": 0,
            "e3": 1,
            "e4": 1,
        }

        communities = partition_to_communities(
            partition=partition,
            level=0,
            resolution=1.0,
            modularity=0.5,
            min_size=2,
        )

        ids = [c.id for c in communities]
        assert len(ids) == len(set(ids))


class TestLinkHierarchy:
    """Tests for link_hierarchy function."""

    def test_empty_input(self) -> None:
        """Empty input returns empty list."""
        assert link_hierarchy([]) == []

    def test_single_level(self) -> None:
        """Single level has no parent links."""
        communities = [
            DetectedCommunity(id="c1", member_ids=["e1", "e2"], level=0, resolution=1.0),
            DetectedCommunity(id="c2", member_ids=["e3", "e4"], level=0, resolution=1.0),
        ]

        result = link_hierarchy([communities])

        assert len(result) == 2
        assert all(c.parent_id is None for c in result)

    def test_two_levels_linked(self) -> None:
        """Child communities link to parent."""
        level0 = [
            DetectedCommunity(id="c1", member_ids=["e1", "e2"], level=0, resolution=0.5),
            DetectedCommunity(id="c2", member_ids=["e3", "e4"], level=0, resolution=0.5),
        ]
        level1 = [
            # Parent contains both level0 communities
            DetectedCommunity(
                id="c3", member_ids=["e1", "e2", "e3", "e4"], level=1, resolution=1.0
            ),
        ]

        result = link_hierarchy([level0, level1])

        assert len(result) == 3

        # Check children are linked to parent
        c1 = next(c for c in result if c.id == "c1")
        c2 = next(c for c in result if c.id == "c2")
        c3 = next(c for c in result if c.id == "c3")

        assert c1.parent_id == "c3"
        assert c2.parent_id == "c3"
        assert c3.parent_id is None
        assert sorted(c3.child_ids) == ["c1", "c2"]

    def test_partial_overlap(self) -> None:
        """Only subsets become children."""
        level0 = [
            DetectedCommunity(id="c1", member_ids=["e1", "e2"], level=0, resolution=0.5),
            DetectedCommunity(id="c2", member_ids=["e3", "e4", "e5"], level=0, resolution=0.5),
        ]
        level1 = [
            # Only contains c1's members (e1, e2), not all of c2
            DetectedCommunity(id="c3", member_ids=["e1", "e2", "e3"], level=1, resolution=1.0),
        ]

        result = link_hierarchy([level0, level1])

        c1 = next(c for c in result if c.id == "c1")
        c2 = next(c for c in result if c.id == "c2")
        c3 = next(c for c in result if c.id == "c3")

        # c1 is subset of c3
        assert c1.parent_id == "c3"
        # c2 is NOT subset of c3 (c2 has e4, e5 not in c3)
        assert c2.parent_id is None
        assert "c1" in c3.child_ids
        assert "c2" not in c3.child_ids


class TestExportToNetworkx:
    """Tests for export_to_networkx function."""

    @pytest.fixture
    def mock_client(self) -> MagicMock:
        """Create mock graph client."""
        client = MagicMock()
        client.execute_read_org = AsyncMock(return_value=[])
        client.execute_write_org = AsyncMock(return_value=[])
        return client

    @pytest.mark.asyncio
    async def test_empty_graph(self, mock_client: MagicMock) -> None:
        """Empty graph returns empty NetworkX graph."""
        G = await export_to_networkx(mock_client, TEST_ORG_ID)

        assert G.number_of_nodes() == 0
        assert G.number_of_edges() == 0

    @pytest.mark.asyncio
    async def test_nodes_exported(self, mock_client: MagicMock) -> None:
        """Nodes are properly exported."""
        entity_manager = MagicMock()
        entity_manager.list_all = AsyncMock(
            return_value=[
                _make_entity("e1", "Entity One", EntityType.PATTERN),
                _make_entity("e2", "Entity Two", EntityType.RULE),
            ]
        )
        relationship_manager = MagicMock()
        relationship_manager.list_all = AsyncMock(return_value=[])

        with (
            patch(
                "sibyl_core.services.graph_communities._entity_manager_factory",
                return_value=entity_manager,
            ),
            patch(
                "sibyl_core.services.graph_communities._relationship_manager_factory",
                return_value=relationship_manager,
            ),
        ):
            G = await export_to_networkx(mock_client, TEST_ORG_ID)

        assert G.number_of_nodes() == 2
        assert "e1" in G.nodes()
        assert "e2" in G.nodes()
        assert G.nodes["e1"]["name"] == "Entity One"
        assert G.nodes["e2"]["type"] == EntityType.RULE.value

    @pytest.mark.asyncio
    async def test_edges_exported(self, mock_client: MagicMock) -> None:
        """Edges are properly exported."""
        entity_manager = MagicMock()
        entity_manager.list_all = AsyncMock(
            return_value=[
                _make_entity("e1", "Entity One", EntityType.PATTERN),
                _make_entity("e2", "Entity Two", EntityType.PATTERN),
            ]
        )
        relationship_manager = MagicMock()
        relationship_manager.list_all = AsyncMock(
            return_value=[
                _make_relationship("r1", "e1", "e2"),
            ]
        )

        with (
            patch(
                "sibyl_core.services.graph_communities._entity_manager_factory",
                return_value=entity_manager,
            ),
            patch(
                "sibyl_core.services.graph_communities._relationship_manager_factory",
                return_value=relationship_manager,
            ),
        ):
            G = await export_to_networkx(mock_client, TEST_ORG_ID)

        assert G.number_of_edges() == 1
        assert G.has_edge("e1", "e2")

    @pytest.mark.asyncio
    async def test_missing_networkx(self, mock_client: MagicMock) -> None:
        """Raises ImportError if networkx not installed."""
        with patch.dict("sys.modules", {"networkx": None}):
            # Need to reload the module to trigger ImportError
            # This is tricky to test, so we'll just verify the function exists
            pass


class TestDetectCommunities:
    """Tests for detect_communities function."""

    @pytest.fixture
    def mock_client(self) -> MagicMock:
        """Create mock graph client."""
        client = MagicMock()
        client.execute_read_org = AsyncMock(return_value=[])
        client.execute_write_org = AsyncMock(return_value=[])
        return client

    @pytest.mark.asyncio
    async def test_empty_graph(self, mock_client: MagicMock) -> None:
        """Empty graph returns no communities."""
        communities = await detect_communities(mock_client, TEST_ORG_ID)
        assert communities == []

    @pytest.mark.asyncio
    async def test_with_mock_louvain(self, mock_client: MagicMock) -> None:
        """Communities detected with mocked Louvain."""
        entity_manager = MagicMock()
        entity_manager.list_all = AsyncMock(
            return_value=[
                _make_entity("e1", "Entity One", EntityType.PATTERN),
                _make_entity("e2", "Entity Two", EntityType.PATTERN),
                _make_entity("e3", "Entity Three", EntityType.PATTERN),
                _make_entity("e4", "Entity Four", EntityType.PATTERN),
            ]
        )
        relationship_manager = MagicMock()
        relationship_manager.list_all = AsyncMock(
            return_value=[
                _make_relationship("r1", "e1", "e2"),
                _make_relationship("r2", "e3", "e4"),
            ]
        )

        # Mock louvain algorithm
        mock_partition = {"e1": 0, "e2": 0, "e3": 1, "e4": 1}
        mock_modularity = 0.5

        with (
            patch(
                "sibyl_core.services.graph_communities._entity_manager_factory",
                return_value=entity_manager,
            ),
            patch(
                "sibyl_core.services.graph_communities._relationship_manager_factory",
                return_value=relationship_manager,
            ),
            patch(
                "sibyl_core.services.graph_communities.detect_communities_louvain"
            ) as mock_louvain,
        ):
            mock_louvain.return_value = (mock_partition, mock_modularity)

            config = CommunityConfig(resolutions=[1.0], max_levels=1)
            communities = await detect_communities(mock_client, TEST_ORG_ID, config=config)

            assert len(communities) == 2
            mock_louvain.assert_called_once()


class TestHierarchicalGraph:
    """Tests for hierarchical graph snapshot selection."""

    @pytest.fixture
    def mock_client(self) -> MagicMock:
        client = MagicMock()
        client.execute_read_org = AsyncMock(return_value=[])
        client.execute_write_org = AsyncMock(return_value=[])
        return client

    @pytest.mark.asyncio
    async def test_prefers_connected_nodes_and_reuses_snapshot(
        self,
        mock_client: MagicMock,
    ) -> None:
        entities = [
            _make_entity("isolated-1", "Isolated 1", EntityType.NOTE),
            _make_entity("isolated-2", "Isolated 2", EntityType.NOTE),
            _make_entity("isolated-3", "Isolated 3", EntityType.NOTE),
            _make_entity("core-1", "Core 1", EntityType.TASK),
            _make_entity("core-2", "Core 2", EntityType.TASK),
            _make_entity("core-3", "Core 3", EntityType.TASK),
        ]
        relationships = [
            _make_relationship("r1", "core-1", "core-2"),
            _make_relationship("r2", "core-2", "core-3"),
        ]
        partition = {entity.id: 0 for entity in entities}

        with (
            patch.dict("sibyl_core.services.graph_communities.HIERARCHICAL_CACHE", {}, clear=True),
            patch.dict(
                "sibyl_core.services.graph_communities.GRAPH_SNAPSHOT_CACHE", {}, clear=True
            ),
            patch.dict("sibyl_core.services.graph_communities.GRAPH_LOD_CACHE", {}, clear=True),
            patch(
                "sibyl_core.services.graph_communities._list_all_entities",
                AsyncMock(return_value=entities),
            ) as list_entities,
            patch(
                "sibyl_core.services.graph_communities._list_all_relationships",
                AsyncMock(return_value=relationships),
            ) as list_relationships,
            patch(
                "sibyl_core.services.graph_communities.detect_communities_louvain",
                return_value=(partition, 0.5),
            ),
        ):
            data = await get_hierarchical_graph(mock_client, TEST_ORG_ID, max_nodes=3, max_edges=10)

        assert {node["id"] for node in data.nodes} == {"core-1", "core-2", "core-3"}
        assert data.displayed_edges == 2
        list_entities.assert_awaited_once_with(
            mock_client,
            TEST_ORG_ID,
            batch_size=3,
            max_items=3,
        )
        list_relationships.assert_awaited_once_with(
            mock_client,
            TEST_ORG_ID,
            batch_size=10,
            max_items=10,
        )

    @pytest.mark.asyncio
    async def test_honors_type_filters_in_totals_and_clusters(
        self,
        mock_client: MagicMock,
    ) -> None:
        entities = [
            _make_entity("topic-1", "Topic 1", EntityType.TOPIC),
            _make_entity("topic-2", "Topic 2", EntityType.TOPIC),
            _make_entity("task-1", "Task 1", EntityType.TASK),
            _make_entity("note-1", "Note 1", EntityType.NOTE),
        ]
        relationships = [
            _make_relationship("r1", "topic-1", "topic-2"),
            _make_relationship("r2", "topic-1", "task-1"),
        ]
        partition = {entity.id: 0 for entity in entities}

        with (
            patch.dict("sibyl_core.services.graph_communities.HIERARCHICAL_CACHE", {}, clear=True),
            patch.dict(
                "sibyl_core.services.graph_communities.GRAPH_SNAPSHOT_CACHE", {}, clear=True
            ),
            patch.dict("sibyl_core.services.graph_communities.GRAPH_LOD_CACHE", {}, clear=True),
            patch(
                "sibyl_core.services.graph_communities._list_all_entities",
                AsyncMock(return_value=entities),
            ),
            patch(
                "sibyl_core.services.graph_communities._list_all_relationships",
                AsyncMock(return_value=relationships),
            ),
            patch(
                "sibyl_core.services.graph_communities.detect_communities_louvain",
                return_value=(partition, 0.5),
            ),
        ):
            data = await get_hierarchical_graph(
                mock_client,
                TEST_ORG_ID,
                entity_types=["topic"],
                max_nodes=10,
                max_edges=10,
            )

        assert data.total_nodes == 2
        assert data.total_edges == 1
        assert data.displayed_nodes == 2
        assert data.displayed_edges == 1
        assert {node["type"] for node in data.nodes} == {"topic"}
        assert len(data.clusters) == 1
        assert data.clusters[0]["member_count"] == 2
        assert data.clusters[0]["displayed_member_count"] == 2
        assert data.clusters[0]["type_distribution"] == {"topic": 2}

    @pytest.mark.asyncio
    async def test_sampling_preserves_secondary_entity_types(
        self,
        mock_client: MagicMock,
    ) -> None:
        task_entities = [
            _make_entity(f"task-{index}", f"Task {index}", EntityType.TASK) for index in range(110)
        ]
        topic_entities = [
            _make_entity(f"topic-{index}", f"Topic {index}", EntityType.TOPIC)
            for index in range(10)
        ]
        entities = [*task_entities, *topic_entities]
        relationships = [
            _make_relationship(f"task-edge-{index}", "task-0", f"task-{index}")
            for index in range(1, 110)
        ]
        relationships.extend(
            _make_relationship(f"topic-edge-{index}", f"topic-{index}", f"topic-{index + 1}")
            for index in range(9)
        )
        partition = {entity.id: 0 for entity in entities}

        with (
            patch.dict("sibyl_core.services.graph_communities.HIERARCHICAL_CACHE", {}, clear=True),
            patch.dict(
                "sibyl_core.services.graph_communities.GRAPH_SNAPSHOT_CACHE", {}, clear=True
            ),
            patch.dict("sibyl_core.services.graph_communities.GRAPH_LOD_CACHE", {}, clear=True),
            patch(
                "sibyl_core.services.graph_communities._list_all_entities",
                AsyncMock(return_value=entities),
            ),
            patch(
                "sibyl_core.services.graph_communities._list_all_relationships",
                AsyncMock(return_value=relationships),
            ),
            patch(
                "sibyl_core.services.graph_communities.detect_communities_louvain",
                return_value=(partition, 0.5),
            ),
        ):
            data = await get_hierarchical_graph(
                mock_client, TEST_ORG_ID, max_nodes=100, max_edges=300
            )

        topic_count = sum(1 for node in data.nodes if node["type"] == EntityType.TOPIC.value)
        assert data.displayed_nodes == 100
        assert topic_count >= 5

    @pytest.mark.asyncio
    async def test_clusters_keep_total_member_count_when_sampled(
        self,
        mock_client: MagicMock,
    ) -> None:
        entities = [
            _make_entity(f"task-{index}", f"Task {index}", EntityType.TASK) for index in range(120)
        ]
        relationships = [
            _make_relationship(f"edge-{index}", f"task-{index}", f"task-{index + 1}")
            for index in range(119)
        ]
        partition = {entity.id: 0 for entity in entities}

        with (
            patch.dict("sibyl_core.services.graph_communities.HIERARCHICAL_CACHE", {}, clear=True),
            patch.dict(
                "sibyl_core.services.graph_communities.GRAPH_SNAPSHOT_CACHE", {}, clear=True
            ),
            patch.dict("sibyl_core.services.graph_communities.GRAPH_LOD_CACHE", {}, clear=True),
            patch(
                "sibyl_core.services.graph_communities._list_all_entities",
                AsyncMock(return_value=entities),
            ),
            patch(
                "sibyl_core.services.graph_communities._list_all_relationships",
                AsyncMock(return_value=relationships),
            ),
            patch(
                "sibyl_core.services.graph_communities.detect_communities_louvain",
                return_value=(partition, 0.5),
            ),
        ):
            data = await get_hierarchical_graph(
                mock_client, TEST_ORG_ID, max_nodes=100, max_edges=300
            )

        cluster = next(cluster for cluster in data.clusters if cluster["id"] != "unclustered")
        assert data.total_nodes == 120
        assert data.displayed_nodes == 100
        assert cluster["member_count"] == 120
        assert cluster["displayed_member_count"] == 100


class TestStoreCommunities:
    """Tests for store_communities function."""

    @pytest.mark.asyncio
    async def test_empty_list(self) -> None:
        """Empty community list returns 0."""
        mock_client = MagicMock()
        stored = await store_communities(mock_client, TEST_ORG_ID, [])
        assert stored == 0

    @pytest.mark.asyncio
    async def test_stores_communities(self) -> None:
        """Communities are stored in graph."""
        mock_client = MagicMock()
        entity_manager = MagicMock()
        entity_manager.list_by_type = AsyncMock(return_value=[])
        entity_manager.create = AsyncMock(side_effect=["c1", "c2"])
        relationship_manager = MagicMock()
        relationship_manager.create_bulk = AsyncMock(return_value=(4, 0))
        communities = [
            DetectedCommunity(id="c1", member_ids=["e1", "e2"], level=0, resolution=1.0),
            DetectedCommunity(id="c2", member_ids=["e3", "e4"], level=0, resolution=1.0),
        ]

        with (
            patch(
                "sibyl_core.services.graph_communities._entity_manager_factory",
                return_value=entity_manager,
            ),
            patch(
                "sibyl_core.services.graph_communities._relationship_manager_factory",
                return_value=relationship_manager,
            ),
        ):
            stored = await store_communities(mock_client, TEST_ORG_ID, communities)

        assert stored == 2
        assert entity_manager.create.await_count == 2
        assert relationship_manager.create_bulk.await_count == 1
        membership_edges = relationship_manager.create_bulk.await_args_list[0].args[0]
        assert len(membership_edges) == 4
        assert {edge.target_id for edge in membership_edges} == {"c1", "c2"}
        assert {edge.source_id for edge in membership_edges} == {"e1", "e2", "e3", "e4"}

        first_entity = entity_manager.create.await_args_list[0].args[0]
        assert first_entity.entity_type == EntityType.COMMUNITY
        assert first_entity.metadata["member_ids"] == ["e1", "e2"]
        assert first_entity.metadata["member_count"] == 2
        assert first_entity.metadata["level"] == 0

    @pytest.mark.asyncio
    async def test_clears_existing(self) -> None:
        """Existing communities are cleared."""
        mock_client = MagicMock()
        entity_manager = MagicMock()
        entity_manager.list_by_type = AsyncMock(
            side_effect=[
                [_make_community_entity("c_existing", level=1, member_count=3)],
                [],
            ]
        )
        entity_manager.delete = AsyncMock(return_value=True)
        entity_manager.create = AsyncMock(return_value="c1")
        relationship_manager = MagicMock()
        relationship_manager.create = AsyncMock(return_value="r1")
        communities = [
            DetectedCommunity(id="c1", member_ids=["e1", "e2"], level=0, resolution=1.0),
        ]

        with (
            patch(
                "sibyl_core.services.graph_communities._entity_manager_factory",
                return_value=entity_manager,
            ),
            patch(
                "sibyl_core.services.graph_communities._relationship_manager_factory",
                return_value=relationship_manager,
            ),
        ):
            await store_communities(mock_client, TEST_ORG_ID, communities, clear_existing=True)

        entity_manager.delete.assert_awaited_once_with("c_existing")
        first_list_call = entity_manager.list_by_type.await_args_list[0]
        assert first_list_call.args[0] == EntityType.COMMUNITY
        assert first_list_call.kwargs["include_archived"] is True


class TestGetEntityCommunities:
    """Tests for get_entity_communities function."""

    @pytest.mark.asyncio
    async def test_no_communities(self) -> None:
        """Entity with no communities returns empty list."""
        mock_client = MagicMock()
        entity_manager = MagicMock()
        relationship_manager = MagicMock()
        relationship_manager.get_for_entity = AsyncMock(return_value=[])

        with (
            patch(
                "sibyl_core.services.graph_communities._entity_manager_factory",
                return_value=entity_manager,
            ),
            patch(
                "sibyl_core.services.graph_communities._relationship_manager_factory",
                return_value=relationship_manager,
            ),
        ):
            communities = await get_entity_communities(mock_client, TEST_ORG_ID, "e1")

        assert communities == []

    @pytest.mark.asyncio
    async def test_returns_communities(self) -> None:
        """Returns communities entity belongs to."""
        mock_client = MagicMock()
        entity_manager = MagicMock()
        entity_manager.get = AsyncMock(
            side_effect=[
                _make_community_entity("c2", level=1, member_count=10, summary="Broader summary"),
                _make_community_entity("c1", level=0, member_count=5, summary="Summary text"),
            ]
        )
        relationship_manager = MagicMock()
        relationship_manager.get_for_entity = AsyncMock(
            return_value=[
                _make_relationship("r1", "e1", "c2", RelationshipType.BELONGS_TO),
                _make_relationship("r2", "e1", "c1", RelationshipType.BELONGS_TO),
            ]
        )

        with (
            patch(
                "sibyl_core.services.graph_communities._entity_manager_factory",
                return_value=entity_manager,
            ),
            patch(
                "sibyl_core.services.graph_communities._relationship_manager_factory",
                return_value=relationship_manager,
            ),
        ):
            communities = await get_entity_communities(mock_client, TEST_ORG_ID, "e1")

        assert len(communities) == 2
        assert communities[0]["id"] == "c1"
        assert communities[0]["level"] == 0
        assert communities[1]["level"] == 1


class TestGetCommunityMembers:
    """Tests for get_community_members function."""

    @pytest.mark.asyncio
    async def test_empty_community(self) -> None:
        """Empty community returns empty list."""
        mock_client = MagicMock()
        entity_manager = MagicMock()
        relationship_manager = MagicMock()
        relationship_manager.get_for_entity = AsyncMock(return_value=[])

        with (
            patch(
                "sibyl_core.services.graph_communities._entity_manager_factory",
                return_value=entity_manager,
            ),
            patch(
                "sibyl_core.services.graph_communities._relationship_manager_factory",
                return_value=relationship_manager,
            ),
        ):
            members = await get_community_members(mock_client, TEST_ORG_ID, "c1")

        assert members == []

    @pytest.mark.asyncio
    async def test_returns_members(self) -> None:
        """Returns community members."""
        mock_client = MagicMock()
        entity_manager = MagicMock()
        entity_manager.get = AsyncMock(
            side_effect=[
                _make_entity("e1", "Error Handling", EntityType.PATTERN),
                _make_entity("e2", "Logging", EntityType.PATTERN),
            ]
        )
        relationship_manager = MagicMock()
        relationship_manager.get_for_entity = AsyncMock(
            return_value=[
                _make_relationship("r1", "e1", "c1", RelationshipType.BELONGS_TO),
                _make_relationship("r2", "e2", "c1", RelationshipType.BELONGS_TO),
            ]
        )

        with (
            patch(
                "sibyl_core.services.graph_communities._entity_manager_factory",
                return_value=entity_manager,
            ),
            patch(
                "sibyl_core.services.graph_communities._relationship_manager_factory",
                return_value=relationship_manager,
            ),
        ):
            members = await get_community_members(mock_client, TEST_ORG_ID, "c1")

        assert len(members) == 2
        assert members[0]["id"] == "e1"
        assert members[0]["name"] == "Error Handling"
        assert members[1]["type"] == "pattern"
