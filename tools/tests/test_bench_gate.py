from __future__ import annotations

import json
from pathlib import Path

import pytest
from tools.bench import eval_gate

EXPECTED_SUCCESS_AT_5 = 0.5
EXPECTED_LATENCY_MS = 1200.0
EXPECTED_MRR = 0.4


def _write_report(
    path: Path, *, metrics: dict[str, float], metadata: dict[str, str] | None = None
) -> None:
    path.write_text(
        json.dumps(
            {
                "label": "surreal acceptance",
                "search_type": "unified",
                "metrics": metrics,
                "metadata": metadata or {},
            }
        ),
        encoding="utf-8",
    )


def test_extract_metrics_supports_eval_report_payload(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    _write_report(
        path,
        metrics={
            "success@5": EXPECTED_SUCCESS_AT_5,
            "latency_ms": EXPECTED_LATENCY_MS,
            "mrr": EXPECTED_MRR,
        },
    )

    metrics = eval_gate.extract_metrics(eval_gate.load_report(path))

    assert metrics["success@5"] == EXPECTED_SUCCESS_AT_5
    assert metrics["latency_ms"] == EXPECTED_LATENCY_MS
    assert metrics["mrr"] == EXPECTED_MRR


def test_evaluate_report_acceptance_profile_passes() -> None:
    report = {
        "metrics": {
            "success@5": 0.55,
            "ndcg@10": 0.44,
            "mrr": 0.31,
            "latency_ms": 1450.0,
        },
        "metadata": {"store": "surreal"},
    }

    failures = eval_gate.evaluate_report(
        report,
        profile="acceptance",
        required_metadata={"store": "surreal"},
    )

    assert failures == []


def test_evaluate_report_context_pack_profile_passes() -> None:
    report = {
        "metrics": {
            "pass_rate": 1.0,
            "source_metadata_coverage": 1.0,
            "facet_order_match_rate": 1.0,
            "forbidden_term_matches": 0.0,
        },
        "metadata": {"store": "surreal"},
    }

    failures = eval_gate.evaluate_report(report, profile="context-pack")

    assert failures == []


def test_evaluate_report_context_pack_profile_blocks_leaks() -> None:
    report = {
        "metrics": {
            "pass_rate": 0.95,
            "source_metadata_coverage": 0.75,
            "facet_order_match_rate": 0.50,
            "forbidden_term_matches": 1.0,
        },
    }

    failures = eval_gate.evaluate_report(report, profile="context-pack")

    assert "metric 'pass_rate' below minimum 1.0000: 0.9500" in failures
    assert "metric 'source_metadata_coverage' below minimum 1.0000: 0.7500" in failures
    assert "metric 'facet_order_match_rate' below minimum 1.0000: 0.5000" in failures
    assert "metric 'forbidden_term_matches' above maximum 0.0000: 1.0000" in failures


def test_evaluate_report_reports_threshold_and_metadata_failures() -> None:
    report = {
        "metrics": {
            "success@5": 0.10,
            "ndcg@10": 0.20,
            "mrr": 0.10,
            "latency_ms": 4200.0,
        },
        "metadata": {"store": "legacy"},
    }

    failures = eval_gate.evaluate_report(
        report,
        profile="acceptance",
        required_metadata={"store": "surreal"},
    )

    assert "metadata['store'] expected 'surreal', got 'legacy'" in failures
    assert "metric 'latency_ms' above maximum 3000.0000: 4200.0000" in failures
    assert "metric 'mrr' below minimum 0.2500: 0.1000" in failures


def test_main_returns_nonzero_when_gate_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "report.json"
    _write_report(
        path,
        metrics={"success@5": 0.15, "ndcg@10": 0.20, "mrr": 0.10, "latency_ms": 3500.0},
        metadata={"store": "legacy"},
    )

    exit_code = eval_gate.main(
        [
            str(path),
            "--require-metadata",
            "store=surreal",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Gate failed" in captured.out
