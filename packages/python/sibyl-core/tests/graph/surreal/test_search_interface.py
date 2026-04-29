"""Surreal-native Graphiti search interface tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import CommunityNode, EntityNode, EpisodeType, EpisodicNode
from graphiti_core.search.search_filters import ComparisonOperator, DateFilter, SearchFilters

from sibyl_core.backends.surreal import SurrealDriver
from sibyl_core.backends.surreal.schema import EMBEDDING_DIM
from sibyl_core.graph.search_interface import SurrealSearchInterface


def _entity(uuid: str, group_id: str, *, name: str, summary: str) -> EntityNode:
    return EntityNode(
        uuid=uuid,
        name=name,
        name_embedding=[0.1] * EMBEDDING_DIM,
        group_id=group_id,
        labels=["Entity", "Pattern"],
        created_at=datetime.now(UTC),
        summary=summary,
        attributes={},
    )


def _edge(uuid: str, group_id: str, source: str, target: str, *, fact: str) -> EntityEdge:
    now = datetime.now(UTC)
    return EntityEdge(
        uuid=uuid,
        source_node_uuid=source,
        target_node_uuid=target,
        fact=fact,
        fact_embedding=[0.1] * EMBEDDING_DIM,
        name="RELATES_TO",
        group_id=group_id,
        episodes=[],
        created_at=now,
        expired_at=None,
        valid_at=now,
        invalid_at=None,
        attributes={},
    )


def _episode(uuid: str, group_id: str, *, content: str) -> EpisodicNode:
    now = datetime.now(UTC)
    return EpisodicNode(
        uuid=uuid,
        name="Surreal episode",
        content=content,
        source=EpisodeType.text,
        source_description="",
        group_id=group_id,
        created_at=now,
        valid_at=now,
        entity_edges=[],
    )


def _community(uuid: str, group_id: str, *, name: str, summary: str) -> CommunityNode:
    return CommunityNode(
        uuid=uuid,
        name=name,
        group_id=group_id,
        summary=summary,
        name_embedding=[0.2] * EMBEDDING_DIM,
        created_at=datetime.now(UTC),
    )


class TestSurrealSearchInterfaceIntegration:
    @pytest.mark.asyncio
    async def test_node_fulltext_and_similarity_search(self, surreal_schema: SurrealDriver) -> None:
        gid = surreal_schema.group_id
        interface = SurrealSearchInterface()
        await surreal_schema.entity_node_ops.save(
            surreal_schema,
            _entity("node-search", gid, name="Surreality Prism", summary="northstar memory"),
        )

        fulltext = await interface.node_fulltext_search(
            surreal_schema,
            "surreality",
            SearchFilters(node_labels=["Pattern"]),
            [gid],
            5,
        )
        similarity = await interface.node_similarity_search(
            surreal_schema,
            [0.1] * EMBEDDING_DIM,
            SearchFilters(node_labels=["Pattern"]),
            [gid],
            5,
            0.0,
        )

        assert [node.uuid for node in fulltext] == ["node-search"]
        assert [node.uuid for node in similarity] == ["node-search"]

    @pytest.mark.asyncio
    async def test_edge_fulltext_and_similarity_search(self, surreal_schema: SurrealDriver) -> None:
        gid = surreal_schema.group_id
        interface = SurrealSearchInterface()
        await surreal_schema.entity_node_ops.save(
            surreal_schema,
            _entity("source-node", gid, name="Source", summary="source"),
        )
        await surreal_schema.entity_node_ops.save(
            surreal_schema,
            _entity("target-node", gid, name="Target", summary="target"),
        )
        await surreal_schema.entity_edge_ops.save(
            surreal_schema,
            _edge(
                "edge-search",
                gid,
                "source-node",
                "target-node",
                fact="Surreality links coding memory",
            ),
        )

        fulltext = await interface.edge_fulltext_search(
            surreal_schema,
            "surreality",
            SearchFilters(edge_uuids=["edge-search"], node_labels=["Pattern"]),
            [gid],
            5,
        )
        similarity = await interface.edge_similarity_search(
            surreal_schema,
            [0.1] * EMBEDDING_DIM,
            "source-node",
            "target-node",
            SearchFilters(
                edge_uuids=["edge-search"],
                valid_at=[
                    [
                        DateFilter(
                            date=datetime(2100, 1, 1, tzinfo=UTC),
                            comparison_operator=ComparisonOperator.less_than_equal,
                        )
                    ]
                ],
            ),
            [gid],
            5,
            0.0,
        )
        filtered_out = await interface.edge_similarity_search(
            surreal_schema,
            [0.1] * EMBEDDING_DIM,
            "source-node",
            "target-node",
            SearchFilters(
                edge_uuids=["edge-search"],
                valid_at=[
                    [
                        DateFilter(
                            date=datetime(2020, 1, 1, tzinfo=UTC),
                            comparison_operator=ComparisonOperator.less_than,
                        )
                    ]
                ],
            ),
            [gid],
            5,
            0.0,
        )

        assert [edge.uuid for edge in fulltext] == ["edge-search"]
        assert [edge.uuid for edge in similarity] == ["edge-search"]
        assert filtered_out == []

    @pytest.mark.asyncio
    async def test_episode_fulltext_search(self, surreal_schema: SurrealDriver) -> None:
        gid = surreal_schema.group_id
        interface = SurrealSearchInterface()
        await surreal_schema.episode_node_ops.save(
            surreal_schema,
            _episode("episode-search", gid, content="Surreality captures raw memory"),
        )

        results = await interface.episode_fulltext_search(
            surreal_schema,
            "surreality",
            SearchFilters(),
            [gid],
            5,
        )

        assert [episode.uuid for episode in results] == ["episode-search"]

    @pytest.mark.asyncio
    async def test_community_fulltext_similarity_and_embedding_load(
        self, surreal_schema: SurrealDriver
    ) -> None:
        gid = surreal_schema.group_id
        interface = SurrealSearchInterface()
        community = _community(
            "community-search",
            gid,
            name="Surreality Guild",
            summary="native community search",
        )
        await surreal_schema.community_node_ops.save(surreal_schema, community)

        fulltext = await interface.community_fulltext_search(
            surreal_schema,
            "surreality",
            [gid],
            5,
        )
        similarity = await interface.community_similarity_search(
            surreal_schema,
            [0.2] * EMBEDDING_DIM,
            [gid],
            5,
            0.0,
        )
        embeddings = await interface.get_embeddings_for_communities(
            surreal_schema,
            [community],
        )
        blank_fulltext = await interface.community_fulltext_search(
            surreal_schema,
            "   ",
            [gid],
            5,
        )
        blank_similarity = await interface.community_similarity_search(
            surreal_schema,
            [],
            [gid],
            5,
            0.0,
        )
        wrong_group = await interface.community_fulltext_search(
            surreal_schema,
            "surreality",
            ["other-group"],
            5,
        )

        assert [community.uuid for community in fulltext] == ["community-search"]
        assert [community.uuid for community in similarity] == ["community-search"]
        assert embeddings["community-search"] == [0.2] * EMBEDDING_DIM
        assert blank_fulltext == []
        assert blank_similarity == []
        assert wrong_group == []
