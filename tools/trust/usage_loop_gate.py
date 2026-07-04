#!/usr/bin/env python3
"""Run the focused release gate for memory usage feedback loops."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from shutil import which
from types import SimpleNamespace
from typing import Any

from sibyl.jobs.consolidation import _priority_decay_score
from sibyl_core.backends.surreal.records import coerce_datetime
from tools.trust.dogfood_receipts import (
    DOGFOOD_DEPLOYMENT_BUDGETS,
    DebugQueryRunner,
    build_deployment_metrics,
    evidence_checks,
    list_of_mappings,
    load_deployment_evidence,
    load_dogfood_evidence,
    run_sibyl_debug_query,
    string_value,
    validate_metric_budgets,
    validate_required_checks,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RECEIPT_SCHEMA_VERSION = "sibyl-usage-loop-receipt-v1"
DOGFOOD_RECEIPT_SCHEMA_VERSION = "sibyl-usage-loop-dogfood-receipt-v1"
DEFAULT_RECEIPT_PATH = (
    REPO_ROOT / "benchmarks" / "results" / "ai-memory" / "usage-loop-receipt.json"
)
DEFAULT_DOGFOOD_RECEIPT_PATH = (
    REPO_ROOT / "benchmarks" / "results" / "ai-memory" / "usage-loop-dogfood-receipt.json"
)

Runner = Callable[[tuple[str, ...]], int]
Echo = Callable[[str], None]

USAGE_LOOP_BUDGETS = {
    "exposure_stamp_coverage": 1.0,
    "citation_event_count": 1,
    "duplicate_stored_event_count": 0,
    "usage_ordered_consolidation_input_count": 1,
    "cited_decay_score_advantage": 0.1,
}
DOGFOOD_USAGE_LOOP_BUDGETS = {
    **DOGFOOD_DEPLOYMENT_BUDGETS,
    "exposure_event_count": 1.0,
    "citation_event_count": 1.0,
    "duplicate_stored_event_count": 0.0,
    "dedupe_key_coverage": 1.0,
    "usage_stamp_coverage": 1.0,
    "cited_decay_score_advantage": 0.1,
}
DOGFOOD_LOWER_IS_BETTER = frozenset(("duplicate_stored_event_count",))
DOGFOOD_REQUIRED_SURFACES: tuple[str, ...] = (
    "live deployment provenance",
    "live exposure events",
    "live citation events",
    "live dedupe",
    "live usage stamps",
    "live decay divergence",
    "dogfood approval boundary",
)
USAGE_EVENTS_QUERY = """
SELECT uuid, session_key, message_key, source_surface, item_kind, item_id,
    signal_type, event_at, created_at
FROM memory_usage_events
ORDER BY event_at DESC
LIMIT {limit}
"""
RAW_USAGE_STAMPS_QUERY = """
SELECT uuid, created_at, last_recalled_at, last_used_at, retrieval_count, citation_count
FROM raw_captures
WHERE last_recalled_at != NONE OR last_used_at != NONE
ORDER BY created_at DESC
LIMIT {limit}
"""
GRAPH_USAGE_STAMPS_QUERY = """
SELECT uuid, name, entity_type, created_at, status, last_recalled_at, last_used_at,
    retrieval_count, citation_count, attributes, metadata
FROM entity
WHERE group_id = $group_id
ORDER BY created_at DESC
LIMIT {limit}
"""


@dataclass(frozen=True)
class GateCheck:
    name: str
    description: str
    surfaces: tuple[str, ...]
    command: tuple[str, ...]


@dataclass(frozen=True)
class GateResult:
    check: GateCheck
    exit_code: int
    elapsed_seconds: float
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


GATE_CHECKS: tuple[GateCheck, ...] = (
    GateCheck(
        name="core-usage-feedback",
        description="usage recorder, exposure stamping, and citation accounting",
        surfaces=("exposure stamping", "citation stamping", "idempotence"),
        command=(
            "moon",
            "run",
            "core:test",
            "--",
            "tests/test_usage_service.py",
            "tests/test_tools.py",
            "-k",
            "record_memory_usage or usage_exposure or usage_citation",
        ),
    ),
    GateCheck(
        name="api-usage-citation",
        description="REST and MCP citation surfaces stamp usage feedback",
        surfaces=("cite endpoint", "task completion", "reflect", "mcp", "citation stamping"),
        command=(
            "moon",
            "run",
            "api:test",
            "--",
            "tests/test_routes_tasks.py::TestCompleteTaskRoute::"
            "test_complete_task_records_cited_memories",
            "tests/test_routes_context.py::TestReflectRoute::test_reflect_records_cited_memories",
            "tests/test_routes_memory.py::test_cite_memory_records_usage_and_audit",
            "tests/test_server_accessible_projects.py::"
            "test_manage_mcp_complete_task_records_cited_memories",
            "tests/test_server_accessible_projects.py::"
            "test_reflect_mcp_memory_records_cited_memories",
        ),
    ),
    GateCheck(
        name="cli-usage-citation",
        description="CLI cite, reflect, and task completion pass cited IDs",
        surfaces=("cli", "cite endpoint", "task completion", "reflect"),
        command=(
            "moon",
            "run",
            "cli:test",
            "--",
            "tests/test_main_capture.py::test_cite_command_records_cited_memories",
            "tests/test_main_capture.py::test_reflect_command_passes_cited_ids",
            "tests/test_task.py::test_task_complete_with_cited_ids_reports_usage",
        ),
    ),
    GateCheck(
        name="api-usage-aware-consolidation",
        description="consolidation consumes usage signals before age-only fallback",
        surfaces=(
            "usage-aware consolidation",
            "usage-ordered consolidation input",
            "cited decay divergence",
        ),
        command=(
            "moon",
            "run",
            "api:test",
            "--",
            "tests/test_jobs_consolidation.py",
            "-k",
            "priority_decay",
        ),
    ),
    GateCheck(
        name="ai-memory-contracts",
        description="committed AI-memory manifest carries W6 usage-loop budgets",
        surfaces=("manifest", "release contract"),
        command=("moon", "run", "bench-gate"),
    ),
)

REQUIRED_SURFACES: tuple[str, ...] = (
    "exposure stamping",
    "citation stamping",
    "idempotence",
    "cite endpoint",
    "task completion",
    "reflect",
    "mcp",
    "cli",
    "usage-aware consolidation",
    "usage-ordered consolidation input",
    "cited decay divergence",
    "manifest",
    "release contract",
)

CONTRACT_CHECK_NAMES = frozenset(("ai-memory-contracts",))
RECEIPT_NOW = datetime(2026, 7, 3, tzinfo=UTC)
RECENCY_HALF_LIFE_DAYS = 180
ACCOUNTED_EXPOSURE_STATUSES = frozenset(("excluded", "stamped"))


def covered_surfaces(checks: Iterable[GateCheck] = GATE_CHECKS) -> set[str]:
    return {surface for check in checks for surface in check.surfaces}


def missing_required_surfaces(checks: Sequence[GateCheck] = GATE_CHECKS) -> list[str]:
    covered = covered_surfaces(checks)
    return [surface for surface in REQUIRED_SURFACES if surface not in covered]


def _entity(*, memory_id: str, cited: bool) -> SimpleNamespace:
    metadata: dict[str, Any] = {"importance": 0.1}
    if cited:
        metadata.update(
            {
                "citation_count": 1,
                "last_used_at": (RECEIPT_NOW - timedelta(days=3)).isoformat(),
            }
        )
    return SimpleNamespace(
        id=memory_id,
        created_at=RECEIPT_NOW - timedelta(days=420),
        metadata=metadata,
    )


def build_usage_loop_receipt() -> dict[str, Any]:
    exposed_items = (
        {"cite_id": "decision-1", "kind": "graph_entity", "status": "stamped"},
        {"cite_id": "raw_memory:raw-1", "kind": "raw_capture", "status": "stamped"},
        {"cite_id": "document:doc-1", "kind": "document", "status": "excluded"},
    )
    covered_exposure_count = sum(
        1 for item in exposed_items if item["status"] in ACCOUNTED_EXPOSURE_STATUSES
    )
    exposure_coverage = covered_exposure_count / len(exposed_items)

    citation_event_keys = (
        "session:message:graph_entity:decision-1:citation",
        "session:message:graph_entity:decision-1:citation",
        "session:message:raw_capture:raw-1:citation",
    )
    stored_citation_event_keys = tuple(dict.fromkeys(citation_event_keys))
    unique_citation_events = set(stored_citation_event_keys)
    duplicate_suppressed_count = len(citation_event_keys) - len(stored_citation_event_keys)
    duplicate_stored_count = len(stored_citation_event_keys) - len(unique_citation_events)

    uncited = _entity(memory_id="stale-uncited-twin", cited=False)
    cited = _entity(memory_id="protected-cited-twin", cited=True)
    uncited_score = _priority_decay_score(
        uncited,
        now=RECEIPT_NOW,
        recency_half_life_days=RECENCY_HALF_LIFE_DAYS,
    )
    cited_score = _priority_decay_score(
        cited,
        now=RECEIPT_NOW,
        recency_half_life_days=RECENCY_HALF_LIFE_DAYS,
    )

    consolidation_inputs = sorted(
        (
            {
                "memory_id": uncited.id,
                "latest_usage_at": None,
                "priority_decay_score": round(uncited_score, 6),
            },
            {
                "memory_id": cited.id,
                "latest_usage_at": cited.metadata["last_used_at"],
                "priority_decay_score": round(cited_score, 6),
            },
        ),
        key=lambda item: (item["latest_usage_at"] is not None, item["priority_decay_score"]),
        reverse=True,
    )

    metrics = {
        "exposure_stamp_coverage": exposure_coverage,
        "citation_event_count": len(unique_citation_events),
        "duplicate_stored_event_count": duplicate_stored_count,
        "duplicate_suppressed_event_count": duplicate_suppressed_count,
        "usage_ordered_consolidation_input_count": len(consolidation_inputs),
        "cited_decay_score_advantage": round(cited_score - uncited_score, 6),
    }
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "fixture": "usage-feedback-loop-v1",
        "budgets": dict(USAGE_LOOP_BUDGETS),
        "metrics": metrics,
        "exposure": {
            "returned_count": len(exposed_items),
            "covered_count": covered_exposure_count,
            "items": list(exposed_items),
        },
        "citations": {
            "received_event_count": len(citation_event_keys),
            "unique_event_count": len(unique_citation_events),
            "duplicate_suppressed_event_count": duplicate_suppressed_count,
        },
        "consolidation_inputs": consolidation_inputs,
        "decay_twins": {
            "cited": {
                "memory_id": cited.id,
                "score": round(cited_score, 6),
                "citation_count": cited.metadata["citation_count"],
                "last_used_at": cited.metadata["last_used_at"],
            },
            "uncited": {
                "memory_id": uncited.id,
                "score": round(uncited_score, 6),
                "citation_count": 0,
                "last_used_at": None,
            },
        },
    }


def build_usage_loop_dogfood_receipt(evidence: dict[str, Any]) -> dict[str, Any]:
    usage = evidence.get("usage")
    usage_evidence = usage if isinstance(usage, dict) else {}
    exposure_events = list_of_mappings(usage_evidence.get("exposure_events"))
    citation_events = list_of_mappings(usage_evidence.get("citation_events"))
    events = [*exposure_events, *citation_events]
    stored_dedupe_keys = []
    for event in events:
        key = _dedupe_key(event)
        if key and event_stored(event):
            stored_dedupe_keys.append(key)
    duplicate_stored_count = len(stored_dedupe_keys) - len(set(stored_dedupe_keys))
    usage_stamp_count = sum(
        1 for event in exposure_events if string_value(event.get("last_recalled_at"))
    ) + sum(1 for event in citation_events if string_value(event.get("last_used_at")))
    metrics = {
        **build_deployment_metrics(evidence),
        "exposure_event_count": len(exposure_events),
        "citation_event_count": len(citation_events),
        "duplicate_stored_event_count": duplicate_stored_count,
        "dedupe_key_coverage": _coverage(events, stored_dedupe_keys),
        "usage_stamp_coverage": _coverage(events, range(usage_stamp_count)),
        "cited_decay_score_advantage": _cited_decay_score_advantage(usage_evidence),
    }
    return {
        "schema_version": DOGFOOD_RECEIPT_SCHEMA_VERSION,
        "evidence_kind": "live-dogfood-usage-loop",
        "deployment": evidence.get("deployment", {}),
        "budgets": dict(DOGFOOD_USAGE_LOOP_BUDGETS),
        "metrics": metrics,
        "events": {
            "exposure": exposure_events,
            "citation": citation_events,
        },
        "checks": evidence_checks(evidence),
    }


def collect_usage_loop_dogfood_evidence(
    deployment: dict[str, Any],
    *,
    query_runner: DebugQueryRunner = run_sibyl_debug_query,
    limit: int = 200,
) -> dict[str, Any]:
    query_limit = max(1, min(int(limit), 1000))
    usage_events = query_runner(USAGE_EVENTS_QUERY.format(limit=query_limit))
    raw_stamps = query_runner(RAW_USAGE_STAMPS_QUERY.format(limit=query_limit))
    graph_rows = query_runner(GRAPH_USAGE_STAMPS_QUERY.format(limit=query_limit))
    raw_by_id = _rows_by_uuid(raw_stamps)
    graph_by_id = _rows_by_uuid(graph_rows)
    exposure_events = [
        _usage_event_with_stamp(event, raw_by_id=raw_by_id, graph_by_id=graph_by_id)
        for event in usage_events
        if string_value(event.get("signal_type")) == "exposure"
    ]
    citation_events = [
        _usage_event_with_stamp(event, raw_by_id=raw_by_id, graph_by_id=graph_by_id)
        for event in usage_events
        if string_value(event.get("signal_type")) == "citation"
    ]
    return {
        "deployment": dict(deployment),
        "usage": {
            "exposure_events": exposure_events,
            "citation_events": citation_events,
            "cited_decay_score_advantage": _live_cited_decay_score_advantage(graph_rows),
        },
        "checks": [
            {
                "name": "live-usage-loop-observer",
                "status": "PASS",
                "surfaces": list(DOGFOOD_REQUIRED_SURFACES),
            }
        ],
    }


def validate_usage_loop_receipt(receipt: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        failures.append(f"receipt schema_version must be {RECEIPT_SCHEMA_VERSION}")
    metrics = receipt.get("metrics")
    if not isinstance(metrics, dict):
        return [*failures, "receipt metrics must be an object"]
    failures.extend(_validate_receipt_metrics(metrics))
    failures.extend(_validate_receipt_checks(receipt.get("checks")))
    return failures


def validate_usage_loop_dogfood_receipt(receipt: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if receipt.get("schema_version") != DOGFOOD_RECEIPT_SCHEMA_VERSION:
        failures.append(f"receipt schema_version must be {DOGFOOD_RECEIPT_SCHEMA_VERSION}")
    metrics = receipt.get("metrics")
    if not isinstance(metrics, dict):
        return [*failures, "receipt metrics must be an object"]

    failures.extend(
        validate_metric_budgets(
            metrics,
            DOGFOOD_USAGE_LOOP_BUDGETS,
            lower_is_better=DOGFOOD_LOWER_IS_BETTER,
        )
    )
    failures.extend(
        validate_required_checks(
            receipt,
            required_surfaces=DOGFOOD_REQUIRED_SURFACES,
        )
    )
    return failures


def _validate_receipt_metrics(metrics: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for metric, budget in USAGE_LOOP_BUDGETS.items():
        value = metrics.get(metric)
        if not isinstance(value, int | float) or isinstance(value, bool):
            failures.append(f"metric {metric!r} must be numeric")
            continue
        if metric == "duplicate_stored_event_count":
            if float(value) > float(budget):
                failures.append(f"metric {metric!r} exceeds budget {budget}: {value}")
        elif float(value) < float(budget):
            failures.append(f"metric {metric!r} below budget {budget}: {value}")
    return failures


def event_stored(event: dict[str, Any]) -> bool:
    return event.get("stored") is not False


def _dedupe_key(event: dict[str, Any]) -> str:
    return string_value(event.get("dedupe_key") or event.get("event_key"))


def _coverage(items: Sequence[Any], covered_items: Sequence[Any]) -> float:
    if not items:
        return 0.0
    return len(covered_items) / len(items)


def _cited_decay_score_advantage(usage_evidence: dict[str, Any]) -> float:
    explicit = usage_evidence.get("cited_decay_score_advantage")
    if isinstance(explicit, int | float) and not isinstance(explicit, bool):
        return float(explicit)

    twins = usage_evidence.get("decay_twins")
    if not isinstance(twins, dict):
        return 0.0
    cited = twins.get("cited")
    uncited = twins.get("uncited")
    if not isinstance(cited, dict) or not isinstance(uncited, dict):
        return 0.0
    cited_score = cited.get("score")
    uncited_score = uncited.get("score")
    if (
        isinstance(cited_score, int | float)
        and not isinstance(cited_score, bool)
        and isinstance(uncited_score, int | float)
        and not isinstance(uncited_score, bool)
    ):
        return float(cited_score) - float(uncited_score)
    return 0.0


def _rows_by_uuid(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        row_uuid: row
        for row in rows
        if (row_uuid := string_value(row.get("uuid") or row.get("id")))
    }


def _usage_event_with_stamp(
    event: dict[str, Any],
    *,
    raw_by_id: dict[str, dict[str, Any]],
    graph_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    item_kind = string_value(event.get("item_kind"))
    item_id = string_value(event.get("item_id"))
    signal_type = string_value(event.get("signal_type"))
    stamp: dict[str, Any] = {}
    if item_kind == "raw_capture":
        stamp = raw_by_id.get(item_id) or {}
    else:
        stamp = graph_by_id.get(item_id) or {}
    observed = {
        "memory_id": item_id,
        "item_kind": item_kind,
        "signal_type": signal_type,
        "source_surface": string_value(event.get("source_surface")),
        "event_at": string_value(event.get("event_at")),
        "dedupe_key": _usage_event_dedupe_key(event),
        "stored": True,
    }
    if signal_type == "exposure":
        observed["last_recalled_at"] = string_value(stamp.get("last_recalled_at"))
    elif signal_type == "citation":
        observed["last_used_at"] = string_value(stamp.get("last_used_at"))
    return observed


def _usage_event_dedupe_key(event: dict[str, Any]) -> str:
    return ":".join(
        (
            string_value(event.get("session_key")),
            string_value(event.get("message_key")),
            string_value(event.get("source_surface")),
            string_value(event.get("item_kind")),
            string_value(event.get("item_id")),
            string_value(event.get("signal_type")),
        )
    )


def _live_cited_decay_score_advantage(graph_rows: Sequence[dict[str, Any]]) -> float:
    now = datetime.now(UTC)
    cited_scores: list[float] = []
    uncited_scores: list[float] = []
    for row in graph_rows:
        entity = _decay_entity(row, now=now)
        score = _priority_decay_score(
            entity,
            now=now,
            recency_half_life_days=RECENCY_HALF_LIFE_DAYS,
        )
        if _has_citation_usage(entity.metadata):
            cited_scores.append(score)
        else:
            uncited_scores.append(score)
    if not cited_scores or not uncited_scores:
        return 0.0
    return round(max(cited_scores) - min(uncited_scores), 6)


def _decay_entity(row: dict[str, Any], *, now: datetime) -> SimpleNamespace:
    metadata = _row_metadata(row)
    return SimpleNamespace(
        id=string_value(row.get("uuid") or row.get("id")),
        created_at=coerce_datetime(row.get("created_at")) or now,
        metadata=metadata,
    )


def _row_metadata(row: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for field in ("metadata", "attributes"):
        value = row.get(field)
        if isinstance(value, dict):
            metadata.update(value)
    for field in (
        "status",
        "last_recalled_at",
        "last_used_at",
        "retrieval_count",
        "citation_count",
    ):
        if row.get(field) is not None:
            metadata[field] = row[field]
    return metadata


def _has_citation_usage(metadata: dict[str, Any]) -> bool:
    return (
        bool(string_value(metadata.get("last_used_at")))
        or _int_metric(metadata.get("citation_count")) > 0
    )


def _int_metric(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _validate_receipt_checks(checks: Any) -> list[str]:
    failures: list[str] = []
    if checks is None:
        return failures
    if not isinstance(checks, list):
        return ["receipt checks must be a list"]
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            failures.append(f"receipt checks[{index}] must be an object")
            continue
        if check.get("status") != "PASS":
            failures.append(f"receipt checks[{index}] did not pass")
    return failures


def format_command(command: Sequence[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def with_check_results(receipt: dict[str, Any], results: Sequence[GateResult]) -> dict[str, Any]:
    return {
        **receipt,
        "checks": [
            {
                "name": result.check.name,
                "status": "PASS" if result.passed else "FAIL",
                "exit_code": result.exit_code,
                "command": format_command(result.check.command),
                "surfaces": list(result.check.surfaces),
            }
            for result in results
        ],
    }


def write_receipt(receipt: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{json.dumps(receipt, indent=2, sort_keys=True)}\n", encoding="utf-8")


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _echo(message: str = "") -> None:
    sys.stdout.write(f"{message}\n")


def _real_runner(command: tuple[str, ...]) -> int:
    executable = which(command[0])
    if executable is None:
        msg = f"Required executable not found on PATH: {command[0]}"
        raise RuntimeError(msg)
    env = dict(os.environ)
    env.setdefault("MOON_COLOR", "false")
    completed = subprocess.run(  # noqa: S603
        (executable, *command[1:]),
        cwd=REPO_ROOT,
        env=env,
        check=False,
    )
    return completed.returncode


def _run_check(check: GateCheck, *, runner: Runner, echo: Echo) -> GateResult:
    echo("")
    echo(f"[{check.name}] {check.description}")
    echo(f"surfaces: {', '.join(check.surfaces)}")
    echo(f"command: {format_command(check.command)}")

    started = time.perf_counter()
    error: str | None = None
    try:
        exit_code = runner(check.command)
    except Exception as exc:
        exit_code = 1
        error = f"{type(exc).__name__}: {exc}"
    elapsed = time.perf_counter() - started

    status = "PASS" if exit_code == 0 else f"FAIL exit={exit_code}"
    if error is not None:
        status = f"{status} error={error}"
    echo(f"result: {status} in {elapsed:.2f}s")
    return GateResult(check=check, exit_code=exit_code, elapsed_seconds=elapsed, error=error)


def _print_receipt(receipt: dict[str, Any], results: Sequence[GateResult], *, echo: Echo) -> None:
    passed = [result for result in results if result.passed]
    failed = [result for result in results if not result.passed]
    status = "PASS" if not failed else "FAIL"
    surfaces = sorted(covered_surfaces(result.check for result in results))

    echo("")
    echo("Usage Loop Gate Receipt")
    echo(f"status: {status}")
    echo(f"checks: {len(passed)} passed, {len(failed)} failed")
    echo(
        "metrics: " + ", ".join(f"{metric}={value}" for metric, value in receipt["metrics"].items())
    )
    echo(f"surfaces: {', '.join(surfaces)}")
    for result in results:
        check_status = "PASS" if result.passed else f"FAIL exit={result.exit_code}"
        error = f"; error={result.error}" if result.error is not None else ""
        echo(f"- {check_status} {result.check.name} ({result.elapsed_seconds:.2f}s){error}")


def run_gate(
    checks: Sequence[GateCheck] = GATE_CHECKS,
    *,
    runner: Runner | None = None,
    echo: Echo = _echo,
    receipt_path: Path | None = DEFAULT_RECEIPT_PATH,
) -> int:
    missing = missing_required_surfaces(checks)
    if missing:
        echo("Usage loop gate is missing required surfaces:")
        for surface in missing:
            echo(f"- {surface}")
        return 2

    receipt = build_usage_loop_receipt()
    receipt_failures = validate_usage_loop_receipt(receipt)
    if receipt_failures:
        echo("Usage loop receipt failed:")
        for failure in receipt_failures:
            echo(f"- {failure}")
        return 1

    active_runner = runner or _real_runner
    echo("Usage Loop Gate")
    echo(f"checks: {len(checks)}")
    echo(f"receipt_schema: {receipt['schema_version']}")
    if receipt_path is not None:
        echo(f"receipt: {display_path(receipt_path)}")

    evidence_checks = [check for check in checks if check.name not in CONTRACT_CHECK_NAMES]
    contract_checks = [check for check in checks if check.name in CONTRACT_CHECK_NAMES]
    results = [_run_check(check, runner=active_runner, echo=echo) for check in evidence_checks]

    evidence_receipt = with_check_results(receipt, results)
    receipt_failures = validate_usage_loop_receipt(evidence_receipt)
    if receipt_failures:
        echo("Usage loop receipt failed:")
        for failure in receipt_failures:
            echo(f"- {failure}")
        if receipt_path is not None:
            write_receipt(evidence_receipt, receipt_path)
        _print_receipt(evidence_receipt, results, echo=echo)
        return 1
    if receipt_path is not None:
        write_receipt(evidence_receipt, receipt_path)

    results.extend(_run_check(check, runner=active_runner, echo=echo) for check in contract_checks)
    final_receipt = with_check_results(receipt, results)
    if receipt_path is not None:
        write_receipt(final_receipt, receipt_path)
    _print_receipt(final_receipt, results, echo=echo)
    return 0 if all(result.passed for result in results) else 1


def run_dogfood_receipt(
    evidence_path: Path,
    *,
    receipt_path: Path | None = DEFAULT_DOGFOOD_RECEIPT_PATH,
    echo: Echo = _echo,
) -> int:
    try:
        evidence = load_dogfood_evidence(evidence_path)
    except (OSError, TypeError, json.JSONDecodeError, ValueError) as exc:
        echo(f"Usage loop dogfood evidence failed to load: {exc}")
        return 1

    receipt = build_usage_loop_dogfood_receipt(evidence)
    failures = validate_usage_loop_dogfood_receipt(receipt)
    if receipt_path is not None:
        write_receipt(receipt, receipt_path)

    status = "PASS" if not failures else "FAIL"
    echo("Usage Loop Dogfood Receipt")
    echo(f"status: {status}")
    echo(f"receipt_schema: {receipt['schema_version']}")
    echo(
        "metrics: " + ", ".join(f"{metric}={value}" for metric, value in receipt["metrics"].items())
    )
    if receipt_path is not None:
        echo(f"receipt: {display_path(receipt_path)}")
    for failure in failures:
        echo(f"- {failure}")
    return 0 if not failures else 1


def run_collect_dogfood_receipt(
    deployment_path: Path,
    *,
    evidence_path: Path,
    receipt_path: Path | None = DEFAULT_DOGFOOD_RECEIPT_PATH,
    query_runner: DebugQueryRunner = run_sibyl_debug_query,
    echo: Echo = _echo,
) -> int:
    try:
        deployment = load_deployment_evidence(deployment_path)
        evidence = collect_usage_loop_dogfood_evidence(
            deployment,
            query_runner=query_runner,
        )
    except (OSError, TypeError, json.JSONDecodeError, ValueError, RuntimeError) as exc:
        echo(f"Usage loop dogfood collection failed: {exc}")
        return 1

    write_receipt(evidence, evidence_path)
    receipt = build_usage_loop_dogfood_receipt(evidence)
    failures = validate_usage_loop_dogfood_receipt(receipt)
    if receipt_path is not None:
        write_receipt(receipt, receipt_path)
    status = "PASS" if not failures else "FAIL"
    echo("Usage Loop Dogfood Collection")
    echo(f"status: {status}")
    echo(f"evidence: {display_path(evidence_path)}")
    if receipt_path is not None:
        echo(f"receipt: {display_path(receipt_path)}")
    for failure in failures:
        echo(f"- {failure}")
    return 0 if not failures else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run focused usage-loop release-gate checks.")
    parser.add_argument(
        "--list",
        action="store_true",
        help="List checks and exit without running them.",
    )
    parser.add_argument(
        "--dogfood-evidence",
        type=Path,
        help="Normalize and validate a live dogfood evidence JSON file.",
    )
    parser.add_argument(
        "--dogfood-receipt",
        type=Path,
        default=DEFAULT_DOGFOOD_RECEIPT_PATH,
        help="Dogfood receipt path written when --dogfood-evidence is set.",
    )
    parser.add_argument(
        "--collect-dogfood-evidence",
        type=Path,
        help="Collect live dogfood evidence into this JSON path, then validate a receipt.",
    )
    parser.add_argument(
        "--deployment-evidence",
        type=Path,
        help="JSON deployment provenance required by --collect-dogfood-evidence.",
    )
    args = parser.parse_args(argv)

    if args.list:
        for check in GATE_CHECKS:
            _echo(f"{check.name}: {format_command(check.command)}")
        return 0
    if args.dogfood_evidence is not None:
        return run_dogfood_receipt(
            args.dogfood_evidence,
            receipt_path=args.dogfood_receipt,
        )
    if args.collect_dogfood_evidence is not None:
        if args.deployment_evidence is None:
            _echo("--deployment-evidence is required with --collect-dogfood-evidence")
            return 1
        return run_collect_dogfood_receipt(
            args.deployment_evidence,
            evidence_path=args.collect_dogfood_evidence,
            receipt_path=args.dogfood_receipt,
        )

    return run_gate()


if __name__ == "__main__":
    raise SystemExit(main())
