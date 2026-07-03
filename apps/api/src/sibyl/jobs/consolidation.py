"""Memory consolidation and forgetting jobs.

Background jobs that maintain knowledge graph quality:
- consolidate_org: Cluster related episodes, generate summaries, archive stale edges
- priority_decay: Archive low-importance entities that haven't been accessed recently

Run on a schedule via arq cron or triggered manually via the API.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from sibyl_core.models.entities import EntityType
from sibyl_core.retrieval.temporal import get_entity_decay_timestamp, usage_retention_multiplier

log = structlog.get_logger()

_PRIORITY_DECAY_ENTITY_TYPES = (
    EntityType.EPISODE,
    EntityType.CLAIM,
    EntityType.IDEA,
    EntityType.PLAN,
    EntityType.NOTE,
)


class PriorityDecayCandidate:
    __slots__ = ("created_at", "entity_id", "last_seen_at", "reason", "retrieval_count", "score")

    def __init__(
        self,
        *,
        entity_id: str,
        created_at: datetime,
        score: float,
        reason: str,
        retrieval_count: int,
        last_seen_at: datetime,
    ) -> None:
        self.entity_id = entity_id
        self.created_at = created_at
        self.score = score
        self.reason = reason
        self.retrieval_count = retrieval_count
        self.last_seen_at = last_seen_at


async def _get_graph_runtime(group_id: str) -> Any:
    from sibyl_core.services.graph import get_surreal_graph_runtime

    return await get_surreal_graph_runtime(group_id)


async def _list_organization_ids() -> list[str]:
    from sibyl.persistence.organization_runtime import list_org_ids

    return await list_org_ids()


async def consolidate_org(
    ctx: dict[str, Any],  # noqa: ARG001
    group_id: str,
    similarity_threshold: float = 0.90,
    max_merges_per_run: int = 50,
) -> dict[str, Any]:
    """Consolidate duplicate and near-duplicate entities within an org.

    Runs the deduplicator to find semantically similar entities, then
    merges the highest-confidence pairs. Designed to run as a nightly
    cron job to prevent unbounded graph growth.

    Args:
        ctx: arq context
        group_id: Organization ID to consolidate
        similarity_threshold: Minimum similarity for merge candidates
        max_merges_per_run: Safety cap on merges per execution

    Returns:
        Dict with consolidation statistics
    """
    from sibyl_core.retrieval.dedup import DedupConfig, EntityDeduplicator

    log.info(
        "consolidation_started",
        group_id=group_id,
        threshold=similarity_threshold,
    )

    try:
        runtime = await _get_graph_runtime(group_id)
        client = runtime.client
        entity_manager = runtime.entity_manager

        config = DedupConfig(
            similarity_threshold=similarity_threshold,
            same_type_only=True,
            min_name_overlap=0.3,
        )
        deduplicator = EntityDeduplicator(
            client=client,
            entity_manager=entity_manager,
            config=config,
        )

        pairs = await deduplicator.find_duplicates()

        merges_completed = 0
        merges_failed = 0

        for pair in pairs[:max_merges_per_run]:
            try:
                success = await deduplicator.merge_entities(
                    keep_id=pair.suggested_keep,
                    remove_id=(
                        pair.entity2_id
                        if pair.suggested_keep == pair.entity1_id
                        else pair.entity1_id
                    ),
                    merge_metadata=True,
                )
                if success:
                    merges_completed += 1
                else:
                    merges_failed += 1
            except Exception as e:
                log.warning(
                    "consolidation_merge_failed",
                    keep=pair.suggested_keep,
                    error=str(e),
                )
                merges_failed += 1

        result = {
            "group_id": group_id,
            "duplicates_found": len(pairs),
            "merges_completed": merges_completed,
            "merges_failed": merges_failed,
            "merges_skipped": max(0, len(pairs) - max_merges_per_run),
        }

        log.info("consolidation_completed", **result)
        return result

    except Exception as e:
        log.exception("consolidation_failed", group_id=group_id, error=str(e))
        raise


async def priority_decay(
    ctx: dict[str, Any],  # noqa: ARG001
    group_id: str,
    min_age_days: int = 180,
    max_archives_per_run: int = 100,
    decay_threshold: float = 0.35,
    recency_half_life_days: int = 180,
    entity_types: Sequence[EntityType] | None = None,
) -> dict[str, Any]:
    """Archive low-importance entities that haven't been accessed recently.

    Implements the Priority Decay forgetting policy from FiFA research:
    entities are scored by importance * recency_decay, and those below
    a threshold are archived (excluded from default search but still
    retrievable with include_archived=True).

    Only targets derived memory entities by default. Sources, documents,
    sessions, patterns, rules, tasks, projects, procedures, and preferences are
    preserved unless explicitly passed through ``entity_types``.

    Args:
        ctx: arq context
        group_id: Organization ID
        min_age_days: Minimum age before an entity is eligible for archival
        max_archives_per_run: Safety cap on archives per execution

    Returns:
        Dict with archival statistics
    """
    log.info(
        "priority_decay_started",
        group_id=group_id,
        min_age_days=min_age_days,
    )

    try:
        runtime = await _get_graph_runtime(group_id)
        entity_manager = runtime.entity_manager

        cutoff = datetime.now(UTC) - timedelta(days=min_age_days)
        page_size = max(200, min(max_archives_per_run * 2, 1000))
        threshold = max(0.0, min(float(decay_threshold), 1.0))
        half_life_days = max(int(recency_half_life_days), 1)
        candidates: list[PriorityDecayCandidate] = []
        target_entity_types = (
            _PRIORITY_DECAY_ENTITY_TYPES if entity_types is None else tuple(entity_types)
        )

        for entity_type in target_entity_types:
            type_candidates: list[PriorityDecayCandidate] = []
            offset = 0
            while True:
                batch = await entity_manager.list_by_type(
                    entity_type,
                    limit=page_size,
                    offset=offset,
                    include_archived=False,
                )
                if not batch:
                    break

                offset += len(batch)
                for entity in batch:
                    created_at = _aware_datetime(entity.created_at)
                    if (
                        _is_archived(entity)
                        or _is_pinned_for_retention(entity)
                        or created_at >= cutoff
                    ):
                        continue
                    score = _priority_decay_score(
                        entity,
                        now=datetime.now(UTC),
                        recency_half_life_days=half_life_days,
                    )
                    if score >= threshold:
                        continue
                    type_candidates.append(
                        PriorityDecayCandidate(
                            entity_id=entity.id,
                            created_at=created_at,
                            score=score,
                            reason=_priority_decay_reason(entity),
                            retrieval_count=_entity_retrieval_count(entity),
                            last_seen_at=_aware_datetime(_entity_last_seen_at(entity)),
                        )
                    )
            candidates.extend(type_candidates)

        candidates.sort(
            key=lambda candidate: (
                candidate.retrieval_count,
                candidate.last_seen_at,
                candidate.score,
                candidate.created_at,
            )
        )
        candidates = candidates[:max_archives_per_run]

        archived_count = 0
        now_iso = datetime.now(UTC).isoformat()
        for candidate in candidates:
            try:
                await entity_manager.update(
                    candidate.entity_id,
                    {
                        "status": "archived",
                        "archived_at": now_iso,
                        "decay_score": round(candidate.score, 6),
                        "decay_threshold": threshold,
                        "decay_reason": candidate.reason,
                    },
                )
                archived_count += 1
            except Exception as e:
                log.warning(
                    "priority_decay_archive_failed",
                    entity_id=candidate.entity_id,
                    error=str(e),
                )

        result = {
            "group_id": group_id,
            "candidates_found": len(candidates),
            "archived": archived_count,
            "min_age_days": min_age_days,
        }

        log.info("priority_decay_completed", **result)
        return result

    except Exception as e:
        log.exception("priority_decay_failed", group_id=group_id, error=str(e))
        raise


def _priority_decay_score(
    entity: Any,
    *,
    now: datetime,
    recency_half_life_days: int,
) -> float:
    importance = _usage_adjusted_importance(entity, _entity_importance(entity))
    if _is_superseded_or_stale(entity):
        importance *= 0.25
    last_seen = _entity_last_seen_at(entity)
    age_days = max((now - _aware_datetime(last_seen)).total_seconds() / 86400, 0.0)
    adjusted_half_life_days = recency_half_life_days * usage_retention_multiplier(entity)
    recency_decay = 0.5 ** (age_days / adjusted_half_life_days)
    return max(0.0, min(importance * recency_decay, 1.0))


def _priority_decay_reason(entity: Any) -> str:
    if _is_superseded_or_stale(entity):
        return "superseded_or_stale"
    return "low_priority_decay_score"


def _entity_importance(entity: Any) -> float:
    metadata = entity.metadata or {}
    for key in (
        "retention_importance",
        "importance",
        "memory_importance",
        "promotion_confidence",
        "reflection_confidence",
        "projection_confidence",
        "confidence",
    ):
        if (value := _metadata_float(metadata, key)) is not None:
            return max(0.0, min(value, 1.0))
    return 0.5


def _usage_adjusted_importance(entity: Any, importance: float) -> float:
    metadata = entity.metadata or {}
    if _metadata_int(metadata.get("citation_count")) > 0:
        return max(importance, 0.70)
    if _metadata_int(metadata.get("retrieval_count")) > 0:
        return max(importance, 0.40)
    return importance


def _entity_retrieval_count(entity: Any) -> int:
    metadata = entity.metadata or {}
    return max(_metadata_int(metadata.get("retrieval_count")), 0)


def _entity_last_seen_at(entity: Any) -> datetime:
    return get_entity_decay_timestamp(entity) or entity.created_at


def _is_archived(entity: Any) -> bool:
    metadata = entity.metadata or {}
    status = str(metadata.get("status") or "").lower()
    return status == "archived"


def _is_pinned_for_retention(entity: Any) -> bool:
    metadata = entity.metadata or {}
    if metadata.get("pinned") is True:
        return True
    retention = str(metadata.get("retention") or metadata.get("retention_policy") or "").lower()
    return retention in {"pinned", "preserve", "keep"}


def _is_superseded_or_stale(entity: Any) -> bool:
    metadata = entity.metadata or {}
    lifecycle_values = {
        str(metadata.get("lifecycle_state") or "").lower(),
        str(metadata.get("review_state") or "").lower(),
        str(metadata.get("status") or "").lower(),
    }
    if lifecycle_values & {"superseded", "stale"}:
        return True
    return bool(
        metadata.get("superseded_by_raw_memory_id")
        or metadata.get("superseded_by_source_id")
        or metadata.get("superseded_by")
    )


def _metadata_float(metadata: Mapping[str, Any], key: str) -> float | None:
    value = metadata.get(key)
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _metadata_int(value: object) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


async def consolidate_all_orgs(
    ctx: dict[str, Any],
) -> dict[str, Any]:
    """Run consolidation across all organizations.

    Designed as a cron job that discovers all orgs and runs
    consolidation + priority decay for each.
    """
    log.info("consolidate_all_orgs_started")

    try:
        org_ids = await _list_organization_ids()

        log.info("consolidate_all_orgs_discovered", org_count=len(org_ids))

        results = []
        for org_id in org_ids:
            try:
                consolidation = await consolidate_org(ctx, group_id=org_id)
                decay = await priority_decay(ctx, group_id=org_id)
                results.append(
                    {
                        "org_id": org_id,
                        "consolidation": consolidation,
                        "decay": decay,
                    }
                )
            except Exception as e:
                log.warning("consolidate_org_failed", org_id=org_id, error=str(e))
                results.append({"org_id": org_id, "error": str(e)})

        summary = {
            "orgs_processed": len(results),
            "orgs_succeeded": sum(1 for r in results if "error" not in r),
            "orgs_failed": sum(1 for r in results if "error" in r),
        }

        log.info("consolidate_all_orgs_completed", **summary)
        return summary

    except Exception as e:
        log.exception("consolidate_all_orgs_failed", error=str(e))
        raise
