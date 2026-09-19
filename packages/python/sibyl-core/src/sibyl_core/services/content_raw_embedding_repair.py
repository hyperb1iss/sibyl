"""Give recallable raw captures the vector the raw recall lane expects."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import cast

import structlog

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.embeddings.providers import EmbeddingProvider
from sibyl_core.projection.repair import LifecycleRepairResult
from sibyl_core.services import content_client
from sibyl_core.services import content_models as models
from sibyl_core.services.content_models import RawMemory
from sibyl_core.services.content_raw_persistence import (
    _RAW_MEMORY_EMBEDDING_AUTO,
    _raw_memories_with_embeddings,
    _raw_memory_without_embedding,
)

log = structlog.get_logger()

RAW_EMBEDDING_REPAIR_PAGE_SIZE = 256

# Only the vector and its provenance move. A full-row upsert would bump the
# revision, and revision is what sealed readers compare against their snapshots.
_RAW_EMBEDDING_UPDATE_QUERY = """
UPDATE raw_captures SET
    embedding = $embedding,
    metadata.embedding_metadata = $embedding_metadata
WHERE uuid = $uuid AND organization_id = $organization_id AND revision = $revision
RETURN uuid;
"""


def raw_memory_embedding_current(memory: RawMemory, provider: EmbeddingProvider) -> bool:
    """A vector counts only when its recorded provenance matches the configured provider."""
    if memory.embedding is None:
        return False
    return memory.metadata.get("embedding_metadata") == models.raw_memory_embedding_metadata(
        provider.metadata
    )


def _repair_candidate(memory: RawMemory, provider: EmbeddingProvider) -> bool:
    return (
        memory.deleted_at is None
        and models.raw_memory_recallable(memory)
        and not raw_memory_embedding_current(memory, provider)
    )


async def repair_raw_capture_embeddings(
    organization_id: str,
    *,
    page_size: int = RAW_EMBEDDING_REPAIR_PAGE_SIZE,
    embedding_provider: EmbeddingProvider | object | None = _RAW_MEMORY_EMBEDDING_AUTO,
) -> LifecycleRepairResult:
    """Embed recallable raw captures whose vector is missing or from another provider.

    Captures restored from an archive, or written while no embedding provider
    was configured, otherwise stay lexical-only forever: the raw_vector lane
    reads only rows that carry a vector. Pages are keyset-ordered by uuid so a
    second pass rediscovers nothing it already repaired, and a row whose
    revision moved between read and write is reported pending rather than
    overwritten.
    """
    provider = (
        models.configured_raw_memory_embedding_provider()
        if embedding_provider is _RAW_MEMORY_EMBEDDING_AUTO
        else cast("EmbeddingProvider | None", embedding_provider)
    )
    if provider is None:
        return LifecycleRepairResult()
    limit = max(1, page_size)
    counts = {"checked": 0, "recovered": 0, "pending": 0, "failed": 0}
    cursor = ""
    while True:
        async with content_client.surreal_content_client() as client:
            rows = await content_client.select_many(
                client,
                "SELECT * FROM raw_captures "
                "WHERE organization_id = $organization_id AND uuid > $cursor "
                "ORDER BY uuid ASC LIMIT $limit;",
                organization_id=organization_id,
                cursor=cursor,
                limit=limit,
            )
            if not rows:
                break
            cursor = str(rows[-1]["uuid"])
            memories = [models.raw_memory_from_record(row) for row in rows]
            targets = [memory for memory in memories if _repair_candidate(memory, provider)]
            counts["checked"] += len(targets)
            if targets:
                outcomes = await _repair_page(client, targets, provider, organization_id)
                for outcome in outcomes:
                    counts[outcome] += 1
        if len(rows) < limit:
            break
    log.info(
        "raw_capture_embedding_repair_completed",
        organization_id=organization_id,
        **counts,
    )
    return LifecycleRepairResult(**counts)


async def _repair_page(
    client: SurrealContentClient,
    targets: Sequence[RawMemory],
    provider: EmbeddingProvider,
    organization_id: str,
) -> list[str]:
    stripped = [_raw_memory_without_embedding(memory) for memory in targets]
    try:
        embedded = await _raw_memories_with_embeddings(stripped, provider)
    except Exception as exc:
        log.warning(
            "raw_capture_embedding_repair_page_failed",
            organization_id=organization_id,
            rows=len(targets),
            error_type=type(exc).__name__,
        )
        return ["failed"] * len(targets)
    writes = await asyncio.gather(
        *(
            _write_embedding(client, memory, organization_id)
            for memory in embedded
            if memory.embedding is not None
        ),
        return_exceptions=True,
    )
    outcomes: list[str] = []
    for memory, write in zip(
        [memory for memory in embedded if memory.embedding is not None], writes, strict=True
    ):
        if isinstance(write, BaseException):
            log.warning(
                "raw_capture_embedding_repair_write_failed",
                organization_id=organization_id,
                memory_id=memory.id,
                error_type=type(write).__name__,
            )
            outcomes.append("failed")
        else:
            outcomes.append("recovered" if write else "pending")
    # A target the provider left unembedded is still work for a later pass.
    outcomes.extend(["pending"] * sum(1 for memory in embedded if memory.embedding is None))
    return outcomes


async def _write_embedding(
    client: SurrealContentClient, memory: RawMemory, organization_id: str
) -> bool:
    rows = await content_client.select_many(
        client,
        _RAW_EMBEDDING_UPDATE_QUERY,
        uuid=memory.id,
        organization_id=organization_id,
        revision=memory.revision,
        embedding=memory.embedding,
        embedding_metadata=memory.metadata.get("embedding_metadata"),
    )
    return bool(rows)
