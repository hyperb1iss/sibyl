from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from shutil import which
from typing import NotRequired, TypedDict, cast

import pytest
from tools.trust import doc_claim_gate

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


def test_default_receipt_meets_doc_claim_budgets() -> None:
    receipt = doc_claim_gate.build_doc_claim_receipt()

    assert receipt["schema_version"] == doc_claim_gate.RECEIPT_SCHEMA_VERSION
    assert receipt["metrics"] == {
        "approval_boundary_label_count": len(doc_claim_gate.APPROVAL_BOUNDARY_PHRASES),
        "documented_claim_axis_count": len(doc_claim_gate.CLAIM_REQUIREMENTS),
        "unsupported_public_claim_count": 0,
        "v12_handoff_count": len(doc_claim_gate.V12_HANDOFF_PHRASES),
    }
    assert doc_claim_gate.validate_doc_claim_receipt(receipt) == []


def test_receipt_validation_rejects_missing_required_phrase() -> None:
    docs = doc_claim_gate.load_claim_docs()
    docs["docs/testing/longmemeval.md"] = docs["docs/testing/longmemeval.md"].replace(
        "not an answer-quality metric",
        "not the same category",
    )

    receipt = doc_claim_gate.build_doc_claim_receipt(docs)

    assert receipt["metrics"]["documented_claim_axis_count"] == (
        len(doc_claim_gate.CLAIM_REQUIREMENTS) - 1
    )
    failures = doc_claim_gate.validate_doc_claim_receipt(receipt)
    assert "metric 'documented_claim_axis_count' below budget" in failures[0]
    assert "retrieval-recall-axis" in failures[-1]


def test_receipt_validation_rejects_forbidden_claim() -> None:
    docs = doc_claim_gate.load_claim_docs()
    docs["docs/architecture/SIBYL_POST_1_0_ROADMAP.md"] += "\nround-trips back into Sibyl\n"

    receipt = doc_claim_gate.build_doc_claim_receipt(docs)

    assert receipt["metrics"]["unsupported_public_claim_count"] == 1
    assert receipt["unsupported_claims"][0]["reason"].startswith("OKF v1.1")
    assert doc_claim_gate.validate_doc_claim_receipt(receipt)[0] == (
        "metric 'unsupported_public_claim_count' exceeds budget 0: 1"
    )


def test_gate_checks_cover_required_surfaces() -> None:
    assert doc_claim_gate.missing_required_surfaces() == []


def test_gate_checks_use_moon_docs_and_manifest_slices() -> None:
    commands = [check.command for check in doc_claim_gate.GATE_CHECKS]

    assert commands == [
        ("moon", "run", "docs:lint"),
        ("moon", "run", "docs:build"),
        ("moon", "run", "bench-gate"),
    ]


def test_run_gate_prints_release_receipt(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    commands: list[tuple[str, ...]] = []
    receipt_path = tmp_path / "doc-claim-receipt.json"

    def runner(command: tuple[str, ...]) -> int:
        if command == ("moon", "run", "bench-gate"):
            assert receipt_path.exists()
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            assert [check["name"] for check in receipt["checks"]] == [
                "doc-claim-scan",
                "docs-claim-lint",
                "docs-claim-build",
            ]
        commands.append(command)
        return 0

    exit_code = doc_claim_gate.run_gate(runner=runner, receipt_path=receipt_path)

    captured = capsys.readouterr()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert commands == [check.command for check in doc_claim_gate.GATE_CHECKS]
    assert receipt["checks"][3]["command"] == "moon run bench-gate"
    assert "Doc Claim Gate Receipt" in captured.out
    assert "status: PASS" in captured.out
    assert "unsupported_public_claim_count=0" in captured.out


def test_run_gate_skips_manifest_contract_after_docs_failure(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    commands: list[tuple[str, ...]] = []
    failing_check = doc_claim_gate.GATE_CHECKS[1]

    def runner(command: tuple[str, ...]) -> int:
        commands.append(command)
        return 1 if command == failing_check.command else 0

    exit_code = doc_claim_gate.run_gate(
        runner=runner,
        receipt_path=tmp_path / "doc-claim-receipt.json",
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert commands == [
        check.command for check in doc_claim_gate.GATE_CHECKS if check.name != "ai-memory-contracts"
    ]
    assert "status: FAIL" in captured.out
    assert f"FAIL exit=1 {failing_check.name}" in captured.out


def test_run_gate_rejects_missing_required_surface() -> None:
    check = doc_claim_gate.GateCheck(
        name="partial",
        description="partial coverage",
        surfaces=("public docs build",),
        command=("moon", "run", "docs:build"),
    )
    messages: list[str] = []

    exit_code = doc_claim_gate.run_gate(
        [check],
        runner=lambda _: 0,
        echo=messages.append,
        receipt_path=None,
    )

    assert exit_code == MISSING_SURFACE_EXIT_CODE
    assert "Doc claim gate is missing required surfaces:" in messages
    assert "- public docs formatting" in messages


def test_main_lists_gate_checks(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = doc_claim_gate.main(["--list"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "doc-claim-scan: internal doc-claim-scan" in captured.out
    assert "ai-memory-contracts: moon run bench-gate" in captured.out


def test_root_moon_tasks_expose_doc_claim_gate() -> None:
    tasks = _root_moon_tasks()

    gate = tasks["doc-claim-gate"]
    assert gate["target"] == "root:doc-claim-gate"
    assert gate["command"] == "uv"
    assert gate["args"] == ["run", "python", "-m", "tools.trust.doc_claim_gate"]

    test_task = tasks["doc-claim-gate-test"]
    assert test_task["target"] == "root:doc-claim-gate-test"
    assert test_task["command"] == "uv"
    assert test_task["args"] == [
        "run",
        "pytest",
        "tools/tests/test_doc_claim_gate.py",
        "-v",
    ]
