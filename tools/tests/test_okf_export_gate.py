from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from shutil import which
from typing import NotRequired, TypedDict, cast

import pytest
from tools.trust import okf_export_gate

MISSING_SURFACE_EXIT_CODE = 2
REPO_ROOT = Path(__file__).resolve().parents[2]
OKF_MEMORY_CHANGELOG_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "okf-memory-changelog.yml"


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


def test_default_receipt_meets_okf_export_budgets() -> None:
    receipt = okf_export_gate.build_okf_export_receipt()

    assert receipt["schema_version"] == okf_export_gate.RECEIPT_SCHEMA_VERSION
    assert receipt["metrics"]["valid_okf_projection"] == 1
    assert receipt["metrics"]["byte_stable_reexport"] == 1
    assert receipt["metrics"]["graph_reconstruction_diff_count"] == 0
    assert receipt["metrics"]["memory_changelog_ready"] == 1
    assert receipt["okf"]["reserved_files"] == ["index.md", "log.md"]
    assert okf_export_gate.validate_okf_export_receipt(receipt) == []


def test_receipt_validation_rejects_budget_failures() -> None:
    receipt = okf_export_gate.build_okf_export_receipt()
    receipt["metrics"].update(
        {
            "valid_okf_projection": 0,
            "byte_stable_reexport": 0,
            "graph_reconstruction_diff_count": 1,
            "memory_changelog_ready": 0,
        }
    )

    assert okf_export_gate.validate_okf_export_receipt(receipt) == [
        "metric 'valid_okf_projection' below budget 1: 0",
        "metric 'byte_stable_reexport' below budget 1: 0",
        "metric 'graph_reconstruction_diff_count' exceeds budget 0: 1",
        "metric 'memory_changelog_ready' below budget 1: 0",
    ]


def test_gate_checks_cover_required_surfaces() -> None:
    assert okf_export_gate.missing_required_surfaces() == []


def test_gate_checks_use_moon_package_slices() -> None:
    commands = [check.command for check in okf_export_gate.GATE_CHECKS]

    assert commands == [
        ("moon", "run", "core:test", "--", "tests/test_okf_export.py"),
        ("moon", "run", "api:test", "--", "tests/test_cli_export.py", "-k", "okf"),
        ("moon", "run", "bench-gate"),
    ]


def test_run_gate_prints_and_writes_release_receipt(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    commands: list[tuple[str, ...]] = []
    receipt_path = tmp_path / "okf-export-receipt.json"

    def runner(command: tuple[str, ...]) -> int:
        if command == ("moon", "run", "bench-gate"):
            assert receipt_path.exists()
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            assert [check["name"] for check in receipt["checks"]] == [
                "core-okf-projection",
                "api-okf-cli",
            ]
        commands.append(command)
        return 0

    exit_code = okf_export_gate.run_gate(runner=runner, receipt_path=receipt_path)

    captured = capsys.readouterr()
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert commands == [check.command for check in okf_export_gate.GATE_CHECKS]
    assert payload["schema_version"] == okf_export_gate.RECEIPT_SCHEMA_VERSION
    assert payload["status"] == "PASS"
    assert payload["checks"][2]["command"] == "moon run bench-gate"
    assert "OKF Export Gate Receipt" in captured.out
    assert "status: PASS" in captured.out
    assert "graph_reconstruction_diff_count=0" in captured.out


def test_run_gate_executes_checks_before_failure(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    commands: list[tuple[str, ...]] = []
    failing_check = okf_export_gate.GATE_CHECKS[1]

    def runner(command: tuple[str, ...]) -> int:
        commands.append(command)
        return 1 if command == failing_check.command else 0

    exit_code = okf_export_gate.run_gate(
        runner=runner,
        receipt_path=tmp_path / "okf-export-receipt.json",
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert commands == [
        check.command
        for check in okf_export_gate.GATE_CHECKS
        if check.name != "ai-memory-contracts"
    ]
    assert "status: FAIL" in captured.out
    assert f"FAIL exit=1 {failing_check.name}" in captured.out


def test_run_gate_rejects_missing_required_surface() -> None:
    check = okf_export_gate.GateCheck(
        name="partial",
        description="partial coverage",
        surfaces=("valid OKF projection",),
        command=("moon", "run", "core:test"),
    )
    messages: list[str] = []

    exit_code = okf_export_gate.run_gate(
        [check],
        runner=lambda _: 0,
        echo=messages.append,
        receipt_path=None,
    )

    assert exit_code == MISSING_SURFACE_EXIT_CODE
    assert "OKF export gate is missing required surfaces:" in messages
    assert "- byte stable re-export" in messages


def test_main_lists_gate_checks(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = okf_export_gate.main(["--list"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "core-okf-projection: moon run core:test" in captured.out
    assert "ai-memory-contracts: moon run bench-gate" in captured.out


def test_root_moon_tasks_expose_okf_export_gate() -> None:
    tasks = _root_moon_tasks()

    gate = tasks["okf-export-gate"]
    assert gate["target"] == "root:okf-export-gate"
    assert gate["command"] == "uv"
    assert gate["args"] == ["run", "python", "-m", "tools.trust.okf_export_gate"]

    test_task = tasks["okf-export-gate-test"]
    assert test_task["target"] == "root:okf-export-gate-test"
    assert test_task["command"] == "uv"
    assert test_task["args"] == [
        "run",
        "pytest",
        "tools/tests/test_okf_export_gate.py",
        "-v",
    ]


def test_okf_memory_changelog_workflow_runs_gate_and_exports_when_archive_exists() -> None:
    workflow = OKF_MEMORY_CHANGELOG_WORKFLOW.read_text(encoding="utf-8")

    assert "name: OKF Memory Changelog" in workflow
    assert "archive_path:" in workflow
    assert "SIBYL_OKF_ARCHIVE_URL" in workflow
    assert 'if [[ ! -f "$ARCHIVE_PATH" ]]; then' in workflow
    assert "hashFiles(env.ARCHIVE_PATH)" not in workflow
    assert "uv run --directory apps/api sibyld export okf" in workflow
    assert "moon run okf-export-gate" in workflow
    assert "okf-memory-changelog.tar.gz" in workflow
    assert "benchmarks/results/ai-memory/okf-export-receipt.json" in workflow
