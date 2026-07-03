from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from shutil import which
from typing import NotRequired, TypedDict, cast

import pytest
from tools.trust import write_path_integrity_gate

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


def test_observed_receipt_meets_zero_budgets() -> None:
    receipt = write_path_integrity_gate.build_observed_write_path_receipt()

    assert receipt["schema_version"] == write_path_integrity_gate.RECEIPT_SCHEMA_VERSION
    assert receipt["metrics"] == {
        "hallucinated_fact_count": 0,
        "self_referential_write_count": 0,
        "low_signal_write_count": 0,
    }
    assert write_path_integrity_gate.validate_write_path_integrity_receipt(receipt) == []


def test_seeded_fixture_counts_hallucinated_forbidden_facts() -> None:
    receipt = write_path_integrity_gate.build_write_path_integrity_receipt(
        extraction_cases=(
            write_path_integrity_gate.SeededExtractionCase(
                source_id="source-1",
                content="The Lumen route uses native RRF.",
                forbidden_terms=("mercury route",),
                entities=(
                    write_path_integrity_gate.SeededEntity(
                        name="Mercury route",
                        summary="The Mercury route uses native RRF.",
                        evidence="The Mercury route uses native RRF.",
                    ),
                ),
            ),
        )
    )

    assert receipt["metrics"]["hallucinated_fact_count"] == 1
    assert write_path_integrity_gate.validate_write_path_integrity_receipt(receipt) == [
        "metric 'hallucinated_fact_count' exceeds budget 0: 1"
    ]


def test_seeded_fixture_counts_self_referential_selection() -> None:
    receipt = write_path_integrity_gate.build_write_path_integrity_receipt(
        dream_source_cases=(
            write_path_integrity_gate.DreamSourceCase(
                "reflection-output",
                "reflection",
                True,
            ),
        )
    )

    assert receipt["metrics"]["self_referential_write_count"] == 1


def test_seeded_fixture_counts_low_signal_writes() -> None:
    receipt = write_path_integrity_gate.build_write_path_integrity_receipt(
        low_signal_cases=(
            write_path_integrity_gate.LowSignalCase(
                source_id="ack",
                no_op_sources=0,
                extracted_entities=1,
                projected_entities=1,
            ),
        )
    )

    assert receipt["metrics"]["low_signal_write_count"] == 1


def test_gate_checks_cover_required_surfaces() -> None:
    assert write_path_integrity_gate.missing_required_surfaces() == []


def test_gate_checks_use_moon_package_slices() -> None:
    commands = [check.command for check in write_path_integrity_gate.GATE_CHECKS]

    assert commands == [
        ("moon", "run", "core:write-path-integrity-test"),
        ("moon", "run", "api:write-path-integrity-test"),
        ("moon", "run", "bench-gate"),
    ]


def test_run_gate_prints_release_receipt(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    commands: list[tuple[str, ...]] = []
    receipt_path = tmp_path / "write-path-integrity-receipt.json"

    def runner(command: tuple[str, ...]) -> int:
        assert receipt_path.exists()
        commands.append(command)
        return 0

    exit_code = write_path_integrity_gate.run_gate(runner=runner, receipt_path=receipt_path)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert commands == [check.command for check in write_path_integrity_gate.GATE_CHECKS]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["checks"][2]["command"] == "moon run bench-gate"
    assert "Write-Path Integrity Gate Receipt" in captured.out
    assert "status: PASS" in captured.out
    assert "hallucinated_fact_count=0" in captured.out
    assert "self-feeding guard" in captured.out
    assert "low-signal no-op" in captured.out


def test_run_gate_executes_all_checks_before_failure(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    commands: list[tuple[str, ...]] = []
    failing_check = write_path_integrity_gate.GATE_CHECKS[1]
    failing_command = failing_check.command

    def runner(command: tuple[str, ...]) -> int:
        commands.append(command)
        return 1 if command == failing_command else 0

    exit_code = write_path_integrity_gate.run_gate(
        runner=runner,
        receipt_path=tmp_path / "receipt.json",
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert commands == [check.command for check in write_path_integrity_gate.GATE_CHECKS]
    assert "status: FAIL" in captured.out
    assert f"FAIL exit=1 {failing_check.name}" in captured.out


def test_run_gate_rejects_missing_required_surface() -> None:
    check = write_path_integrity_gate.GateCheck(
        name="partial",
        description="partial coverage",
        surfaces=("extraction",),
        command=("moon", "run", "api:test"),
    )
    messages: list[str] = []

    exit_code = write_path_integrity_gate.run_gate(
        [check],
        runner=lambda _: 0,
        echo=messages.append,
        receipt_path=None,
    )

    assert exit_code == MISSING_SURFACE_EXIT_CODE
    assert "Write-path integrity gate is missing required surfaces:" in messages
    assert "- low-signal no-op" in messages


def test_main_lists_gate_checks(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = write_path_integrity_gate.main(["--list"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "core-write-path-integrity: moon run core:write-path-integrity-test" in captured.out
    assert "api-write-path-integrity: moon run api:write-path-integrity-test" in captured.out
    assert "ai-memory-contracts: moon run bench-gate" in captured.out


def test_root_moon_tasks_expose_write_path_integrity_gate() -> None:
    tasks = _root_moon_tasks()

    gate = tasks["write-path-integrity-gate"]
    assert gate["target"] == "root:write-path-integrity-gate"
    assert gate["command"] == "uv"
    assert gate["args"] == [
        "run",
        "python",
        "-m",
        "tools.trust.write_path_integrity_gate",
    ]

    test_task = tasks["write-path-integrity-gate-test"]
    assert test_task["target"] == "root:write-path-integrity-gate-test"
    assert test_task["command"] == "uv"
    assert test_task["args"] == [
        "run",
        "pytest",
        "tools/tests/test_write_path_integrity_gate.py",
        "-v",
    ]
