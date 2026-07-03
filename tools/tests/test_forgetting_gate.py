from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from shutil import which
from typing import NotRequired, TypedDict, cast

from tools.trust import forgetting_gate

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


def test_default_receipt_meets_w7_budgets() -> None:
    receipt = forgetting_gate.build_forgetting_receipt()

    assert receipt["schema_version"] == forgetting_gate.RECEIPT_SCHEMA_VERSION
    assert receipt["survival_semantics"] == forgetting_gate.SURVIVAL_SEMANTICS
    assert receipt["metrics"] == {
        "stale_uncited_byte_reduction": 0.5,
        "protected_cited_false_archive_count": 0,
        "strict_recall_at_5_drop": 0.0,
        "write_integrity_error_count": 0,
        "cited_survival_delta": 1,
    }
    observations = receipt["observations"]
    by_memory_id = {observation["memory_id"]: observation for observation in observations}
    assert by_memory_id["stale-uncited-a"]["archived"] is True
    assert (
        by_memory_id["stale-uncited-a"]["decay_score"]
        < by_memory_id["stale-uncited-a"]["decay_threshold"]
    )
    assert by_memory_id["stale-uncited-b"]["archived"] is False
    assert (
        by_memory_id["stale-uncited-b"]["decay_score"]
        > by_memory_id["stale-uncited-b"]["decay_threshold"]
    )
    assert by_memory_id["stale-uncited-b"]["survival_signal"] == "exposure"
    assert by_memory_id["protected-cited"]["survival_signal"] == "citation"
    assert by_memory_id["legacy-access-only"]["survival_signal"] == "legacy_access"
    assert by_memory_id["legacy-access-capped"]["survival_signal"] == (
        "citation_with_legacy_access_cap"
    )
    assert by_memory_id["legacy-access-only"]["archived"] is False
    assert by_memory_id["legacy-access-capped"]["archived"] is False
    assert (
        by_memory_id["legacy-access-only"]["decay_score"]
        > by_memory_id["legacy-access-capped"]["decay_score"]
    )
    assert forgetting_gate.validate_forgetting_receipt(receipt) == []


def test_receipt_validation_rejects_budget_failures() -> None:
    receipt = forgetting_gate.build_forgetting_receipt(
        (
            forgetting_gate.ForgettingFixture(
                memory_id="uncited-large",
                bytes_before=1_000,
                metadata={
                    "last_recalled_at": (
                        forgetting_gate.RECEIPT_NOW - forgetting_gate.timedelta(days=2)
                    ).isoformat()
                },
                strict_recall_before=True,
            ),
            forgetting_gate.ForgettingFixture(
                memory_id="uncited-small",
                bytes_before=100,
                metadata={"importance": 0.1},
            ),
            forgetting_gate.ForgettingFixture(
                memory_id="cited",
                bytes_before=100,
                cited=True,
                metadata={"importance": 0.1},
                strict_recall_before=True,
            ),
        )
    )

    assert forgetting_gate.validate_forgetting_receipt(receipt) == [
        "metric 'stale_uncited_byte_reduction' below budget 0.2: 0.09090909090909091",
        "metric 'protected_cited_false_archive_count' exceeds budget 0: 1",
        "metric 'strict_recall_at_5_drop' exceeds budget 0.005: 0.5",
        "metric 'cited_survival_delta' below budget 0.0: -1",
    ]


def test_gate_checks_cover_required_surfaces() -> None:
    assert forgetting_gate.missing_required_surfaces() == []


def test_gate_checks_use_moon_package_slices() -> None:
    commands = [check.command for check in forgetting_gate.GATE_CHECKS]

    assert commands == [
        (
            "moon",
            "run",
            "core:test",
            "--",
            "tests/test_retrieval_advanced.py",
            "-k",
            "usage_aware_decay or citation_stamp or exposure_below_citation or "
            "last_accessed_compatibility or validity_floor or explicit_temporal_target or "
            "episode_record_candidates",
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


def test_run_gate_prints_release_receipt(capsys, tmp_path: Path) -> None:
    commands: list[tuple[str, ...]] = []
    receipt_path = tmp_path / "forgetting-receipt.json"

    def runner(command: tuple[str, ...]) -> int:
        if command == ("moon", "run", "bench-gate"):
            assert receipt_path.exists()
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            assert [check["name"] for check in receipt["checks"]] == [
                "core-usage-aware-ranking",
                "api-priority-decay",
            ]
        commands.append(command)
        return 0

    exit_code = forgetting_gate.run_gate(runner=runner, receipt_path=receipt_path)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert commands == [check.command for check in forgetting_gate.GATE_CHECKS]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["checks"][2]["command"] == "moon run bench-gate"
    assert "Forgetting Gate Receipt" in captured.out
    assert "status: PASS" in captured.out
    assert "stale_uncited_byte_reduction=0.5" in captured.out
    assert "cited survival" in captured.out


def test_run_gate_rejects_missing_required_surface() -> None:
    check = forgetting_gate.GateCheck(
        name="partial",
        description="partial coverage",
        surfaces=("priority decay",),
        command=("moon", "run", "api:test"),
    )
    messages: list[str] = []

    exit_code = forgetting_gate.run_gate(
        [check],
        runner=lambda _: 0,
        echo=messages.append,
        receipt_path=None,
    )

    assert exit_code == MISSING_SURFACE_EXIT_CODE
    assert "Forgetting gate is missing required surfaces:" in messages
    assert "- cited survival" in messages


def test_main_lists_gate_checks(capsys) -> None:
    exit_code = forgetting_gate.main(["--list"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "core-usage-aware-ranking: moon run core:test" in captured.out
    assert "api-priority-decay: moon run api:test" in captured.out
    assert "ai-memory-contracts: moon run bench-gate" in captured.out


def test_root_moon_tasks_expose_forgetting_gate() -> None:
    tasks = _root_moon_tasks()

    gate = tasks["forgetting-gate"]
    assert gate["target"] == "root:forgetting-gate"
    assert gate["command"] == "uv"
    assert gate["args"] == [
        "run",
        "python",
        "-m",
        "tools.trust.forgetting_gate",
    ]

    test_task = tasks["forgetting-gate-test"]
    assert test_task["target"] == "root:forgetting-gate-test"
    assert test_task["command"] == "uv"
    assert test_task["args"] == [
        "run",
        "pytest",
        "tools/tests/test_forgetting_gate.py",
        "-v",
    ]
