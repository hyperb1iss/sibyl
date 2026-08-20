#!/usr/bin/env python3
"""Run the focused release gate for automatic memory review."""

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
GATE_NAME = "Autonomy Gate"
MISSING_SURFACES_HEADING = "Autonomy gate is missing required surfaces:"
CLI_DESCRIPTION = "Run focused autonomy release-gate checks."

GATE_CHECKS: tuple[GateCheck, ...] = (
    GateCheck(
        name="core-autonomy-decision",
        description="deterministic auto-promotion and exception routing decisions",
        surfaces=(
            "auto-promotion decision",
            "exception routing",
            "dry-run parity",
            "confidence threshold",
            "sensitivity routing",
        ),
        command=("moon", "run", "core:autonomy-test"),
    ),
    GateCheck(
        name="api-autonomy-review",
        description="REST auto-review applies safe promotions through policy-backed promotion",
        surfaces=(
            "auto-promotion apply",
            "exception routing",
            "dry-run parity",
            "audit receipt",
        ),
        command=("moon", "run", "api:autonomy-test"),
    ),
    GateCheck(
        name="cli-autonomy-review",
        description="CLI can request automatic review without bypassing the API",
        surfaces=("cli", "auto-promotion apply", "dry-run parity"),
        command=("moon", "run", "cli:autonomy-test"),
    ),
)

REQUIRED_SURFACES: tuple[str, ...] = (
    "auto-promotion decision",
    "auto-promotion apply",
    "exception routing",
    "dry-run parity",
    "confidence threshold",
    "sensitivity routing",
    "audit receipt",
    "cli",
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
