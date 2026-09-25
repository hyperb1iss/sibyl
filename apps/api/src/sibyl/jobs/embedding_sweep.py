"""Application wiring for the document chunk plane of the embedding sweep.

Chunks are embedded by the crawler's embedding service, so the sweep embeds
them through the same service: whatever provider it is configured for, the
sweep produces exactly the vectors and stamp a fresh crawl would.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sibyl.crawler.chunker import Chunk
from sibyl.crawler.embedder import EmbeddingService
from sibyl_core.services.document_embedding_sweep import ChunkEmbedder
from sibyl_core.services.embedding_sweep import EmbeddingStamp, SweepRow


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


__all__ = ["chunk_rows_embedder", "document_chunk_sweep_inputs"]
