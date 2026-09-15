"""Drive one complete ordinary consolidation cycle over the 233 admitted captures.

The driver owns no memory logic. It calls the product job
``sibyl.jobs.reflection.run_reflection_dream_cycle`` in process, with the
product's own runtime bootstrap (schema, runtime settings, core runtime ports,
shared Surreal connectivity, local queue broker), and stops when every original
source is terminal. Nothing here writes to ``raw_captures`` or ``entity``.

Two product behaviours shape the loop.

The dream source pager wraps. ``list_reflection_dream_source_memories`` keysets
on ``uuid > $cursor`` and, when a page is exhausted, resets the cursor to "" and
pages again from the start. On 233 sources a blind third pass of 100 would pull
33 fresh sources and then 67 already-consolidated ones, form new cohort digests
over them, and pay for the repeat. So before each proposal pass the driver reads
the dispatch cursor and counts eligible sources above it, then asks for exactly
``min(source_page, 100, remaining)`` and stops when nothing remains.

A candidate whose automatic correction returns ``pending`` /
``unresolved_progress`` keeps ``review_state='pending'`` forever and is
re-selected by every drain. The driver bounds drain passes and stops early when
a pass produces no state transition, reporting those ids as ``stuck_pending``.

Pricing defaults are the campaign's accepted Anthropic binding; see the module
constants below for the artifact path they are cited from.
"""

# Product imports are deliberately lazy. run_phase stamps SIBYL_* into the
# environment before the first sibyl import, and the unit tests import this
# module with no database or broker in reach.
# ruff: noqa: PLC0415

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

DEFAULT_GROUP_ID = "b60d61fd-d388-4cb0-9581-eca4f583544b"
DEFAULT_PRINCIPAL_ID = "91bd4035-be71-4e14-a43f-c861fc75699c"

EXPECTED_SOURCES = 233

#: The product clamps ``source_limit`` into ``[0, 100]``; asking for more is a lie.
PRODUCT_SOURCE_LIMIT_CEILING = 100

#: The product clamps ``candidate_limit`` into ``[0, 200]``.
PRODUCT_CANDIDATE_LIMIT_CEILING = 200

#: The campaign's accepted Anthropic pricing binding for ``claude-opus-5``, in
#: USD per million tokens, as enforced by the ordinary-count contract
#: (``prepared-bundle/entry/count_contract.py`` rejects any quote whose rate
#: strings are not exactly "5" and "25").
#:
#: Caveat carried from the witness: 5/25 is the base tier. The same hashed
#: document lists a long-context tier of 10/50 per million above a 200K-token
#: prompt, and consolidation requests in this campaign have run 235K-272K input
#: tokens. The campaign binds 5/25 unconditionally, so a token-priced row here
#: is a lower bound whenever its request crossed that threshold. Rows whose
#: provider usage arrives with ``cost_complete`` are unaffected.
PRICING_SOURCE: str | None = (
    "/Users/bliss/dev/eval-artifacts/sibyl/full-cohort-pricing-20260914/sources.json"
    " (sha256 22623e55aff2bd2ed5c7062148c774f05430b1b6e9a81503f459c839c9597327)"
)
DEFAULT_PRICE_INPUT_PER_MILLION: Decimal | None = Decimal("5")
DEFAULT_PRICE_OUTPUT_PER_MILLION: Decimal | None = Decimal("25")

#: The campaign's own recorded ceiling is 300 USD
#: (``ordinary-count-outcome-root-20260915-r4bxev1s/acceptance.json``); this
#: lower default is the screen48 diagnostic's own guard and is overridable.
DEFAULT_COST_CEILING_USD = Decimal("232.75")

_RECEIPT_NAME = "cycle.json"
_INVOCATIONS_NAME = "invocations.jsonl"

#: Keyset page size for the driver's own read-only scans.
_SCAN_PAGE = 512

STATUS_COMPLETE = "complete_original_cycle"
STATUS_INCOMPLETE = "incomplete_original_cycle"
STATUS_COST_CEILING = "stopped_cost_ceiling"


class CycleError(RuntimeError):
    """The cycle could not be driven through product code paths."""


@dataclass(frozen=True, kw_only=True)
class CycleConfig:
    """Fixed policy for one consolidation-cycle run."""

    group_id: str = DEFAULT_GROUP_ID
    principal_id: str = DEFAULT_PRINCIPAL_ID
    source_page: int = PRODUCT_SOURCE_LIMIT_CEILING
    candidate_limit: int = PRODUCT_CANDIDATE_LIMIT_CEILING
    max_drain_passes: int = 6
    cost_ceiling_usd: Decimal
    price_input_per_million: Decimal
    price_output_per_million: Decimal
    expected_sources: int = EXPECTED_SOURCES


# --------------------------------------------------------------------------
# Pure policy
# --------------------------------------------------------------------------


def plan_source_page(remaining: int, source_page: int) -> int:
    """Return the source budget for one proposal pass, never past ``remaining``.

    Asking for more than remains is what makes the dream pager wrap onto
    already-consolidated sources and pay for them a second time.
    """
    return max(0, min(source_page, PRODUCT_SOURCE_LIMIT_CEILING, remaining))


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _token_cost(
    input_tokens: int,
    output_tokens: int,
    *,
    price_input_per_million: Decimal,
    price_output_per_million: Decimal,
) -> Decimal:
    million = Decimal(1_000_000)
    return (
        Decimal(input_tokens) * price_input_per_million
        + Decimal(output_tokens) * price_output_per_million
    ) / million


def summarize_usage(
    rows: Sequence[dict[str, Any]],
    *,
    price_input_per_million: Decimal,
    price_output_per_million: Decimal,
) -> dict[str, Any]:
    """Sum durable validation-execution usage into a priced total.

    ``cost_complete`` rows carry the provider's own ``cost_usd``. Everything
    else is priced from its token counts, and a ``FailedExtractionUsage`` row
    (no token fields at all) contributes zero of both.
    """
    totals = {
        "rows": 0,
        "rows_with_usage": 0,
        "rows_cost_complete": 0,
        "rows_priced_from_tokens": 0,
        "rows_unparsable": 0,
        "input_tokens": 0,
        "output_tokens": 0,
    }
    states: dict[str, int] = {}
    cost = Decimal(0)
    for row in rows:
        totals["rows"] += 1
        state = str(row.get("state") or "unknown")
        states[state] = states.get(state, 0) + 1
        raw = row.get("usage_json")
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            totals["rows_unparsable"] += 1
            continue
        if not isinstance(payload, dict):
            totals["rows_unparsable"] += 1
            continue
        totals["rows_with_usage"] += 1
        input_tokens = _as_int(payload.get("input_tokens"))
        output_tokens = _as_int(payload.get("output_tokens"))
        totals["input_tokens"] += input_tokens
        totals["output_tokens"] += output_tokens
        cost_usd = payload.get("cost_usd")
        if payload.get("cost_complete") is True and isinstance(cost_usd, int | float | str):
            totals["rows_cost_complete"] += 1
            cost += Decimal(str(cost_usd))
            continue
        totals["rows_priced_from_tokens"] += 1
        cost += _token_cost(
            input_tokens,
            output_tokens,
            price_input_per_million=price_input_per_million,
            price_output_per_million=price_output_per_million,
        )
    return {
        **totals,
        "states": states,
        "cost_usd": float(cost),
        "cost_usd_exact": str(cost),
        "price_input_per_million": str(price_input_per_million),
        "price_output_per_million": str(price_output_per_million),
    }


def terminal_status(
    predicates: dict[str, Any],
    *,
    stuck_pending: Sequence[str],
    cost_ceiling_hit: bool,
) -> tuple[str, list[str]]:
    """Fold the four predicates plus the two guards into one terminal status."""
    reasons: list[str] = [
        f"predicate_{name}_unsatisfied"
        for name in sorted(predicates)
        if predicates[name].get("satisfied") is not True
    ]
    if stuck_pending:
        reasons.append("stuck_pending_candidates")
    if cost_ceiling_hit:
        return STATUS_COST_CEILING, ["cost_ceiling_exceeded", *reasons]
    if reasons:
        return STATUS_INCOMPLETE, reasons
    return STATUS_COMPLETE, []


# --------------------------------------------------------------------------
# Product seams. Every database or product touch goes through one of these.
# --------------------------------------------------------------------------


def _excluded_capture_surfaces() -> list[str]:
    from sibyl_core.services.content_raw_recall import (
        _REFLECTION_DREAM_EXCLUDED_CAPTURE_SURFACES,
    )

    return sorted(_REFLECTION_DREAM_EXCLUDED_CAPTURE_SURFACES)


async def _content_rows(query: str, **params: object) -> list[dict[str, Any]]:
    from sibyl_core.services import content_client

    async with content_client.surreal_content_client() as client:
        rows = await content_client.select_many(client, query, **params)
    return [dict(row) for row in rows]


async def bootstrap_runtime() -> None:
    """Install the product runtime the queued worker installs, in process."""
    from sibyl.config import settings as api_settings
    from sibyl.coordination.broker import get_broker, get_queue_backend
    from sibyl.runtime_services import (
        bootstrap_surreal_runtime_schemas,
        install_core_runtime_ports,
        install_llm_db_config_source,
        load_runtime_settings_from_db,
    )
    from sibyl.services.surreal_connectivity import initialize_shared_surreal_connectivity

    backend = api_settings.resolved_coordination_backend
    if backend != "local":
        raise CycleError(f"coordination backend must be local, resolved {backend!r}")
    await bootstrap_surreal_runtime_schemas()
    await load_runtime_settings_from_db()
    install_llm_db_config_source()
    install_core_runtime_ports()
    await initialize_shared_surreal_connectivity()
    if get_queue_backend() != "local":
        raise CycleError("queue backend must be local")
    # Embeddings enqueue during the candidate drain, not only in the repair
    # phase, so the broker has to be live before the first invocation or every
    # enqueue is swallowed into {"status": "failed"}.
    await get_broker().startup()


async def shutdown_runtime() -> None:
    """Drain the local queue, then release the shared Surreal clients."""
    from sibyl.coordination.broker import get_broker
    from sibyl.services.surreal_connectivity import stop_surreal_connectivity_monitor
    from sibyl_core.services.content_client import close_shared_surreal_content_client

    await get_broker().shutdown()
    await stop_surreal_connectivity_monitor()
    await close_shared_surreal_content_client()


async def broker_health() -> dict[str, Any]:
    from sibyl.coordination.broker import get_broker

    return dict(await get_broker().health())


async def invoke_dream_cycle(
    *, group_id: str, source_limit: int, candidate_limit: int
) -> dict[str, Any]:
    """Run the product job once. ``ctx`` is ignored by the job itself."""
    from sibyl.jobs.reflection import run_reflection_dream_cycle

    return await run_reflection_dream_cycle(
        {},
        group_id,
        dry_run=False,
        source_limit=source_limit,
        candidate_limit=candidate_limit,
    )


async def load_cursor(group_id: str) -> tuple[str, int]:
    from sibyl_core.services.dream_checkpoints import load_dream_cursor

    return await load_dream_cursor(group_id)


async def count_eligible_sources(group_id: str, cursor: str) -> int:
    """Count dream-eligible captures above the dispatch cursor.

    Same organization and same excluded capture surfaces as the product pager,
    so the count and the pager agree on what a remaining source is.
    """
    rows = await _content_rows(
        "SELECT count() AS total FROM raw_captures "
        "WHERE organization_id = $org AND uuid > $cursor "
        "AND (capture_surface NOT IN $excluded OR capture_surface = NONE) "
        "GROUP ALL;",
        org=group_id,
        cursor=cursor,
        excluded=_excluded_capture_surfaces(),
    )
    return _as_int(rows[0].get("total")) if rows else 0


async def list_eligible_source_ids(group_id: str) -> list[str]:
    """Enumerate every dream-eligible capture id, keyset paged like the product."""
    identifiers: list[str] = []
    cursor = ""
    while True:
        rows = await _content_rows(
            "SELECT uuid FROM raw_captures "
            "WHERE organization_id = $org AND uuid > $cursor "
            "AND (capture_surface NOT IN $excluded OR capture_surface = NONE) "
            "ORDER BY uuid ASC LIMIT $limit;",
            org=group_id,
            cursor=cursor,
            excluded=_excluded_capture_surfaces(),
            limit=_SCAN_PAGE,
        )
        if not rows:
            return identifiers
        identifiers.extend(str(row["uuid"]) for row in rows)
        cursor = str(rows[-1]["uuid"])
        if len(rows) < _SCAN_PAGE:
            return identifiers


async def pending_candidate_ids(group_id: str, *, limit: int = 500) -> list[str]:
    from sibyl_core.services.surreal_content import list_reflection_candidate_reviews

    candidates = await list_reflection_candidate_reviews(
        organization_id=group_id, review_state="pending", limit=limit
    )
    return [candidate.id for candidate in candidates]


async def live_execution_count(group_id: str) -> int:
    rows = await _content_rows(
        "SELECT count() AS total FROM memory_validation_executions "
        "WHERE organization_id = $org AND state IN ['running', 'recorded'] GROUP ALL;",
        org=group_id,
    )
    return _as_int(rows[0].get("total")) if rows else 0


async def usage_rows(group_id: str, since: datetime) -> list[dict[str, Any]]:
    return await _content_rows(
        "SELECT usage_json, state, created_at FROM memory_validation_executions "
        "WHERE organization_id = $org AND created_at >= $since;",
        org=group_id,
        since=since,
    )


async def capture_surface_counts(group_id: str) -> dict[str, int]:
    rows = await _content_rows(
        "SELECT capture_surface, count() AS total FROM raw_captures "
        "WHERE organization_id = $org GROUP BY capture_surface;",
        org=group_id,
    )
    return {str(row.get("capture_surface")): _as_int(row.get("total")) for row in rows}


async def review_state_counts(group_id: str) -> dict[str, int]:
    rows = await _content_rows(
        "SELECT review_state, count() AS total FROM raw_captures "
        "WHERE organization_id = $org GROUP BY review_state;",
        org=group_id,
    )
    return {str(row.get("review_state")): _as_int(row.get("total")) for row in rows}


async def returned_execution_source_ids(group_id: str) -> set[str]:
    rows = await _content_rows(
        "SELECT source_ids FROM memory_validation_executions "
        "WHERE organization_id = $org AND state = 'returned';",
        org=group_id,
    )
    covered: set[str] = set()
    for row in rows:
        value = row.get("source_ids")
        if isinstance(value, list | tuple):
            covered.update(str(item) for item in value)
    return covered


async def completed_checkpoint_source_ids(group_id: str) -> set[str]:
    rows = await _content_rows(
        "SELECT source_id FROM dream_source_checkpoints "
        "WHERE organization_id = $org AND completion_json != NONE;",
        org=group_id,
    )
    return {str(row["source_id"]) for row in rows if row.get("source_id")}


async def graph_runtime(group_id: str):
    from sibyl_core.services.graph_runtime import get_surreal_graph_runtime

    return await get_surreal_graph_runtime(group_id)


async def entity_count(group_id: str) -> int:
    from sibyl_core.services.graph_common import normalize_graph_records

    runtime = await graph_runtime(group_id)
    rows = normalize_graph_records(
        await runtime.client.execute_query(
            "SELECT count() AS total FROM entity WHERE group_id = $group_id GROUP ALL;",
            group_id=group_id,
        )
    )
    return _as_int(dict(rows[0]).get("total")) if rows else 0


async def missing_embedding_candidate_ids(group_id: str) -> list[str]:
    """Scan promoted candidates whose ``name_embedding`` is absent or stale.

    Same projection and same currency test as ``repair_promoted_embeddings``,
    read only: this is predicate (c), not a repair.
    """
    from sibyl_core.embeddings.providers import configured_embedding_provider
    from sibyl_core.services.graph_common import normalize_graph_records
    from sibyl_core.services.memory_embedding import _embedding_current

    provider = configured_embedding_provider()
    if provider is None:
        return []
    runtime = await graph_runtime(group_id)
    missing: list[str] = []
    cursor = ""
    while True:
        rows = [
            dict(row)
            for row in normalize_graph_records(
                await runtime.client.execute_query(
                    "SELECT uuid, (name_embedding != NONE) AS embedding_present, "
                    "attributes.embedding_metadata AS embedding_metadata "
                    "FROM entity WITH INDEX idx_entity_reflection_candidate_uuid "
                    "WHERE group_id=$group_id AND uuid > $cursor AND derivation_required=true "
                    "AND attributes.reflection_identity.purpose='candidate' "
                    "AND attributes.operational_source_id IS NONE "
                    "ORDER BY uuid LIMIT $limit;",
                    group_id=group_id,
                    cursor=cursor,
                    limit=_SCAN_PAGE,
                )
            )
        ]
        if not rows:
            return missing
        cursor = str(rows[-1]["uuid"])
        missing.extend(
            str(row["uuid"])
            for row in rows
            if not _embedding_current(
                row.get("embedding_present") is True,
                row.get("embedding_metadata"),
                provider.metadata,
            )
        )
        if len(rows) < _SCAN_PAGE:
            return missing


async def repair_embeddings(group_id: str) -> dict[str, int]:
    """Re-enqueue lost embedding work through the product repair path."""
    from dataclasses import asdict

    from sibyl_core.embeddings.providers import configured_embedding_provider
    from sibyl_core.services.memory_embedding import repair_promoted_embeddings

    if configured_embedding_provider() is None:
        return {"checked": 0, "recovered": 0, "pending": 0, "failed": 0, "disabled": 1}
    runtime = await graph_runtime(group_id)
    return dict(asdict(await repair_promoted_embeddings(runtime)))


# --------------------------------------------------------------------------
# Receipt assembly
# --------------------------------------------------------------------------


def _archived_exception_reasons(receipts: Sequence[dict[str, Any]]) -> dict[str, int]:
    """Count auto-archived route-to-review exceptions by reason.

    These are lost memories, not parked ones, so the receipt names them.
    """
    counts: dict[str, int] = {}
    for receipt in receipts:
        for candidate in receipt.get("candidates") or []:
            if candidate.get("archived") is not True:
                continue
            reasons = candidate.get("exception_reasons") or []
            named = [str(reason) for reason in reasons] or [
                str(candidate.get("reason") or "unnamed")
            ]
            for reason in named:
                counts[reason] = counts.get(reason, 0) + 1
    return counts


def _embedding_statuses(receipts: Sequence[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for receipt in receipts:
        for candidate in receipt.get("candidates") or []:
            backfill = candidate.get("embedding_backfill")
            if isinstance(backfill, dict):
                status = str(backfill.get("status") or "unknown")
                counts[status] = counts.get(status, 0) + 1
    return counts


class _Evidence:
    """Durable partial evidence: one appended line per product invocation."""

    def __init__(self, output: Path) -> None:
        self.output = output
        self.receipt_path = output / _RECEIPT_NAME
        self.invocations_path = output / _INVOCATIONS_NAME
        self.records: list[dict[str, Any]] = []

    def reserve(self, header: dict[str, Any]) -> None:
        self.output.mkdir(parents=True, exist_ok=True)
        try:
            with self.receipt_path.open("x", encoding="utf-8") as handle:
                json.dump({**header, "status": "running"}, handle, indent=2, sort_keys=True)
                handle.write("\n")
        except FileExistsError as exc:
            raise CycleError(f"{self.receipt_path} already exists; refusing to overwrite") from exc

    def append(self, record: dict[str, Any]) -> None:
        self.records.append(record)
        with self.invocations_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def finish(self, receipt: dict[str, Any]) -> None:
        with self.receipt_path.open("w", encoding="utf-8") as handle:
            json.dump(receipt, handle, indent=2, sort_keys=True, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())


class _CostGuard:
    """Stop dispatching once measured spend passes the ceiling."""

    def __init__(self, config: CycleConfig, started: datetime) -> None:
        self.config = config
        self.started = started
        self.usage: dict[str, Any] = summarize_usage(
            [],
            price_input_per_million=config.price_input_per_million,
            price_output_per_million=config.price_output_per_million,
        )
        self.exceeded = False

    async def measure(self) -> dict[str, Any]:
        rows = await usage_rows(self.config.group_id, self.started)
        self.usage = summarize_usage(
            rows,
            price_input_per_million=self.config.price_input_per_million,
            price_output_per_million=self.config.price_output_per_million,
        )
        if Decimal(self.usage["cost_usd_exact"]) > self.config.cost_ceiling_usd:
            self.exceeded = True
        return self.usage


async def _snapshot(config: CycleConfig) -> dict[str, Any]:
    cursor_id, cursor_revision = await load_cursor(config.group_id)
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "capture_surface_counts": await capture_surface_counts(config.group_id),
        "review_state_counts": await review_state_counts(config.group_id),
        "entity_count": await entity_count(config.group_id),
        "dream_cursor": {"source_id": cursor_id, "revision": cursor_revision},
        "eligible_sources_above_cursor": await count_eligible_sources(config.group_id, cursor_id),
        "live_executions": await live_execution_count(config.group_id),
        "pending_candidates": len(await pending_candidate_ids(config.group_id)),
    }


async def _run_invocation(
    config: CycleConfig,
    evidence: _Evidence,
    guard: _CostGuard,
    *,
    phase: str,
    source_limit: int,
    context: dict[str, Any],
) -> dict[str, Any]:
    started = datetime.now(UTC)
    receipt = await invoke_dream_cycle(
        group_id=config.group_id,
        source_limit=source_limit,
        candidate_limit=config.candidate_limit,
    )
    finished = datetime.now(UTC)
    usage = await guard.measure()
    evidence.append(
        {
            "index": len(evidence.records),
            "phase": phase,
            "requested_source_limit": source_limit,
            "requested_candidate_limit": config.candidate_limit,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "measured_cost_usd": usage["cost_usd_exact"],
            "cost_ceiling_usd": str(config.cost_ceiling_usd),
            "cost_ceiling_exceeded": guard.exceeded,
            "context": context,
            "receipt": receipt,
        }
    )
    return receipt


async def _proposal_passes(
    config: CycleConfig, evidence: _Evidence, guard: _CostGuard
) -> dict[str, Any]:
    """Propose over every remaining source without ever wrapping the pager."""
    passes: list[dict[str, Any]] = []
    while True:
        cursor_id, cursor_revision = await load_cursor(config.group_id)
        remaining = await count_eligible_sources(config.group_id, cursor_id)
        budget = plan_source_page(remaining, config.source_page)
        if budget <= 0:
            break
        context = {
            "cursor_source_id": cursor_id,
            "cursor_revision": cursor_revision,
            "remaining_above_cursor": remaining,
        }
        receipt = await _run_invocation(
            config,
            evidence,
            guard,
            phase="proposal",
            source_limit=budget,
            context=context,
        )
        passes.append(
            {
                **context,
                "source_limit": budget,
                "sources_scanned": receipt.get("sources_scanned"),
                "sources_reflected": receipt.get("sources_reflected"),
                "run_id": receipt.get("run_id"),
            }
        )
        if guard.exceeded:
            break
        if len(passes) > config.expected_sources:
            raise CycleError("proposal passes exceeded the source count; refusing to spin")
    cursor_id, _ = await load_cursor(config.group_id)
    return {
        "passes": passes,
        "remaining_above_cursor": await count_eligible_sources(config.group_id, cursor_id),
    }


async def _drain_passes(
    config: CycleConfig, evidence: _Evidence, guard: _CostGuard
) -> dict[str, Any]:
    """Drain pending candidates with ``source_limit=0`` until nothing moves."""
    passes: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    stuck: list[str] = []
    for index in range(config.max_drain_passes):
        before = set(await pending_candidate_ids(config.group_id))
        for candidate_id in before:
            seen[candidate_id] = seen.get(candidate_id, 0) + 1
        if not before:
            break
        receipt = await _run_invocation(
            config,
            evidence,
            guard,
            phase="drain",
            source_limit=0,
            context={"pass_index": index, "pending_before": len(before)},
        )
        after = set(await pending_candidate_ids(config.group_id))
        passes.append(
            {
                "pass_index": index,
                "pending_before": sorted(before),
                "pending_after": sorted(after),
                "candidates_scanned": receipt.get("candidates_scanned"),
                "promoted": receipt.get("promoted"),
                "archived": receipt.get("archived"),
                "run_id": receipt.get("run_id"),
            }
        )
        if guard.exceeded:
            break
        if after == before:
            stuck = sorted(after)
            break
    return {
        "passes": passes,
        "stuck_pending": stuck,
        "pending_seen_counts": dict(sorted(seen.items())),
        "exhausted_pass_budget": len(passes) >= config.max_drain_passes and not stuck,
    }


async def _predicates(config: CycleConfig, originals: Sequence[str]) -> dict[str, Any]:
    pending = await pending_candidate_ids(config.group_id)
    live = await live_execution_count(config.group_id)
    missing = await missing_embedding_candidate_ids(config.group_id)
    covered = await returned_execution_source_ids(config.group_id)
    completed = await completed_checkpoint_source_ids(config.group_id)
    uncovered = sorted(set(originals) - covered - completed)
    return {
        "a_no_pending_candidates": {"satisfied": not pending, "pending_ids": pending},
        "b_no_live_executions": {"satisfied": live == 0, "live_executions": live},
        "c_no_missing_embeddings": {
            "satisfied": not missing,
            "missing_entity_ids": missing,
        },
        "d_every_source_terminal": {
            "satisfied": not uncovered and len(originals) == config.expected_sources,
            "original_source_count": len(originals),
            "expected_sources": config.expected_sources,
            "covered_by_returned_execution": len(set(originals) & covered),
            "covered_by_checkpoint_completion": len(set(originals) & completed),
            "uncovered_source_ids": uncovered,
        },
    }


def _header(config: CycleConfig, started: datetime) -> dict[str, Any]:
    return {
        "group_id": config.group_id,
        "principal_id": config.principal_id,
        "started_at": started.isoformat(),
        "config": {
            "source_page": config.source_page,
            "candidate_limit": config.candidate_limit,
            "max_drain_passes": config.max_drain_passes,
            "cost_ceiling_usd": str(config.cost_ceiling_usd),
            "price_input_per_million": str(config.price_input_per_million),
            "price_output_per_million": str(config.price_output_per_million),
            "expected_sources": config.expected_sources,
            "pricing_source": PRICING_SOURCE,
        },
    }


async def _dispatch_phases(
    config: CycleConfig, evidence: _Evidence, guard: _CostGuard, receipt: dict[str, Any]
) -> None:
    receipt["original_source_ids"] = await list_eligible_source_ids(config.group_id)
    receipt["snapshot_before"] = await _snapshot(config)
    receipt["proposal"] = await _proposal_passes(config, evidence, guard)
    if not guard.exceeded:
        receipt["drain"] = await _drain_passes(config, evidence, guard)
    receipt["embedding_repair"] = await repair_embeddings(config.group_id)
    receipt["broker_health"] = await broker_health()


async def _finalize(
    config: CycleConfig, evidence: _Evidence, guard: _CostGuard, receipt: dict[str, Any]
) -> None:
    """Read the after-state while the runtime is still up."""
    receipt["snapshot_after"] = await _snapshot(config)
    receipt["predicates"] = await _predicates(config, receipt.get("original_source_ids") or [])
    receipt["usage"] = await guard.measure()
    invocation_receipts = [record["receipt"] for record in evidence.records]
    receipt["archived_exception_reasons"] = _archived_exception_reasons(invocation_receipts)
    receipt["embedding_enqueue_statuses"] = _embedding_statuses(invocation_receipts)


def _seal(
    receipt: dict[str, Any], guard: _CostGuard, evidence: _Evidence, errors: list[str]
) -> None:
    drain = receipt.get("drain") or {}
    predicates = receipt.get("predicates")
    if not isinstance(predicates, dict) or not predicates:
        predicates = {"a_no_pending_candidates": {"satisfied": False}}
    status, reasons = terminal_status(
        predicates,
        stuck_pending=drain.get("stuck_pending") or [],
        cost_ceiling_hit=guard.exceeded,
    )
    if errors:
        status = STATUS_COST_CEILING if guard.exceeded else STATUS_INCOMPLETE
        reasons = [*reasons, *(f"driver_error:{error}" for error in errors)]
    receipt.setdefault("usage", guard.usage)
    receipt["errors"] = errors
    receipt["invocations"] = len(evidence.records)
    receipt["status"] = status
    receipt["reasons"] = reasons
    receipt["finished_at"] = datetime.now(UTC).isoformat()


async def run_cycle(config: CycleConfig, output: Path) -> dict[str, Any]:
    """Drive the ordinary consolidation cycle and write a durable receipt."""
    started = datetime.now(UTC)
    evidence = _Evidence(output)
    receipt: dict[str, Any] = dict(_header(config, started))
    evidence.reserve(receipt)
    guard = _CostGuard(config, started)
    errors: list[str] = []
    try:
        await bootstrap_runtime()
        try:
            await _dispatch_phases(config, evidence, guard, receipt)
        except Exception as exc:  # a dispatch failure still owes evidence
            errors.append(f"{type(exc).__name__}: {exc}")
        try:
            await _finalize(config, evidence, guard, receipt)
        except Exception as exc:  # so does a read-back failure
            errors.append(f"finalize:{type(exc).__name__}: {exc}")
    finally:
        try:
            await shutdown_runtime()
        except Exception as exc:  # a drain failure must not eat the receipt
            errors.append(f"shutdown:{type(exc).__name__}: {exc}")
    _seal(receipt, guard, evidence, errors)
    evidence.finish(receipt)
    return receipt


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="screen48-cycle", description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--ceiling-usd",
        type=Decimal,
        default=DEFAULT_COST_CEILING_USD,
        help="stop dispatching once measured spend passes this",
    )
    parser.add_argument(
        "--price-input",
        type=Decimal,
        default=DEFAULT_PRICE_INPUT_PER_MILLION,
        required=DEFAULT_PRICE_INPUT_PER_MILLION is None,
        help="USD per million input tokens",
    )
    parser.add_argument(
        "--price-output",
        type=Decimal,
        default=DEFAULT_PRICE_OUTPUT_PER_MILLION,
        required=DEFAULT_PRICE_OUTPUT_PER_MILLION is None,
        help="USD per million output tokens",
    )
    parser.add_argument("--group-id", default=DEFAULT_GROUP_ID)
    parser.add_argument("--principal-id", default=DEFAULT_PRINCIPAL_ID)
    parser.add_argument("--source-page", type=int, default=PRODUCT_SOURCE_LIMIT_CEILING)
    parser.add_argument("--candidate-limit", type=int, default=PRODUCT_CANDIDATE_LIMIT_CEILING)
    parser.add_argument("--max-drain-passes", type=int, default=6)
    parser.add_argument("--expected-sources", type=int, default=EXPECTED_SOURCES)
    return parser


def config_from_args(args: argparse.Namespace) -> CycleConfig:
    return CycleConfig(
        group_id=args.group_id,
        principal_id=args.principal_id,
        source_page=args.source_page,
        candidate_limit=args.candidate_limit,
        max_drain_passes=args.max_drain_passes,
        cost_ceiling_usd=args.ceiling_usd,
        price_input_per_million=args.price_input,
        price_output_per_million=args.price_output,
        expected_sources=args.expected_sources,
    )


def main(argv: Sequence[str] | None = None) -> int:
    import asyncio

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        receipt = asyncio.run(run_cycle(config_from_args(args), args.output))
    except CycleError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    sys.stdout.write(
        json.dumps(
            {
                "status": receipt["status"],
                "reasons": receipt["reasons"],
                "invocations": receipt["invocations"],
                "cost_usd": receipt["usage"]["cost_usd_exact"],
                "receipt": str(args.output / _RECEIPT_NAME),
            },
            sort_keys=True,
        )
        + "\n"
    )
    return 0 if receipt["status"] == STATUS_COMPLETE else 1


if __name__ == "__main__":
    raise SystemExit(main())
