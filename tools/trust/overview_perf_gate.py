#!/usr/bin/env python3
"""Run the focused release gate for overview and metrics performance."""

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
GATE_NAME = "Overview Performance Gate"
MISSING_SURFACES_HEADING = "Overview performance gate is missing required surfaces:"
CLI_DESCRIPTION = "Run focused overview performance checks."

GATE_CHECKS: tuple[GateCheck, ...] = (
    GateCheck(
        name="api-overview-metrics",
        description="admin stats and org/project metrics use bounded fast paths",
        surfaces=(
            "admin stats",
            "org metrics fast path",
            "project summary fast path",
        ),
        command=("moon", "run", "api:overview-perf-test"),
    ),
    GateCheck(
        name="web-overview-dashboard",
        description="overview dashboard renders from metrics without task entity fetches",
        surfaces=(
            "dashboard overview",
            "server stats fetch",
            "no task entity fetch",
        ),
        command=("moon", "run", "web:overview-perf-test"),
    ),
)

REQUIRED_SURFACES: tuple[str, ...] = (
    "admin stats",
    "org metrics fast path",
    "project summary fast path",
    "dashboard overview",
    "server stats fetch",
    "no task entity fetch",
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
