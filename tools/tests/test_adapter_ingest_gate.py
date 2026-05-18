from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from shutil import which
from typing import NotRequired, TypedDict, cast

import pytest
from tools.trust import adapter_ingest_gate

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
    assert adapter_ingest_gate.missing_required_surfaces() == []


def test_gate_checks_use_moon_package_slices() -> None:
    commands = [check.command for check in adapter_ingest_gate.GATE_CHECKS]

    assert all(command[:2] == ("moon", "run") for command in commands)
    assert ("moon", "run", "core:adapter-ingest-contract-test") in commands
    assert ("moon", "run", "core:adapter-ingest-mailbox-test") in commands
    assert ("moon", "run", "large-corpus-rehearsal") in commands


def test_run_gate_prints_release_receipt(capsys: pytest.CaptureFixture[str]) -> None:
    commands: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...]) -> int:
        commands.append(command)
        return 0

    exit_code = adapter_ingest_gate.run_gate(runner=runner)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert commands == [check.command for check in adapter_ingest_gate.GATE_CHECKS]
    assert "Adapter Ingest Gate Receipt" in captured.out
    assert "status: PASS" in captured.out
    assert "source adapter contract" in captured.out
    assert "source-preserving ingest" in captured.out
    assert "import resumability" in captured.out
    assert "dedupe correctness" in captured.out
    assert "private scope enforcement" in captured.out
    assert "large-corpus rehearsal" in captured.out
    assert "source search before extraction" in captured.out
    assert "source import receipts" in captured.out


def test_run_gate_executes_all_checks_before_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    commands: list[tuple[str, ...]] = []
    failing_check = adapter_ingest_gate.GATE_CHECKS[1]
    failing_command = failing_check.command

    def runner(command: tuple[str, ...]) -> int:
        commands.append(command)
        return 1 if command == failing_command else 0

    exit_code = adapter_ingest_gate.run_gate(runner=runner)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert commands == [check.command for check in adapter_ingest_gate.GATE_CHECKS]
    assert "status: FAIL" in captured.out
    assert f"FAIL exit=1 {failing_check.name}" in captured.out


def test_run_gate_turns_runner_exceptions_into_receipts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def runner(command: tuple[str, ...]) -> int:
        msg = f"cannot run {command[0]}"
        raise RuntimeError(msg)

    exit_code = adapter_ingest_gate.run_gate(runner=runner)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "status: FAIL" in captured.out
    assert "RuntimeError: cannot run moon" in captured.out


def test_run_gate_rejects_missing_required_surface() -> None:
    check = adapter_ingest_gate.GateCheck(
        name="partial",
        description="partial coverage",
        surfaces=("source adapter contract",),
        command=("moon", "run", "core:test"),
    )
    messages: list[str] = []

    exit_code = adapter_ingest_gate.run_gate([check], runner=lambda _: 0, echo=messages.append)

    assert exit_code == MISSING_SURFACE_EXIT_CODE
    assert "Adapter ingest gate is missing required surfaces:" in messages
    assert "- source-preserving ingest" in messages


def test_main_lists_gate_checks(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = adapter_ingest_gate.main(["--list"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert (
        "core-source-adapter-contract: moon run core:adapter-ingest-contract-test" in captured.out
    )
    assert "core-mailbox-ingest: moon run core:adapter-ingest-mailbox-test" in captured.out
    assert "large-corpus-rehearsal: moon run large-corpus-rehearsal" in captured.out


def test_root_moon_tasks_expose_adapter_ingest_gate() -> None:
    tasks = _moon_tasks("root")

    gate = tasks["adapter-ingest-gate"]
    assert gate["target"] == "root:adapter-ingest-gate"
    assert gate["command"] == "uv"
    assert gate["args"] == ["run", "python", "-m", "tools.trust.adapter_ingest_gate"]

    test_task = tasks["adapter-ingest-gate-test"]
    assert test_task["target"] == "root:adapter-ingest-gate-test"
    assert test_task["command"] == "uv"
    assert test_task["args"] == [
        "run",
        "pytest",
        "tools/tests/test_adapter_ingest_gate.py",
        "-v",
    ]


def test_core_moon_tasks_expose_adapter_ingest_gate_slices() -> None:
    tasks = _moon_tasks("core")

    contract = tasks["adapter-ingest-contract-test"]
    assert contract["target"] == "core:adapter-ingest-contract-test"
    assert contract["command"] == "uv"
    assert contract["args"] == [
        "run",
        "pytest",
        "tests/test_source_adapters.py",
        "-v",
    ]

    mailbox = tasks["adapter-ingest-mailbox-test"]
    assert mailbox["target"] == "core:adapter-ingest-mailbox-test"
    assert mailbox["command"] == "uv"
    assert mailbox["args"] == [
        "run",
        "pytest",
        "tests/test_mailbox_adapter.py",
        "-v",
    ]
