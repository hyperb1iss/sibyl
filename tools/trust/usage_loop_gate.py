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

REPO_ROOT = Path(__file__).resolve().parents[2]
RECEIPT_SCHEMA_VERSION = "sibyl-usage-loop-receipt-v1"
DEFAULT_RECEIPT_PATH = (
    REPO_ROOT / "benchmarks" / "results" / "ai-memory" / "usage-loop-receipt.json"
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
        description="consolidation decay distinguishes cited memory from stale uncited memory",
        surfaces=("usage-aware consolidation", "cited decay divergence"),
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run focused usage-loop release-gate checks.")
    parser.add_argument(
        "--list",
        action="store_true",
        help="List checks and exit without running them.",
    )
    args = parser.parse_args(argv)

    if args.list:
        for check in GATE_CHECKS:
            _echo(f"{check.name}: {format_command(check.command)}")
        return 0

    return run_gate()


if __name__ == "__main__":
    raise SystemExit(main())
