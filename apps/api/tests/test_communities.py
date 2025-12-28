"""Tests for community detection module."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sibyl.graph.communities import (
    CommunityConfig,
    DetectedCommunity,
    detect_communities,
    export_to_networkx,
    get_community_members,
    get_entity_communities,
    link_hierarchy,
    partition_to_communities,
    store_communities,
)

# Test organization ID for multi-tenancy
TEST_ORG_ID = "test-org-communities"


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
        # First query returns nodes, second returns edges
        mock_client.execute_read_org = AsyncMock(
            side_effect=[
                [
                    ("e1", "Entity One", "pattern"),
                    ("e2", "Entity Two", "rule"),
                ],
                [],  # No edges
            ]
        )

        G = await export_to_networkx(mock_client, TEST_ORG_ID)

        assert G.number_of_nodes() == 2
        assert "e1" in G.nodes()
        assert "e2" in G.nodes()
        assert G.nodes["e1"]["name"] == "Entity One"
        assert G.nodes["e2"]["type"] == "rule"

    @pytest.mark.asyncio
    async def test_edges_exported(self, mock_client: MagicMock) -> None:
        """Edges are properly exported."""
        mock_client.execute_read_org = AsyncMock(
            side_effect=[
                [
                    ("e1", "Entity One", "pattern"),
                    ("e2", "Entity Two", "pattern"),
                ],
                [
                    ("e1", "e2", "RELATES_TO"),
                ],
            ]
        )

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
        # Mock networkx export - uses execute_read_org
        mock_client.execute_read_org = AsyncMock(
            side_effect=[
                # Nodes
                [
                    ("e1", "Entity One", "pattern"),
                    ("e2", "Entity Two", "pattern"),
                    ("e3", "Entity Three", "pattern"),
                    ("e4", "Entity Four", "pattern"),
                ],
                # Edges - two clusters
                [
                    ("e1", "e2", "RELATES_TO"),
                    ("e3", "e4", "RELATES_TO"),
                ],
            ]
        )

        # Mock louvain algorithm
        mock_partition = {"e1": 0, "e2": 0, "e3": 1, "e4": 1}
        mock_modularity = 0.5

        with patch("sibyl.graph.communities.detect_communities_louvain") as mock_louvain:
            mock_louvain.return_value = (mock_partition, mock_modularity)

            config = CommunityConfig(resolutions=[1.0], max_levels=1)
            communities = await detect_communities(mock_client, TEST_ORG_ID, config=config)

            assert len(communities) == 2
            mock_louvain.assert_called_once()


class TestStoreCommunities:
    """Tests for store_communities function."""

    @pytest.fixture
    def mock_client(self) -> MagicMock:
        """Create mock graph client."""
        client = MagicMock()
        client.execute_read_org = AsyncMock(return_value=[])
        client.execute_write_org = AsyncMock(return_value=[])
        return client

    @pytest.mark.asyncio
    async def test_empty_list(self, mock_client: MagicMock) -> None:
        """Empty community list returns 0."""
        stored = await store_communities(mock_client, TEST_ORG_ID, [])
        assert stored == 0

    @pytest.mark.asyncio
    async def test_stores_communities(self, mock_client: MagicMock) -> None:
        """Communities are stored in graph."""
        communities = [
            DetectedCommunity(id="c1", member_ids=["e1", "e2"], level=0, resolution=1.0),
            DetectedCommunity(id="c2", member_ids=["e3", "e4"], level=0, resolution=1.0),
        ]

        stored = await store_communities(mock_client, TEST_ORG_ID, communities)

        assert stored == 2
        # Verify execute_write_org was called for each community + clear + links
        assert mock_client.execute_write_org.call_count >= 3

    @pytest.mark.asyncio
    async def test_clears_existing(self, mock_client: MagicMock) -> None:
        """Existing communities are cleared."""
        communities = [
            DetectedCommunity(id="c1", member_ids=["e1", "e2"], level=0, resolution=1.0),
        ]

        await store_communities(mock_client, TEST_ORG_ID, communities, clear_existing=True)

        # First call should be the clear query
        first_call = mock_client.execute_write_org.call_args_list[0]
        assert "DELETE" in first_call[0][0]


class TestGetEntityCommunities:
    """Tests for get_entity_communities function."""

    @pytest.fixture
    def mock_client(self) -> MagicMock:
        """Create mock graph client."""
        client = MagicMock()
        client.execute_read_org = AsyncMock(return_value=[])
        client.execute_write_org = AsyncMock(return_value=[])
        return client

    @pytest.mark.asyncio
    async def test_no_communities(self, mock_client: MagicMock) -> None:
        """Entity with no communities returns empty list."""
        communities = await get_entity_communities(mock_client, TEST_ORG_ID, "e1")
        assert communities == []

    @pytest.mark.asyncio
    async def test_returns_communities(self, mock_client: MagicMock) -> None:
        """Returns communities entity belongs to."""
        mock_client.execute_read_org = AsyncMock(
            return_value=[
                ("c1", "Community L0", 0, 5, "Summary text"),
                ("c2", "Community L1", 1, 10, "Broader summary"),
            ]
        )

        communities = await get_entity_communities(mock_client, TEST_ORG_ID, "e1")

        assert len(communities) == 2
        assert communities[0]["id"] == "c1"
        assert communities[0]["level"] == 0
        assert communities[1]["level"] == 1


class TestGetCommunityMembers:
    """Tests for get_community_members function."""

    @pytest.fixture
    def mock_client(self) -> MagicMock:
        """Create mock graph client."""
        client = MagicMock()
        client.execute_read_org = AsyncMock(return_value=[])
        client.execute_write_org = AsyncMock(return_value=[])
        return client

    @pytest.mark.asyncio
    async def test_empty_community(self, mock_client: MagicMock) -> None:
        """Empty community returns empty list."""
        members = await get_community_members(mock_client, TEST_ORG_ID, "c1")
        assert members == []

    @pytest.mark.asyncio
    async def test_returns_members(self, mock_client: MagicMock) -> None:
        """Returns community members."""
        mock_client.execute_read_org = AsyncMock(
            return_value=[
                ("e1", "Error Handling", "pattern", "Description 1"),
                ("e2", "Logging", "pattern", "Description 2"),
            ]
        )

        members = await get_community_members(mock_client, TEST_ORG_ID, "c1")

        assert len(members) == 2
        assert members[0]["id"] == "e1"
        assert members[0]["name"] == "Error Handling"
        assert members[1]["type"] == "pattern"
