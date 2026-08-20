#!/usr/bin/env python3
"""Run the focused release gate for memory trust surfaces."""

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
GATE_NAME = "Memory Trust Gate"
MISSING_SURFACES_HEADING = "Memory trust gate is missing required surfaces:"
CLI_DESCRIPTION = "Run focused memory trust release-gate checks."

GATE_CHECKS: tuple[GateCheck, ...] = (
    GateCheck(
        name="core-memory-policy",
        description="shared policy plus native promotion/share previews",
        surfaces=("memory policy", "raw memory", "promotion preview", "share preview"),
        command=(
            "moon",
            "run",
            "core:memory-trust-policy-test",
        ),
    ),
    GateCheck(
        name="core-context-pack",
        description="context pack, wake, recall, and raw-memory blending",
        surfaces=("context pack", "wake", "recall", "raw memory"),
        command=(
            "moon",
            "run",
            "core:memory-trust-context-test",
        ),
    ),
    GateCheck(
        name="api-memory-rest",
        description="raw memory REST, previews, audit receipts, and inspect",
        surfaces=(
            "raw memory",
            "recall",
            "promotion preview",
            "share preview",
            "audit",
            "inspect",
        ),
        command=(
            "moon",
            "run",
            "api:memory-trust-rest-test",
        ),
    ),
    GateCheck(
        name="api-context-session",
        description="context pack, session wake, reflection, and audit receipts",
        surfaces=("context pack", "wake", "reflect", "audit"),
        command=(
            "moon",
            "run",
            "api:memory-trust-context-test",
        ),
    ),
    GateCheck(
        name="api-mcp-access",
        description="MCP project scoping, memory writes, reflection, and auth",
        surfaces=("mcp", "context pack", "reflect", "raw memory", "audit"),
        command=(
            "moon",
            "run",
            "api:memory-trust-mcp-test",
        ),
    ),
    GateCheck(
        name="api-jobs-policy",
        description="task-learning job policy payloads and audit receipts",
        surfaces=("jobs", "task learning", "memory policy", "audit"),
        command=(
            "moon",
            "run",
            "api:memory-trust-jobs-test",
        ),
    ),
    GateCheck(
        name="cli-memory",
        description="CLI remember, recall, wake, reflect, prompt hook, preview, and inspect",
        surfaces=(
            "cli",
            "prompt hook",
            "raw memory",
            "recall",
            "context pack",
            "wake",
            "reflect",
            "promotion preview",
            "share preview",
            "audit",
            "inspect",
        ),
        command=(
            "moon",
            "run",
            "cli:memory-trust-test",
        ),
    ),
)

REQUIRED_SURFACES: tuple[str, ...] = (
    "raw memory",
    "context pack",
    "wake",
    "recall",
    "reflect",
    "mcp",
    "jobs",
    "cli",
    "prompt hook",
    "promotion preview",
    "share preview",
    "audit",
    "inspect",
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
