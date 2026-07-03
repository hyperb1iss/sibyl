#!/usr/bin/env python3
"""Run the focused release gate for OKF export readiness."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from shutil import which
from typing import Any

from sibyl_core.export import (
    build_okf_bundle_from_graph_payload,
    reconstruct_graph_payload_from_okf_bundle,
    validate_okf_bundle,
    write_okf_bundle,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RECEIPT_SCHEMA_VERSION = "sibyl-okf-export-receipt-v1"
DEFAULT_RECEIPT_PATH = (
    REPO_ROOT / "benchmarks" / "results" / "ai-memory" / "okf-export-receipt.json"
)

Runner = Callable[[tuple[str, ...]], int]
Echo = Callable[[str], None]

OKF_EXPORT_BUDGETS = {
    "valid_okf_projection": 1,
    "byte_stable_reexport": 1,
    "graph_reconstruction_diff_count": 0,
    "memory_changelog_ready": 1,
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
        name="core-okf-projection",
        description="OKF bundle generation, validation, byte stability, and reconstruction",
        surfaces=("valid OKF projection", "byte stable re-export", "graph payload reconstruction"),
        command=("moon", "run", "core:test", "--", "tests/test_okf_export.py"),
    ),
    GateCheck(
        name="api-okf-cli",
        description="sibyld export okf projects migration archives into OKF bundles",
        surfaces=("CLI export", "archive projection", "memory changelog"),
        command=("moon", "run", "api:test", "--", "tests/test_cli_export.py", "-k", "okf"),
    ),
    GateCheck(
        name="ai-memory-contracts",
        description="committed AI-memory manifest carries W9 OKF export contracts",
        surfaces=("manifest", "memory changelog"),
        command=("moon", "run", "bench-gate"),
    ),
)

REQUIRED_SURFACES: tuple[str, ...] = (
    "valid OKF projection",
    "byte stable re-export",
    "graph payload reconstruction",
    "CLI export",
    "archive projection",
    "manifest",
    "memory changelog",
)


def covered_surfaces(checks: Iterable[GateCheck] = GATE_CHECKS) -> set[str]:
    return {surface for check in checks for surface in check.surfaces}


def missing_required_surfaces(checks: Sequence[GateCheck] = GATE_CHECKS) -> list[str]:
    covered = covered_surfaces(checks)
    return [surface for surface in REQUIRED_SURFACES if surface not in covered]


def format_command(command: Sequence[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def build_okf_export_receipt() -> dict[str, Any]:
    graph_payload = _fixture_graph_payload()
    first = build_okf_bundle_from_graph_payload(graph_payload)
    second = build_okf_bundle_from_graph_payload(graph_payload)
    byte_stable = first.files == second.files

    with tempfile.TemporaryDirectory(prefix="sibyl_okf_gate_") as tmpdir:
        bundle_dir = Path(tmpdir) / "bundle"
        write_okf_bundle(first, bundle_dir)
        validation_errors = validate_okf_bundle(bundle_dir)
        reconstructed = reconstruct_graph_payload_from_okf_bundle(bundle_dir)

    reconstruction_diff_count = 0 if reconstructed == graph_payload else 1
    metrics = {
        "valid_okf_projection": 1 if not validation_errors else 0,
        "byte_stable_reexport": 1 if byte_stable else 0,
        "graph_reconstruction_diff_count": reconstruction_diff_count,
        "memory_changelog_ready": 1 if "log.md" in first.files else 0,
        "exported_file_count": len(first.files),
    }
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "fixture": "okf-export-gate-v1",
        "budgets": dict(OKF_EXPORT_BUDGETS),
        "metrics": metrics,
        "okf": {
            "version": "0.1",
            "file_count": len(first.files),
            "reserved_files": [path for path in ("index.md", "log.md") if path in first.files],
        },
        "validation_errors": validation_errors,
        "reconstruction": {
            "matches_graph_payload": reconstructed == graph_payload,
            "diff_count": reconstruction_diff_count,
        },
    }


def validate_okf_export_receipt(receipt: dict[str, Any]) -> list[str]:
    metrics = receipt.get("metrics")
    if not isinstance(metrics, dict):
        return ["metrics must be an object"]

    failures: list[str] = []
    for metric, budget in OKF_EXPORT_BUDGETS.items():
        actual = metrics.get(metric)
        if not isinstance(actual, int | float):
            failures.append(f"metric {metric!r} must be numeric")
            continue
        if metric == "graph_reconstruction_diff_count":
            if actual > budget:
                failures.append(f"metric {metric!r} exceeds budget {budget}: {actual}")
        elif actual < budget:
            failures.append(f"metric {metric!r} below budget {budget}: {actual}")
    return failures


def run_gate(
    checks: Sequence[GateCheck] = GATE_CHECKS,
    *,
    runner: Runner | None = None,
    echo: Echo | None = None,
    receipt_path: Path | None = DEFAULT_RECEIPT_PATH,
) -> int:
    active_echo = echo or _echo
    missing = missing_required_surfaces(checks)
    if missing:
        _echo_lines(active_echo, "OKF export gate is missing required surfaces:")
        for surface in missing:
            _echo_lines(active_echo, f"- {surface}")
        return 2

    active_runner = runner or _real_runner
    _echo_lines(active_echo, "OKF Export Gate")
    _echo_lines(active_echo, f"checks: {len(checks)}")

    results: list[GateResult] = []
    for check in checks:
        if check.name == "ai-memory-contracts":
            if any(not result.passed for result in results):
                continue
            _write_pre_contract_receipt(results, receipt_path)
        results.append(_run_check(check, runner=active_runner, echo=active_echo))

    receipt = _build_gate_receipt(results, receipt_path)
    if receipt_path is not None:
        _write_receipt(receipt_path, receipt)
    _print_receipt(results, receipt, echo=active_echo)
    return 0 if receipt["status"] == "PASS" else 1


def _fixture_graph_payload() -> dict[str, Any]:
    return {
        "version": "2.0",
        "created_at": "2026-07-03T12:00:00+00:00",
        "organization_id": "org-okf-gate",
        "entity_count": 2,
        "relationship_count": 1,
        "episode_count": 1,
        "mention_count": 1,
        "entities": [
            {
                "id": "task-okf-gate",
                "entity_type": "task",
                "name": "Ship OKF export",
                "description": "Fixture task for portable memory export.",
            },
            {
                "id": "project-okf-gate",
                "entity_type": "project",
                "name": "Sibyl v1.1",
                "description": "Release project for W9 portability.",
            },
        ],
        "relationships": [
            {
                "id": "rel-task-project",
                "source_id": "task-okf-gate",
                "target_id": "project-okf-gate",
                "relationship_type": "BELONGS_TO",
                "weight": 1.0,
            }
        ],
        "episodes": [
            {
                "uuid": "episode-okf-gate",
                "name": "OKF gate episode",
                "content": "W9 exports a valid OKF bundle.",
            }
        ],
        "mentions": [
            {
                "uuid": "mention-okf-gate",
                "source_node_uuid": "episode-okf-gate",
                "target_node_uuid": "task-okf-gate",
            }
        ],
    }


def _echo(message: str = "") -> None:
    sys.stdout.write(f"{message}\n")


def _echo_lines(echo: Echo, message: str = "") -> None:
    echo(message)


def _real_runner(command: tuple[str, ...]) -> int:
    if not command:
        msg = "Gate command cannot be empty"
        raise ValueError(msg)

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
    _echo_lines(echo)
    _echo_lines(echo, f"[{check.name}] {check.description}")
    _echo_lines(echo, f"surfaces: {', '.join(check.surfaces)}")
    _echo_lines(echo, f"command: {format_command(check.command)}")

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
    _echo_lines(echo, f"result: {status} in {elapsed:.2f}s")
    return GateResult(
        check=check,
        exit_code=exit_code,
        elapsed_seconds=elapsed,
        error=error,
    )


def _write_pre_contract_receipt(
    results: Sequence[GateResult],
    receipt_path: Path | None,
) -> None:
    if receipt_path is None:
        return
    _write_receipt(receipt_path, _build_gate_receipt(results, receipt_path))


def _build_gate_receipt(
    results: Sequence[GateResult],
    receipt_path: Path | None,
) -> dict[str, Any]:
    evidence = build_okf_export_receipt()
    validation_failures = validate_okf_export_receipt(evidence)
    failed = [result for result in results if not result.passed]
    status = "PASS" if not failed and not validation_failures else "FAIL"
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "status": status,
        "created_at": datetime.now(UTC).isoformat(),
        "artifact_path": _display_path(receipt_path) if receipt_path is not None else None,
        "checks": [
            {
                "name": result.check.name,
                "description": result.check.description,
                "command": format_command(result.check.command),
                "surfaces": list(result.check.surfaces),
                "status": "PASS" if result.passed else "FAIL",
                "exit_code": result.exit_code,
                "elapsed_seconds": round(result.elapsed_seconds, 3),
                "error": result.error,
            }
            for result in results
        ],
        "surfaces": sorted(covered_surfaces(result.check for result in results)),
    }
    receipt.update(evidence)
    if validation_failures:
        receipt["validation_failures"] = validation_failures
    return receipt


def _write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _print_receipt(results: Sequence[GateResult], receipt: dict[str, Any], *, echo: Echo) -> None:
    passed = [result for result in results if result.passed]
    failed = [result for result in results if not result.passed]
    surfaces = sorted(covered_surfaces(result.check for result in results))

    _echo_lines(echo)
    _echo_lines(echo, "OKF Export Gate Receipt")
    _echo_lines(echo, f"status: {receipt['status']}")
    _echo_lines(echo, f"checks: {len(passed)} passed, {len(failed)} failed")
    _echo_lines(echo, f"surfaces: {', '.join(surfaces)}")
    _echo_lines(echo, f"artifact: {receipt['artifact_path']}")
    metrics = receipt["metrics"]
    _echo_lines(
        echo,
        "metrics: "
        f"valid_okf_projection={metrics['valid_okf_projection']}, "
        f"byte_stable_reexport={metrics['byte_stable_reexport']}, "
        f"graph_reconstruction_diff_count={metrics['graph_reconstruction_diff_count']}",
    )
    for result in results:
        check_status = "PASS" if result.passed else f"FAIL exit={result.exit_code}"
        error = f"; error={result.error}" if result.error is not None else ""
        _echo_lines(
            echo,
            f"- {check_status} {result.check.name} ({result.elapsed_seconds:.2f}s){error}",
        )


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run OKF export release-gate checks.")
    parser.add_argument(
        "--list",
        action="store_true",
        help="List checks and exit without running them.",
    )
    parser.add_argument(
        "--artifact-path",
        type=Path,
        default=DEFAULT_RECEIPT_PATH,
        help="Path for the release receipt JSON artifact.",
    )
    args = parser.parse_args(argv)

    if args.list:
        for check in GATE_CHECKS:
            _echo_lines(_echo, f"{check.name}: {format_command(check.command)}")
        return 0

    return run_gate(receipt_path=args.artifact_path)


if __name__ == "__main__":
    raise SystemExit(main())
