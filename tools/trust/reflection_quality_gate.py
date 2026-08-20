#!/usr/bin/env python3
"""Run the focused release gate for reflection quality."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from tools.trust.gate_runner import (
    Echo,
    GateCheck,
    Runner,
    stdout_echo,
)
from tools.trust.gate_runner import main as gate_main
from tools.trust.gate_runner import (
    missing_required_surfaces as find_missing_required_surfaces,
)
from tools.trust.gate_runner import run_gate as run_release_gate

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_NAME = "Reflection Quality Gate"
MISSING_SURFACES_HEADING = "Reflection quality gate is missing required surfaces:"
CLI_DESCRIPTION = "Run focused reflection-quality checks."

GATE_CHECKS: tuple[GateCheck, ...] = (
    GateCheck(
        name="core-reflection-quality",
        description="core extraction, lifecycle, recall, and policy quality fixtures",
        surfaces=(
            "extraction",
            "source grounding",
            "duplicate detection",
            "contradiction detection",
            "stale/supersession lifecycle",
            "correction and rollback",
            "permission safety",
            "recall quality",
        ),
        command=("moon", "run", "core:reflection-quality-test"),
    ),
    GateCheck(
        name="api-reflection-quality",
        description="REST reflection, promotion, correction, and audit visibility",
        surfaces=(
            "source grounding",
            "exception routing",
            "correction and rollback",
            "permission safety",
            "CLI/API visibility",
        ),
        command=("moon", "run", "api:reflection-quality-test"),
    ),
    GateCheck(
        name="cli-reflection-quality",
        description="CLI reflection, inspect, auto-review, and exception visibility",
        surfaces=("exception routing", "CLI/API visibility", "source grounding"),
        command=("moon", "run", "cli:reflection-quality-test"),
    ),
    GateCheck(
        name="web-reflection-quality",
        description="web memory workspace reflection receipts and inspect visibility",
        surfaces=(
            "source grounding",
            "exception routing",
            "correction and rollback",
            "CLI/API visibility",
            "product visibility",
        ),
        command=("moon", "run", "web:reflection-quality-test"),
    ),
)

REQUIRED_SURFACES: tuple[str, ...] = (
    "extraction",
    "source grounding",
    "duplicate detection",
    "contradiction detection",
    "stale/supersession lifecycle",
    "correction and rollback",
    "permission safety",
    "recall quality",
    "exception routing",
    "CLI/API visibility",
)


def missing_required_surfaces(
    checks: Sequence[GateCheck] = GATE_CHECKS,
) -> list[str]:
    return find_missing_required_surfaces(checks, REQUIRED_SURFACES)


def run_gate(
    checks: Sequence[GateCheck] = GATE_CHECKS,
    *,
    runner: Runner | None = None,
    echo: Echo = stdout_echo,
) -> int:
    return run_release_gate(
        gate_name=GATE_NAME,
        missing_surfaces_heading=MISSING_SURFACES_HEADING,
        checks=checks,
        required_surfaces=REQUIRED_SURFACES,
        repo_root=REPO_ROOT,
        runner=runner,
        echo=echo,
    )


def main(argv: list[str] | None = None) -> int:
    return gate_main(
        argv,
        parser_description=CLI_DESCRIPTION,
        gate_name=GATE_NAME,
        missing_surfaces_heading=MISSING_SURFACES_HEADING,
        checks=GATE_CHECKS,
        required_surfaces=REQUIRED_SURFACES,
        repo_root=REPO_ROOT,
    )


if __name__ == "__main__":
    raise SystemExit(main())
