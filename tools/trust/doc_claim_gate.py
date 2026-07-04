#!/usr/bin/env python3
"""Run the focused release gate for public docs claim discipline."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from shutil import which
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
RECEIPT_SCHEMA_VERSION = "sibyl-doc-claim-receipt-v1"
DEFAULT_RECEIPT_PATH = REPO_ROOT / "benchmarks" / "results" / "ai-memory" / "doc-claim-receipt.json"

Runner = Callable[[tuple[str, ...]], int]
Echo = Callable[[str], None]


@dataclass(frozen=True)
class ClaimRequirement:
    name: str
    path: str
    phrases: tuple[str, ...]
    surfaces: tuple[str, ...]


@dataclass(frozen=True)
class ForbiddenClaim:
    phrase: str
    reason: str


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


CLAIM_DOC_PATHS: tuple[str, ...] = (
    "docs/testing/longmemeval.md",
    "docs/testing/longmemeval-v2.md",
    "docs/testing/ai-memory-landscape.md",
    "docs/testing/benchmark-methodology.md",
    "docs/architecture/MARKETING_POSITIONING.md",
    "docs/architecture/retrieval-system.md",
    "docs/architecture/SIBYL_POST_1_0_ROADMAP.md",
    "docs/architecture/SIBYL_NORTHSTAR.md",
)

CLAIM_REQUIREMENTS: tuple[ClaimRequirement, ...] = (
    ClaimRequirement(
        name="retrieval-recall-axis",
        path="docs/testing/longmemeval.md",
        phrases=(
            "retrieval metric",
            "not an answer-quality metric",
            "96.96% R@5",
            "98.90% R@10",
        ),
        surfaces=("retrieval recall", "canonical LongMemEval-S receipt"),
    ),
    ClaimRequirement(
        name="qa-accuracy-boundary",
        path="docs/testing/longmemeval.md",
        phrases=(
            "no public QA-accuracy number is citable",
            "pinned model-backed artifact lands",
            "pinned-longmemeval-s-qa.json",
        ),
        surfaces=("QA accuracy boundary", "approval boundary labels"),
    ),
    ClaimRequirement(
        name="longmemeval-v2-boundary",
        path="docs/testing/longmemeval-v2.md",
        phrases=(
            "published V2 score",
            "both domains complete with the official reader and evaluator",
        ),
        surfaces=("LAFS Gain boundary", "approval boundary labels"),
    ),
    ClaimRequirement(
        name="cost-latency-axis",
        path="docs/testing/benchmark-methodology.md",
        phrases=("cost-latency gate", "warning-only", "estimated_total_usd"),
        surfaces=("cost and latency accounting", "approval boundary labels"),
    ),
    ClaimRequirement(
        name="local-embedding-boundary",
        path="docs/testing/ai-memory-landscape.md",
        phrases=("No public local-embedding variant", "OpenAI embeddings"),
        surfaces=("local embedding boundary", "approval boundary labels"),
    ),
    ClaimRequirement(
        name="self-reported-citation-axis",
        path="docs/testing/benchmark-methodology.md",
        phrases=(
            "citation (`last_used_at`) is",
            "Public claims must keep cited",
            "self-reported by the agent",
        ),
        surfaces=("self-reported citation usage", "approval boundary labels"),
    ),
    ClaimRequirement(
        name="okf-importer-boundary",
        path="docs/architecture/SIBYL_POST_1_0_ROADMAP.md",
        phrases=(
            "v1.1 verifies graph-payload reconstruction in test tooling",
            "product importer remains v1.3 scope",
        ),
        surfaces=("OKF export boundary", "OKF importer boundary"),
    ),
    ClaimRequirement(
        name="cross-org-boundary",
        path="docs/architecture/SIBYL_POST_1_0_ROADMAP.md",
        phrases=(
            "Cross-org sharing is out of v1.1",
            "hard isolation boundary",
        ),
        surfaces=("cross-org boundary", "approval boundary labels"),
    ),
    ClaimRequirement(
        name="teammembench-dataset-decision",
        path="docs/architecture/SIBYL_POST_1_0_ROADMAP.md",
        phrases=(
            "TeamMemBench dataset decision: hybrid real-plus-synthetic",
            "Real dogfood traces define the",
        ),
        surfaces=("TeamMemBench decision", "v1.2 handoff ledger"),
    ),
    ClaimRequirement(
        name="ontology-invariant",
        path="docs/architecture/SIBYL_POST_1_0_ROADMAP.md",
        phrases=(
            "types route memories",
            "must never gate",
            "_whether_ a memory",
        ),
        surfaces=("ontology invariant", "v1.2 handoff ledger"),
    ),
)

V12_HANDOFF_PHRASES: tuple[str, ...] = (
    "Coalescence data model & reversibility",
    "Concurrent multi-writer consistency",
    "scale-load-gate",
    "TeamMemBench",
    "Distillation pass",
    "Materialized memory-as-files",
    "Retroactive re-extraction loop",
    "OKF importer",
)

APPROVAL_BOUNDARY_PHRASES: tuple[str, ...] = (
    "no public QA-accuracy number is citable",
    "published V2 score",
    "warning-only",
    "No public local-embedding variant",
    "self-reported by the agent",
)

FORBIDDEN_CLAIMS: tuple[ForbiddenClaim, ...] = (
    ForbiddenClaim(
        phrase="round-trips back into Sibyl",
        reason="OKF v1.1 verifies reconstruction only; product import is v1.3 scope",
    ),
    ForbiddenClaim(
        phrase="Sibyl publishes a LongMemEval-V2 score",
        reason="V2 is approval-bound until the official web and enterprise runs are pinned",
    ),
    ForbiddenClaim(
        phrase="Sibyl publishes a QA-accuracy number",
        reason="QA accuracy is approval-bound until the model-backed artifact is pinned",
    ),
    ForbiddenClaim(
        phrase="cross-org sharing ships in v1.1",
        reason="v1.1 ships same-org team substrate, not cross-org sharing",
    ),
    ForbiddenClaim(
        phrase="OpenAI-free LongMemEval-S run",
        reason="the citable LongMemEval-S run uses OpenAI embeddings",
    ),
)

DOC_CLAIM_BUDGETS = {
    "unsupported_public_claim_count": 0,
    "documented_claim_axis_count": len(CLAIM_REQUIREMENTS),
    "v12_handoff_count": len(V12_HANDOFF_PHRASES),
    "approval_boundary_label_count": len(APPROVAL_BOUNDARY_PHRASES),
}

SCAN_CHECK = GateCheck(
    name="doc-claim-scan",
    description="public docs carry receipt-backed claim axes and deferred labels",
    surfaces=(
        "claim axis map",
        "unsupported claim scan",
        "approval boundary labels",
        "v1.2 handoff ledger",
        "ontology invariant",
        "TeamMemBench decision",
        "cross-org boundary",
        "OKF importer boundary",
    ),
    command=("internal", "doc-claim-scan"),
)

GATE_CHECKS: tuple[GateCheck, ...] = (
    GateCheck(
        name="docs-claim-lint",
        description="public Markdown claim surfaces are formatted",
        surfaces=("public docs formatting",),
        command=("moon", "run", "docs:lint"),
    ),
    GateCheck(
        name="docs-claim-build",
        description="public docs site builds after claim truth-up",
        surfaces=("public docs build",),
        command=("moon", "run", "docs:build"),
    ),
    GateCheck(
        name="ai-memory-contracts",
        description="committed AI-memory manifest carries W10 doc-claim contract",
        surfaces=("manifest", "release contract"),
        command=("moon", "run", "bench-gate"),
    ),
)

REQUIRED_SURFACES: tuple[str, ...] = (
    "claim axis map",
    "unsupported claim scan",
    "approval boundary labels",
    "v1.2 handoff ledger",
    "ontology invariant",
    "TeamMemBench decision",
    "cross-org boundary",
    "OKF importer boundary",
    "public docs formatting",
    "public docs build",
    "manifest",
    "release contract",
)

CONTRACT_CHECK_NAMES = frozenset(("ai-memory-contracts",))


def covered_surfaces(checks: Iterable[GateCheck] | None = None) -> set[str]:
    active_checks = tuple(checks or (SCAN_CHECK, *GATE_CHECKS))
    return {surface for check in active_checks for surface in check.surfaces}


def missing_required_surfaces(checks: Sequence[GateCheck] = GATE_CHECKS) -> list[str]:
    covered = covered_surfaces((SCAN_CHECK, *checks))
    return [surface for surface in REQUIRED_SURFACES if surface not in covered]


def load_claim_docs(repo_root: Path = REPO_ROOT) -> dict[str, str]:
    docs: dict[str, str] = {}
    for relative_path in CLAIM_DOC_PATHS:
        path = repo_root / relative_path
        docs[relative_path] = path.read_text(encoding="utf-8")
    return docs


def build_doc_claim_receipt(docs: Mapping[str, str] | None = None) -> dict[str, Any]:
    active_docs = dict(docs) if docs is not None else load_claim_docs()
    requirements = [
        _evaluate_requirement(requirement, active_docs) for requirement in CLAIM_REQUIREMENTS
    ]
    handoffs = _phrase_report(V12_HANDOFF_PHRASES, active_docs)
    approval_boundaries = _phrase_report(APPROVAL_BOUNDARY_PHRASES, active_docs)
    unsupported_claims = _find_forbidden_claims(active_docs)
    documented_count = sum(1 for requirement in requirements if requirement["status"] == "PASS")
    handoff_count = sum(1 for item in handoffs if item["locations"])
    approval_boundary_count = sum(1 for item in approval_boundaries if item["locations"])
    metrics = {
        "unsupported_public_claim_count": len(unsupported_claims),
        "documented_claim_axis_count": documented_count,
        "v12_handoff_count": handoff_count,
        "approval_boundary_label_count": approval_boundary_count,
    }
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "fixture": "doc-claim-truth-up-v1",
        "budgets": dict(DOC_CLAIM_BUDGETS),
        "metrics": metrics,
        "docs": sorted(active_docs),
        "requirements": requirements,
        "handoffs": handoffs,
        "approval_boundaries": approval_boundaries,
        "unsupported_claims": unsupported_claims,
    }


def validate_doc_claim_receipt(receipt: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        failures.append(f"receipt schema_version must be {RECEIPT_SCHEMA_VERSION}")
    metrics = receipt.get("metrics")
    if not isinstance(metrics, dict):
        return [*failures, "receipt metrics must be an object"]
    failures.extend(_validate_receipt_metrics(metrics))
    failures.extend(_validate_requirements(receipt.get("requirements")))
    failures.extend(_validate_receipt_checks(receipt.get("checks")))
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
        active_echo("Doc claim gate is missing required surfaces:")
        for surface in missing:
            active_echo(f"- {surface}")
        return 2

    receipt = build_doc_claim_receipt()
    scan_failures = validate_doc_claim_receipt(receipt)
    scan_result = GateResult(
        check=SCAN_CHECK,
        exit_code=0 if not scan_failures else 1,
        elapsed_seconds=0.0,
        error="; ".join(scan_failures) if scan_failures else None,
    )
    if scan_failures:
        final_receipt = with_check_results(receipt, [scan_result])
        if receipt_path is not None:
            write_receipt(final_receipt, receipt_path)
        _print_receipt(final_receipt, [scan_result], echo=active_echo)
        return 1

    active_runner = runner or _real_runner
    active_echo("Doc Claim Gate")
    active_echo(f"checks: {len(checks) + 1}")
    active_echo(f"receipt_schema: {receipt['schema_version']}")
    if receipt_path is not None:
        active_echo(f"receipt: {display_path(receipt_path)}")

    evidence_checks = [check for check in checks if check.name not in CONTRACT_CHECK_NAMES]
    contract_checks = [check for check in checks if check.name in CONTRACT_CHECK_NAMES]
    results = [scan_result]
    for check in evidence_checks:
        results.append(_run_check(check, runner=active_runner, echo=active_echo))

    evidence_receipt = with_check_results(receipt, results)
    receipt_failures = validate_doc_claim_receipt(evidence_receipt)
    if receipt_failures:
        active_echo("Doc claim receipt failed:")
        for failure in receipt_failures:
            active_echo(f"- {failure}")
        if receipt_path is not None:
            write_receipt(evidence_receipt, receipt_path)
        _print_receipt(evidence_receipt, results, echo=active_echo)
        return 1
    if receipt_path is not None:
        write_receipt(evidence_receipt, receipt_path)

    for check in contract_checks:
        results.append(_run_check(check, runner=active_runner, echo=active_echo))
    final_receipt = with_check_results(receipt, results)
    if receipt_path is not None:
        write_receipt(final_receipt, receipt_path)
    _print_receipt(final_receipt, results, echo=active_echo)
    return 0 if all(result.passed for result in results) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run focused public-doc claim release checks.")
    parser.add_argument(
        "--list",
        action="store_true",
        help="List checks and exit without running them.",
    )
    args = parser.parse_args(argv)

    if args.list:
        for check in (SCAN_CHECK, *GATE_CHECKS):
            _echo(f"{check.name}: {format_command(check.command)}")
        return 0

    return run_gate(echo=_echo)


def _evaluate_requirement(
    requirement: ClaimRequirement,
    docs: Mapping[str, str],
) -> dict[str, Any]:
    text = docs.get(requirement.path, "")
    missing = [phrase for phrase in requirement.phrases if phrase not in text]
    return {
        "name": requirement.name,
        "path": requirement.path,
        "status": "PASS" if not missing else "FAIL",
        "missing_phrases": missing,
        "surfaces": list(requirement.surfaces),
    }


def _phrase_report(phrases: Sequence[str], docs: Mapping[str, str]) -> list[dict[str, Any]]:
    return [
        {
            "phrase": phrase,
            "locations": [path for path, text in sorted(docs.items()) if phrase in text],
        }
        for phrase in phrases
    ]


def _find_forbidden_claims(docs: Mapping[str, str]) -> list[dict[str, str]]:
    matches: list[dict[str, str]] = []
    for path, text in sorted(docs.items()):
        for forbidden in FORBIDDEN_CLAIMS:
            if forbidden.phrase in text:
                matches.append(
                    {
                        "path": path,
                        "phrase": forbidden.phrase,
                        "reason": forbidden.reason,
                    }
                )
    return matches


def _validate_receipt_metrics(metrics: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for metric, budget in DOC_CLAIM_BUDGETS.items():
        value = metrics.get(metric)
        if not isinstance(value, int | float) or isinstance(value, bool):
            failures.append(f"metric {metric!r} must be numeric")
            continue
        if metric == "unsupported_public_claim_count":
            if float(value) > float(budget):
                failures.append(f"metric {metric!r} exceeds budget {budget}: {value}")
        elif float(value) < float(budget):
            failures.append(f"metric {metric!r} below budget {budget}: {value}")
    return failures


def _validate_requirements(requirements: Any) -> list[str]:
    if not isinstance(requirements, list) or not requirements:
        return ["receipt requirements must be a non-empty list"]
    failures: list[str] = []
    for index, requirement in enumerate(requirements):
        if not isinstance(requirement, dict):
            failures.append(f"receipt requirements[{index}] must be an object")
            continue
        if requirement.get("status") != "PASS":
            name = requirement.get("name", index)
            missing = requirement.get("missing_phrases", [])
            failures.append(f"requirement {name!r} did not pass; missing {missing!r}")
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


def _print_receipt(
    receipt: dict[str, Any],
    results: Sequence[GateResult],
    *,
    echo: Echo,
) -> None:
    passed = [result for result in results if result.passed]
    failed = [result for result in results if not result.passed]
    status = "PASS" if not failed else "FAIL"
    surfaces = sorted(covered_surfaces(result.check for result in results))

    echo("")
    echo("Doc Claim Gate Receipt")
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


if __name__ == "__main__":
    raise SystemExit(main())
