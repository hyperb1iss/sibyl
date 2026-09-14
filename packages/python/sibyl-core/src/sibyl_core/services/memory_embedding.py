"""Schedule configured indexing after a reflection publication has committed."""

import asyncio
from dataclasses import replace

import structlog

from sibyl_core.embeddings.providers import EmbeddingMetadata, configured_embedding_provider
from sibyl_core.projection.repair import LifecycleRepairResult
from sibyl_core.runtime_ports import get_queue_port
from sibyl_core.services.graph_common import normalize_graph_records
from sibyl_core.services.graph_read_availability import available_graph_entities
from sibyl_core.services.graph_runtime import GraphRuntime
from sibyl_core.services.memory_contract import ReflectionPromotionResult

log = structlog.get_logger()


def _embedding_current(present: bool, metadata: object, provider: EmbeddingMetadata) -> bool:
    return present and metadata == provider.to_dict()


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
        outcomes = await asyncio.gather(
            *(
                get_queue_port().enqueue_entity_embedding_backfill(
                    entities_data=[entity.model_dump(mode="json")],
                    group_id=runtime.client.group_id,
                    relationships=None,
                )
                for entity in current.values()
            ),
            return_exceptions=True,
        )
        failed = sum(isinstance(outcome, BaseException) for outcome in outcomes)
        counts["pending"] += len(ids) - failed
        counts["failed"] += failed
        if len(rows) < 512:
            break
    return LifecycleRepairResult(**counts)
