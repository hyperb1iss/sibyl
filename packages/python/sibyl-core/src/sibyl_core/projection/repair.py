"""Discover and retry pending graph lifecycle checks without regenerating text."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass

import structlog

from sibyl_core.projection.pending import PENDING_KEYS
from sibyl_core.projection.reconcile import reconcile_with_capture, reconcile_with_parent
from sibyl_core.services.graph_common import normalize_graph_records
from sibyl_core.services.graph_runtime import GraphRuntime

log = structlog.get_logger()
_PAGE_SIZE = 512
# Leave the terminator to callers so qualification can append EXPLAIN.
PENDING_REPAIR_QUERY = (
    "SELECT uuid, lifecycle_repair_key FROM entity WITH INDEX idx_entity_lifecycle_repair_key "
    "WHERE lifecycle_repair_key > $cursor "
    "AND group_id = $group_id ORDER BY lifecycle_repair_key LIMIT $limit"
)


@dataclass(frozen=True, slots=True)
class LifecycleRepairResult:
    """Recovered counts cleared checks; authored exclusions may still apply."""

    checked: int = 0
    recovered: int = 0
    pending: int = 0
    failed: int = 0


async def _repair_row(runtime: GraphRuntime, row_id: str) -> str:
    manager = runtime.entity_manager
    try:
        row = await manager.get(row_id)
    except KeyError:
        return "missing"
    owners: set[str] = set()
    for key in PENDING_KEYS:
        value = row.metadata.get(key)
        if isinstance(value, Mapping):
            owners.update(
                owner for owner, pending in value.items() if pending and isinstance(owner, str)
            )
    for owner in sorted(owners):
        kind, separator, source_id = owner.partition(":")
        if not separator or not source_id or owner == "capture:unbound":
            continue
        if kind == "capture-signature-v1":
            capture_id, separator, signature = source_id.rpartition(":")
            if (
                not separator
                or not capture_id
                or len(signature) != 64
                or any(char not in "0123456789abcdef" for char in signature)
            ):
                continue
            await reconcile_with_capture(
                manager,
                organization_id=runtime.client.group_id,
                metadata={"raw_memory_id": capture_id},
                row_ids=[row_id],
                expected_signature=signature,
            )
        elif kind == "parent":
            await reconcile_with_parent(
                manager,
                source_id=source_id,
                row_ids=[row_id],
                organization_id=runtime.client.group_id,
            )
        elif kind in {"capture", "capture-source"}:
            locator = "raw_memory_id" if kind == "capture" else "raw_source_id"
            await reconcile_with_capture(
                manager,
                organization_id=runtime.client.group_id,
                metadata={locator: source_id},
                row_ids=[row_id],
            )
    try:
        current = await manager.get(row_id)
    except KeyError:
        return "missing"
    return "pending" if any(current.metadata.get(key) for key in PENDING_KEYS) else "recovered"


async def repair_graph_lifecycle(runtime: GraphRuntime) -> LifecycleRepairResult:
    """Retry each pending row once per sweep with the existing source-owned CAS.

    Unknown legacy owners stay pending until their provenance can be migrated.
    Keyset pagination advances past unresolved rows so they cannot starve later
    work. Concurrent sweeps are safe because every reconciliation is fenced.
    """
    cursor = ""
    counts = {"checked": 0, "recovered": 0, "pending": 0, "failed": 0}
    while True:
        rows = normalize_graph_records(
            await runtime.client.execute_query(
                PENDING_REPAIR_QUERY + ";",
                cursor=cursor,
                group_id=runtime.client.group_id,
                limit=_PAGE_SIZE,
            )
        )
        if not rows:
            break
        outcomes = await asyncio.gather(
            *(_repair_row(runtime, str(row["uuid"])) for row in rows),
            return_exceptions=True,
        )
        counts["checked"] += len(rows)
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                counts["failed"] += 1
                log.warning(
                    "lifecycle_repair_row_failed",
                    group_id=runtime.client.group_id,
                    error_type=type(outcome).__name__,
                )
            elif outcome in counts:
                counts[outcome] += 1
        cursor = str(rows[-1]["lifecycle_repair_key"])
        if len(rows) < _PAGE_SIZE:
            break
    return LifecycleRepairResult(**counts)
