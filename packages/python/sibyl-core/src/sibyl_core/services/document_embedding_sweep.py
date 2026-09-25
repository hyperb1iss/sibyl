"""The document chunk plane of the embedding sweep.

Chunks live in the shared content namespace, scoped by organization. The
chunk embedder is owned by the application (it builds contextual chunk text
and resolves the content embedding configuration), so callers inject it as a
function that returns vectors together with the stamp it wrote them under.

Chunks gained a stamp in the same release as the sweep, so a plane's first
verdict reads the organization's raw captures instead: they are embedded by
the same content configuration and have carried a stamp for longer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import EMBEDDING_DIM
from sibyl_core.backends.surreal.schema_invariants import fetch_vector_field_dimension
from sibyl_core.embeddings.provenance import UNVERIFIED_EMBEDDING_PROVIDER
from sibyl_core.services import content_client
from sibyl_core.services.embedding_sweep import (
    SWEEP_SKIPPED_NO_PROVIDER,
    EmbeddingStamp,
    EmbeddingSweepResult,
    LegacyEvidence,
    SweepPlane,
    SweepRow,
    SweepTable,
    ensure_legacy_decision,
    mark_plane_for_reembed,
    run_embedding_sweep,
)

DOCUMENT_CHUNK_EMBEDDING_PLANE = "document_chunks"

type ChunkEmbedder = Callable[
    [Sequence[SweepRow]], Awaitable[tuple[list[list[float]], EmbeddingStamp]]
]

_CHUNK_FENCE = (
    "content = $rows_by_uuid[uuid].content "
    "AND (context ?? '') = ($rows_by_uuid[uuid].context ?? '') "
    "AND heading_path = $rows_by_uuid[uuid].heading_path"
)
_RAW_STAMP = "metadata.embedding_metadata"
_RAW_MODEL_DIFFERS = (
    f"SELECT uuid FROM raw_captures WHERE organization_id = $scope AND {_RAW_STAMP} != NONE "
    f"AND {_RAW_STAMP}.provider != $unverified AND ({_RAW_STAMP}.provider != $provider "
    f"OR {_RAW_STAMP}.model != $model OR {_RAW_STAMP}.dimensions != $dimensions) LIMIT 1;"
)
_RAW_MODEL_MATCHES = (
    f"SELECT uuid FROM raw_captures WHERE organization_id = $scope "
    f"AND {_RAW_STAMP}.provider = $provider AND {_RAW_STAMP}.model = $model "
    f"AND {_RAW_STAMP}.dimensions = $dimensions LIMIT 1;"
)


def document_chunk_sweep_table(dimensions: int = EMBEDDING_DIM) -> SweepTable:
    return SweepTable(
        name="document_chunks",
        scope_field="organization_id",
        vector_field="embedding",
        metadata_path="embedding_metadata",
        projection="content, context, heading_path",
        fence=_CHUNK_FENCE,
        dimensions=dimensions,
    )


async def document_chunk_embedding_plane(
    organization_id: str,
    *,
    client: SurrealContentClient,
    stamp: EmbeddingStamp,
    embed_chunks: ChunkEmbedder,
) -> SweepPlane:
    """Build the chunk plane against the chunk vector field as the database declares it.

    The content schema sizes that field once and never resizes it, so the
    configured dimension can move past it; the declared size is the one a
    write must fit.
    """

    async def execute(query: str, **params: object) -> object:
        return await content_client.select_many(client, query, **params)

    declared = await fetch_vector_field_dimension(
        client.execute_query, "document_chunks", "embedding"
    )
    table = document_chunk_sweep_table(declared or EMBEDDING_DIM)

    async def embed(
        _table: SweepTable, rows: Sequence[SweepRow]
    ) -> tuple[list[list[float]], EmbeddingStamp]:
        return await embed_chunks(rows)

    async def evidence() -> LegacyEvidence:
        # Chunk stamps are all written by this release or later, so they say
        # nothing about the model behind unstamped chunks. Raw captures share
        # the content embedding configuration and were stamped long before.
        params = {
            "scope": organization_id,
            "unverified": UNVERIFIED_EMBEDDING_PROVIDER,
            "provider": stamp.get("provider"),
            "model": stamp.get("model"),
            "dimensions": stamp.get("dimensions"),
        }
        return LegacyEvidence(
            differs=bool(await execute(_RAW_MODEL_DIFFERS, **params)),
            matches=bool(await execute(_RAW_MODEL_MATCHES, **params)),
        )

    dimensions = stamp.get("dimensions")
    return SweepPlane(
        name=DOCUMENT_CHUNK_EMBEDDING_PLANE,
        organization_id=organization_id,
        execute=execute,
        tables=(table,),
        stamp=dict(stamp),
        embed=embed,
        provider_dimensions=int(dimensions) if isinstance(dimensions, int) else 0,
        schema_dimensions=table.dimensions,
        evidence=evidence,
    )


@asynccontextmanager
async def _content_session(
    client: SurrealContentClient | None,
) -> AsyncIterator[SurrealContentClient]:
    if client is not None:
        yield client
        return
    async with content_client.surreal_content_client() as shared:
        yield shared


async def decide_document_chunk_legacy_vectors(
    organization_id: str,
    *,
    stamp: EmbeddingStamp | None,
    embed_chunks: ChunkEmbedder,
    client: SurrealContentClient | None = None,
) -> dict[str, Any] | None:
    """Record the chunk plane's legacy verdict before raw repair restamps captures."""
    if stamp is None:
        return None
    async with _content_session(client) as session:
        plane = await document_chunk_embedding_plane(
            organization_id, client=session, stamp=stamp, embed_chunks=embed_chunks
        )
        return await ensure_legacy_decision(plane)


async def sweep_document_chunk_embeddings(
    organization_id: str,
    *,
    stamp: EmbeddingStamp | None,
    embed_chunks: ChunkEmbedder,
    client: SurrealContentClient | None = None,
    **options: Any,
) -> EmbeddingSweepResult:
    """Re-embed chunk vectors from another model, or chunks whose model is unknown.

    ``stamp`` is what the chunk embedder writes today; ``None`` means no
    embedder is configured.
    """
    if stamp is None:
        return EmbeddingSweepResult(
            plane=DOCUMENT_CHUNK_EMBEDDING_PLANE, status=SWEEP_SKIPPED_NO_PROVIDER
        )
    async with _content_session(client) as session:
        plane = await document_chunk_embedding_plane(
            organization_id, client=session, stamp=stamp, embed_chunks=embed_chunks
        )
        return await run_embedding_sweep(plane, **options)


async def mark_document_chunk_embeddings_for_reembed(
    organization_id: str, *, client: SurrealContentClient | None = None
) -> int:
    """Queue every chunk vector of one organization for the sweep to replace."""
    async with _content_session(client) as session:

        async def execute(query: str, **params: object) -> object:
            return await content_client.select_many(session, query, **params)

        declared = await fetch_vector_field_dimension(
            session.execute_query, "document_chunks", "embedding"
        )
        return await mark_plane_for_reembed(
            plane=DOCUMENT_CHUNK_EMBEDDING_PLANE,
            organization_id=organization_id,
            execute=execute,
            tables=(document_chunk_sweep_table(declared or EMBEDDING_DIM),),
        )


__all__ = [
    "DOCUMENT_CHUNK_EMBEDDING_PLANE",
    "ChunkEmbedder",
    "decide_document_chunk_legacy_vectors",
    "document_chunk_embedding_plane",
    "document_chunk_sweep_table",
    "mark_document_chunk_embeddings_for_reembed",
    "sweep_document_chunk_embeddings",
]
