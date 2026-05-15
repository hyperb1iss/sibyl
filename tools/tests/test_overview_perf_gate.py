from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from shutil import which
from typing import NotRequired, TypedDict, cast

import pytest
from tools.trust import overview_perf_gate

MISSING_SURFACE_EXIT_CODE = 2
REPO_ROOT = Path(__file__).resolve().parents[2]


class MoonTask(TypedDict):
    command: str
    args: NotRequired[list[str]]
    target: str


class MoonTaskQuery(TypedDict):
    tasks: dict[str, dict[str, MoonTask]]


def _root_moon_tasks() -> dict[str, MoonTask]:
    moon = which("moon")
    assert moon is not None

    result = subprocess.run(  # noqa: S603
        [moon, "query", "tasks", "--project", "root"],
        cwd=REPO_ROOT,
        env={**os.environ, "MOON_COLOR": "false"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = cast(MoonTaskQuery, json.loads(result.stdout))
    return payload["tasks"]["root"]


def test_gate_checks_cover_required_surfaces() -> None:
    assert overview_perf_gate.missing_required_surfaces() == []


def test_gate_checks_use_moon_package_slices() -> None:
    commands = [check.command for check in overview_perf_gate.GATE_CHECKS]

    assert commands == [
        ("moon", "run", "api:overview-perf-test"),
        ("moon", "run", "web:overview-perf-test"),
    ]


def test_run_gate_prints_release_receipt(capsys: pytest.CaptureFixture[str]) -> None:
    commands: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...]) -> int:
        commands.append(command)
        return 0

    exit_code = overview_perf_gate.run_gate(runner=runner)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert commands == [check.command for check in overview_perf_gate.GATE_CHECKS]
    assert "Overview Performance Gate Receipt" in captured.out
    assert "status: PASS" in captured.out
    assert "admin stats" in captured.out
    assert "no task entity fetch" in captured.out


def test_run_gate_executes_all_checks_before_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    commands: list[tuple[str, ...]] = []
    failing_check = overview_perf_gate.GATE_CHECKS[1]
    failing_command = failing_check.command

    def runner(command: tuple[str, ...]) -> int:
        commands.append(command)
        return 1 if command == failing_command else 0

    exit_code = overview_perf_gate.run_gate(runner=runner)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert commands == [check.command for check in overview_perf_gate.GATE_CHECKS]
    assert "status: FAIL" in captured.out
    assert f"FAIL exit=1 {failing_check.name}" in captured.out


def test_run_gate_turns_runner_exceptions_into_receipts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def runner(command: tuple[str, ...]) -> int:
        msg = f"cannot run {command[0]}"
        raise RuntimeError(msg)

    exit_code = overview_perf_gate.run_gate(runner=runner)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "status: FAIL" in captured.out
    assert "RuntimeError: cannot run moon" in captured.out


def test_run_gate_rejects_missing_required_surface() -> None:
    check = overview_perf_gate.GateCheck(
        name="partial",
        description="partial coverage",
        surfaces=("admin stats",),
        command=("moon", "run", "api:test"),
    )
    messages: list[str] = []

    exit_code = overview_perf_gate.run_gate([check], runner=lambda _: 0, echo=messages.append)

    assert exit_code == MISSING_SURFACE_EXIT_CODE
    assert "Overview performance gate is missing required surfaces:" in messages
    assert "- dashboard overview" in messages


def test_main_lists_gate_checks(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = overview_perf_gate.main(["--list"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "api-overview-metrics: moon run api:overview-perf-test" in captured.out
    assert "web-overview-dashboard: moon run web:overview-perf-test" in captured.out


def test_root_moon_tasks_expose_overview_perf_gate() -> None:
    tasks = _root_moon_tasks()

    gate = tasks["overview-perf-gate"]
    assert gate["target"] == "root:overview-perf-gate"
    assert gate["command"] == "uv"
    assert gate["args"] == ["run", "python", "-m", "tools.trust.overview_perf_gate"]

    test_task = tasks["overview-perf-gate-test"]
    assert test_task["target"] == "root:overview-perf-gate-test"
    assert test_task["command"] == "uv"
    assert test_task["args"] == [
        "run",
        "pytest",
        "tools/tests/test_overview_perf_gate.py",
        "-v",
    ]
