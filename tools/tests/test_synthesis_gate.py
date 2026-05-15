from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from shutil import which
from typing import NotRequired, TypedDict, cast

import pytest
from tools.trust import synthesis_gate

MISSING_SURFACE_EXIT_CODE = 2
REPO_ROOT = Path(__file__).resolve().parents[2]


class MoonTask(TypedDict):
    command: str
    args: NotRequired[list[str]]
    target: str


class MoonTaskQuery(TypedDict):
    tasks: dict[str, dict[str, MoonTask]]


def _moon_tasks(project: str) -> dict[str, MoonTask]:
    moon = which("moon")
    assert moon is not None

    result = subprocess.run(  # noqa: S603
        [moon, "query", "tasks", "--project", project],
        cwd=REPO_ROOT,
        env={**os.environ, "MOON_COLOR": "false"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = cast(MoonTaskQuery, json.loads(result.stdout))
    return payload["tasks"][project]


def test_gate_checks_cover_required_surfaces() -> None:
    assert synthesis_gate.missing_required_surfaces() == []


def test_gate_checks_use_moon_package_slices() -> None:
    commands = [check.command for check in synthesis_gate.GATE_CHECKS]

    assert all(command[:2] == ("moon", "run") for command in commands)
    assert ("moon", "run", "core:synthesis-gate-test") in commands
    assert ("moon", "run", "core:synthesis-tool-gate-test") in commands


def test_run_gate_prints_release_receipt(capsys: pytest.CaptureFixture[str]) -> None:
    commands: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...]) -> int:
        commands.append(command)
        return 0

    exit_code = synthesis_gate.run_gate(runner=runner)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert commands == [check.command for check in synthesis_gate.GATE_CHECKS]
    assert "Synthesis Gate Receipt" in captured.out
    assert "status: PASS" in captured.out
    assert "source ids per section" in captured.out
    assert "hidden-scope absence" in captured.out
    assert "unresolved-gap reporting" in captured.out
    assert "artifact provenance" in captured.out


def test_run_gate_executes_all_checks_before_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    commands: list[tuple[str, ...]] = []
    failing_check = synthesis_gate.GATE_CHECKS[1]
    failing_command = failing_check.command

    def runner(command: tuple[str, ...]) -> int:
        commands.append(command)
        return 1 if command == failing_command else 0

    exit_code = synthesis_gate.run_gate(runner=runner)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert commands == [check.command for check in synthesis_gate.GATE_CHECKS]
    assert "status: FAIL" in captured.out
    assert f"FAIL exit=1 {failing_check.name}" in captured.out


def test_run_gate_turns_runner_exceptions_into_receipts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def runner(command: tuple[str, ...]) -> int:
        msg = f"cannot run {command[0]}"
        raise RuntimeError(msg)

    exit_code = synthesis_gate.run_gate(runner=runner)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "status: FAIL" in captured.out
    assert "RuntimeError: cannot run moon" in captured.out


def test_run_gate_rejects_missing_required_surface() -> None:
    check = synthesis_gate.GateCheck(
        name="partial",
        description="partial coverage",
        surfaces=("source ids per section",),
        command=("moon", "run", "core:test"),
    )
    messages: list[str] = []

    exit_code = synthesis_gate.run_gate([check], runner=lambda _: 0, echo=messages.append)

    assert exit_code == MISSING_SURFACE_EXIT_CODE
    assert "Synthesis gate is missing required surfaces:" in messages
    assert "- hidden-scope absence" in messages


def test_main_lists_gate_checks(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = synthesis_gate.main(["--list"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "core-synthesis-contract: moon run core:synthesis-gate-test" in captured.out
    assert "core-synthesis-tools: moon run core:synthesis-tool-gate-test" in captured.out


def test_root_moon_tasks_expose_synthesis_gate() -> None:
    tasks = _moon_tasks("root")

    gate = tasks["synthesis-gate"]
    assert gate["target"] == "root:synthesis-gate"
    assert gate["command"] == "uv"
    assert gate["args"] == ["run", "python", "-m", "tools.trust.synthesis_gate"]

    test_task = tasks["synthesis-gate-test"]
    assert test_task["target"] == "root:synthesis-gate-test"
    assert test_task["command"] == "uv"
    assert test_task["args"] == ["run", "pytest", "tools/tests/test_synthesis_gate.py", "-v"]


def test_core_moon_tasks_expose_synthesis_gate_slices() -> None:
    tasks = _moon_tasks("core")

    contract = tasks["synthesis-gate-test"]
    assert contract["target"] == "core:synthesis-gate-test"
    assert contract["command"] == "uv"
    assert contract["args"] == ["run", "pytest", "tests/test_synthesis.py", "-v"]

    tools = tasks["synthesis-tool-gate-test"]
    assert tools["target"] == "core:synthesis-tool-gate-test"
    assert tools["command"] == "uv"
    assert tools["args"] == [
        "run",
        "pytest",
        "tests/test_tools.py::test_synthesis_plan_tool_materializes_section_sources",
        "tests/test_tools.py::test_synthesis_draft_tool_can_remember_artifact",
        "tests/test_tools.py::test_synthesis_verify_tool_reports_gaps_without_artifact",
        "-v",
    ]
