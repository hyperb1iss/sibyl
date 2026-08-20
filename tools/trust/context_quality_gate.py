#!/usr/bin/env python3
"""Run the focused release gate for context quality surfaces."""

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
GATE_NAME = "Context Quality Gate"
MISSING_SURFACES_HEADING = "Context quality gate is missing required surfaces:"
CLI_DESCRIPTION = "Run focused context quality checks."

GATE_CHECKS: tuple[GateCheck, ...] = (
    GateCheck(
        name="core-context-quality",
        description="wake, recall, deep_search, budgets, and source metadata fixtures",
        surfaces=(
            "wake",
            "recall",
            "deep_search",
            "token budget",
            "latency budget",
            "source metadata",
            "privacy leak fixture",
        ),
        command=("moon", "run", "core:context-quality-test"),
    ),
    GateCheck(
        name="api-context-quality",
        description="context pack REST, session wake, audit receipts, and project scoping",
        surfaces=(
            "context pack REST",
            "session wake",
            "audit receipt",
            "project scoping",
            "source metadata",
        ),
        command=("moon", "run", "api:context-quality-test"),
    ),
    GateCheck(
        name="cli-context-quality",
        description="CLI recall, prompt hook, session bundle, and agent diary context",
        surfaces=(
            "CLI recall",
            "prompt hook",
            "session bundle",
            "agent diary",
            "wake",
        ),
        command=("moon", "run", "cli:context-quality-test"),
    ),
)

REQUIRED_SURFACES: tuple[str, ...] = (
    "wake",
    "recall",
    "deep_search",
    "token budget",
    "latency budget",
    "source metadata",
    "privacy leak fixture",
    "context pack REST",
    "session wake",
    "audit receipt",
    "project scoping",
    "CLI recall",
    "prompt hook",
    "session bundle",
    "agent diary",
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
