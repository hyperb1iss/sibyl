"""Document chunk vectors follow the configured content model."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

from sibyl_core.backends.surreal.content_schema import EMBEDDING_DIM
from sibyl_core.embeddings.provenance import (
    UNVERIFIED_ORIGIN_ARCHIVE,
    document_chunk_embedding_metadata,
    unverified_embedding_metadata,
)
from sibyl_core.services import content_client
from sibyl_core.services.document_embedding_sweep import (
    DOCUMENT_CHUNK_EMBEDDING_PLANE,
    decide_document_chunk_legacy_vectors,
    sweep_document_chunk_embeddings,
)
from sibyl_core.services.embedding_sweep import (
    SWEEP_COMPLETED,
    SWEEP_SKIPPED_DIMENSION_MISMATCH,
    SWEEP_SKIPPED_NO_PROVIDER,
    LegacyVectorBasis,
    LegacyVectorDecision,
    read_embedding_sweep_state,
)
from tests.test_reflection_identity import content_store as content_store

PREVIOUS = document_chunk_embedding_metadata(
    provider="openai", model="text-embedding-3-small", dimensions=EMBEDDING_DIM
)
CURRENT = document_chunk_embedding_metadata(
    provider="bedrock", model="cohere.embed-v4:0", dimensions=EMBEDDING_DIM
)


class ChunkEmbedder:
    def __init__(self, stamp: dict[str, object]) -> None:
        self.stamp = stamp
        self.rows: list[dict[str, object]] = []

    async def __call__(self, rows: Sequence[dict[str, object]]):
        self.rows.extend(rows)
        return [[0.0, 1.0, *([0.0] * (EMBEDDING_DIM - 2))] for _ in rows], dict(self.stamp)


async def _chunk(org: str, chunk_id: str, *, stamp=None, vector=True) -> None:
    record: dict[str, object] = {
        "uuid": chunk_id,
        "organization_id": org,
        "source_id": "source",
        "document_id": f"doc-{chunk_id}",
        "chunk_index": 0,
        "content": f"Chunk body {chunk_id}",
        "context": "Surrounding context",
        "heading_path": ["Guide"],
    }
    if vector:
        record["embedding"] = [1.0, *([0.0] * (EMBEDDING_DIM - 1))]
    if stamp is not None:
        record["embedding_metadata"] = stamp
    async with content_client.surreal_content_client() as client:
        await content_client.select_many(
            client, "CREATE document_chunks CONTENT $record RETURN NONE;", record=record
        )


async def _raw_capture(org: str, stamp: dict[str, object]) -> None:
    async with content_client.surreal_content_client() as client:
        await content_client.select_many(
            client,
            "CREATE raw_captures CONTENT $record RETURN NONE;",
            record={
                "uuid": str(uuid4()),
                "organization_id": org,
                "principal_id": "owner",
                "source_id": str(uuid4()),
                "raw_content": "captured",
                "metadata": {"embedding_metadata": stamp},
            },
        )


async def _stamps(org: str) -> dict[str, object]:
    async with content_client.surreal_content_client() as client:
        rows = await content_client.select_many(
            client,
            "SELECT uuid, embedding_metadata FROM document_chunks WHERE organization_id = $org;",
            org=org,
        )
    return {str(row["uuid"]): row.get("embedding_metadata") for row in rows}


async def _state(org: str) -> dict[str, object]:
    async with content_client.surreal_content_client() as client:
        return await read_embedding_sweep_state(
            DOCUMENT_CHUNK_EMBEDDING_PLANE,
            org,
            lambda query, **params: content_client.select_many(client, query, **params),
        )


async def test_raw_capture_stamps_prove_a_switch_for_unstamped_chunks(content_store) -> None:
    org = str(uuid4())
    await _raw_capture(org, {**PREVIOUS, "cache_namespace": "raw-memory"})
    await _chunk(org, "legacy")
    embed = ChunkEmbedder(CURRENT)

    await decide_document_chunk_legacy_vectors(org, stamp=CURRENT, embed_chunks=embed)
    # The raw repair restamps captures right after the verdict; it must not matter.
    async with content_client.surreal_content_client() as client:
        await content_client.select_many(
            client,
            "UPDATE raw_captures SET metadata.embedding_metadata = $stamp;",
            stamp={**CURRENT, "cache_namespace": "raw-memory"},
        )
    result = await sweep_document_chunk_embeddings(org, stamp=CURRENT, embed_chunks=embed)

    assert result.status == SWEEP_COMPLETED
    assert result.legacy_decision == LegacyVectorDecision.REEMBED.value
    assert (result.adopted, result.recovered) == (0, 1)
    assert (await _stamps(org))["legacy"] == CURRENT
    assert embed.rows[0]["content"] == "Chunk body legacy"
    assert embed.rows[0]["context"] == "Surrounding context"
    assert (await _state(org))["legacy_basis"] == LegacyVectorBasis.PRIOR_STAMPS_DIFFER.value


async def test_matching_raw_stamps_adopt_unstamped_chunks_in_place(content_store) -> None:
    org = str(uuid4())
    await _raw_capture(org, {**CURRENT, "cache_namespace": "raw-memory"})
    await _chunk(org, "legacy")
    await _chunk(org, "lexical", vector=False)
    embed = ChunkEmbedder(CURRENT)

    result = await sweep_document_chunk_embeddings(org, stamp=CURRENT, embed_chunks=embed)

    assert result.legacy_decision == LegacyVectorDecision.ADOPT.value
    assert (result.adopted, result.recovered) == (1, 0)
    assert embed.rows == []
    stamps = await _stamps(org)
    assert stamps["legacy"] == CURRENT
    assert stamps["lexical"] is None


async def test_imported_and_stale_chunks_are_reembedded_and_current_ones_left(
    content_store,
) -> None:
    org = str(uuid4())
    await _chunk(org, "current", stamp=CURRENT)
    await _chunk(org, "stale", stamp=PREVIOUS)
    await _chunk(org, "imported", stamp=unverified_embedding_metadata(UNVERIFIED_ORIGIN_ARCHIVE))
    other_org = str(uuid4())
    await _chunk(other_org, "elsewhere", stamp=PREVIOUS)
    embed = ChunkEmbedder(CURRENT)

    result = await sweep_document_chunk_embeddings(org, stamp=CURRENT, embed_chunks=embed)

    assert result.recovered == 2
    assert {row["uuid"] for row in embed.rows} == {"stale", "imported"}
    assert all(stamp == CURRENT for stamp in (await _stamps(org)).values())
    # Another organization's chunks are its own sweep's business.
    assert (await _stamps(other_org))["elsewhere"] == PREVIOUS


async def test_chunk_sweep_refuses_a_dimension_the_schema_cannot_hold(content_store) -> None:
    org = str(uuid4())
    await _chunk(org, "stale", stamp=PREVIOUS)
    wide = {**CURRENT, "dimensions": EMBEDDING_DIM + 16}
    embed = ChunkEmbedder(wide)

    result = await sweep_document_chunk_embeddings(org, stamp=wide, embed_chunks=embed)

    assert result.status == SWEEP_SKIPPED_DIMENSION_MISMATCH
    assert embed.rows == []


async def test_chunk_sweep_without_an_embedder_does_nothing(content_store) -> None:
    result = await sweep_document_chunk_embeddings(
        str(uuid4()), stamp=None, embed_chunks=ChunkEmbedder(CURRENT)
    )
    assert result.status == SWEEP_SKIPPED_NO_PROVIDER


async def test_fresh_chunk_stamps_are_not_evidence_about_older_chunks(content_store) -> None:
    org = str(uuid4())
    # Written by the new release with the new model before its first sweep.
    await _chunk(org, "fresh", stamp=CURRENT)
    await _chunk(org, "legacy")

    result = await sweep_document_chunk_embeddings(
        org, stamp=CURRENT, embed_chunks=ChunkEmbedder(CURRENT)
    )

    assert result.legacy_decision == LegacyVectorDecision.ADOPT.value
    assert (await _state(org))["legacy_basis"] == LegacyVectorBasis.NO_PRIOR_EVIDENCE.value
