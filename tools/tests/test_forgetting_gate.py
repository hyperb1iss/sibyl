from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from shutil import which
from typing import NotRequired, TypedDict, cast

from tools.trust import forgetting_gate

MISSING_SURFACE_EXIT_CODE = 2
EXPECTED_PROTECTED_DOGFOOD_SAMPLE_COUNT = 2.0
EXPECTED_CITED_SURVIVAL_DOGFOOD_DELTA = 2.0
REPO_ROOT = Path(__file__).resolve().parents[2]
API_DIGEST = f"sha256:{'a' * 64}"
WEB_DIGEST = f"sha256:{'b' * 64}"


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


def _dogfood_evidence() -> dict[str, object]:
    return {
        "deployment": {
            "version": "1.1.0-rc.1",
            "expected_version": "1.1.0-rc.1",
            "image_digests": {"api": API_DIGEST, "web": WEB_DIGEST},
            "expected_image_digests": {"api": API_DIGEST, "web": WEB_DIGEST},
            "source_commits": [
                "36094084",
                "e59e9be1",
                "b9e3ade8",
                "6bf8881f",
                "4bf80afd",
                "2095b616",
            ],
        },
        "forgetting": {
            "dry_run": True,
            "strict_recall_at_5_before": 200,
            "strict_recall_at_5_after": 200,
            "write_integrity_error_count": 0,
            "context_recall_decay_applied": True,
            "observations": [
                {
                    "memory_id": "entity:stale-uncited",
                    "stale": True,
                    "cited": False,
                    "archived": True,
                    "last_recalled_at": "2026-01-01T00:00:00+00:00",
                },
                {
                    "memory_id": "entity:protected-cited",
                    "stale": True,
                    "cited": True,
                    "protected": True,
                    "archived": False,
                    "last_used_at": "2026-07-04T12:00:00+00:00",
                },
                {
                    "memory_id": "entity:protected-cited-2",
                    "stale": True,
                    "cited": True,
                    "protected": True,
                    "archived": False,
                    "last_used_at": "2026-07-04T12:05:00+00:00",
                },
            ],
        },
        "checks": [
            {
                "name": "live-forgetting-observation",
                "status": "PASS",
                "surfaces": list(forgetting_gate.DOGFOOD_REQUIRED_SURFACES),
            }
        ],
    }


def _deployment_evidence() -> dict[str, object]:
    return cast(dict[str, object], _dogfood_evidence()["deployment"])


def _write_integrity_receipt(path: Path, *, passed: bool = True) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "sibyl-write-path-integrity-receipt-v1",
                "metrics": {
                    "hallucinated_fact_count": 0 if passed else 1,
                    "self_referential_write_count": 0,
                    "low_signal_write_count": 0,
                },
                "checks": [{"name": "write-path", "status": "PASS" if passed else "FAIL"}],
            }
        ),
        encoding="utf-8",
    )


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


def test_dogfood_receipt_meets_live_forgetting_contract() -> None:
    receipt = forgetting_gate.build_forgetting_dogfood_receipt(_dogfood_evidence())

    assert receipt["schema_version"] == forgetting_gate.DOGFOOD_RECEIPT_SCHEMA_VERSION
    assert receipt["metrics"]["deployed_version_match"] == 1.0
    assert receipt["metrics"]["image_digest_match"] == 1.0
    assert receipt["metrics"]["required_source_commit_coverage"] == 1.0
    assert receipt["metrics"]["stale_uncited_sample_count"] == 1.0
    assert receipt["metrics"]["stale_uncited_reduction_count"] == 1.0
    assert (
        receipt["metrics"]["cited_protected_sample_count"]
        == EXPECTED_PROTECTED_DOGFOOD_SAMPLE_COUNT
    )
    assert receipt["metrics"]["cited_survival_delta"] == EXPECTED_CITED_SURVIVAL_DOGFOOD_DELTA
    assert receipt["metrics"]["protected_cited_false_archive_count"] == 0
    assert receipt["metrics"]["strict_recall_at_5_drop"] == 0.0
    assert receipt["metrics"]["dry_run_mode"] == 1.0
    assert receipt["metrics"]["write_integrity_error_count"] == 0.0
    assert receipt["metrics"]["context_recall_decay_applied"] == 1.0
    assert forgetting_gate.validate_forgetting_dogfood_receipt(receipt) == []


def test_collect_forgetting_dogfood_evidence_from_debug_query(tmp_path: Path) -> None:
    queries: list[str] = []
    write_integrity_path = tmp_path / "write-path-integrity-receipt.json"
    _write_integrity_receipt(write_integrity_path)

    def query_runner(query: str) -> list[dict[str, object]]:
        queries.append(query)
        assert "FROM entity" in query
        return [
            {
                "uuid": "entity-stale-uncited",
                "created_at": "2025-01-01T00:00:00+00:00",
                "metadata": {"importance": 0.1},
            },
            {
                "uuid": "entity-protected-cited",
                "created_at": "2025-01-01T00:00:00+00:00",
                "last_used_at": "2026-07-04T12:00:00+00:00",
                "citation_count": 1,
            },
            {
                "uuid": "entity-exposed",
                "created_at": "2025-01-01T00:00:00+00:00",
                "last_recalled_at": "2026-07-04T12:05:00+00:00",
                "retrieval_count": 1,
            },
        ]

    evidence = forgetting_gate.collect_forgetting_dogfood_evidence(
        _deployment_evidence(),
        query_runner=query_runner,
        write_integrity_receipt_path=write_integrity_path,
    )
    receipt = forgetting_gate.build_forgetting_dogfood_receipt(evidence)

    assert len(queries) == 1
    assert evidence["forgetting"]["dry_run"] is True
    assert receipt["metrics"]["stale_uncited_reduction_count"] == 1.0
    assert receipt["metrics"]["protected_cited_false_archive_count"] == 0.0
    assert receipt["metrics"]["context_recall_decay_applied"] == 1.0
    assert receipt["metrics"]["write_integrity_error_count"] == 0.0
    assert forgetting_gate.validate_forgetting_dogfood_receipt(receipt) == []


def test_collect_forgetting_dogfood_evidence_requires_write_integrity(
    tmp_path: Path,
) -> None:
    write_integrity_path = tmp_path / "write-path-integrity-receipt.json"
    _write_integrity_receipt(write_integrity_path, passed=False)

    evidence = forgetting_gate.collect_forgetting_dogfood_evidence(
        _deployment_evidence(),
        query_runner=lambda _: [
            {
                "uuid": "entity-stale-uncited",
                "created_at": "2025-01-01T00:00:00+00:00",
                "metadata": {"importance": 0.1},
            },
            {
                "uuid": "entity-protected-cited",
                "created_at": "2025-01-01T00:00:00+00:00",
                "last_used_at": "2026-07-04T12:00:00+00:00",
                "citation_count": 1,
                "last_recalled_at": "2026-07-04T12:00:00+00:00",
            },
        ],
        write_integrity_receipt_path=write_integrity_path,
    )
    receipt = forgetting_gate.build_forgetting_dogfood_receipt(evidence)
    failures = forgetting_gate.validate_forgetting_dogfood_receipt(receipt)

    assert "metric 'write_integrity_error_count' exceeds budget 0: 1.0" in failures
    assert "dogfood receipt checks[1] did not pass" in failures


def test_dogfood_receipt_rejects_apply_or_protected_archive_evidence() -> None:
    evidence = _dogfood_evidence()
    deployment = cast(dict[str, object], evidence["deployment"])
    forgetting = cast(dict[str, object], evidence["forgetting"])
    checks = cast(list[dict[str, object]], evidence["checks"])
    deployment["version"] = "1.0.2"
    deployment["source_commits"] = ["5150d2de"]
    forgetting["dry_run"] = False
    forgetting["strict_recall_at_5_after"] = 198
    forgetting["write_integrity_error_count"] = 1
    observations = cast(list[dict[str, object]], forgetting["observations"])
    observations[0]["archived"] = False
    observations[1]["archived"] = True
    checks[0]["status"] = "FAIL"

    receipt = forgetting_gate.build_forgetting_dogfood_receipt(evidence)
    failures = forgetting_gate.validate_forgetting_dogfood_receipt(receipt)

    assert "metric 'deployed_version_match' below budget 1: 0.0" in failures
    assert "metric 'required_source_commit_coverage' below budget 1: 0.0" in failures
    assert "metric 'stale_uncited_reduction_count' below budget 1: 0.0" in failures
    assert "metric 'cited_survival_delta' below budget 1: 0.0" in failures
    assert "metric 'protected_cited_false_archive_count' exceeds budget 0: 1" in failures
    assert "metric 'strict_recall_at_5_drop' exceeds budget 0.005: 0.01" in failures
    assert "metric 'dry_run_mode' below budget 1: 0.0" in failures
    assert "metric 'write_integrity_error_count' exceeds budget 0: 1.0" in failures
    assert "dogfood receipt checks[0] did not pass" in failures


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


def test_main_writes_dogfood_receipt_from_evidence(capsys, tmp_path: Path) -> None:
    evidence_path = tmp_path / "forgetting-evidence.json"
    receipt_path = tmp_path / "forgetting-dogfood-receipt.json"
    evidence_path.write_text(json.dumps(_dogfood_evidence()), encoding="utf-8")

    exit_code = forgetting_gate.main(
        [
            "--dogfood-evidence",
            str(evidence_path),
            "--dogfood-receipt",
            str(receipt_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Forgetting Dogfood Receipt" in captured.out
    assert "status: PASS" in captured.out
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["schema_version"] == forgetting_gate.DOGFOOD_RECEIPT_SCHEMA_VERSION


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
