#!/usr/bin/env python3
"""Run the focused release gate for v0.11 trust-control surfaces."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from shutil import which

REPO_ROOT = Path(__file__).resolve().parents[2]

Runner = Callable[[tuple[str, ...]], int]
Echo = Callable[[str], None]


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


def covered_surfaces(checks: Iterable[GateCheck] = GATE_CHECKS) -> set[str]:
    return {surface for check in checks for surface in check.surfaces}


def missing_required_surfaces(checks: Sequence[GateCheck] = GATE_CHECKS) -> list[str]:
    covered = covered_surfaces(checks)
    return [surface for surface in REQUIRED_SURFACES if surface not in covered]


def format_command(command: Sequence[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def _echo(message: str = "") -> None:
    sys.stdout.write(f"{message}\n")


def _real_runner(command: tuple[str, ...]) -> int:
    if not command:
        msg = "Gate command cannot be empty"
        raise ValueError(msg)

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
    return GateResult(
        check=check,
        exit_code=exit_code,
        elapsed_seconds=elapsed,
        error=error,
    )


def _print_receipt(results: Sequence[GateResult], *, echo: Echo) -> None:
    passed = [result for result in results if result.passed]
    failed = [result for result in results if not result.passed]
    status = "PASS" if not failed else "FAIL"
    surfaces = sorted(covered_surfaces(result.check for result in results))

    echo("")
    echo("Trust Control Gate Receipt")
    echo(f"status: {status}")
    echo(f"checks: {len(passed)} passed, {len(failed)} failed")
    echo(f"surfaces: {', '.join(surfaces)}")
    for result in results:
        check_status = "PASS" if result.passed else f"FAIL exit={result.exit_code}"
        error = f"; error={result.error}" if result.error is not None else ""
        echo(f"- {check_status} {result.check.name} ({result.elapsed_seconds:.2f}s){error}")


def run_gate(
    checks: Sequence[GateCheck] = GATE_CHECKS,
    *,
    runner: Runner | None = None,
    echo: Echo = _echo,
) -> int:
    missing = missing_required_surfaces(checks)
    if missing:
        echo("Trust control gate is missing required surfaces:")
        for surface in missing:
            echo(f"- {surface}")
        return 2

    active_runner = runner or _real_runner
    echo("Trust Control Gate")
    echo(f"checks: {len(checks)}")

    results = [_run_check(check, runner=active_runner, echo=echo) for check in checks]
    _print_receipt(results, echo=echo)
    return 0 if all(result.passed for result in results) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run focused v0.11 trust-control checks.")
    parser.add_argument(
        "--list",
        action="store_true",
        help="List checks and exit without running them.",
    )
    args = parser.parse_args(argv)

    if args.list:
        for check in GATE_CHECKS:
            _echo(f"{check.name}: {format_command(check.command)}")
        return 0

    return run_gate()


if __name__ == "__main__":
    raise SystemExit(main())
