from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from tools.bench import eval_gate

EXPECTED_SUCCESS_AT_5 = 0.5
EXPECTED_LATENCY_MS = 1200.0
EXPECTED_MRR = 0.4
ARGPARSE_USAGE_ERROR = 2
RELEASE_METADATA = {
    "retrieval_mode": "native",
    "embedding_provider": "gemini",
    "embedding_model": "gemini-embedding-2",
    "embedding_dimensions": "768",
    "embedding_cache_namespace": "graph",
    "tokenizer_estimate_method": "provider-default",
    "dataset_name": "context_pack_cases",
    "corpus_hash": "sha256:abc123",
    "repeat_count": "20",
    "auth_manifest_id": "sha256:def456",
    "sibyl_commit": "abc123",
    "runtime_mode": "live-api",
}
ACCOUNTING = {
    "schema_version": "sibyl-eval-accounting-v1",
    "gate_status": "warning-only-until-two-citable-baselines",
    "latency": {"p50_ms": 100.0, "p95_ms": 120.0, "max_ms": 140.0},
    "tokens": {
        "estimated_input_tokens": 500.0,
        "estimated_output_tokens": 0.0,
        "full_context_baseline_estimated_tokens": 500.0,
        "estimator": "approximate_character_count",
    },
    "embedding": {
        "calls": 2,
        "provider": "openai",
        "model": "text-embedding-3-small",
        "estimated_input_tokens": 500.0,
        "estimated_cost_usd": 0.00001,
        "cost_basis": "openai:text-embedding-3-small",
    },
    "reader": {
        "estimated_input_tokens": 0.0,
        "estimated_output_tokens": 0.0,
        "estimated_cost_usd": 0.0,
        "cost_basis": "not-metered-by-runner",
    },
    "judge": {
        "estimated_input_tokens": 0.0,
        "estimated_output_tokens": 0.0,
        "estimated_cost_usd": 0.0,
        "cost_basis": "not-metered-by-runner",
    },
    "cost": {
        "estimated_total_usd": 0.00001,
        "currency": "USD",
        "enforcement": "warning-only-until-two-citable-baselines",
    },
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
    accounting = _clone_report(ACCOUNTING)
    accounting["embedding"]["provider"] = "chromadb"
    accounting["embedding"]["model"] = "chromadb_default"
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
            "embedding_cache_namespace": "chromadb",
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
        "accounting": accounting,
    }


def _clone_report(report: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(report))


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


def _external_ai_memory_report(report: dict[str, Any] | None = None) -> dict[str, Any]:
    external_report = _clone_report(report or _ai_memory_report(mode="hybrid"))
    case_results = external_report["case_results"]
    assert isinstance(case_results, list)
    external_report["case_results"] = len(case_results)
    external_report["external_artifact"] = {
        "provider": "github-actions",
        "repo": "hyperb1iss/sibyl",
        "run_id": "123456789",
        "run_url": "https://github.com/hyperb1iss/sibyl/actions/runs/123456789",
        "job_name": "LongMemEval Live Full",
        "artifact_name": "longmemeval-live-full-abc123",
        "artifact_path": "longmemeval_live_full.json",
        "sha256": "a" * 64,
        "size_bytes": 7073488,
        "archive_size_bytes": 933836,
        "expires_at": "2026-08-20T18:21:29Z",
        "verified_at": "2026-06-10T14:11:37Z",
        "verification_command": "sha256sum artifact.json && wc -c artifact.json",
        "verification_receipt": "sha256 aaaa; size 7073488",
        "gate_profile": "ai-memory",
        "gate_command": "moon run bench-gate -- artifact.json --profile ai-memory",
        "gate_passed": True,
        "gate_receipt": "Gate passed",
    }
    return external_report


def _external_manifest_entry(
    report: dict[str, Any],
    external_artifact_manifest: str = "external/artifact-manifest.json",
) -> dict[str, Any]:
    return {
        "suite": report["suite"],
        "suite_version": report["suite_version"],
        "mode": report["mode"],
        "external_artifact_manifest": external_artifact_manifest,
        "status": "citable",
        "gate_profile": "ai-memory",
        "sibyl_commit": report["sibyl_commit"],
        "questions": report["total_questions"],
        "case_results": report["case_results"],
        "elapsed_seconds": report["elapsed_seconds"],
        "dataset": report["dataset"],
        "runtime": report["runtime"],
        "overall": report["overall"],
        "claim_boundary": report["claim_boundary"],
        "repeat_count": report["repeat_count"],
        "auth_manifest_id": report["auth_manifest_id"],
    }


def _manifest_payload(
    *,
    citable: list[dict[str, Any]],
    planned: list[dict[str, Any]] | None = None,
    no_regression: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "sibyl-ai-memory-benchmark-ledger-v2",
        "updated_at": "2026-07-03",
        "release_scope": "test",
        "artifact_policy": "Only citable entries may appear in release notes.",
        "history": {
            "directory": "history",
            "summary_schema": "sibyl-ai-memory-history-summary-v1",
            "append_policy": "immutable-json",
        },
        "gate_contracts": [
            {
                "name": "eval-regression-gate",
                "owner_wave": "W2A",
                "status": "blocking",
                "profile": "ai-memory",
                "blocking": True,
                "metric_contracts": [
                    {
                        "metric": "recall@5",
                        "mode": "no-regression",
                        "direction": "higher",
                        "baseline": "latest-citable-hybrid",
                        "max_regression": 0.005,
                    }
                ],
            }
        ],
        "citable": citable,
        "planned": planned or [],
    }
    if no_regression is not None:
        payload["no_regression"] = no_regression
    return payload


def _write_history_summary(
    tmp_path: Path,
    *,
    baseline_key: str = "previous-run",
    metrics: dict[str, float] | None = None,
    gate_passed: bool = True,
) -> Path:
    history_dir = tmp_path / "history"
    history_dir.mkdir(exist_ok=True)
    history_path = history_dir / f"{baseline_key}.json"
    history_path.write_text(
        json.dumps(
            {
                "schema_version": "sibyl-ai-memory-history-summary-v1",
                "baseline_key": baseline_key,
                "generated_at": "2026-07-03T00:00:00Z",
                "source": {"artifact": "baseline.json"},
                "profile": "ai-memory",
                "metrics": metrics or {"recall@5": 1.0, "ndcg@5": 1.0},
                "gate_command": "moon run bench-gate",
                "gate_passed": gate_passed,
            }
        ),
        encoding="utf-8",
    )
    return history_path


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
    _write_history_summary(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(_manifest_payload(citable=[entry], planned=planned)),
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
    accounting = _clone_report(ACCOUNTING)
    accounting["embedding"]["provider"] = RELEASE_METADATA["embedding_provider"]
    accounting["embedding"]["model"] = RELEASE_METADATA["embedding_model"]
    accounting["latency"]["p95_ms"] = 500.0
    accounting["latency"]["max_ms"] = 500.0
    report = {
        "label": "retrieval-native",
        "metrics": {
            "pass_rate": 1.0,
            "latency_p50_ms": 100.0,
            "latency_p95_ms": 500.0,
            "max_latency_ms": 500.0,
            "estimated_input_tokens": 500.0,
            "estimated_output_tokens": 0.0,
            "full_context_baseline_estimated_tokens": 500.0,
            "embedding_call_count": 2.0,
            "embedding_estimated_input_tokens": 500.0,
            "source_metadata_coverage": 1.0,
            "facet_order_match_rate": 1.0,
            "leak_count": 0.0,
            "forbidden_term_matches": 0.0,
        },
        "metadata": RELEASE_METADATA,
        "accounting": accounting,
    }

    failures = eval_gate.evaluate_report(report, profile="context-pack", require_accounting=True)

    assert failures == []


def test_evaluate_report_context_pack_require_accounting_rejects_token_mismatch() -> None:
    accounting = _clone_report(ACCOUNTING)
    accounting["embedding"]["provider"] = RELEASE_METADATA["embedding_provider"]
    accounting["embedding"]["model"] = RELEASE_METADATA["embedding_model"]
    accounting["latency"]["p95_ms"] = 500.0
    accounting["latency"]["max_ms"] = 500.0
    accounting["tokens"]["estimated_input_tokens"] = 0.0
    report = {
        "label": "retrieval-native",
        "metrics": {
            "pass_rate": 1.0,
            "latency_p95_ms": 500.0,
            "estimated_input_tokens": 500.0,
            "full_context_baseline_estimated_tokens": 500.0,
            "embedding_call_count": 2.0,
            "embedding_estimated_input_tokens": 500.0,
            "source_metadata_coverage": 1.0,
            "facet_order_match_rate": 1.0,
            "leak_count": 0.0,
            "forbidden_term_matches": 0.0,
        },
        "metadata": RELEASE_METADATA,
        "accounting": accounting,
    }

    failures = eval_gate.evaluate_report(report, profile="context-pack", require_accounting=True)

    assert any("estimated_input_tokens" in failure for failure in failures)


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
    assert "metadata missing non-empty field 'embedding_cache_namespace'" in failures
    assert "metadata['repeat_count'] must be a positive integer" in failures
    assert "label 'context-pack' must include retrieval mode 'native'" in failures


def test_evaluate_report_context_pack_profile_rejects_embedding_dimension_mismatch() -> None:
    metadata = dict(RELEASE_METADATA)
    metadata.update(
        {
            "embedding_provider": "local",
            "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
            "embedding_dimensions": "1024",
            "embedding_cache_namespace": "graph",
            "tokenizer_estimate_method": "sentence-transformers",
        }
    )
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
        "metadata": metadata,
    }

    failures = eval_gate.evaluate_report(report, profile="context-pack")

    assert (
        "metadata['embedding_dimensions'] must be 384 for local "
        "embedding_model 'sentence-transformers/all-minilm-l6-v2'"
    ) in failures


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
            "embedding_cache_namespace": "chromadb",
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


def test_evaluate_report_ai_memory_profile_accepts_local_embedding_runtime() -> None:
    report = _ai_memory_report(mode="hybrid")
    report["schema_version"] = "longmemeval-live-v1"
    report["suite"] = "LongMemEval-S live API"
    report["runtime"].update(
        {
            "runtime_mode": "live-api-ephemeral",
            "graph_engine": "surreal",
            "store": "surreal",
            "retrieval_mode": "hybrid",
            "embedding_provider": "local",
            "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
            "embedding_dimensions": 384,
            "embedding_cache_namespace": "graph",
            "tokenizer_estimate_method": "sentence-transformers",
        }
    )

    failures = eval_gate.evaluate_report(report, profile="ai-memory")

    assert failures == []


def test_evaluate_report_ai_memory_profile_rejects_local_embedding_dimension_mismatch() -> None:
    report = _ai_memory_report(mode="hybrid")
    report["schema_version"] = "longmemeval-live-v1"
    report["suite"] = "LongMemEval-S live API"
    report["runtime"].update(
        {
            "runtime_mode": "live-api-ephemeral",
            "graph_engine": "surreal",
            "store": "surreal",
            "retrieval_mode": "hybrid",
            "embedding_provider": "local",
            "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
            "embedding_dimensions": 1024,
            "embedding_cache_namespace": "graph",
            "tokenizer_estimate_method": "sentence-transformers",
        }
    )

    failures = eval_gate.evaluate_report(report, profile="ai-memory")

    assert (
        "runtime['embedding_dimensions'] must be 384 for local "
        "embedding_model 'sentence-transformers/all-minilm-l6-v2'"
    ) in failures


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
        "embedding_cache_namespace": "not-applicable",
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


def test_evaluate_report_ai_memory_profile_skips_tiny_slice_thresholds() -> None:
    report = _ai_memory_report(mode="hybrid")
    report["diagnostics"] = {"question_type_counts": {"temporal-reasoning": {"cases": 4.0}}}
    report["per_type"]["temporal-reasoning"]["ndcg@5"] = 0.10

    failures = eval_gate.evaluate_report(report, profile="ai-memory")

    assert failures == []


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


def test_evaluate_report_reports_runtime_requirement_failures() -> None:
    report = _ai_memory_report(mode="hybrid")

    failures = eval_gate.evaluate_report(
        report,
        profile="ai-memory",
        required_runtime={
            "embedding_provider": "local",
            "embedding_provider_status": "enabled",
        },
    )

    assert "runtime['embedding_provider'] expected 'local', got 'chromadb'" in failures
    assert "runtime['embedding_provider_status'] expected 'enabled', got None" in failures


def test_evaluate_report_require_accounting_rejects_missing_block() -> None:
    report = _ai_memory_report(mode="hybrid")
    report.pop("accounting")

    failures = eval_gate.evaluate_report(
        report,
        profile="ai-memory",
        require_accounting=True,
    )

    assert "missing non-empty field 'accounting'" in failures


def test_evaluate_report_validates_present_accounting_block() -> None:
    report = _ai_memory_report(mode="hybrid")
    report["accounting"]["latency"].pop("p95_ms")
    report["accounting"]["embedding"]["calls"] = 1.5

    failures = eval_gate.evaluate_report(report, profile="ai-memory")

    assert "accounting['latency']['p95_ms'] must be a finite non-negative number" in failures
    assert "accounting['embedding']['calls'] must be a non-negative integer" in failures


def test_evaluate_report_require_accounting_rejects_metric_mismatch() -> None:
    report = _ai_memory_report(mode="hybrid")
    report["runtime"]["embedding_provider"] = "openai"
    report["runtime"]["embedding_model"] = "text-embedding-3-small"
    report["overall"]["latency_p95_ms"] = 125.0
    report["overall"]["embedding_call_count"] = 3.0
    report["overall"]["embedding_estimated_input_tokens"] = 800.0
    report["accounting"]["embedding"]["provider"] = "openai"
    report["accounting"]["embedding"]["model"] = "text-embedding-3-small"
    report["accounting"]["embedding"]["calls"] = 0
    report["accounting"]["embedding"]["estimated_input_tokens"] = 0.0

    failures = eval_gate.evaluate_report(
        report,
        profile="ai-memory",
        require_accounting=True,
    )

    assert any(
        "accounting['embedding']['calls'] must match metric" in failure for failure in failures
    )
    assert any("embedding_estimated_input_tokens" in failure for failure in failures)


def test_evaluate_baseline_regressions_blocks_quality_drop() -> None:
    baseline = _ai_memory_report()
    candidate = _clone_report(baseline)
    candidate["overall"]["recall@5"] = 0.98

    failures = eval_gate.evaluate_baseline_regressions(
        candidate,
        baseline,
        profile="ai-memory",
        metrics=["recall@5"],
    )

    assert failures == [
        "metric 'recall@5' regressed below baseline 1.0000 by 0.0200; allowed 0.0000"
    ]


def test_evaluate_baseline_regressions_honors_lower_is_better_tolerance() -> None:
    baseline = {"metrics": {"success@5": 0.5, "latency_ms": 100.0}}
    candidate = {"metrics": {"success@5": 0.5, "latency_ms": 125.0}}

    failures = eval_gate.evaluate_baseline_regressions(
        candidate,
        baseline,
        profile="smoke",
        metrics=["latency_ms"],
        max_regressions={"latency_ms": 20.0},
    )

    assert failures == [
        "metric 'latency_ms' regressed above baseline 100.0000 by 25.0000; allowed 20.0000"
    ]


def test_evaluate_baseline_regressions_passes_with_named_tolerance() -> None:
    baseline = _ai_memory_report()
    candidate = _clone_report(baseline)
    candidate["overall"]["ndcg@5"] = 0.99

    failures = eval_gate.evaluate_baseline_regressions(
        candidate,
        baseline,
        profile="ai-memory",
        metrics=["ndcg@5"],
        max_regressions={"ndcg@5": 0.02},
    )

    assert failures == []


def test_evaluate_baseline_regressions_rejects_non_finite_tolerance() -> None:
    baseline = _ai_memory_report()
    candidate = _clone_report(baseline)
    candidate["overall"]["recall@5"] = 0.1

    failures = eval_gate.evaluate_baseline_regressions(
        candidate,
        baseline,
        profile="ai-memory",
        metrics=["recall@5"],
        max_regressions={"recall@5": float("nan")},
    )

    assert "max regression for metric 'recall@5' must be finite" in failures


def test_evaluate_baseline_regressions_rejects_non_finite_candidate_metric() -> None:
    baseline = {"metrics": {"success@5": 0.5, "latency_ms": 100.0}}
    candidate = {"metrics": {"success@5": float("nan"), "latency_ms": 100.0}}

    failures = eval_gate.evaluate_baseline_regressions(
        candidate,
        baseline,
        profile="smoke",
        metrics=["success@5"],
    )

    assert failures == ["candidate missing metric 'success@5'"]


def test_evaluate_baseline_regressions_rejects_unknown_metric_direction() -> None:
    baseline = {"metrics": {"custom_quality": 1.0}}
    candidate = {"metrics": {"custom_quality": 0.9}}

    failures = eval_gate.evaluate_baseline_regressions(
        candidate,
        baseline,
        profile="smoke",
        metrics=["custom_quality"],
    )

    assert failures == ["metric 'custom_quality' has unknown regression direction"]


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
                    "embedding_cache_namespace": "chromadb",
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


def test_validate_ai_memory_manifest_enforces_accounting_when_required(
    tmp_path: Path,
) -> None:
    report = _ai_memory_report()
    report.pop("accounting")
    entry = _manifest_entry(report)
    entry["accounting_required"] = True
    manifest_path = _write_ai_memory_manifest(tmp_path, report=report, entry=entry)

    failures = eval_gate.validate_ai_memory_manifest(manifest_path)

    assert "artifact.json: missing non-empty field 'accounting'" in failures


def test_validate_ai_memory_manifest_accepts_external_artifact_manifest(
    tmp_path: Path,
) -> None:
    report = _external_ai_memory_report()
    entry = _external_manifest_entry(report)
    external_path = tmp_path / "external" / "artifact-manifest.json"
    external_path.parent.mkdir()
    external_path.write_text(json.dumps(report), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    _write_history_summary(tmp_path)
    manifest_path.write_text(json.dumps(_manifest_payload(citable=[entry])), encoding="utf-8")

    failures = eval_gate.validate_ai_memory_manifest(manifest_path)

    assert failures == []


def test_validate_ai_memory_manifest_rejects_external_artifact_manifest_drift(
    tmp_path: Path,
) -> None:
    report = _external_ai_memory_report()
    entry = _external_manifest_entry(report)
    entry["overall"] = {"recall@5": 0.25}
    entry["external_artifact_manifest"] = "external/drift.json"
    external_path = tmp_path / "external" / "drift.json"
    external_path.parent.mkdir()
    external_path.write_text(json.dumps(report), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    _write_history_summary(tmp_path)
    manifest_path.write_text(json.dumps(_manifest_payload(citable=[entry])), encoding="utf-8")

    failures = eval_gate.validate_ai_memory_manifest(manifest_path)

    assert "external/drift.json: manifest overall does not match artifact" in failures


def test_validate_ai_memory_manifest_rejects_partial_external_artifact_summary(
    tmp_path: Path,
) -> None:
    report = _external_ai_memory_report()
    report["total_questions"] = 2
    entry = _external_manifest_entry(report, external_artifact_manifest="external/partial.json")
    external_path = tmp_path / "external" / "partial.json"
    external_path.parent.mkdir()
    external_path.write_text(json.dumps(report), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    _write_history_summary(tmp_path)
    manifest_path.write_text(json.dumps(_manifest_payload(citable=[entry])), encoding="utf-8")

    failures = eval_gate.validate_ai_memory_manifest(manifest_path)

    assert "external/partial.json: case_results must equal total_questions" in failures


def test_validate_ai_memory_manifest_rejects_planned_artifact_fields(tmp_path: Path) -> None:
    planned = [
        {"suite": "Future suite", "status": "planned", "artifact": "future.json"},
        {
            "suite": "Future external suite",
            "status": "planned",
            "external_artifact_manifest": "future.json",
        },
        {"suite": "Mistagged suite", "status": "citable"},
    ]
    manifest_path = _write_ai_memory_manifest(tmp_path, planned=planned)

    failures = eval_gate.validate_ai_memory_manifest(manifest_path)

    assert "planned[0] must not include artifact" in failures
    assert "planned[1] must not include external_artifact_manifest" in failures
    assert "planned[2] status is not 'planned'" in failures


def test_validate_ai_memory_manifest_rejects_empty_citable_list(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"citable": []}), encoding="utf-8")

    failures = eval_gate.validate_ai_memory_manifest(manifest_path)

    assert failures == ["manifest missing non-empty citable list"]


def test_validate_ai_memory_manifest_rejects_null_no_regression(tmp_path: Path) -> None:
    manifest_path = _write_ai_memory_manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["no_regression"] = None
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    failures = eval_gate.validate_ai_memory_manifest(manifest_path)

    assert failures == ["no_regression must be a list"]


def test_validate_ai_memory_manifest_accepts_v1_without_v2_contracts(tmp_path: Path) -> None:
    manifest_path = _write_ai_memory_manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = "sibyl-ai-memory-benchmark-ledger-v1"
    manifest.pop("history")
    manifest.pop("gate_contracts")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    failures = eval_gate.validate_ai_memory_manifest(manifest_path)

    assert failures == []


def test_validate_ai_memory_manifest_rejects_invalid_schema_version(tmp_path: Path) -> None:
    manifest_path = _write_ai_memory_manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 2
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    failures = eval_gate.validate_ai_memory_manifest(manifest_path)

    assert "manifest schema_version must be a supported string" in failures

    manifest["schema_version"] = "sibyl-ai-memory-benchmark-ledger-v99"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    failures = eval_gate.validate_ai_memory_manifest(manifest_path)

    assert any(
        "manifest schema_version 'sibyl-ai-memory-benchmark-ledger-v99'" in failure
        for failure in failures
    )


def test_validate_ai_memory_manifest_rejects_missing_v2_fields(tmp_path: Path) -> None:
    manifest_path = _write_ai_memory_manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("history")
    manifest.pop("gate_contracts")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    failures = eval_gate.validate_ai_memory_manifest(manifest_path)

    assert "manifest missing non-empty field 'history'" in failures
    assert "manifest missing non-empty field 'gate_contracts'" in failures
    assert "manifest history must be an object" in failures
    assert "manifest gate_contracts must be a non-empty list" in failures


def test_validate_ai_memory_manifest_rejects_missing_v2_contracts(tmp_path: Path) -> None:
    manifest_path = _write_ai_memory_manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["history"] = {
        "directory": str(tmp_path / "ai-memory-history"),
        "summary_schema": "",
        "append_policy": "mutable",
    }
    manifest["gate_contracts"] = [
        {
            "name": "eval-regression-gate",
            "owner_wave": "",
            "status": "soft",
            "profile": "unknown",
            "blocking": "yes",
            "metric_contracts": [
                {
                    "metric": "recall@5",
                    "mode": "no-regression",
                    "direction": "higher",
                    "max_regression": -0.001,
                },
                {
                    "metric": "latency_p95_ms",
                    "mode": "threshold",
                    "direction": "lower",
                },
                {"metric": "receipt", "mode": "receipt"},
                {"metric": "unknown", "mode": "mystery"},
            ],
        }
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    failures = eval_gate.validate_ai_memory_manifest(manifest_path)

    assert "manifest history directory must be repository-relative" in failures
    assert "manifest history missing non-empty summary_schema" in failures
    assert "manifest history append_policy must be 'immutable-json'" in failures
    assert "gate_contracts[0] missing non-empty owner_wave" in failures
    assert any(
        "gate_contracts[0] has unsupported status 'soft'; expected one of" in failure
        for failure in failures
    )
    assert any(
        "gate_contracts[0] has unsupported profile 'unknown'; expected one of" in failure
        for failure in failures
    )
    assert "gate_contracts[0] blocking must be boolean" in failures
    assert "gate_contracts[0].metric_contracts[0] max_regression must be non-negative" in failures
    assert "gate_contracts[0].metric_contracts[0] missing non-empty baseline" in failures
    assert "gate_contracts[0].metric_contracts[1] threshold must be finite numeric" in failures
    assert "gate_contracts[0].metric_contracts[2] missing non-empty required_receipt" in failures
    assert any(
        "gate_contracts[0].metric_contracts[3] has unsupported mode 'mystery'" in failure
        for failure in failures
    )


def test_validate_ai_memory_manifest_rejects_missing_history_directory(
    tmp_path: Path,
) -> None:
    manifest_path = _write_ai_memory_manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["history"]["directory"] = "missing-history"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    failures = eval_gate.validate_ai_memory_manifest(manifest_path)

    assert "manifest history directory does not exist: 'missing-history'" in failures


def test_validate_ai_memory_manifest_rejects_malformed_history_summary(
    tmp_path: Path,
) -> None:
    manifest_path = _write_ai_memory_manifest(tmp_path)
    history_path = tmp_path / "history" / "previous-run.json"
    summary = json.loads(history_path.read_text(encoding="utf-8"))
    summary.pop("gate_command")
    summary["schema_version"] = "future-schema"
    summary["metrics"] = {"recall@5": "nan"}
    summary["gate_passed"] = False
    history_path.write_text(json.dumps(summary), encoding="utf-8")

    failures = eval_gate.validate_ai_memory_manifest(manifest_path)

    assert "history/previous-run.json missing non-empty field 'gate_command'" in failures
    assert (
        "history/previous-run.json schema_version must be 'sibyl-ai-memory-history-summary-v1'"
    ) in failures
    assert "history/previous-run.json metrics['recall@5'] must be finite numeric" in failures
    assert "history/previous-run.json gate_passed must be true" in failures


def test_validate_ai_memory_manifest_rejects_duplicate_history_baselines(
    tmp_path: Path,
) -> None:
    manifest_path = _write_ai_memory_manifest(tmp_path)
    duplicate_path = tmp_path / "history" / "duplicate.json"
    duplicate_path.write_text(
        (tmp_path / "history" / "previous-run.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    failures = eval_gate.validate_ai_memory_manifest(manifest_path)

    assert "history/previous-run.json duplicates history baseline 'previous-run'" in failures


def test_validate_ai_memory_manifest_rejects_gate_contract_drift(tmp_path: Path) -> None:
    manifest_path = _write_ai_memory_manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["gate_contracts"].append(dict(manifest["gate_contracts"][0]))
    manifest["gate_contracts"][0]["blocking"] = False
    manifest["gate_contracts"][0]["metric_contracts"][0]["direction"] = "sideways"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    failures = eval_gate.validate_ai_memory_manifest(manifest_path)

    assert "gate_contracts[0] blocking must match status 'blocking'" in failures
    assert "gate_contracts[0].metric_contracts[0] direction must be 'higher' or 'lower'" in failures
    assert "gate_contracts[1] duplicates gate contract 'eval-regression-gate'" in failures


def test_validate_ai_memory_manifest_accepts_v11_gate_contracts(tmp_path: Path) -> None:
    manifest_path = _write_ai_memory_manifest(tmp_path)
    receipt_path = tmp_path / "write-path-integrity-receipt.json"
    receipt_path.write_text(
        json.dumps(
            {
                "schema_version": "sibyl-write-path-integrity-receipt-v1",
                "metrics": {
                    "hallucinated_fact_count": 0,
                    "self_referential_write_count": 0,
                    "low_signal_write_count": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["gate_contracts"] = [
        {
            "name": "eval-regression-gate",
            "owner_wave": "W2A",
            "status": "blocking",
            "profile": "ai-memory",
            "blocking": True,
            "metric_contracts": [
                {
                    "metric": "recall@5",
                    "mode": "no-regression",
                    "direction": "higher",
                    "baseline": "latest-citable-hybrid",
                    "max_regression": 0.005,
                },
                {
                    "metric": "latency_p95_ms",
                    "mode": "threshold",
                    "direction": "lower",
                    "threshold": 1000,
                },
            ],
        },
        {
            "name": "write-path-integrity-gate",
            "owner_wave": "W4",
            "status": "blocking",
            "profile": "product",
            "blocking": True,
            "metric_contracts": [
                {
                    "metric": "hallucinated_fact_count",
                    "mode": "receipt",
                    "required_receipt": "write-path-integrity-receipt.json",
                    "receipt_schema": "sibyl-write-path-integrity-receipt-v1",
                    "direction": "lower",
                    "threshold": 0,
                },
                {
                    "metric": "self_referential_write_count",
                    "mode": "receipt",
                    "required_receipt": "write-path-integrity-receipt.json",
                    "receipt_schema": "sibyl-write-path-integrity-receipt-v1",
                    "direction": "lower",
                    "threshold": 0,
                },
                {
                    "metric": "low_signal_write_count",
                    "mode": "receipt",
                    "required_receipt": "write-path-integrity-receipt.json",
                    "receipt_schema": "sibyl-write-path-integrity-receipt-v1",
                    "direction": "lower",
                    "threshold": 0,
                },
            ],
        },
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    failures = eval_gate.validate_ai_memory_manifest(manifest_path)

    assert failures == []


def test_validate_ai_memory_manifest_rejects_blocking_receipt_metric_failure(
    tmp_path: Path,
) -> None:
    manifest_path = _write_ai_memory_manifest(tmp_path)
    receipt_path = tmp_path / "write-path-integrity-receipt.json"
    receipt_path.write_text(
        json.dumps(
            {
                "schema_version": "sibyl-write-path-integrity-receipt-v1",
                "metrics": {"low_signal_write_count": 1},
            }
        ),
        encoding="utf-8",
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["gate_contracts"].append(
        {
            "name": "write-path-integrity-gate",
            "owner_wave": "W4",
            "status": "blocking",
            "profile": "product",
            "blocking": True,
            "metric_contracts": [
                {
                    "metric": "low_signal_write_count",
                    "mode": "receipt",
                    "required_receipt": "write-path-integrity-receipt.json",
                    "direction": "lower",
                    "threshold": 0,
                }
            ],
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    failures = eval_gate.validate_ai_memory_manifest(manifest_path)

    assert (
        "gate_contracts[1].metric_contracts[0] required_receipt metric "
        "'low_signal_write_count'=1 exceeds threshold 0"
    ) in failures


def test_validate_ai_memory_manifest_enforces_no_regression_entries(tmp_path: Path) -> None:
    baseline = _ai_memory_report()
    candidate = _clone_report(baseline)
    candidate["overall"]["ndcg@5"] = 0.95
    (tmp_path / "baseline.json").write_text(json.dumps(baseline), encoding="utf-8")
    (tmp_path / "candidate.json").write_text(json.dumps(candidate), encoding="utf-8")
    _write_history_summary(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            _manifest_payload(
                citable=[_manifest_entry(candidate, artifact="candidate.json")],
                no_regression=[
                    {
                        "candidate": "candidate.json",
                        "baseline": "baseline.json",
                        "profile": "ai-memory",
                        "metrics": ["ndcg@5"],
                    }
                ],
            )
        ),
        encoding="utf-8",
    )

    failures = eval_gate.validate_ai_memory_manifest(manifest_path)

    assert failures == [
        "no_regression[0] candidate.json: metric 'ndcg@5' regressed below "
        "baseline 1.0000 by 0.0500; allowed 0.0000"
    ]


def test_validate_ai_memory_manifest_uses_history_baseline(tmp_path: Path) -> None:
    baseline = _ai_memory_report()
    candidate = _clone_report(baseline)
    candidate["overall"]["ndcg@5"] = 0.99
    (tmp_path / "candidate.json").write_text(json.dumps(candidate), encoding="utf-8")
    _write_history_summary(
        tmp_path,
        baseline_key="previous-run",
        metrics={"ndcg@5": baseline["overall"]["ndcg@5"]},
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            _manifest_payload(
                citable=[_manifest_entry(candidate, artifact="candidate.json")],
                no_regression=[
                    {
                        "candidate": "candidate.json",
                        "baseline_history": "previous-run",
                        "profile": "ai-memory",
                        "metrics": ["ndcg@5"],
                        "max_regression": {"ndcg@5": 0.02},
                    }
                ],
            )
        ),
        encoding="utf-8",
    )

    failures = eval_gate.validate_ai_memory_manifest(manifest_path)

    assert failures == []


def test_validate_ai_memory_manifest_uses_external_artifact_history_baseline(
    tmp_path: Path,
) -> None:
    baseline = _ai_memory_report(mode="hybrid")
    candidate = _external_ai_memory_report(baseline)
    external_path = tmp_path / "external" / "candidate.json"
    external_path.parent.mkdir()
    external_path.write_text(json.dumps(candidate), encoding="utf-8")
    _write_history_summary(
        tmp_path,
        baseline_key="previous-run",
        metrics={"recall@5": baseline["overall"]["recall@5"]},
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            _manifest_payload(
                citable=[
                    _external_manifest_entry(
                        candidate,
                        external_artifact_manifest="external/candidate.json",
                    )
                ],
                no_regression=[
                    {
                        "candidate": "external/candidate.json",
                        "baseline_history": "previous-run",
                        "profile": "ai-memory",
                        "metrics": ["recall@5"],
                        "max_regression": {"recall@5": 0.005},
                    }
                ],
            )
        ),
        encoding="utf-8",
    )

    failures = eval_gate.validate_ai_memory_manifest(manifest_path)

    assert failures == []


def test_validate_ai_memory_manifest_rejects_missing_history_baseline(
    tmp_path: Path,
) -> None:
    candidate = _ai_memory_report()
    (tmp_path / "candidate.json").write_text(json.dumps(candidate), encoding="utf-8")
    _write_history_summary(tmp_path, baseline_key="previous-run")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            _manifest_payload(
                citable=[_manifest_entry(candidate, artifact="candidate.json")],
                no_regression=[
                    {
                        "candidate": "candidate.json",
                        "baseline_history": "missing-run",
                        "profile": "ai-memory",
                        "metrics": ["recall@5"],
                    }
                ],
            )
        ),
        encoding="utf-8",
    )

    failures = eval_gate.validate_ai_memory_manifest(manifest_path)

    assert failures == ["no_regression[0] history baseline does not exist: missing-run"]


def test_validate_ai_memory_manifest_rejects_history_baseline_regression(
    tmp_path: Path,
) -> None:
    baseline = _ai_memory_report()
    candidate = _clone_report(baseline)
    candidate["overall"]["ndcg@5"] = 0.95
    (tmp_path / "candidate.json").write_text(json.dumps(candidate), encoding="utf-8")
    _write_history_summary(
        tmp_path,
        baseline_key="previous-run",
        metrics={"ndcg@5": baseline["overall"]["ndcg@5"]},
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            _manifest_payload(
                citable=[_manifest_entry(candidate, artifact="candidate.json")],
                no_regression=[
                    {
                        "candidate": "candidate.json",
                        "baseline_history": "previous-run",
                        "profile": "ai-memory",
                        "metrics": ["ndcg@5"],
                        "max_regression": {"ndcg@5": 0.02},
                    }
                ],
            )
        ),
        encoding="utf-8",
    )

    failures = eval_gate.validate_ai_memory_manifest(manifest_path)

    assert failures == [
        "no_regression[0] candidate.json: metric 'ndcg@5' regressed below "
        "baseline 1.0000 by 0.0500; allowed 0.0200"
    ]


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


def test_main_returns_nonzero_when_baseline_regresses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    _write_report(
        baseline_path,
        metrics={"success@5": 0.50, "latency_ms": 100.0},
    )
    _write_report(
        candidate_path,
        metrics={"success@5": 0.49, "latency_ms": 100.0},
    )

    exit_code = eval_gate.main(
        [
            str(candidate_path),
            "--profile",
            "smoke",
            "--baseline",
            str(baseline_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Baseline comparison" in captured.out
    assert "metric 'success@5' regressed below baseline" in captured.out


def test_main_rejects_baseline_without_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline_path = tmp_path / "baseline.json"
    _write_report(
        baseline_path,
        metrics={"success@5": 0.50, "latency_ms": 100.0},
    )

    with pytest.raises(SystemExit) as exc:
        eval_gate.main(["--baseline", str(baseline_path)])

    captured = capsys.readouterr()
    assert exc.value.code == ARGPARSE_USAGE_ERROR
    assert "--baseline, runtime, and accounting options require a report argument" in captured.err


def test_ai_memory_manifest_tracks_full_citable_artifacts() -> None:
    repo_root = Path(__file__).parents[2]
    manifest_path = repo_root / "benchmarks" / "results" / "ai-memory" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    citable = manifest["citable"]
    assert citable

    for entry in citable:
        if "artifact" in entry:
            artifact = manifest_path.parent / entry["artifact"]
            assert artifact.exists()
            report = eval_gate.load_report(artifact)
            assert eval_gate.evaluate_report(report, profile="ai-memory") == []
            case_result_count = len(report["case_results"])
        else:
            artifact = manifest_path.parent / entry["external_artifact_manifest"]
            assert artifact.exists()
            report = eval_gate.load_report(artifact)
            assert eval_gate.evaluate_external_ai_memory_report(report) == []
            case_result_count = report["case_results"]
            external_artifact = report["external_artifact"]
            assert external_artifact["provider"] == "github-actions"
            assert external_artifact["sha256"]
            assert external_artifact["expires_at"]
            assert external_artifact["verified_at"]
            assert external_artifact["verification_receipt"]
            assert external_artifact["gate_passed"] is True
        assert entry["status"] == "citable"
        assert entry["suite"] == report["suite"]
        assert entry["suite_version"] == report["suite_version"]
        assert entry["sibyl_commit"] == report["sibyl_commit"]
        assert entry["mode"] == report["mode"]
        assert entry["questions"] == report["total_questions"]
        assert entry["case_results"] == case_result_count
        assert entry["runtime"] == report["runtime"]
        assert entry["dataset"] == report["dataset"]
        for metric, expected in entry["overall"].items():
            assert report["overall"][metric] == pytest.approx(expected)

    planned = manifest["planned"]
    assert planned
    for entry in planned:
        assert entry["status"] == "planned"
        assert "artifact" not in entry
        assert "external_artifact_manifest" not in entry

    history_regressions = [
        entry for entry in manifest["no_regression"] if "baseline_history" in entry
    ]
    assert history_regressions == [
        {
            "candidate": "external/longmemeval_sibyl_live_full_26304777971.json",
            "baseline_history": "latest-citable-hybrid",
            "profile": "ai-memory",
            "metrics": ["recall@5"],
            "max_regression": {"recall@5": 0.005},
        }
    ]
    assert eval_gate.validate_ai_memory_manifest(manifest_path) == []
