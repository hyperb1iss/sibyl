#!/usr/bin/env python3
"""Run the focused release gate for workspace trust surfaces."""

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
GATE_NAME = "Workspace Trust Gate"
MISSING_SURFACES_HEADING = "Workspace trust gate is missing required surfaces:"
CLI_DESCRIPTION = "Run focused workspace trust checks."

GATE_CHECKS: tuple[GateCheck, ...] = (
    GateCheck(
        name="api-workspace-trust",
        description="inspect, correction, promotion, import progress, jobs, and audit",
        surfaces=(
            "source inspect",
            "correction preview",
            "correction restore",
            "promotion preview",
            "source import progress",
            "job progress",
            "audit receipt",
        ),
        command=("moon", "run", "api:workspace-trust-test"),
    ),
    GateCheck(
        name="cli-workspace-trust",
        description="CLI inspect, import status, promotion, review, and synthesis receipts",
        surfaces=(
            "CLI inspect",
            "CLI import status",
            "CLI promotion preview",
            "CLI review status",
            "synthesis receipts",
        ),
        command=("moon", "run", "cli:workspace-trust-test"),
    ),
    GateCheck(
        name="web-workspace-trust",
        description="memory workspace, inspect UI, import progress, review, and synthesis",
        surfaces=(
            "memory workspace",
            "source inspect UI",
            "correction dialog",
            "source import progress",
            "raw capture review",
            "synthesis receipts",
        ),
        command=("moon", "run", "web:workspace-trust-test"),
    ),
)

REQUIRED_SURFACES: tuple[str, ...] = (
    "source inspect",
    "correction preview",
    "correction restore",
    "promotion preview",
    "source import progress",
    "job progress",
    "audit receipt",
    "CLI inspect",
    "CLI import status",
    "CLI promotion preview",
    "CLI review status",
    "memory workspace",
    "source inspect UI",
    "correction dialog",
    "raw capture review",
    "synthesis receipts",
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
