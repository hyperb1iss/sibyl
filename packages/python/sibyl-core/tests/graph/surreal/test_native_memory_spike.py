from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from graphiti_core.search.search_filters import SearchFilters

from sibyl_core.backends.surreal import SurrealDriver
from sibyl_core.backends.surreal.content_client import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.backends.surreal.schema import EMBEDDING_DIM
from sibyl_core.graph.search_interface import SurrealSearchInterface
from sibyl_core.services.surreal_content import RawMemory, recall_raw_memory, remember_raw_memory
from sibyl_core.tools.context import compile_context, context_pack_to_markdown
from sibyl_core.tools.responses import SearchResponse, SearchResult


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
            organization_id="org-native-spike",
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
                summary: "direct SurrealQL context pack entity",
                labels: ["Entity", "Artifact"],
                attributes: {content: "context pack renders raw source ids"},
                group_id: $group_id,
                project_id: "project_native",
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

        async def native_search(**kwargs: Any) -> SearchResponse:
            query = kwargs["query"]
            if kwargs["types"] != ["session", "episode", "note"]:
                return SearchResponse(results=[], total=0, query=query, filters={})
            return SearchResponse(
                results=[
                    SearchResult(
                        id=lexical_nodes[0].uuid,
                        type="artifact",
                        name=lexical_nodes[0].name,
                        content=lexical_nodes[0].summary or "",
                        score=1.0,
                        source="surreal:native",
                        metadata={"entity_type": "artifact", "project_id": "project_native"},
                    )
                ],
                total=1,
                query=query,
                filters={},
                graph_count=1,
            )

        pack = await compile_context(
            "Surreal native context pack source rendering",
            intent="build",
            project="project_native",
            organization_id="org-native-spike",
            principal_id="user-native",
            search_fn=native_search,
            raw_memory_recall_fn=raw_recall,
            limit=6,
        )
        markdown = context_pack_to_markdown(pack, max_items=6)

        assert [node.uuid for node in lexical_nodes] == ["native-raw-memory"]
        assert [edge.uuid for edge in vector_edges] == ["native-edge"]
        assert [node.uuid for node in graph_nodes] == ["native-context-pack"]
        assert [episode.uuid for episode in episode_results] == ["native-episode"]
        assert current_node.name == lexical_nodes[0].name
        assert current_edge.fact == vector_edges[0].fact
        assert current_episode.content == episode_results[0].content
        assert f"raw_memory:{raw_memory.id}" in markdown
        assert "src=cli:remember:native-spike" in markdown
        assert "preserves verbatim source context" in markdown
    finally:
        await content_client.close()
