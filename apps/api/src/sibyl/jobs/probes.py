"""Re-ask probe-carrying memories whether they can still be found.

A write-time rehearsal answers for the graph as it stood at that moment. The
graph keeps moving: new memories crowd the ranking, consolidation merges rows,
decay archives them, and an embedding backfill can change what a query matches.
A memory that was retrievable on Tuesday can be unreachable by Friday without
anything having been written to it.

So the probes run again on a schedule and the verdict lands on the row as
``probe_last_replay``. The summary log is metric-shaped on purpose: probes_total
and probes_retrievable per org make "percent of probe-carrying memories that can
still find themselves" a query over logs rather than a study.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from sibyl_core.memory_pipeline.rehearsal import rehearse_memory_probes
from sibyl_core.memory_pipeline.structure import (
    PROBE_LAST_REPLAY_METADATA_KEY,
    probes_from_metadata,
)

log = structlog.get_logger()

# One replay is a search per probe, so the window and the cap are what bound the
# job. A week covers every memory written since the last several runs, which
# means a row missed to an outage is picked up rather than skipped forever.
DEFAULT_REPLAY_WINDOW_HOURS = 168
DEFAULT_MAX_MEMORIES_PER_RUN = 200

# Three column names are load-bearing and none of them is the obvious one. The
# model's ``metadata`` is stored in the ``attributes`` column. The logical id an
# entity is addressed by is ``uuid``; the ``id`` column holds a Surreal record id
# that ``EntityManager.update`` (which matches ``WHERE uuid = $uuid``) and search
# results do not speak. And ``created_at`` has to appear in a non-star projection
# for 3.x to accept ordering on it.
_CANDIDATE_QUERY = """
SELECT uuid, attributes, created_at FROM entity
WHERE attributes.memory_probes != NONE
  AND created_at > $since
ORDER BY created_at DESC
LIMIT $limit;
"""

# One query for the whole batch rather than one per parent: a passage counts as
# finding its memory, so the targets have to be known before any probe runs.
_PASSAGE_QUERY = """
SELECT uuid, attributes.parent_entity_id AS parent_entity_id FROM entity
WHERE attributes.parent_entity_id IN $parents;
"""


async def _get_graph_runtime(group_id: str) -> Any:
    from sibyl_core.services.graph import get_surreal_graph_runtime

    return await get_surreal_graph_runtime(group_id)


async def _list_organization_ids() -> list[str]:
    from sibyl.persistence.organization_runtime import list_org_ids

    return await list_org_ids()


async def replay_memory_probes(
    ctx: dict[str, Any],  # noqa: ARG001
    group_id: str,
    window_hours: int = DEFAULT_REPLAY_WINDOW_HOURS,
    max_memories: int = DEFAULT_MAX_MEMORIES_PER_RUN,
) -> dict[str, Any]:
    """Re-run every probe on recently written memories in one org.

    Args:
        ctx: arq context
        group_id: Organization ID whose memories are replayed
        window_hours: How far back to look for probe-carrying memories
        max_memories: Safety cap on memories replayed per execution

    Returns:
        Counters for the run, including how many probes still find their memory.
    """
    from sibyl_core.services.graph import normalize_records

    started_at = time.perf_counter()
    log.info("probe_replay_started", group_id=group_id, window_hours=window_hours)

    runtime = await _get_graph_runtime(group_id)
    client = runtime.client
    entity_manager = runtime.entity_manager

    since = datetime.now(UTC) - timedelta(hours=max(int(window_hours), 1))
    rows = normalize_records(
        await client.execute_query(
            _CANDIDATE_QUERY,
            since=since,
            limit=max(int(max_memories), 1),
            _query_label="probes.replay.candidates",
        )
    )
    candidates = [
        (entity_id, metadata, probes)
        for row in rows
        if (entity_id := str(row.get("uuid") or ""))
        and isinstance(metadata := row.get("attributes"), dict)
        and (probes := probes_from_metadata(metadata))
    ]
    if not candidates:
        log.info(
            "probe_replay_completed",
            group_id=group_id,
            memories=0,
            probes_total=0,
            probes_retrievable=0,
            duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
        )
        return {
            "group_id": group_id,
            "memories": 0,
            "probes_total": 0,
            "probes_retrievable": 0,
            "failures": 0,
        }

    passages = await _passages_by_parent(
        client,
        normalize_records,
        [entity_id for entity_id, _metadata, _probes in candidates],
    )

    memories = 0
    probes_total = 0
    probes_retrievable = 0
    failures = 0
    for entity_id, metadata, probes in candidates:
        scope_key = _optional_str(metadata.get("scope_key"))
        try:
            receipt = await rehearse_memory_probes(
                probes=probes,
                organization_id=group_id,
                entity_id=entity_id,
                passage_ids=passages.get(entity_id, ()),
                principal_id=_optional_str(metadata.get("principal_id")),
                memory_scope=str(metadata.get("memory_scope") or "private"),
                scope_key=scope_key,
                project=_optional_str(metadata.get("project_id")),
                accessible_projects={scope_key} if scope_key else None,
                surface="replay",
            )
        except Exception as exc:
            failures += 1
            log.warning(
                "probe_replay_memory_failed",
                group_id=group_id,
                entity_id=entity_id,
                error_type=type(exc).__name__,
            )
            continue

        memories += 1
        probes_total += int(receipt.get("total") or 0)
        probes_retrievable += int(receipt.get("retrievable") or 0)
        try:
            written = await entity_manager.update(
                entity_id,
                {"metadata": {**metadata, PROBE_LAST_REPLAY_METADATA_KEY: receipt}},
            )
            if written is None:
                # The update matches on the logical id and returns None rather
                # than raising when nothing matched, so an addressing mistake
                # here would otherwise look like a clean run forever.
                failures += 1
                log.warning(
                    "probe_replay_receipt_matched_no_row",
                    group_id=group_id,
                    entity_id=entity_id,
                )
        except Exception as exc:
            failures += 1
            log.warning(
                "probe_replay_receipt_not_stored",
                group_id=group_id,
                entity_id=entity_id,
                error_type=type(exc).__name__,
            )

    summary = {
        "group_id": group_id,
        "memories": memories,
        "probes_total": probes_total,
        "probes_retrievable": probes_retrievable,
        "failures": failures,
    }
    log.info(
        "probe_replay_completed",
        duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
        **summary,
    )
    return summary


async def replay_memory_probes_all_orgs(ctx: dict[str, Any]) -> dict[str, Any]:
    """Replay probes across every organization.

    One org's retrieval being down says nothing about the next one's, so a
    failure is counted and the walk continues.
    """
    log.info("probe_replay_all_orgs_started")

    org_ids = await _list_organization_ids()
    orgs_succeeded = 0
    orgs_failed = 0
    probes_total = 0
    probes_retrievable = 0
    for org_id in org_ids:
        try:
            result = await replay_memory_probes(ctx, group_id=org_id)
        except Exception as exc:
            orgs_failed += 1
            log.warning("probe_replay_org_failed", org_id=org_id, error_type=type(exc).__name__)
            continue
        orgs_succeeded += 1
        probes_total += int(result.get("probes_total") or 0)
        probes_retrievable += int(result.get("probes_retrievable") or 0)

    summary = {
        "orgs_processed": len(org_ids),
        "orgs_succeeded": orgs_succeeded,
        "orgs_failed": orgs_failed,
        "probes_total": probes_total,
        "probes_retrievable": probes_retrievable,
        # Emitted rather than derived downstream so a single log line answers
        # "are our memories still findable" without a join.
        "self_retrievable_pct": (
            round(100.0 * probes_retrievable / probes_total, 2) if probes_total else None
        ),
    }
    log.info("probe_replay_all_orgs_completed", **summary)
    return summary


async def _passages_by_parent(
    client: Any,
    normalize: Any,
    parents: list[str],
) -> dict[str, tuple[str, ...]]:
    if not parents:
        return {}
    try:
        rows = normalize(
            await client.execute_query(
                _PASSAGE_QUERY,
                parents=parents,
                _query_label="probes.replay.passages",
            )
        )
    except Exception as exc:
        # A probe can still match the parent, so a missing passage map costs
        # recall on the receipt rather than the whole replay.
        log.warning("probe_replay_passage_lookup_failed", error_type=type(exc).__name__)
        return {}
    grouped: dict[str, list[str]] = {}
    for row in rows:
        parent = _optional_str(row.get("parent_entity_id"))
        passage_id = _optional_str(row.get("uuid"))
        if parent and passage_id:
            grouped.setdefault(parent, []).append(passage_id)
    return {parent: tuple(ids) for parent, ids in grouped.items()}


def _optional_str(value: object) -> str | None:
    return str(value) if value else None


__all__ = [
    "DEFAULT_MAX_MEMORIES_PER_RUN",
    "DEFAULT_REPLAY_WINDOW_HOURS",
    "replay_memory_probes",
    "replay_memory_probes_all_orgs",
]
