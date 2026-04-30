"""Surreal-native Graphiti search interface tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from graphiti_core import Graphiti
from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.edges import EntityEdge, EpisodicEdge
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.nodes import CommunityNode, EntityNode, EpisodeType, EpisodicNode
from graphiti_core.search.search_filters import ComparisonOperator, DateFilter, SearchFilters
from graphiti_core.search.search_utils import get_embeddings_for_edges, get_embeddings_for_nodes

from sibyl_core.backends.surreal import SurrealDriver
from sibyl_core.backends.surreal.schema import EMBEDDING_DIM
from sibyl_core.graph.mock_llm import MockLLMClient
from sibyl_core.graph.search_interface import SurrealSearchInterface


class _FakeEmbedder(EmbedderClient):
    async def create(self, input_data: object) -> list[float]:
        return [0.0] * EMBEDDING_DIM

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        return [[0.0] * EMBEDDING_DIM for _ in input_data_list]


class _FakeCrossEncoder(CrossEncoderClient):
    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        return [(passage, 0.0) for passage in passages]


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


def _mention(uuid: str, group_id: str, source: str, target: str) -> EpisodicEdge:
    return EpisodicEdge(
        uuid=uuid,
        group_id=group_id,
        source_node_uuid=source,
        target_node_uuid=target,
        created_at=datetime.now(UTC),
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

    @pytest.mark.asyncio
    async def test_graphiti_model_methods_use_surreal_graph_operations_interface(
        self, surreal_schema: SurrealDriver
    ) -> None:
        gid = surreal_schema.group_id
        source = _entity("graphiti-source", gid, name="Graphiti Source", summary="source")
        target = _entity("graphiti-target", gid, name="Graphiti Target", summary="target")
        edge = _edge(
            "graphiti-edge",
            gid,
            "graphiti-source",
            "graphiti-target",
            fact="Graphiti native operation edge",
        )
        episode = _episode(
            "graphiti-episode",
            gid,
            content="Graphiti native operation episode",
        )

        await source.save(surreal_schema)
        await target.save(surreal_schema)
        await edge.save(surreal_schema)
        await episode.save(surreal_schema)

        loaded_source = await EntityNode.get_by_uuid(surreal_schema, "graphiti-source")
        loaded_edge = await EntityEdge.get_by_uuid(surreal_schema, "graphiti-edge")
        recent_episodes = await surreal_schema.graph_operations_interface.retrieve_episodes(
            surreal_schema,
            datetime.now(UTC),
            1,
            [gid],
        )
        node_embeddings = await get_embeddings_for_nodes(surreal_schema, [loaded_source])
        edge_embeddings = await get_embeddings_for_edges(surreal_schema, [loaded_edge])

        assert loaded_source.uuid == "graphiti-source"
        assert loaded_edge.uuid == "graphiti-edge"
        assert [episode.uuid for episode in recent_episodes] == ["graphiti-episode"]
        assert node_embeddings["graphiti-source"] == [0.1] * EMBEDDING_DIM
        assert edge_embeddings["graphiti-edge"] == [0.1] * EMBEDDING_DIM

    @pytest.mark.asyncio
    async def test_graphiti_bulk_dict_payloads_use_surreal_graph_operations_interface(
        self, surreal_schema: SurrealDriver
    ) -> None:
        gid = surreal_schema.group_id
        now = datetime.now(UTC)
        interface = surreal_schema.graph_operations_interface

        await interface.node_save_bulk(
            None,
            surreal_schema,
            None,
            [
                {
                    "uuid": "bulk-source",
                    "name": "Bulk Source",
                    "group_id": gid,
                    "labels": ["Entity", "Tool"],
                    "summary": "source",
                    "created_at": now,
                    "name_embedding": [0.1] * EMBEDDING_DIM,
                    "project_id": "project-bulk",
                },
                {
                    "uuid": "bulk-target",
                    "name": "Bulk Target",
                    "group_id": gid,
                    "labels": ["Entity", "Tool"],
                    "summary": "target",
                    "created_at": now,
                    "name_embedding": [0.1] * EMBEDDING_DIM,
                },
            ],
        )
        await interface.edge_save_bulk(
            None,
            surreal_schema,
            None,
            [
                {
                    "uuid": "bulk-edge",
                    "source_node_uuid": "bulk-source",
                    "target_node_uuid": "bulk-target",
                    "name": "RELATES_TO",
                    "fact": "Bulk source relates to bulk target",
                    "fact_embedding": [0.1] * EMBEDDING_DIM,
                    "group_id": gid,
                    "episodes": [],
                    "created_at": now,
                    "expired_at": None,
                    "valid_at": now,
                    "invalid_at": None,
                    "confidence": 0.9,
                }
            ],
        )

        source = await EntityNode.get_by_uuid(surreal_schema, "bulk-source")
        edge = await EntityEdge.get_by_uuid(surreal_schema, "bulk-edge")

        assert source.attributes["project_id"] == "project-bulk"
        assert edge.attributes["confidence"] == 0.9

    @pytest.mark.asyncio
    async def test_graphiti_add_episode_with_saga_uses_surreal_bulk_adapter(
        self, surreal_schema: SurrealDriver
    ) -> None:
        graph = Graphiti(
            graph_driver=surreal_schema,
            llm_client=MockLLMClient(),
            embedder=_FakeEmbedder(),
            cross_encoder=_FakeCrossEncoder(),
        )

        result = await graph.add_episode(
            name="Graphiti add episode",
            episode_body="Bliss is testing Surreal add_episode compatibility.",
            source_description="test",
            reference_time=datetime(2026, 1, 1, tzinfo=UTC),
            source=EpisodeType.message,
            group_id=surreal_schema.group_id,
            saga="daily",
        )

        episodes = await surreal_schema.execute_query("SELECT uuid, name FROM episode;")
        sagas = await surreal_schema.execute_query("SELECT uuid, name FROM saga;")
        saga_edges = await surreal_schema.execute_query("SELECT uuid FROM has_episode;")

        assert result.episode.name == "Graphiti add episode"
        assert [episode["uuid"] for episode in episodes] == [result.episode.uuid]
        assert [saga["name"] for saga in sagas] == ["daily"]
        assert len(saga_edges) == 1

    @pytest.mark.asyncio
    async def test_bfs_and_rerankers_use_native_surreal_queries(
        self, surreal_schema: SurrealDriver
    ) -> None:
        gid = surreal_schema.group_id
        interface = SurrealSearchInterface()
        for node in (
            _entity("bfs-origin", gid, name="Origin", summary="origin"),
            _entity("bfs-mid", gid, name="Middle", summary="middle"),
            _entity("bfs-target", gid, name="Target", summary="target"),
            _entity("bfs-cross-group", "other-group", name="Cross", summary="cross"),
        ):
            await surreal_schema.entity_node_ops.save(surreal_schema, node)
        for episode in (
            _episode("bfs-episode-a", gid, content="mentions middle"),
            _episode("bfs-episode-b", gid, content="mentions middle again"),
            _episode("bfs-episode-c", gid, content="mentions target"),
        ):
            await surreal_schema.episode_node_ops.save(surreal_schema, episode)
        for edge in (
            _edge("bfs-edge-1", gid, "bfs-origin", "bfs-mid", fact="origin to middle"),
            _edge("bfs-edge-2", gid, "bfs-mid", "bfs-target", fact="middle to target"),
            _edge(
                "bfs-cross-edge",
                gid,
                "bfs-origin",
                "bfs-cross-group",
                fact="cross group leak candidate",
            ),
        ):
            await surreal_schema.entity_edge_ops.save(surreal_schema, edge)
        for mention in (
            _mention("mention-a", gid, "bfs-episode-a", "bfs-mid"),
            _mention("mention-b", gid, "bfs-episode-b", "bfs-mid"),
            _mention("mention-c", gid, "bfs-episode-c", "bfs-target"),
            _mention("mention-cross", gid, "bfs-episode-a", "bfs-cross-group"),
        ):
            await surreal_schema.episodic_edge_ops.save(surreal_schema, mention)

        nodes_from_entity = await interface.node_bfs_search(
            surreal_schema,
            ["bfs-origin"],
            SearchFilters(node_labels=["Pattern"]),
            2,
            [gid],
            5,
        )
        nodes_from_episode = await interface.node_bfs_search(
            surreal_schema,
            ["bfs-episode-a"],
            SearchFilters(node_labels=["Pattern"]),
            2,
            [gid],
            5,
        )
        edges_from_entity = await interface.edge_bfs_search(
            surreal_schema,
            ["bfs-origin"],
            2,
            SearchFilters(),
            [gid],
            5,
        )
        edges_from_episode = await interface.edge_bfs_search(
            surreal_schema,
            ["bfs-episode-a"],
            2,
            SearchFilters(),
            [gid],
            5,
        )
        distance_uuids, distance_scores = await interface.node_distance_reranker(
            surreal_schema,
            ["bfs-target", "bfs-mid", "bfs-origin"],
            "bfs-origin",
        )
        mention_uuids, mention_scores = await interface.episode_mentions_reranker(
            surreal_schema,
            [["bfs-target", "bfs-mid"], ["bfs-mid"]],
            1,
        )
        scoped_distance_uuids, scoped_distance_scores = await interface.node_distance_reranker(
            surreal_schema,
            ["bfs-mid", "bfs-cross-group"],
            "bfs-origin",
            1,
        )
        scoped_mention_uuids, scoped_mention_scores = await interface.episode_mentions_reranker(
            surreal_schema,
            [["bfs-mid", "bfs-cross-group"]],
            1,
        )

        assert [node.uuid for node in nodes_from_entity] == ["bfs-mid", "bfs-target"]
        assert [node.uuid for node in nodes_from_episode] == ["bfs-mid", "bfs-target"]
        assert [edge.uuid for edge in edges_from_entity] == ["bfs-edge-1", "bfs-edge-2"]
        assert [edge.uuid for edge in edges_from_episode] == ["bfs-edge-2"]
        assert distance_uuids == ["bfs-origin", "bfs-mid", "bfs-target"]
        assert distance_scores == [0.1, 1.0, 0.0]
        assert mention_uuids == ["bfs-mid", "bfs-target"]
        assert mention_scores == [2.0, 1.0]
        assert scoped_distance_uuids == ["bfs-mid"]
        assert scoped_distance_scores == [1.0]
        assert scoped_mention_uuids == ["bfs-mid"]
        assert scoped_mention_scores == [2.0]
