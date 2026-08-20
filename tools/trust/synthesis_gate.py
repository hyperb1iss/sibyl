#!/usr/bin/env python3
"""Run the focused release gate for source-grounded synthesis surfaces."""

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
GATE_NAME = "Synthesis Gate"
MISSING_SURFACES_HEADING = "Synthesis gate is missing required surfaces:"
CLI_DESCRIPTION = "Run focused synthesis release-gate checks."

GATE_CHECKS: tuple[GateCheck, ...] = (
    GateCheck(
        name="core-synthesis-contract",
        description="planner, materialization, drafting, gaps, and provenance",
        surfaces=(
            "source ids per section",
            "hidden-scope absence",
            "redaction handling",
            "freshness gaps",
            "correction impact",
            "unresolved-gap reporting",
            "artifact provenance",
        ),
        command=("moon", "run", "core:synthesis-gate-test"),
    ),
    GateCheck(
        name="core-synthesis-tools",
        description="MCP/CLI synthesis plan, draft, verify, and remember wrappers",
        surfaces=(
            "source ids per section",
            "unresolved-gap reporting",
            "artifact provenance",
            "remember provenance",
        ),
        command=("moon", "run", "core:synthesis-tool-gate-test"),
    ),
)

REQUIRED_SURFACES: tuple[str, ...] = (
    "source ids per section",
    "hidden-scope absence",
    "redaction handling",
    "freshness gaps",
    "correction impact",
    "unresolved-gap reporting",
    "artifact provenance",
    "remember provenance",
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
