#!/usr/bin/env python3
"""Run the focused release gate for v0.11 trust-control surfaces."""

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
GATE_NAME = "Trust Control Gate"
MISSING_SURFACES_HEADING = "Trust control gate is missing required surfaces:"
CLI_DESCRIPTION = "Run focused v0.11 trust-control checks."

GATE_CHECKS: tuple[GateCheck, ...] = (
    GateCheck(
        name="core-trust-policy",
        description="memory policy, delegated reads, and project/private boundaries",
        surfaces=(
            "agent identity",
            "delegated authority",
            "project/private leak fixture",
            "memory policy",
        ),
        command=("moon", "run", "core:memory-trust-policy-test"),
    ),
    GateCheck(
        name="api-trust-control-auth",
        description="control-plane records, API-key scopes, and auth claim projection",
        surfaces=(
            "persisted memory-space CRUD",
            "memory-space membership",
            "API-key project restriction",
            "API-key memory-space restriction",
            "auth claims",
            "session refresh",
        ),
        command=("moon", "run", "api:trust-control-auth-test"),
    ),
    GateCheck(
        name="api-trust-control-rest",
        description="REST memory policy, inspect, audit, and leak fixtures",
        surfaces=(
            "REST leak fixture",
            "API-key memory-space restriction",
            "inspect/audit parity",
            "memory-space membership",
        ),
        command=("moon", "run", "api:trust-control-rest-test"),
    ),
    GateCheck(
        name="api-trust-control-mcp",
        description="MCP add/manage closure, delegated identity, and deny cases",
        surfaces=(
            "MCP add policy",
            "MCP manage policy",
            "MCP leak fixture",
            "delegated authority",
            "API-key project restriction",
            "API-key memory-space restriction",
        ),
        command=("moon", "run", "api:trust-control-mcp-test"),
    ),
    GateCheck(
        name="api-trust-control-jobs",
        description="background job policy receipts and task-learning boundaries",
        surfaces=(
            "job policy receipt",
            "jobs leak fixture",
            "MCP manage policy",
        ),
        command=("moon", "run", "api:trust-control-jobs-test"),
    ),
    GateCheck(
        name="cli-trust-control",
        description="CLI memory inspect, prompt hook, and scoped API-key client payloads",
        surfaces=(
            "CLI leak fixture",
            "prompt hook leak fixture",
            "inspect/audit parity",
            "API-key project restriction",
            "API-key memory-space restriction",
        ),
        command=("moon", "run", "cli:trust-control-test"),
    ),
    GateCheck(
        name="web-trust-control",
        description="workspace trust UI, source inspect, spaces, and audit visibility",
        surfaces=(
            "web leak fixture",
            "inspect/audit parity",
            "persisted memory-space CRUD",
            "memory-space membership",
        ),
        command=("moon", "run", "web:trust-control-test"),
    ),
)

REQUIRED_SURFACES: tuple[str, ...] = (
    "persisted memory-space CRUD",
    "memory-space membership",
    "agent identity",
    "delegated authority",
    "API-key project restriction",
    "API-key memory-space restriction",
    "MCP add policy",
    "MCP manage policy",
    "job policy receipt",
    "REST leak fixture",
    "CLI leak fixture",
    "MCP leak fixture",
    "web leak fixture",
    "jobs leak fixture",
    "prompt hook leak fixture",
    "inspect/audit parity",
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
