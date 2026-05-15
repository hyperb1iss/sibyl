from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from shutil import which
from typing import NotRequired, TypedDict, cast

import pytest
from tools.trust import trust_control_gate

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
    assert trust_control_gate.missing_required_surfaces() == []


def test_gate_checks_use_moon_package_slices() -> None:
    commands = [check.command for check in trust_control_gate.GATE_CHECKS]

    assert all(command[:2] == ("moon", "run") for command in commands)
    assert ("moon", "run", "core:memory-trust-policy-test") in commands
    assert ("moon", "run", "api:trust-control-auth-test") in commands
    assert ("moon", "run", "api:trust-control-rest-test") in commands
    assert ("moon", "run", "api:trust-control-mcp-test") in commands
    assert ("moon", "run", "api:trust-control-jobs-test") in commands
    assert ("moon", "run", "cli:trust-control-test") in commands
    assert ("moon", "run", "web:trust-control-test") in commands


def test_run_gate_prints_release_receipt(capsys: pytest.CaptureFixture[str]) -> None:
    commands: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...]) -> int:
        commands.append(command)
        return 0

    exit_code = trust_control_gate.run_gate(runner=runner)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert commands == [check.command for check in trust_control_gate.GATE_CHECKS]
    assert "Trust Control Gate Receipt" in captured.out
    assert "status: PASS" in captured.out
    assert "API-key memory-space restriction" in captured.out
    assert "MCP manage policy" in captured.out
    assert "prompt hook leak fixture" in captured.out
    assert "inspect/audit parity" in captured.out


def test_run_gate_executes_all_checks_before_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    commands: list[tuple[str, ...]] = []
    failing_check = trust_control_gate.GATE_CHECKS[1]
    failing_command = failing_check.command

    def runner(command: tuple[str, ...]) -> int:
        commands.append(command)
        return 1 if command == failing_command else 0

    exit_code = trust_control_gate.run_gate(runner=runner)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert commands == [check.command for check in trust_control_gate.GATE_CHECKS]
    assert "status: FAIL" in captured.out
    assert f"FAIL exit=1 {failing_check.name}" in captured.out


def test_run_gate_turns_runner_exceptions_into_receipts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def runner(command: tuple[str, ...]) -> int:
        msg = f"cannot run {command[0]}"
        raise RuntimeError(msg)

    exit_code = trust_control_gate.run_gate(runner=runner)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "status: FAIL" in captured.out
    assert "RuntimeError: cannot run moon" in captured.out


def test_run_gate_rejects_missing_required_surface() -> None:
    check = trust_control_gate.GateCheck(
        name="partial",
        description="partial coverage",
        surfaces=("persisted memory-space CRUD",),
        command=("moon", "run", "core:test"),
    )
    messages: list[str] = []

    exit_code = trust_control_gate.run_gate([check], runner=lambda _: 0, echo=messages.append)

    assert exit_code == MISSING_SURFACE_EXIT_CODE
    assert "Trust control gate is missing required surfaces:" in messages
    assert "- API-key memory-space restriction" in messages


def test_main_lists_gate_checks(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = trust_control_gate.main(["--list"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "api-trust-control-auth: moon run api:trust-control-auth-test" in captured.out
    assert "web-trust-control: moon run web:trust-control-test" in captured.out


def test_root_moon_tasks_expose_trust_control_gate() -> None:
    tasks = _root_moon_tasks()

    gate = tasks["trust-control-gate"]
    assert gate["target"] == "root:trust-control-gate"
    assert gate["command"] == "uv"
    assert gate["args"] == ["run", "python", "-m", "tools.trust.trust_control_gate"]

    test_task = tasks["trust-control-gate-test"]
    assert test_task["target"] == "root:trust-control-gate-test"
    assert test_task["command"] == "uv"
    assert test_task["args"] == [
        "run",
        "pytest",
        "tools/tests/test_trust_control_gate.py",
        "-v",
    ]
