"""Application wiring for the document chunk plane of the embedding sweep.

Chunks are embedded by the crawler's embedding service, so the sweep embeds
them through the same service: whatever provider it is configured for, the
sweep produces exactly the vectors and stamp a fresh crawl would.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import structlog

from sibyl.crawler.chunker import Chunk
from sibyl.crawler.embedder import EmbeddingService
from sibyl_core.embeddings.providers import configured_embedding_provider
from sibyl_core.services import content_client
from sibyl_core.services.document_embedding_sweep import ChunkEmbedder
from sibyl_core.services.embedding_evidence import record_deployment_models
from sibyl_core.services.embedding_sweep import EmbeddingStamp, SweepRow

log = structlog.get_logger()


def chunk_rows_embedder(service: EmbeddingService) -> ChunkEmbedder:
    """Embed swept chunk rows with the contextual text the crawler embeds."""

    async def embed(rows: Sequence[SweepRow]) -> tuple[list[list[float]], EmbeddingStamp]:
        chunks = [
            Chunk(
                content=str(row.get("content") or ""),
                context=row.get("context") or None,
                heading_path=[str(part) for part in row.get("heading_path") or []],
            )
            for row in rows
        ]
        vectors, stamp = await service.embed_chunks_with_metadata(chunks)
        return [[float(value) for value in vector] for vector in vectors], dict(stamp)

    return embed


async def document_chunk_sweep_inputs(
    service: EmbeddingService | None = None,
) -> tuple[dict[str, Any], bool, ChunkEmbedder]:
    """The stamp chunk writes carry today, whether it can embed, and the embedder."""
    embedder = service or EmbeddingService()
    stamp, runnable = await embedder.chunk_embedding_metadata()
    return stamp, runnable, chunk_rows_embedder(embedder)


async def record_configured_embedding_models(
    service: EmbeddingService | None = None,
) -> dict[str, Any] | None:
    """Write the graph and content models this process runs with to the deployment record.

    Called at every startup and before each lifecycle pass. The record keeps
    the first models it ever saw, so a later start on another model is
    switch evidence for planes whose verdict is still open. A failure is
    logged and returns ``None``: the record only adds evidence, and the
    migration-time stamps still decide every plane they cover.
    """
    try:
        provider = configured_embedding_provider()
        graph = provider.metadata.to_dict() if provider is not None else None
        content, _runnable = await (service or EmbeddingService()).chunk_embedding_metadata()
        async with content_client.surreal_content_client() as client:

            async def execute(query: str, **params: object) -> object:
                return await content_client.select_many(client, query, **params)

            return await record_deployment_models(execute, graph=graph, content=content)
    except Exception as exc:
        log.warning("embedding_models_record_failed", error_type=type(exc).__name__)
        return None


__all__ = [
    "chunk_rows_embedder",
    "document_chunk_sweep_inputs",
    "record_configured_embedding_models",
]
