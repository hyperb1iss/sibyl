"""Schedule configured indexing after a reflection publication has committed."""

import asyncio
from collections.abc import Sequence
from dataclasses import replace

import structlog

from sibyl_core.embeddings.provenance import same_vector_identity
from sibyl_core.embeddings.providers import (
    EmbeddingMetadata,
    EmbeddingProvider,
    configured_embedding_provider,
)
from sibyl_core.projection.repair import LifecycleRepairResult
from sibyl_core.runtime_ports import get_queue_port
from sibyl_core.services.graph_common import normalize_graph_records
from sibyl_core.services.graph_read_availability import available_graph_entities
from sibyl_core.services.graph_runtime import GraphRuntime
from sibyl_core.services.memory_contract import ReflectionPromotionResult

log = structlog.get_logger()


def _embedding_current(present: bool, metadata: object, provider: EmbeddingMetadata) -> bool:
    return present and same_vector_identity(metadata, provider.to_dict())


async def enqueue_promoted_embedding(
    result: ReflectionPromotionResult, organization_id: str
) -> ReflectionPromotionResult:
    """Keep lexical publication available and repair a missed enqueue on replay."""
    if not result.success or not result.promoted_id:
        return result
    status: dict[str, object]
    try:
        provider = configured_embedding_provider()
        if provider is None:
            status = {"status": "disabled"}
        else:
            available = await available_graph_entities(organization_id, [result.promoted_id])
            entity = available.get(result.promoted_id)
            if entity is None:
                status = {"status": "unavailable"}
            elif _embedding_current(
                bool(entity.embedding), entity.metadata.get("embedding_metadata"), provider.metadata
            ):
                status = {"status": "ready"}
            else:
                job_id = await get_queue_port().enqueue_entity_embedding_backfill(
                    entities_data=[entity.model_dump(mode="json")],
                    group_id=organization_id,
                    relationships=None,
                )
                status = {"status": "queued", "job_ids": [job_id]}
    except Exception as exc:
        log.warning(
            "reflection_embedding_enqueue_failed",
            promoted_id=result.promoted_id,
            error_type=type(exc).__name__,
        )
        status = {"status": "failed", "error_type": type(exc).__name__}
    return replace(result, metadata={**(result.metadata or {}), "embedding_backfill": status})


async def repair_promoted_embeddings(runtime: GraphRuntime) -> LifecycleRepairResult:
    """Rediscover missing vectors after enqueue loss through the existing queue.

    The graph row itself is durable pending work. Source validation is repeated
    before enqueue and by the worker; operational projections retain their owner.
    """
    provider = configured_embedding_provider()
    if provider is None:
        return LifecycleRepairResult()
    cursor = ""
    counts = {"checked": 0, "recovered": 0, "pending": 0, "failed": 0}
    while True:
        rows = normalize_graph_records(
            await runtime.client.execute_query(
                "SELECT uuid, (name_embedding != NONE) AS embedding_present, "
                "attributes.embedding_metadata AS embedding_metadata "
                "FROM entity WITH INDEX idx_entity_reflection_candidate_uuid "
                "WHERE group_id=$group_id AND uuid > $cursor AND derivation_required=true "
                "AND attributes.reflection_identity.purpose='candidate' "
                "AND attributes.operational_source_id IS NONE "
                "ORDER BY uuid LIMIT $limit;",
                group_id=runtime.client.group_id,
                cursor=cursor,
                limit=512,
            )
        )
        if not rows:
            break
        # Advance over every candidate, including pages whose vectors are current.
        cursor = str(rows[-1]["uuid"])
        ids = [
            str(row["uuid"])
            for row in rows
            if not _embedding_current(
                row.get("embedding_present") is True,
                row.get("embedding_metadata"),
                provider.metadata,
            )
        ]
        counts["checked"] += len(ids)
        current = (
            await available_graph_entities(runtime.client.group_id, ids, runtime=runtime)
            if ids
            else {}
        )
        targets = list(current.values())
        outcomes = await asyncio.gather(
            *(
                get_queue_port().enqueue_entity_embedding_backfill(
                    entities_data=[entity.model_dump(mode="json")],
                    group_id=runtime.client.group_id,
                    relationships=None,
                )
                for entity in targets
            ),
            return_exceptions=True,
        )
        failed_ids = {
            entity.id
            for entity, outcome in zip(targets, outcomes, strict=True)
            if isinstance(outcome, BaseException)
        }
        # Re-read the rows this pass handed to the queue. An in-process queue has
        # already written their vectors, so recovery is reported rather than
        # accumulating as pending work a later pass has to rediscover.
        recovered = await _embedding_current_ids(
            runtime, [entity_id for entity_id in ids if entity_id not in failed_ids], provider
        )
        counts["recovered"] += len(recovered)
        counts["failed"] += len(failed_ids)
        counts["pending"] += len(ids) - len(failed_ids) - len(recovered)
        if len(rows) < 512:
            break
    return LifecycleRepairResult(**counts)


async def _embedding_current_ids(
    runtime: GraphRuntime, entity_ids: Sequence[str], provider: EmbeddingProvider
) -> set[str]:
    """Report which of these rows now carry a vector from the current provider."""
    ids = list(dict.fromkeys(entity_ids))
    if not ids:
        return set()
    rows = normalize_graph_records(
        await runtime.client.execute_query(
            "SELECT uuid, (name_embedding != NONE) AS embedding_present, "
            "attributes.embedding_metadata AS embedding_metadata "
            "FROM entity WHERE group_id=$group_id AND uuid IN $ids;",
            group_id=runtime.client.group_id,
            ids=ids,
        )
    )
    return {
        str(row["uuid"])
        for row in rows
        if _embedding_current(
            row.get("embedding_present") is True,
            row.get("embedding_metadata"),
            provider.metadata,
        )
    }
