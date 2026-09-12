"""Fresh-process qualification for an already accepted native capture job."""

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from sibyl.jobs import entities as jobs
from sibyl.jobs.lifecycle_repair import resolve_source_authority
from sibyl_core.backends.surreal import SurrealAuthClient, SurrealContentClient
from sibyl_core.backends.surreal.schema import EMBEDDING_DIM
from sibyl_core.embeddings.providers import DeterministicEmbeddingProvider, EmbeddingMetadata
from sibyl_core.runtime_ports import install_source_authority_resolver
from sibyl_core.services.graph_client import SurrealGraphClient
from sibyl_core.services.graph_entities import EntityManager
from sibyl_core.services.graph_relationships import RelationshipManager
from sibyl_core.services.graph_runtime import GraphRuntime


async def resume(values):
    auth = SurrealAuthClient(url=values["url"], namespace=values["auth"])
    content = SurrealContentClient(url=values["url"], namespace=values["content"])
    graph = SurrealGraphClient(
        url=values["url"], group_id=values["org"], namespace_prefix=values["graph_prefix"]
    )
    calls = []

    class Spy(DeterministicEmbeddingProvider):
        async def embed_texts(self, texts, *, input_kind="document"):
            calls.extend(texts)
            return await super().embed_texts(texts, input_kind=input_kind)

    provider = Spy(EmbeddingMetadata("test", "one", EMBEDDING_DIM, "test", "bytes"))
    runtime = GraphRuntime(
        client=graph,
        entity_manager=EntityManager(graph, group_id=values["org"], embedding_provider=provider),
        relationship_manager=RelationshipManager(
            graph, group_id=values["org"], embedding_provider=provider
        ),
    )

    @asynccontextmanager
    async def scope():
        yield content

    install_source_authority_resolver(resolve_source_authority)
    try:
        with (
            patch(
                "sibyl.persistence.surreal.auth.get_shared_surreal_auth_client",
                AsyncMock(return_value=auth),
            ),
            patch("sibyl.persistence.surreal.auth.build_surreal_auth_client", lambda: auth),
            patch("sibyl_core.services.content_client.surreal_content_client", scope),
            patch(
                "sibyl_core.services.graph_runtime.get_surreal_graph_runtime",
                AsyncMock(return_value=runtime),
            ),
            patch(
                "sibyl_core.services.graph.get_surreal_graph_client", AsyncMock(return_value=graph)
            ),
            patch.object(jobs, "get_surreal_graph_runtime", AsyncMock(return_value=runtime)),
            patch.object(jobs, "configured_embedding_provider", lambda: provider),
        ):
            result = await jobs.backfill_entity_embeddings({}, *values["args"], **values["kwargs"])
            assert result["manifest_state"] in {"complete", "completed"}, result
            sys.stdout.write(
                "OWNED_RESULT="
                + json.dumps({"state": result["manifest_state"], "calls": len(calls)})
                + "\n"
            )
    finally:
        await graph.close()
        await content.close()
        await auth.close()


if __name__ == "__main__":
    asyncio.run(resume(json.load(sys.stdin)))
