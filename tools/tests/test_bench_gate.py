from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from tools.bench import eval_gate

EXPECTED_SUCCESS_AT_5 = 0.5
EXPECTED_LATENCY_MS = 1200.0
EXPECTED_MRR = 0.4
RELEASE_METADATA = {
    "retrieval_mode": "native",
    "embedding_provider": "gemini",
    "embedding_model": "gemini-embedding-2",
    "embedding_dimensions": "768",
    "tokenizer_estimate_method": "provider-default",
    "dataset_name": "context_pack_cases",
    "corpus_hash": "sha256:abc123",
    "repeat_count": "20",
    "auth_manifest_id": "sha256:def456",
    "sibyl_commit": "abc123",
    "runtime_mode": "live-api",
}


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


def _ai_memory_report(mode: str = "raw") -> dict[str, Any]:
    return {
        "schema_version": "longmemeval-offline-v2",
        "suite": "LongMemEval-style offline",
        "suite_version": "offline-runner-v2",
        "generated_at": "2026-05-13T12:00:00+00:00",
        "sibyl_commit": "abc123",
        "command": ["benchmarks/longmemeval_bench.py", "fixture.json"],
        "runtime": {
            "runtime_mode": "offline",
            "graph_engine": "none",
            "store": "chromadb_ephemeral",
            "retrieval_mode": mode,
            "embedding_provider": "chromadb",
            "embedding_model": "chromadb_default",
            "embedding_dimensions": 384,
            "tokenizer_estimate_method": "chromadb_default",
        },
        "dataset": {
            "name": "fixture",
            "corpus_hash": "sha256:abc123",
            "evaluated_entries": 1,
        },
        "mode": mode,
        "repeat_count": 1,
        "auth_manifest_id": "not-applicable:offline",
        "overall": {
            "recall@5": 1.0,
            "ndcg@5": 1.0,
            "recall@10": 1.0,
            "ndcg@10": 1.0,
        },
        "per_type": {
            "temporal-reasoning": {
                "recall@5": 1.0,
                "ndcg@5": 1.0,
                "recall@10": 1.0,
                "ndcg@10": 1.0,
            }
        },
        "case_results": [
            {
                "question_id": "q1",
                "answer_session_ids": ["s1"],
                "ranked_session_ids": ["s1", "s2"],
                "recall@5": 1.0,
                "ndcg@5": 1.0,
            }
        ],
        "total_questions": 1,
        "elapsed_seconds": 1.25,
        "claim_boundary": "Offline component retrieval baseline only.",
    }


def _manifest_entry(report: dict[str, Any], artifact: str = "artifact.json") -> dict[str, Any]:
    case_results = report["case_results"]
    assert isinstance(case_results, list)
    return {
        "suite": report["suite"],
        "suite_version": report["suite_version"],
        "mode": report["mode"],
        "artifact": artifact,
        "status": "citable",
        "gate_profile": "ai-memory",
        "sibyl_commit": report["sibyl_commit"],
        "questions": report["total_questions"],
        "case_results": len(case_results),
        "elapsed_seconds": report["elapsed_seconds"],
        "dataset": report["dataset"],
        "runtime": report["runtime"],
        "overall": report["overall"],
        "claim_boundary": report["claim_boundary"],
        "repeat_count": report["repeat_count"],
        "auth_manifest_id": report["auth_manifest_id"],
    }


def _write_ai_memory_manifest(
    tmp_path: Path,
    *,
    report: dict[str, Any] | None = None,
    entry: dict[str, Any] | None = None,
    planned: list[dict[str, Any]] | None = None,
    write_artifact: bool = True,
) -> Path:
    report = report or _ai_memory_report()
    entry = entry or _manifest_entry(report)
    if write_artifact:
        artifact = tmp_path / str(entry["artifact"])
        artifact.write_text(json.dumps(report), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"citable": [entry], "planned": planned or []}),
        encoding="utf-8",
    )
    return manifest_path


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
        "label": "retrieval-native",
        "metrics": {
            "pass_rate": 1.0,
            "latency_p95_ms": 500.0,
            "source_metadata_coverage": 1.0,
            "facet_order_match_rate": 1.0,
            "leak_count": 0.0,
            "forbidden_term_matches": 0.0,
        },
        "metadata": RELEASE_METADATA,
    }

    failures = eval_gate.evaluate_report(report, profile="context-pack")

    assert failures == []


def test_evaluate_report_context_pack_profile_blocks_leaks() -> None:
    report = {
        "metrics": {
            "pass_rate": 0.95,
            "latency_p95_ms": 1250.0,
            "source_metadata_coverage": 0.75,
            "facet_order_match_rate": 0.50,
            "leak_count": 1.0,
            "forbidden_term_matches": 1.0,
        },
    }

    failures = eval_gate.evaluate_report(report, profile="context-pack")

    assert "metric 'pass_rate' below minimum 1.0000: 0.9500" in failures
    assert "metric 'latency_p95_ms' above maximum 1000.0000: 1250.0000" in failures
    assert "metric 'source_metadata_coverage' below minimum 1.0000: 0.7500" in failures
    assert "metric 'facet_order_match_rate' below minimum 1.0000: 0.5000" in failures
    assert "metric 'leak_count' above maximum 0.0000: 1.0000" in failures
    assert "metric 'forbidden_term_matches' above maximum 0.0000: 1.0000" in failures


def test_evaluate_report_context_pack_profile_rejects_missing_release_metadata() -> None:
    report = {
        "label": "context-pack",
        "metrics": {
            "pass_rate": 1.0,
            "latency_p95_ms": 500.0,
            "source_metadata_coverage": 1.0,
            "facet_order_match_rate": 1.0,
            "leak_count": 0.0,
            "forbidden_term_matches": 0.0,
        },
        "metadata": {"retrieval_mode": "native"},
    }

    failures = eval_gate.evaluate_report(report, profile="context-pack")

    assert "metadata missing non-empty field 'embedding_provider'" in failures
    assert "metadata['repeat_count'] must be a positive integer" in failures
    assert "label 'context-pack' must include retrieval mode 'native'" in failures


def test_evaluate_report_ai_memory_profile_accepts_full_records() -> None:
    report = {
        "schema_version": "longmemeval-offline-v2",
        "suite": "LongMemEval-style offline",
        "generated_at": "2026-05-13T12:00:00+00:00",
        "sibyl_commit": "abc123",
        "command": ["benchmarks/longmemeval_bench.py", "fixture.json"],
        "runtime": {
            "runtime_mode": "offline",
            "graph_engine": "none",
            "store": "chromadb_ephemeral",
            "retrieval_mode": "raw",
            "embedding_provider": "chromadb",
            "embedding_model": "chromadb_default",
            "embedding_dimensions": 384,
            "tokenizer_estimate_method": "chromadb_default",
        },
        "dataset": {
            "name": "fixture",
            "corpus_hash": "sha256:abc123",
            "evaluated_entries": 1,
        },
        "mode": "raw",
        "repeat_count": 1,
        "auth_manifest_id": "not-applicable:offline",
        "overall": {
            "recall@5": 1.0,
            "ndcg@5": 1.0,
            "recall@10": 1.0,
            "ndcg@10": 1.0,
        },
        "per_type": {
            "temporal-reasoning": {
                "recall@5": 1.0,
                "ndcg@5": 1.0,
                "recall@10": 1.0,
                "ndcg@10": 1.0,
            }
        },
        "case_results": [
            {
                "question_id": "q1",
                "answer_session_ids": ["s1"],
                "ranked_session_ids": ["s1", "s2"],
                "recall@5": 1.0,
                "ndcg@5": 1.0,
            }
        ],
    }

    failures = eval_gate.evaluate_report(report, profile="ai-memory")

    assert failures == []


def test_evaluate_report_ai_memory_profile_accepts_non_embedding_live_path() -> None:
    report = _ai_memory_report(mode="hybrid")
    report["schema_version"] = "longmemeval-live-v1"
    report["suite"] = "LongMemEval-S live API"
    report["runtime"] = {
        "runtime_mode": "live-api-ephemeral",
        "graph_engine": "surreal",
        "store": "surreal",
        "retrieval_mode": "hybrid",
        "embedding_provider": "none",
        "embedding_model": "not-applicable",
        "embedding_dimensions": 0,
        "tokenizer_estimate_method": "not-applicable",
    }
    report["auth_manifest_id"] = "ephemeral-local-signup-v1"

    failures = eval_gate.evaluate_report(report, profile="ai-memory")

    assert failures == []


def test_evaluate_report_ai_memory_profile_rejects_headline_only_records() -> None:
    report = {
        "suite": "LOCOMO-style long-memory suite",
        "overall": {"score": 0.7},
    }

    failures = eval_gate.evaluate_report(report, profile="ai-memory")

    assert "missing non-empty field 'schema_version'" in failures
    assert "missing non-empty field 'sibyl_commit'" in failures
    assert "missing non-empty field 'command'" in failures
    assert "missing non-empty field 'dataset' or 'corpus'" in failures
    assert "missing non-empty field 'runtime'" in failures
    assert "missing non-empty field 'auth_manifest_id'" in failures
    assert "repeat_count must be a positive integer" in failures
    assert "missing non-empty field 'case_results'" in failures


def test_evaluate_report_ai_memory_profile_enforces_quality_thresholds() -> None:
    report = _ai_memory_report(mode="hybrid")
    report["overall"]["recall@5"] = 0.70
    report["overall"]["cross_question_result_count"] = 1.0
    report["per_type"]["temporal-reasoning"]["ndcg@10"] = 0.50

    failures = eval_gate.evaluate_report(report, profile="ai-memory")

    assert "metric 'recall@5' below minimum 0.7500: 0.7000" in failures
    assert "overall cross_question_result_count must be 0.0000: 1.0000" in failures
    assert (
        "per_type['temporal-reasoning']: metric 'ndcg@10' below minimum 0.6000: 0.5000" in failures
    )


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


def test_main_can_gate_ai_memory_record(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "ai-memory.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "longmemeval-offline-v2",
                "suite": "LongMemEval-style offline",
                "generated_at": "2026-05-13T12:00:00+00:00",
                "sibyl_commit": "abc123",
                "command": ["benchmarks/longmemeval_bench.py", "fixture.json"],
                "runtime": {
                    "runtime_mode": "offline",
                    "graph_engine": "none",
                    "store": "chromadb_ephemeral",
                    "retrieval_mode": "raw",
                    "embedding_provider": "chromadb",
                    "embedding_model": "chromadb_default",
                    "embedding_dimensions": 384,
                    "tokenizer_estimate_method": "chromadb_default",
                },
                "dataset": {
                    "name": "fixture",
                    "corpus_hash": "sha256:abc123",
                    "evaluated_entries": 1,
                },
                "mode": "raw",
                "repeat_count": 1,
                "auth_manifest_id": "not-applicable:offline",
                "overall": {
                    "recall@5": 1.0,
                    "ndcg@5": 1.0,
                    "recall@10": 1.0,
                    "ndcg@10": 1.0,
                },
                "per_type": {
                    "single-session-user": {
                        "recall@5": 1.0,
                        "ndcg@5": 1.0,
                        "recall@10": 1.0,
                        "ndcg@10": 1.0,
                    }
                },
                "case_results": [
                    {
                        "question_id": "q1",
                        "answer_session_ids": ["s1"],
                        "ranked_session_ids": ["s1"],
                        "recall@5": 1.0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    exit_code = eval_gate.main([str(path), "--profile", "ai-memory"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Checking LongMemEval-style offline with the ai-memory profile" in captured.out
    assert "Gate passed" in captured.out


def test_main_without_report_gates_ai_memory_manifest(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = eval_gate.main([])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "ai-memory manifest profile" in captured.out
    assert "Gate passed" in captured.out


def test_main_without_report_gates_manifest_from_other_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    exit_code = eval_gate.main([])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "benchmarks/results/ai-memory/manifest.json" in captured.out
    assert "Gate passed" in captured.out


def test_validate_ai_memory_manifest_rejects_missing_artifact(tmp_path: Path) -> None:
    manifest_path = _write_ai_memory_manifest(tmp_path, write_artifact=False)

    failures = eval_gate.validate_ai_memory_manifest(manifest_path)

    assert "citable[0] artifact does not exist: artifact.json" in failures


def test_validate_ai_memory_manifest_rejects_citable_artifact_drift(tmp_path: Path) -> None:
    report = _ai_memory_report()
    entry = _manifest_entry(report)
    entry["overall"] = {"recall@5": 0.25}
    entry["repeat_count"] = 2
    manifest_path = _write_ai_memory_manifest(tmp_path, report=report, entry=entry)

    failures = eval_gate.validate_ai_memory_manifest(manifest_path)

    assert "artifact.json: manifest overall does not match artifact" in failures
    assert "artifact.json: manifest repeat_count does not match artifact" in failures


def test_validate_ai_memory_manifest_rejects_planned_artifact_fields(tmp_path: Path) -> None:
    planned = [
        {"suite": "Future suite", "status": "planned", "artifact": "future.json"},
        {"suite": "Mistagged suite", "status": "citable"},
    ]
    manifest_path = _write_ai_memory_manifest(tmp_path, planned=planned)

    failures = eval_gate.validate_ai_memory_manifest(manifest_path)

    assert "planned[0] must not include artifact" in failures
    assert "planned[1] status is not 'planned'" in failures


def test_validate_ai_memory_manifest_rejects_empty_citable_list(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"citable": []}), encoding="utf-8")

    failures = eval_gate.validate_ai_memory_manifest(manifest_path)

    assert failures == ["manifest missing non-empty citable list"]


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


def test_ai_memory_manifest_tracks_full_citable_artifacts() -> None:
    repo_root = Path(__file__).parents[2]
    manifest_path = repo_root / "benchmarks" / "results" / "ai-memory" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    citable = manifest["citable"]
    assert citable

    for entry in citable:
        artifact = manifest_path.parent / entry["artifact"]
        assert artifact.exists()
        report = eval_gate.load_report(artifact)
        assert eval_gate.evaluate_report(report, profile="ai-memory") == []
        assert entry["status"] == "citable"
        assert entry["suite"] == report["suite"]
        assert entry["suite_version"] == report["suite_version"]
        assert entry["sibyl_commit"] == report["sibyl_commit"]
        assert entry["mode"] == report["mode"]
        assert entry["questions"] == report["total_questions"]
        assert entry["case_results"] == len(report["case_results"])
        assert entry["runtime"] == report["runtime"]
        assert entry["dataset"]["evaluated_entries"] == report["dataset"]["evaluated_entries"]
        for metric, expected in entry["overall"].items():
            assert report["overall"][metric] == pytest.approx(expected)

    planned = manifest["planned"]
    assert planned
    for entry in planned:
        assert entry["status"] == "planned"
        assert "artifact" not in entry
