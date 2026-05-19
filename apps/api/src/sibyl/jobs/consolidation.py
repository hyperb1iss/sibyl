"""Memory consolidation and forgetting jobs.

Background jobs that maintain knowledge graph quality:
- consolidate_org: Cluster related episodes, generate summaries, archive stale edges
- priority_decay: Archive low-importance entities that haven't been accessed recently

Run on a schedule via arq cron or triggered manually via the API.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

log = structlog.get_logger()


async def _get_graph_runtime(group_id: str) -> Any:
    from sibyl_core.services.native_graph import get_native_graph_runtime

    return await get_native_graph_runtime(group_id)


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
) -> dict[str, Any]:
    """Archive low-importance entities that haven't been accessed recently.

    Implements the Priority Decay forgetting policy from FiFA research:
    entities are scored by importance * recency_decay, and those below
    a threshold are archived (excluded from default search but still
    retrievable with include_archived=True).

    Only targets episodic entities — patterns, rules, tasks, and projects
    are preserved regardless of age.

    Args:
        ctx: arq context
        group_id: Organization ID
        min_age_days: Minimum age before an entity is eligible for archival
        max_archives_per_run: Safety cap on archives per execution

    Returns:
        Dict with archival statistics
    """
    from sibyl_core.models.entities import EntityType

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
        offset = 0
        candidates: list[str] = []

        while len(candidates) < max_archives_per_run:
            batch = await entity_manager.list_by_type(
                EntityType.EPISODE,
                limit=page_size,
                offset=offset,
                include_archived=False,
            )
            if not batch:
                break

            offset += len(batch)
            for entity in batch:
                status = str((entity.metadata or {}).get("status") or "").lower()
                if status == "archived" or entity.created_at >= cutoff:
                    continue
                candidates.append(entity.id)
                if len(candidates) >= max_archives_per_run:
                    break

        archived_count = 0
        now_iso = datetime.now(UTC).isoformat()
        for entity_id in candidates:
            try:
                await entity_manager.update(
                    entity_id,
                    {
                        "status": "archived",
                        "archived_at": now_iso,
                    },
                )
                archived_count += 1
            except Exception as e:
                log.warning("priority_decay_archive_failed", entity_id=entity_id, error=str(e))

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
