from __future__ import annotations

import json
from pathlib import Path

from tools.inventory import retrieval_mode_history

EXPECTED_CONSECUTIVE_RUNS = 2
EXPECTED_LATENCY_P95_MS = 500.0
SLOW_LATENCY_P95_MS = 1250.0


def _report(
    *,
    retrieval_mode: str = "compare",
    pass_rate: float = 1.0,
    latency_p95_ms: float = EXPECTED_LATENCY_P95_MS,
    leak_count: float = 0.0,
) -> dict[str, object]:
    return {
        "timestamp": "2026-05-12 09:00:00",
        "label": "retrieval-compare",
        "metadata": {"retrieval_mode": retrieval_mode, "repeat_count": "20"},
        "metrics": {
            "pass_rate": pass_rate,
            "source_metadata_coverage": 1.0,
            "facet_order_match_rate": 1.0,
            "leak_count": leak_count,
            "forbidden_term_matches": 0.0,
            "latency_ms": 250.0,
            "latency_p95_ms": latency_p95_ms,
        },
        "per_case": [{"name": "context-pack-smoke", "passed": True, "error": None}],
    }


def test_current_run_blockers_require_compare_mode_and_clean_policy() -> None:
    blockers = retrieval_mode_history.current_run_blockers(
        _report(retrieval_mode="native", leak_count=1.0),
        branch="feature",
        policy_affecting_diffs=2,
    )

    assert "branch 'feature' is not main" in blockers
    assert "metadata['retrieval_mode'] is not 'compare'" in blockers
    assert "policy_affecting_diffs is 2" in blockers
    assert "metric 'leak_count' above 0.0000: 1.0000" in blockers


def test_current_run_blockers_require_20_run_repeat_metadata() -> None:
    report = _report()
    report["metadata"] = {"retrieval_mode": "compare", "repeat_count": "1"}

    blockers = retrieval_mode_history.current_run_blockers(
        report,
        branch="main",
        policy_affecting_diffs=0,
    )

    assert "metadata['repeat_count'] is not '20'" in blockers


def test_consecutive_count_stops_at_last_nonqualifying_main_run() -> None:
    records = [
        {"branch": "main", "qualifies": True},
        {"branch": "main", "qualifies": False},
        {"branch": "feature", "qualifies": True},
        {"branch": "main", "qualifies": True},
        {"branch": "main", "qualifies": True},
    ]

    consecutive = retrieval_mode_history.consecutive_qualifying_count(records)

    assert consecutive == EXPECTED_CONSECUTIVE_RUNS


def test_main_records_history_and_reports_not_ready(
    tmp_path: Path,
    capsys,
) -> None:
    report_path = tmp_path / "report.json"
    history_path = tmp_path / "history.json"
    report_path.write_text(json.dumps(_report()), encoding="utf-8")

    exit_code = retrieval_mode_history.main(
        [
            str(report_path),
            "--history",
            str(history_path),
            "--branch",
            "main",
            "--sha",
            "abc123",
            "--run-id",
            "run-1",
            "--run-attempt",
            "1",
        ]
    )

    captured = capsys.readouterr()
    history = json.loads(history_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert "consecutive_main_qualifying: 1/3" in captured.out
    assert history["records"][0]["qualifies"] is True
    assert history["records"][0]["retrieval_mode"] == "compare"
    assert history["records"][0]["metrics"]["latency_p95_ms"] == EXPECTED_LATENCY_P95_MS


def test_main_returns_failure_for_current_nonqualifying_run(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    history_path = tmp_path / "history.json"
    report_path.write_text(
        json.dumps(_report(pass_rate=0.5, latency_p95_ms=SLOW_LATENCY_P95_MS)),
        encoding="utf-8",
    )

    exit_code = retrieval_mode_history.main(
        [
            str(report_path),
            "--history",
            str(history_path),
            "--branch",
            "main",
        ]
    )

    history = json.loads(history_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert history["records"][0]["qualifies"] is False
    assert "metric 'pass_rate' below 1.0000: 0.5000" in history["records"][0]["blockers"]
    assert "metric 'latency_p95_ms' above 1000.0000: 1250.0000" in history["records"][0]["blockers"]
