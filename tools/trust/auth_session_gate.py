#!/usr/bin/env python3
"""Run the focused release gate for auth and session stability."""

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
GATE_NAME = "Auth Session Gate"
MISSING_SURFACES_HEADING = "Auth session gate is missing required surfaces:"
CLI_DESCRIPTION = "Run focused auth-session release checks."

GATE_CHECKS: tuple[GateCheck, ...] = (
    GateCheck(
        name="api-auth-session",
        description="API sessions, refresh rotation, revocation, MCP auth, and user routes",
        surfaces=(
            "api session validation",
            "refresh rotation",
            "revocation",
            "mcp tokens",
            "multi-org selection",
            "session list",
        ),
        command=("moon", "run", "api:auth-session-test"),
    ),
    GateCheck(
        name="cli-auth-session",
        description="CLI refresh, token-store replacement, and cross-process token reuse",
        surfaces=(
            "cli refresh",
            "token store",
            "cross-process refresh",
            "manual token guard",
        ),
        command=("moon", "run", "cli:auth-session-test"),
    ),
)

REQUIRED_SURFACES: tuple[str, ...] = (
    "api session validation",
    "refresh rotation",
    "revocation",
    "mcp tokens",
    "multi-org selection",
    "session list",
    "cli refresh",
    "token store",
    "cross-process refresh",
    "manual token guard",
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
