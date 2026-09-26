"""Give recallable raw captures the vector the raw recall lane expects."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import cast

import structlog

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import EMBEDDING_DIM
from sibyl_core.backends.surreal.schema_embedding_states import embedding_sweep_schema_ready
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

REPAIR_COMPLETED = "completed"
REPAIR_SKIPPED_NO_PROVIDER = "skipped_no_provider"
REPAIR_SKIPPED_DIMENSION_MISMATCH = "skipped_dimension_mismatch"
REPAIR_SKIPPED_SCHEMA_PENDING = "skipped_schema_pending"


@dataclass(frozen=True, slots=True)
class RawEmbeddingRepairResult(LifecycleRepairResult):
    """Lifecycle counts plus why a pass did no work, when it did none."""

    status: str = REPAIR_COMPLETED
    provider_dimensions: int | None = None
    schema_dimensions: int | None = None


# The walk reads only what the candidate decision needs. raw_content and the
# vector stay on the server until a row is actually going to be embedded; the
# scheduler runs this every minute across every organization.
_RAW_EMBEDDING_WALK_FIELDS = ", ".join(
    (
        "uuid",
        "revision",
        "organization_id",
        "source_id",
        "principal_id",
        "review_state",
        "deleted_at",
        "metadata",
    )
)
_RAW_EMBEDDING_WALK_QUERY = (
    f"SELECT {_RAW_EMBEDDING_WALK_FIELDS} FROM raw_captures "
    "WHERE organization_id = $organization_id AND uuid > $cursor "
    "AND deleted_at = NONE "
    "AND (embedding = NONE OR metadata.embedding_metadata != $expected_metadata) "
    "ORDER BY uuid ASC LIMIT $limit;"
)
_RAW_EMBEDDING_FETCH_QUERY = (
    "SELECT uuid, revision, organization_id, source_id, principal_id, review_state, "
    "deleted_at, title, raw_content, embedding, metadata FROM raw_captures "
    "WHERE organization_id = $organization_id AND uuid IN $ids;"
)

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
    client: SurrealContentClient | None = None,
) -> LifecycleRepairResult:
    """Embed recallable raw captures whose vector is missing or from another provider.

    Captures restored from an archive, or written while no embedding provider
    was configured, otherwise stay lexical-only forever: the raw_vector lane
    reads only rows that carry a vector. Pages are keyset-ordered by uuid so a
    second pass rediscovers nothing it already repaired, and a row whose
    revision moved between read and write is reported pending rather than
    overwritten.

    A caller that already holds the database the captures live in, such as a
    restore, passes its client so the repair cannot land on another store.

    The walk selects only the columns the candidate decision needs and lets
    the server drop rows whose vector already matches the configured provider,
    so a fully current organization costs one empty page. Text is fetched only
    for the rows about to be embedded. A provider whose dimensions differ from
    the schema's embedding field is refused up front: every write would fail
    the typed-array check and the paid embedding call would repeat each pass.
    """
    provider = (
        models.configured_raw_memory_embedding_provider()
        if embedding_provider is _RAW_MEMORY_EMBEDDING_AUTO
        else cast("EmbeddingProvider | None", embedding_provider)
    )
    if provider is None:
        return RawEmbeddingRepairResult(status=REPAIR_SKIPPED_NO_PROVIDER)
    if provider.metadata.dimensions != EMBEDDING_DIM:
        log.warning(
            "raw_capture_embedding_repair_dimension_mismatch",
            organization_id=organization_id,
            provider=provider.metadata.provider,
            model=provider.metadata.model,
            provider_dimensions=provider.metadata.dimensions,
            schema_dimensions=EMBEDDING_DIM,
        )
        return RawEmbeddingRepairResult(
            status=REPAIR_SKIPPED_DIMENSION_MISMATCH,
            provider_dimensions=provider.metadata.dimensions,
            schema_dimensions=EMBEDDING_DIM,
        )
    async with _content_session(client) as session:
        # Raw capture stamps are the chunk plane's evidence of the model that
        # preceded this release; restamping them before the content upgrade
        # photographs them would erase that evidence.
        async def execute(query: str, **params: object) -> object:
            return await content_client.select_many(session, query, **params)

        if not await embedding_sweep_schema_ready(execute, graph=False):
            log.info(
                "raw_capture_embedding_repair_waiting_for_schema",
                organization_id=organization_id,
            )
            return RawEmbeddingRepairResult(status=REPAIR_SKIPPED_SCHEMA_PENDING)
    expected_metadata = models.raw_memory_embedding_metadata(provider.metadata)
    limit = max(1, page_size)
    counts = {"checked": 0, "recovered": 0, "pending": 0, "failed": 0}
    cursor = ""
    while True:
        async with _content_session(client) as session:
            rows = await content_client.select_many(
                session,
                _RAW_EMBEDDING_WALK_QUERY,
                organization_id=organization_id,
                cursor=cursor,
                expected_metadata=expected_metadata,
                limit=limit,
            )
            if not rows:
                break
            cursor = str(rows[-1]["uuid"])
            candidates = [
                memory
                for memory in (models.raw_memory_from_record(row) for row in rows)
                if models.raw_memory_recallable(memory)
            ]
            counts["checked"] += len(candidates)
            if candidates:
                outcomes = await _repair_page(
                    session, [memory.id for memory in candidates], provider, organization_id
                )
                for outcome in outcomes:
                    counts[outcome] += 1
        if len(rows) < limit:
            break
    log.info(
        "raw_capture_embedding_repair_completed",
        organization_id=organization_id,
        **counts,
    )
    return RawEmbeddingRepairResult(
        checked=counts["checked"],
        recovered=counts["recovered"],
        pending=counts["pending"],
        failed=counts["failed"],
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


async def _repair_page(
    client: SurrealContentClient,
    candidate_ids: Sequence[str],
    provider: EmbeddingProvider,
    organization_id: str,
) -> list[str]:
    # Fetch text and the current revision only now, for the rows being embedded.
    fetched = await content_client.select_many(
        client,
        _RAW_EMBEDDING_FETCH_QUERY,
        organization_id=organization_id,
        ids=list(candidate_ids),
    )
    current = [models.raw_memory_from_record(row) for row in fetched]
    targets = [memory for memory in current if _repair_candidate(memory, provider)]
    # A row gone or made current between the walk and this fetch needs nothing
    # from this pass and will not reappear in the next one.
    outcomes: list[str] = ["recovered"] * (len(candidate_ids) - len(targets))
    if not targets:
        return outcomes
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
        return [*outcomes, *(["failed"] * len(targets))]
    writes = await asyncio.gather(
        *(
            _write_embedding(client, memory, organization_id)
            for memory in embedded
            if memory.embedding is not None
        ),
        return_exceptions=True,
    )
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
