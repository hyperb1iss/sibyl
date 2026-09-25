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


WITHDRAWN_HEADLINE_VARIANTS = (
    "Strict recall is 96.96% R@5 on LongMemEval-S.",
    "Strict recall rounds to 97% R@5.",
    "LongMemEval-S retrieval: ~97% strict recall.",
    "`recall@10` = **99%** on the live run.",
    "| recall@5 | **97.0%** (strict multi-answer) |",
    "Sibyl reaches ~97% on LongMemEval-S.",
)


@pytest.mark.parametrize("variant", WITHDRAWN_HEADLINE_VARIANTS)
@pytest.mark.parametrize(
    "path",
    ["README.md", "docs/guide/quick-start.md", "packages/python/sibyl-core/README.md"],
)
def test_withdrawn_headline_fails_on_public_surfaces_outside_the_claim_corpus(
    path: str, variant: str
) -> None:
    public_docs = doc_claim_gate.load_public_docs()
    assert path in public_docs
    assert path not in doc_claim_gate.CLAIM_DOC_PATHS
    public_docs[path] += f"\n{variant}\n"

    receipt = doc_claim_gate.build_doc_claim_receipt(public_docs=public_docs)

    assert receipt["metrics"]["unsupported_public_claim_count"] == 1
    claim = receipt["unsupported_claims"][0]
    assert claim["path"] == path
    assert claim["reason"] == doc_claim_gate.WITHDRAWN_HEADLINE_REASON
    assert doc_claim_gate.validate_doc_claim_receipt(receipt)[0] == (
        "metric 'unsupported_public_claim_count' exceeds budget 0: 1"
    )


@pytest.mark.parametrize(
    "text",
    [
        "Memweave reports 98.0% R@5 and 99.11% R@10 on a held-out split.",
        "MemPalace raw is 96.6% R@5; agentmemory is 95.2% R@5 and 98.6% R@10.",
        "The HNSW index keeps 99% recall at ef=40.",
        "97% of requests finish under the latency budget.",
        "Replay projected 97.35% strict R@5 before the run was withdrawn.",
        "| MemPalace hybrid | 100% R@5 (full), 98.4% held-out | recall_any@K |",
        "HNSW recall@10 stays at 99% with ef=64",
        "99% recall@10 on SIFT1M",
        "Competitor X claims 97% on LongMemEval",
        "hit@5 is 97% on LongMemEval-S",
        "| recall@5 |\n| 97% of cases |",
    ],
)
def test_withdrawn_headline_pattern_ignores_other_percentages(text: str) -> None:
    assert doc_claim_gate.find_withdrawn_headline(text) == []


def test_public_scan_covers_readmes_and_every_docs_page() -> None:
    public_docs = doc_claim_gate.load_public_docs()
    docs_pages = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "docs").rglob("*.md")
        if not any(
            part == "node_modules" or part.startswith(".")
            for part in path.relative_to(REPO_ROOT).parts
        )
    }

    assert "README.md" in public_docs
    assert "packages/python/sibyl-core/README.md" in public_docs
    assert docs_pages <= set(public_docs)
    assert doc_claim_gate.build_doc_claim_receipt()["public_scan_doc_count"] >= len(docs_pages)
