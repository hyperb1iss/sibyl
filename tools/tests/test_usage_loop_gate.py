from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from shutil import which
from typing import NotRequired, TypedDict, cast

import pytest
from tools.trust import usage_loop_gate

MISSING_SURFACE_EXIT_CODE = 2
EXPECTED_CITATION_EVENT_COUNT = 2
EXPECTED_CONSOLIDATION_INPUT_COUNT = 2
CITED_DECAY_ADVANTAGE_BUDGET = 0.1
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


def test_default_receipt_meets_usage_loop_budgets() -> None:
    receipt = usage_loop_gate.build_usage_loop_receipt()

    assert receipt["schema_version"] == usage_loop_gate.RECEIPT_SCHEMA_VERSION
    assert receipt["metrics"]["exposure_stamp_coverage"] == 1.0
    assert receipt["metrics"]["citation_event_count"] == EXPECTED_CITATION_EVENT_COUNT
    assert receipt["metrics"]["duplicate_stored_event_count"] == 0
    assert receipt["metrics"]["duplicate_suppressed_event_count"] == 1
    assert (
        receipt["metrics"]["usage_ordered_consolidation_input_count"]
        == EXPECTED_CONSOLIDATION_INPUT_COUNT
    )
    assert receipt["metrics"]["cited_decay_score_advantage"] > CITED_DECAY_ADVANTAGE_BUDGET
    assert receipt["consolidation_inputs"][0]["memory_id"] == "protected-cited-twin"
    assert usage_loop_gate.validate_usage_loop_receipt(receipt) == []


def test_receipt_validation_rejects_budget_failures() -> None:
    receipt = usage_loop_gate.build_usage_loop_receipt()
    receipt["metrics"].update(
        {
            "citation_event_count": 0,
            "cited_decay_score_advantage": 0.0,
            "duplicate_stored_event_count": 1,
            "exposure_stamp_coverage": 0.5,
            "usage_ordered_consolidation_input_count": 0,
        }
    )

    assert usage_loop_gate.validate_usage_loop_receipt(receipt) == [
        "metric 'exposure_stamp_coverage' below budget 1.0: 0.5",
        "metric 'citation_event_count' below budget 1: 0",
        "metric 'duplicate_stored_event_count' exceeds budget 0: 1",
        "metric 'usage_ordered_consolidation_input_count' below budget 1: 0",
        "metric 'cited_decay_score_advantage' below budget 0.1: 0.0",
    ]


def test_gate_checks_cover_required_surfaces() -> None:
    assert usage_loop_gate.missing_required_surfaces() == []


def test_gate_checks_use_moon_package_slices() -> None:
    commands = [check.command for check in usage_loop_gate.GATE_CHECKS]

    assert commands == [
        (
            "moon",
            "run",
            "core:test",
            "--",
            "tests/test_usage_service.py",
            "tests/test_tools.py",
            "-k",
            "record_memory_usage or usage_exposure or usage_citation",
        ),
        (
            "moon",
            "run",
            "api:test",
            "--",
            "tests/test_routes_tasks.py::TestCompleteTaskRoute::"
            "test_complete_task_records_cited_memories",
            "tests/test_routes_context.py::TestReflectRoute::test_reflect_records_cited_memories",
            "tests/test_routes_memory.py::test_cite_memory_records_usage_and_audit",
            "tests/test_server_accessible_projects.py::"
            "test_manage_mcp_complete_task_records_cited_memories",
            "tests/test_server_accessible_projects.py::"
            "test_reflect_mcp_memory_records_cited_memories",
        ),
        (
            "moon",
            "run",
            "cli:test",
            "--",
            "tests/test_main_capture.py::test_cite_command_records_cited_memories",
            "tests/test_main_capture.py::test_reflect_command_passes_cited_ids",
            "tests/test_task.py::test_task_complete_with_cited_ids_reports_usage",
        ),
        (
            "moon",
            "run",
            "api:test",
            "--",
            "tests/test_jobs_consolidation.py",
            "-k",
            "priority_decay",
        ),
        ("moon", "run", "bench-gate"),
    ]


def test_run_gate_prints_release_receipt(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    commands: list[tuple[str, ...]] = []
    receipt_path = tmp_path / "usage-loop-receipt.json"

    def runner(command: tuple[str, ...]) -> int:
        if command == ("moon", "run", "bench-gate"):
            assert receipt_path.exists()
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            assert [check["name"] for check in receipt["checks"]] == [
                "core-usage-feedback",
                "api-usage-citation",
                "cli-usage-citation",
                "api-usage-aware-consolidation",
            ]
        commands.append(command)
        return 0

    exit_code = usage_loop_gate.run_gate(runner=runner, receipt_path=receipt_path)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert commands == [check.command for check in usage_loop_gate.GATE_CHECKS]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["checks"][4]["command"] == "moon run bench-gate"
    assert "Usage Loop Gate Receipt" in captured.out
    assert "status: PASS" in captured.out
    assert "citation_event_count=2" in captured.out
    assert "cited decay divergence" in captured.out


def test_run_gate_executes_all_checks_before_failure(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    commands: list[tuple[str, ...]] = []
    failing_check = usage_loop_gate.GATE_CHECKS[1]
    failing_command = failing_check.command

    def runner(command: tuple[str, ...]) -> int:
        commands.append(command)
        return 1 if command == failing_command else 0

    exit_code = usage_loop_gate.run_gate(
        runner=runner,
        receipt_path=tmp_path / "usage-loop-receipt.json",
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert commands == [
        check.command
        for check in usage_loop_gate.GATE_CHECKS
        if check.name != "ai-memory-contracts"
    ]
    assert "status: FAIL" in captured.out
    assert f"FAIL exit=1 {failing_check.name}" in captured.out


def test_run_gate_turns_runner_exceptions_into_receipts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def runner(command: tuple[str, ...]) -> int:
        msg = f"cannot run {command[0]}"
        raise RuntimeError(msg)

    exit_code = usage_loop_gate.run_gate(runner=runner, receipt_path=None)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "status: FAIL" in captured.out
    assert "RuntimeError: cannot run moon" in captured.out


def test_run_gate_rejects_missing_required_surface() -> None:
    check = usage_loop_gate.GateCheck(
        name="partial",
        description="partial coverage",
        surfaces=("citation stamping",),
        command=("moon", "run", "core:test"),
    )
    messages: list[str] = []

    exit_code = usage_loop_gate.run_gate(
        [check],
        runner=lambda _: 0,
        echo=messages.append,
        receipt_path=None,
    )

    assert exit_code == MISSING_SURFACE_EXIT_CODE
    assert "Usage loop gate is missing required surfaces:" in messages
    assert "- exposure stamping" in messages


def test_main_lists_gate_checks(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = usage_loop_gate.main(["--list"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "core-usage-feedback: moon run core:test" in captured.out
    assert "ai-memory-contracts: moon run bench-gate" in captured.out


def test_root_moon_tasks_expose_usage_loop_gate() -> None:
    tasks = _root_moon_tasks()

    gate = tasks["usage-loop-gate"]
    assert gate["target"] == "root:usage-loop-gate"
    assert gate["command"] == "uv"
    assert gate["args"] == ["run", "python", "-m", "tools.trust.usage_loop_gate"]

    test_task = tasks["usage-loop-gate-test"]
    assert test_task["target"] == "root:usage-loop-gate-test"
    assert test_task["command"] == "uv"
    assert test_task["args"] == [
        "run",
        "pytest",
        "tools/tests/test_usage_loop_gate.py",
        "-v",
    ]
