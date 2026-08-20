"""Shared execution machinery for focused release gates."""

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


def covered_surfaces(checks: Iterable[GateCheck]) -> set[str]:
    return {surface for check in checks for surface in check.surfaces}


def missing_required_surfaces(
    checks: Iterable[GateCheck],
    required_surfaces: Iterable[str],
) -> list[str]:
    covered = covered_surfaces(checks)
    return [surface for surface in required_surfaces if surface not in covered]


def format_command(command: Sequence[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def stdout_echo(message: str = "") -> None:
    sys.stdout.write(f"{message}\n")


def run_command(command: tuple[str, ...], *, repo_root: Path) -> int:
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
        cwd=repo_root,
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


def _print_receipt(
    gate_name: str,
    results: Sequence[GateResult],
    *,
    echo: Echo,
) -> None:
    passed = [result for result in results if result.passed]
    failed = [result for result in results if not result.passed]
    status = "PASS" if not failed else "FAIL"
    surfaces = sorted(covered_surfaces(result.check for result in results))

    echo("")
    echo(f"{gate_name} Receipt")
    echo(f"status: {status}")
    echo(f"checks: {len(passed)} passed, {len(failed)} failed")
    echo(f"surfaces: {', '.join(surfaces)}")
    for result in results:
        check_status = "PASS" if result.passed else f"FAIL exit={result.exit_code}"
        error = f"; error={result.error}" if result.error is not None else ""
        echo(f"- {check_status} {result.check.name} ({result.elapsed_seconds:.2f}s){error}")


def run_gate(
    *,
    gate_name: str,
    missing_surfaces_heading: str,
    checks: Sequence[GateCheck],
    required_surfaces: Sequence[str],
    repo_root: Path,
    runner: Runner | None = None,
    echo: Echo = stdout_echo,
) -> int:
    missing = missing_required_surfaces(checks, required_surfaces)
    if missing:
        echo(missing_surfaces_heading)
        for surface in missing:
            echo(f"- {surface}")
        return 2

    active_runner = runner or (lambda command: run_command(command, repo_root=repo_root))
    echo(gate_name)
    echo(f"checks: {len(checks)}")

    results = [_run_check(check, runner=active_runner, echo=echo) for check in checks]
    _print_receipt(gate_name, results, echo=echo)
    return 0 if all(result.passed for result in results) else 1


def main(
    argv: list[str] | None,
    *,
    parser_description: str,
    gate_name: str,
    missing_surfaces_heading: str,
    checks: Sequence[GateCheck],
    required_surfaces: Sequence[str],
    repo_root: Path,
    echo: Echo = stdout_echo,
) -> int:
    parser = argparse.ArgumentParser(description=parser_description)
    parser.add_argument(
        "--list",
        action="store_true",
        help="List checks and exit without running them.",
    )
    args = parser.parse_args(argv)

    if args.list:
        for check in checks:
            echo(f"{check.name}: {format_command(check.command)}")
        return 0

    return run_gate(
        gate_name=gate_name,
        missing_surfaces_heading=missing_surfaces_heading,
        checks=checks,
        required_surfaces=required_surfaces,
        repo_root=repo_root,
        echo=echo,
    )
