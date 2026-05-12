from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from graphiti_core.search.search_filters import SearchFilters

import sibyl_core.retrieval.native as native_retrieval
from sibyl_core.backends.surreal import SurrealDriver
from sibyl_core.backends.surreal.content_client import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.backends.surreal.schema import EMBEDDING_DIM
from sibyl_core.graph.search_interface import SurrealSearchInterface
from sibyl_core.services.surreal_content import RawMemory, recall_raw_memory, remember_raw_memory
from sibyl_core.tools.context import compile_context, context_pack_to_markdown


@pytest.mark.asyncio
async def test_native_surrealql_memory_path_renders_context_pack(
    surreal_schema: SurrealDriver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content_client = SurrealContentClient(url="memory://")
    await bootstrap_content_schema(content_client, reset=True)

    @asynccontextmanager
    async def client_session() -> AsyncIterator[SurrealContentClient]:
        yield content_client

    from sibyl_core.services import surreal_content as content_service

    monkeypatch.setattr(content_service, "surreal_content_client", client_session)

    gid = surreal_schema.group_id
    now = datetime.now(UTC)
    embedding = [0.1] * EMBEDDING_DIM
    try:
        raw_memory = await remember_raw_memory(
            organization_id=gid,
            principal_id="user-native",
            source_id="cli:remember:native-spike",
            raw_content=(
                "Surreal native context pack source rendering stores raw memory "
                "before graph extraction."
            ),
            title="Native Surreal context pack source rendering",
            memory_scope="project",
            scope_key="project_native",
            metadata={"project_id": "project_native"},
            provenance={"fixture": "native_surrealql_spike"},
            capture_surface="cli",
        )

        await surreal_schema.execute_query(
            """
            CREATE entity CONTENT {
                uuid: $source_uuid,
                name: "Native RawMemory",
                entity_type: "artifact",
                summary: "direct SurrealQL source entity",
                labels: ["Entity", "Artifact"],
                attributes: {content: "Surreal native path raw source"},
                group_id: $group_id,
                project_id: "project_native",
                created_at: $now,
                name_embedding: $embedding
            };
            CREATE entity CONTENT {
                uuid: $target_uuid,
                name: "Native ContextPack",
                entity_type: "artifact",
                summary: "Surreal native context pack source rendering entity",
                labels: ["Entity", "Artifact"],
                attributes: {content: "Surreal native context pack source rendering"},
                group_id: $group_id,
                project_id: "project_native",
                created_at: $now,
                name_embedding: $embedding
            };
            CREATE entity CONTENT {
                uuid: $other_project_uuid,
                name: "Native Cross Project",
                entity_type: "artifact",
                summary: "Surreal native context pack source rendering leak candidate",
                labels: ["Entity", "Artifact"],
                attributes: {content: "Surreal native context pack source rendering"},
                group_id: $group_id,
                project_id: "project_other",
                created_at: $now,
                name_embedding: $embedding
            };
            CREATE episode CONTENT {
                uuid: $episode_uuid,
                name: "Native spike episode",
                source: "text",
                source_description: "native surrealql spike",
                content: "Surreal native path relates raw memory to context pack rendering.",
                labels: ["Episode"],
                group_id: $group_id,
                created_at: $now,
                valid_at: $now,
                entity_edges: [$edge_uuid]
            };
            LET $src = (SELECT VALUE id FROM entity WHERE uuid = $source_uuid LIMIT 1)[0];
            LET $tgt = (SELECT VALUE id FROM entity WHERE uuid = $target_uuid LIMIT 1)[0];
            RELATE $src->relates_to->$tgt SET
                uuid = $edge_uuid,
                name = "SUPPORTS",
                fact = "Native RawMemory supports ContextPack source rendering",
                fact_embedding = $embedding,
                group_id = $group_id,
                episodes = [$episode_uuid],
                attributes = {raw_memory_id: $raw_memory_id},
                created_at = $now,
                valid_at = $now;
            """,
            source_uuid="native-raw-memory",
            target_uuid="native-context-pack",
            other_project_uuid="native-cross-project",
            episode_uuid="native-episode",
            edge_uuid="native-edge",
            group_id=gid,
            now=now,
            embedding=embedding,
            raw_memory_id=raw_memory.id,
        )

        interface = SurrealSearchInterface()
        lexical_nodes = await interface.node_fulltext_search(
            surreal_schema,
            "RawMemory",
            SearchFilters(node_labels=["Artifact"]),
            [gid],
            5,
        )
        vector_edges = await interface.edge_similarity_search(
            surreal_schema,
            embedding,
            "native-raw-memory",
            "native-context-pack",
            SearchFilters(edge_uuids=["native-edge"]),
            [gid],
            5,
            0.0,
        )
        graph_nodes = await interface.node_bfs_search(
            surreal_schema,
            ["native-raw-memory"],
            SearchFilters(node_labels=["Artifact"]),
            1,
            [gid],
            5,
        )
        episode_results = await interface.episode_fulltext_search(
            surreal_schema,
            "context pack",
            SearchFilters(),
            [gid],
            5,
        )

        current_node = await surreal_schema.entity_node_ops.get_by_uuid(
            surreal_schema, "native-raw-memory"
        )
        current_edge = await surreal_schema.entity_edge_ops.get_by_uuid(
            surreal_schema, "native-edge"
        )
        current_episode = await surreal_schema.episode_node_ops.get_by_uuid(
            surreal_schema, "native-episode"
        )

        async def raw_recall(**kwargs: Any) -> list[RawMemory]:
            return await recall_raw_memory(**kwargs)

        class FakeEmbedder:
            async def create(self, _input_data: object) -> list[float]:
                return embedding

        fake_graph_client = SimpleNamespace(
            client=SimpleNamespace(embedder=FakeEmbedder()),
            get_org_driver=lambda _organization_id: surreal_schema,
        )

        async def fake_get_graph_runtime(_organization_id: str) -> SimpleNamespace:
            return SimpleNamespace(client=fake_graph_client)

        async def unexpected_search(**_kwargs: Any) -> None:
            raise AssertionError("Graphiti fallback search should not run in native mode")

        monkeypatch.setattr(native_retrieval, "get_graph_runtime", fake_get_graph_runtime)

        pack = await compile_context(
            "Surreal native context pack source rendering",
            intent="build",
            project="project_native",
            accessible_projects={"project_native"},
            organization_id=gid,
            principal_id="user-native",
            search_fn=unexpected_search,
            raw_memory_recall_fn=raw_recall,
            limit=6,
            related_limit=0,
            retrieval_mode="native",
        )
        markdown = context_pack_to_markdown(pack, max_items=6)

        assert [node.uuid for node in lexical_nodes] == ["native-raw-memory"]
        assert [edge.uuid for edge in vector_edges] == ["native-edge"]
        assert [node.uuid for node in graph_nodes] == ["native-context-pack"]
        assert [episode.uuid for episode in episode_results] == ["native-episode"]
        assert current_node.name == lexical_nodes[0].name
        assert current_edge.fact == vector_edges[0].fact
        assert current_episode.content == episode_results[0].content
        assert "native-context-pack" in markdown
        assert "native-cross-project" not in markdown
        assert "native-episode" not in markdown
        assert f"raw_memory:{raw_memory.id}" in markdown
        assert "src=cli:remember:native-spike" in markdown
        assert "preserves verbatim source context" in markdown
    finally:
        await content_client.close()
