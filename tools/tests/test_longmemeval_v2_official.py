from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Awaitable, Callable
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Protocol, TypedDict, cast

import httpx
import pytest
from tools.bench import eval_gate

EXPECTED_REQUIRED_TRAJECTORIES = 2
EXPECTED_LAFS_GAIN = 0.125
EXPECTED_MEMORY_QUERY_AVG_SECONDS = 2.5
EXPECTED_EMBEDDING_JOB_WAIT_TIMEOUT_SECONDS = 1_800.0
EXPECTED_CONTENT_MAX_CHARS = 18_000
EXPECTED_BULK_MAX_ENTITIES = 32
EXPECTED_BULK_MAX_CONTENT_CHARS = 512_000
EXPECTED_EMBEDDING_BACKFILL_MAX_PENDING_JOBS = 8
EXPECTED_MEMORY_API_TIMEOUT_SECONDS = 600.0
EXPECTED_MEMORY_API_RETRY_ATTEMPTS = 3
EXPECTED_MEMORY_API_RETRY_CALLS = 2
EXPECTED_READER_MAX_CONCURRENT_REQUESTS = 16
EXPECTED_READER_RETRY_ATTEMPTS = 4
EXPECTED_TRANSIENT_READER_ATTEMPTS = 2
EXPECTED_EVALUATOR_RETRY_ATTEMPTS = 3
EXPECTED_TRANSIENT_EVALUATOR_ATTEMPTS = 2
EXPECTED_COMBINED_QUESTION_COUNT = 4
EXPECTED_LATENCY_P50_MS = 2_000.0
EXPECTED_LATENCY_P95_MS = 4_000.0
EXPECTED_EMBEDDING_REQUESTS = 10
EXPECTED_READER_REQUESTS = 6
EXPECTED_DOMAIN_READER_REQUESTS = 3
EXPECTED_JUDGE_REQUESTS = 2
EXPECTED_MAX_CHUNKS_PER_TRAJECTORY = 2
EXPECTED_NEIGHBOR_STITCH_ITEMS = 2
EXPECTED_NEIGHBOR_STITCH_SPAN = 1
EXPECTED_STATE_PART_COMPLETION_ITEMS = 2
EXPECTED_STATE_PART_REFINEMENT_MIN_SCORE_GAIN = 0.05
EXPECTED_CONTEXT_EXPANSION_MAX_RATIO = 1.2
EXPECTED_CONTEXT_TOKEN_COUNT = 37
EXPECTED_CONTEXT_TOTAL_CHARS = 60_000
EXPECTED_CONTEXT_BUDGET_ITEMS = 3
EXPECTED_SEARCH_LIMIT_OVERRIDE = 24
EXPECTED_SAVED_USAGE_REQUESTS = 2
EXPECTED_SAVED_USAGE_COST_USD = 0.25
EXPECTED_USAGE_ATTEMPTS = 2
EXPECTED_OPERATIONAL_CREATED_ENTITIES = 4
EXPECTED_OPERATIONAL_EVIDENCE_ITEMS = 8
EXPECTED_OPERATIONAL_RAW_ITEMS = 6
EXPECTED_OPERATIONAL_TYPED_ITEMS = 2
EXPECTED_OPERATIONAL_SUPPORT_ITEMS = 3
EXPECTED_SHARED_RELEVANCE_TYPED_ITEMS = 3
EXPECTED_SHARED_RELEVANCE_RAW_ITEMS = 5
EXPECTED_PLANNER_REQUESTS = 2
EXPECTED_PLANNER_INPUT_TOKENS = 84
EXPECTED_PLANNER_OUTPUT_TOKENS = 22
EXPECTED_RETRIEVAL_MAX_PLANNED_QUERIES = 3
EXPECTED_WHITESPACE_EXPOSURE_CHARS = 2
EXPECTED_SELECTED_WINDOW_COUNT = 2
EXPECTED_ASSEMBLED_RESULT_COUNT = 5
EXPECTED_ASSEMBLED_SEED_COUNT = 4
EXPECTED_RESTORED_SCORE = 0.9
EXPECTED_REFINEMENT_SOURCE_CHUNK = 3
EXPECTED_SHA256_HEX_LENGTH = 64
EXPECTED_CREDENTIAL_FILE_MODE = 0o600
OPERATIONAL_EVIDENCE_MAX_CHARS = 4_000
TEST_CONTENT_MAX_CHARS = 420
TEST_CONTEXT_MAX_CHARS = 800
TEST_CONTEXT_TOTAL_CHARS = 700
TEST_CREDENTIAL = "fresh-credential"
ROTATED_CREDENTIAL = f"rotated-{TEST_CREDENTIAL}"
TEST_EMAIL = "eval@example.test"


class _RequestCall(TypedDict):
    method: str
    path: str
    json: dict[str, object]
    params: dict[str, object]


class _ReaderHarness(Protocol):
    call_reader_model_async: Callable[
        [object, object, list[dict[str, object]]],
        Awaitable[tuple[str, dict[str, int]]],
    ]


class _EvaluatorMetrics(Protocol):
    llm_abstention_checker: Callable[..., bool]
    llm_gotchas_checker: Callable[..., bool]


def _load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_runner_module() -> ModuleType:
    return _load_module(
        Path(__file__).parents[2] / "benchmarks" / "longmemeval_v2_official.py",
        "longmemeval_v2_official",
    )


def _finalize_request_handler(
    calls: list[str],
) -> Callable[..., dict[str, object]]:
    def fake_request(
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        del method, params
        calls.append(path)
        if path == "/jobs/status":
            assert isinstance(json, dict)
            job_ids = json["job_ids"]
            assert isinstance(job_ids, list)
            assert len(job_ids) == 1
            result = None
            if str(job_ids[0]).startswith("embed-"):
                result = {
                    "embedding_usage": {
                        "provider": "openai",
                        "model": "text-embedding-3-small",
                        "requests": 1,
                        "inputs": EXPECTED_OPERATIONAL_CREATED_ENTITIES,
                        "prompt_tokens": 100,
                        "total_tokens": 100,
                        "cost_reported_requests": 0,
                        "cost_usd": 0.0,
                    }
                }
            return {
                "jobs": {
                    str(job_ids[0]): {
                        "status": "complete",
                        "error": None,
                        "result": result,
                    }
                }
            }
        if path == "/context/pack":
            assert json is not None
            assert json["record_exposure"] is False
            assert json["audit"] is True
            assert json["project"] == "project_lme"
            assert json["evidence"] == {
                "types": ["session"],
                "limit": 12,
                "max_results_per_source": EXPECTED_MAX_CHUNKS_PER_TRAJECTORY,
                "content_max_chars": TEST_CONTEXT_MAX_CHARS,
                "include_retrieval_diagnostics": True,
                "retrieval_mode": "fast",
                "max_planned_queries": 3,
            }
            return {
                "sections": [],
                "evidence": {
                    "results": [],
                    "filters": {
                        "retrieval_mode": "native",
                        "stage_timings_ms": {"total": 12.5},
                    },
                },
            }
        raise AssertionError(f"unexpected path: {path}")

    return fake_request


def _load_provider_usage_module() -> ModuleType:
    return _load_module(
        Path(__file__).parents[2] / "benchmarks" / "provider_usage.py",
        "provider_usage",
    )


def _load_memory_module() -> ModuleType:
    return _load_module(
        Path(__file__).parents[2] / "benchmarks" / "longmemeval_v2_memory" / "sibyl_memory.py",
        "sibyl_memory",
    )


def _load_download_module() -> ModuleType:
    return _load_module(
        Path(__file__).parents[2] / "benchmarks" / "longmemeval_v2_download.py",
        "longmemeval_v2_download",
    )


def test_longmemeval_v2_download_patterns_default_to_text_context() -> None:
    module = _load_download_module()

    text_context_patterns = module.download_patterns(include_trajectory_screenshots=False)
    full_patterns = module.download_patterns(include_trajectory_screenshots=True)

    assert "trajectories.jsonl" in text_context_patterns
    assert "question_screenshots/*.png" in text_context_patterns
    assert "trajectory_screenshots/*.tar.gz" not in text_context_patterns
    assert "trajectory_screenshots/*.tar.gz" in full_patterns


def test_official_runner_plan_materializes_honest_runtime_inputs(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_runner_module()
    monkeypatch.setenv("SIBYL_API_TOKEN", "before-test")
    monkeypatch.setenv("LME_SIBYL_EMAIL", "before-test")
    monkeypatch.setenv("LME_SIBYL_PASSWORD", "before-test")
    data_root = tmp_path / "data"
    output_dir = tmp_path / "out"
    credentials_path = tmp_path / "credentials.json"
    credentials_path.write_text(
        json.dumps({"access_token": "access", "refresh_token": "refresh"}),
        encoding="utf-8",
    )
    _write_dataset(data_root)

    assert (
        module.main(
            [
                "--data-root",
                str(data_root),
                "--domain",
                "enterprise",
                "--tier",
                "small",
                "--output-dir",
                str(output_dir),
                "--limit",
                "1",
                "--plan-only",
                "--allow-localhost",
                "--project-id",
                "project-existing",
                "--reuse-existing-project",
                "--api-token",
                TEST_CREDENTIAL,
                "--api-credentials-file",
                str(credentials_path),
                "--email",
                TEST_EMAIL,
                "--password",
                TEST_CREDENTIAL,
                "--context-expansion-max-ratio",
                str(EXPECTED_CONTEXT_EXPANSION_MAX_RATIO),
            ]
        )
        == 0
    )

    runtime_questions = json.loads(
        (output_dir / "runtime_inputs" / "questions.json").read_text(encoding="utf-8")
    )
    runtime_haystack = json.loads(
        (output_dir / "runtime_inputs" / "haystack.json").read_text(encoding="utf-8")
    )
    memory_config = json.loads(
        (output_dir / "runtime_inputs" / "memory_config.json").read_text(encoding="utf-8")
    )
    plan = json.loads(
        (output_dir / "longmemeval_v2_official_plan.json").read_text(encoding="utf-8")
    )

    assert [row["id"] for row in runtime_questions] == ["q-enterprise"]
    assert runtime_haystack == {"q-enterprise": ["t1", "t2"]}
    assert memory_config["memory_type"] == "sibyl_live_api"
    _assert_credentials_stay_process_local(memory_config)
    assert os.environ["SIBYL_API_CREDENTIALS_FILE"] == str(credentials_path)
    assert memory_config["memory_params"]["allow_localhost"] is True
    assert memory_config["memory_params"]["project_id"] == "project-existing"
    assert memory_config["memory_params"]["reuse_existing_project"] is True
    assert memory_config["memory_params"]["defer_embeddings"] is True
    assert (
        memory_config["memory_params"]["content_max_chars"],
        memory_config["memory_params"]["max_context_total_chars"],
        plan["max_context_total_chars"],
    ) == (
        EXPECTED_CONTENT_MAX_CHARS,
        EXPECTED_CONTEXT_TOTAL_CHARS,
        EXPECTED_CONTEXT_TOTAL_CHARS,
    )
    assert memory_config["memory_params"]["chunking_mode"] == "state"
    assert (
        memory_config["memory_params"]["max_chunks_per_trajectory"]
        == EXPECTED_MAX_CHUNKS_PER_TRAJECTORY
    )
    assert memory_config["memory_params"]["neighbor_stitch_items"] == EXPECTED_NEIGHBOR_STITCH_ITEMS
    assert memory_config["memory_params"]["neighbor_stitch_span"] == EXPECTED_NEIGHBOR_STITCH_SPAN
    assert memory_config["memory_params"]["state_part_completion_items"] == 0
    assert memory_config["memory_params"]["state_part_refinement"] is False
    assert (
        memory_config["memory_params"]["context_expansion_max_ratio"]
        == EXPECTED_CONTEXT_EXPANSION_MAX_RATIO
    )
    assert (
        memory_config["memory_params"]["api_timeout_seconds"] == EXPECTED_MEMORY_API_TIMEOUT_SECONDS
    )
    assert (
        memory_config["memory_params"]["api_retry_attempts"] == EXPECTED_MEMORY_API_RETRY_ATTEMPTS
    )
    assert (
        memory_config["memory_params"]["embedding_job_wait_timeout_seconds"]
        == EXPECTED_EMBEDDING_JOB_WAIT_TIMEOUT_SECONDS
    )
    assert memory_config["memory_params"]["bulk_max_entities"] == EXPECTED_BULK_MAX_ENTITIES
    assert (
        memory_config["memory_params"]["bulk_max_content_chars"] == EXPECTED_BULK_MAX_CONTENT_CHARS
    )
    assert (
        memory_config["memory_params"]["embedding_backfill_max_pending_jobs"]
        == EXPECTED_EMBEDDING_BACKFILL_MAX_PENDING_JOBS
    )
    assert plan["reader_max_concurrent_requests"] == EXPECTED_READER_MAX_CONCURRENT_REQUESTS
    assert plan["reader_retry_attempts"] == EXPECTED_READER_RETRY_ATTEMPTS
    assert plan["evaluator_retry_attempts"] == EXPECTED_EVALUATOR_RETRY_ATTEMPTS
    assert plan["memory_api_timeout_seconds"] == EXPECTED_MEMORY_API_TIMEOUT_SECONDS
    assert plan["memory_api_retry_attempts"] == EXPECTED_MEMORY_API_RETRY_ATTEMPTS
    assert plan["chunking_mode"] == "state"
    assert plan["reuse_existing_project"] is True
    assert plan["max_chunks_per_trajectory"] == EXPECTED_MAX_CHUNKS_PER_TRAJECTORY
    assert plan["neighbor_stitch_items"] == EXPECTED_NEIGHBOR_STITCH_ITEMS
    assert plan["neighbor_stitch_span"] == EXPECTED_NEIGHBOR_STITCH_SPAN
    assert plan["context_expansion_max_ratio"] == EXPECTED_CONTEXT_EXPANSION_MAX_RATIO
    assert {
        key: plan[key]
        for key in (
            "evidence_composition_mode",
            "source_evidence_bundling",
            "include_screenshot_refs",
        )
    } == {
        "evidence_composition_mode": "shared_relevance",
        "source_evidence_bundling": False,
        "include_screenshot_refs": False,
    }
    assert plan["honesty_contract"]["answer_gold_visible_to_memory"] is False
    assert plan["required_trajectory_count"] == EXPECTED_REQUIRED_TRAJECTORIES
    assert plan["requirements"]["trajectories_jsonl_exists"] is True
    assert (plan["requirements"]["official_repo_configured"], plan["checkpoint_dir"]) == (
        False,
        None,
    )
    assert plan["provider_usage"] == {
        "reader": str(output_dir / "provider_usage" / "reader.jsonl"),
        "judge": str(output_dir / "provider_usage" / "judge.jsonl"),
    }
    assert {"reader_endpoint_reachable", "torch_available"} <= plan["requirements"].keys()
    _assert_question_id_hash_propagates(module, data_root=data_root, plan=plan)


def test_official_runner_refuses_existing_provider_usage_before_work(tmp_path: Path) -> None:
    module = _load_runner_module()
    output_dir = tmp_path / "output"
    usage_dir = output_dir / "provider_usage"
    usage_dir.mkdir(parents=True)
    (usage_dir / "reader.jsonl").write_text("{}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Use a fresh --output-dir"):
        module.main(
            [
                "--data-root",
                str(tmp_path / "data"),
                "--domain",
                "web",
                "--output-dir",
                str(output_dir),
                "--plan-only",
            ]
        )

    assert not (output_dir / "runtime_inputs").exists()


def test_official_runner_requires_project_id_for_reuse(tmp_path: Path) -> None:
    module = _load_runner_module()

    with pytest.raises(SystemExit):
        module.parse_args(
            [
                "--data-root",
                str(tmp_path / "data"),
                "--domain",
                "enterprise",
                "--output-dir",
                str(tmp_path / "output"),
                "--reuse-existing-project",
            ]
        )


def _assert_question_id_hash_propagates(
    module: ModuleType,
    *,
    data_root: Path,
    plan: dict[str, object],
) -> None:
    assert str(plan["provider_usage_run_id"]).startswith("lme-v2-usage-")
    assert plan["provider_usage_run_id"] != plan["run_id"]
    expected_question_ids_sha256 = module.sha256_question_ids(["q-enterprise"])
    assert plan["selected_question_ids_sha256"] == expected_question_ids_sha256
    dataset_receipt = module.build_dataset_receipt(
        data_root=data_root,
        domain="enterprise",
        tier="small",
        plan=plan,
        aggregated_metrics={},
    )
    assert dataset_receipt["selected_question_ids_sha256"] == expected_question_ids_sha256


def test_official_runner_receipt_only_emits_citable_contract(tmp_path: Path) -> None:
    module = _load_runner_module()
    data_root = tmp_path / "data"
    receipt_dir = tmp_path / "receipt"
    web_output_dir = tmp_path / "runs" / "web"
    enterprise_output_dir = tmp_path / "runs" / "enterprise"
    combined_dir = tmp_path / "combined"
    official_repo = _write_official_repo(tmp_path / "official")
    _write_dataset(data_root)
    _write_official_outputs(web_output_dir, domain="web", legacy_usage_identity=True)
    _write_official_outputs(enterprise_output_dir, domain="enterprise")
    _write_combined_outputs(combined_dir)

    assert (
        module.main(
            [
                "--data-root",
                str(data_root),
                "--domain",
                "combined",
                "--tier",
                "small",
                "--output-dir",
                str(receipt_dir),
                "--official-repo",
                str(official_repo),
                "--receipt-only",
                "--metric-overview",
                str(combined_dir / "metric_overview.json"),
                "--combined-metrics",
                str(combined_dir / "aggregated_metrics.json"),
                "--submission-overview",
                str(combined_dir / "submission_overview.json"),
                "--web-output-dir",
                str(web_output_dir),
                "--enterprise-output-dir",
                str(enterprise_output_dir),
            ]
        )
        == 0
    )

    receipt = json.loads(
        (receipt_dir / "longmemeval_v2_official_receipt.json").read_text(encoding="utf-8")
    )

    assert receipt["schema_version"] == "sibyl-longmemeval-v2-official-receipt-v1"
    assert receipt["domain"] == "combined"
    assert receipt["official_repo"]["commit"]
    assert receipt["dataset"]["questions_sha256"].startswith("sha256:")
    assert receipt["dataset"]["question_count"] == EXPECTED_COMBINED_QUESTION_COUNT
    assert isinstance(receipt["dataset"]["question_count"], int)
    assert receipt["source_runs"]["complete"] is True
    assert receipt["source_runs"]["integrity_complete"] is True
    assert receipt["source_runs"]["api_runtime_consistent"] is True
    assert set(receipt["source_runs"]["domains"]) == {"web", "enterprise"}
    assert receipt["source_runs"]["domains"]["web"]["runtime_inputs"]["questions"][
        "sha256"
    ].startswith("sha256:")
    effective_config = receipt["source_runs"]["domains"]["web"]["effective_memory_config"]
    assert "api_token" not in effective_config["memory_params"]
    assert "email" not in effective_config["memory_params"]
    assert "password" not in effective_config["memory_params"]
    assert receipt["runner_provenance"]["sibyl_commit"] != "unknown"
    assert receipt["metrics"]["lafs_gain"] == EXPECTED_LAFS_GAIN
    assert receipt["metrics"]["memory_query_avg_seconds"] == EXPECTED_MEMORY_QUERY_AVG_SECONDS
    assert receipt["metrics"]["latency_p50_ms"] == EXPECTED_LATENCY_P50_MS
    assert receipt["metrics"]["latency_p95_ms"] == EXPECTED_LATENCY_P95_MS
    assert receipt["metrics"]["max_latency_ms"] == EXPECTED_LATENCY_P95_MS
    assert receipt["accounting"]["embedding"]["calls"] == EXPECTED_EMBEDDING_REQUESTS
    assert receipt["accounting"]["reader"]["requests"] == EXPECTED_READER_REQUESTS
    assert receipt["accounting"]["judge"]["requests"] == EXPECTED_JUDGE_REQUESTS
    assert receipt["accounting"]["cost"]["provider_reported_total_usd"] == pytest.approx(0.1026)
    assert receipt["accounting"]["cost"]["coverage_complete"] is True
    assert {check["status"] for check in receipt["checks"]} == {"PASS"}
    assert eval_gate.evaluate_report(receipt, profile="longmemeval-v2") == []


def test_longmemeval_v2_receipt_gate_rejects_missing_lafs(tmp_path: Path) -> None:
    module = _load_runner_module()
    data_root = tmp_path / "data"
    receipt_dir = tmp_path / "receipt"
    web_output_dir = tmp_path / "runs" / "web"
    enterprise_output_dir = tmp_path / "runs" / "enterprise"
    combined_dir = tmp_path / "combined"
    official_repo = _write_official_repo(tmp_path / "official")
    _write_dataset(data_root)
    _write_official_outputs(web_output_dir, domain="web")
    _write_official_outputs(enterprise_output_dir, domain="enterprise")
    _write_combined_outputs(combined_dir, include_submission_overview=False)

    assert (
        module.main(
            [
                "--data-root",
                str(data_root),
                "--domain",
                "combined",
                "--tier",
                "small",
                "--output-dir",
                str(receipt_dir),
                "--official-repo",
                str(official_repo),
                "--receipt-only",
                "--metric-overview",
                str(combined_dir / "metric_overview.json"),
                "--combined-metrics",
                str(combined_dir / "aggregated_metrics.json"),
                "--web-output-dir",
                str(web_output_dir),
                "--enterprise-output-dir",
                str(enterprise_output_dir),
            ]
        )
        == 0
    )

    receipt = json.loads(
        (receipt_dir / "longmemeval_v2_official_receipt.json").read_text(encoding="utf-8")
    )
    failures = eval_gate.evaluate_report(receipt, profile="longmemeval-v2")

    assert "metrics['lafs_gain'] must be finite numeric" in failures
    assert "checks[5] status must be 'PASS'" in failures


def test_longmemeval_v2_receipt_rejects_corrupt_provider_usage(tmp_path: Path) -> None:
    module = _load_runner_module()
    data_root = tmp_path / "data"
    web_output_dir = tmp_path / "runs" / "web"
    enterprise_output_dir = tmp_path / "runs" / "enterprise"
    combined_dir = tmp_path / "combined"
    official_repo = _write_official_repo(tmp_path / "official")
    _write_dataset(data_root)
    _write_official_outputs(web_output_dir, domain="web")
    _write_official_outputs(enterprise_output_dir, domain="enterprise")
    _write_combined_outputs(combined_dir)
    with (web_output_dir / "provider_usage" / "reader.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{truncated\n")

    args = module.parse_args(
        [
            "--data-root",
            str(data_root),
            "--domain",
            "combined",
            "--output-dir",
            str(tmp_path / "receipt"),
            "--official-repo",
            str(official_repo),
            "--receipt-only",
            "--metric-overview",
            str(combined_dir / "metric_overview.json"),
            "--combined-metrics",
            str(combined_dir / "aggregated_metrics.json"),
            "--submission-overview",
            str(combined_dir / "submission_overview.json"),
            "--web-output-dir",
            str(web_output_dir),
            "--enterprise-output-dir",
            str(enterprise_output_dir),
        ]
    )
    args.command_args = []
    receipt = module.build_receipt_from_artifacts(
        args=args,
        data_root=data_root,
        output_dir=tmp_path / "receipt",
    )

    assert receipt["source_runs"]["integrity_complete"] is False
    assert (
        receipt["source_runs"]["domains"]["web"]["provider_usage"]["reader"]["invalid_line_count"]
        == 1
    )
    source_check = next(check for check in receipt["checks"] if check["name"] == "source runs")
    assert source_check["status"] == "FAIL"


def test_longmemeval_v2_receipt_rejects_foreign_provider_usage(tmp_path: Path) -> None:
    module = _load_runner_module()
    data_root = tmp_path / "data"
    web_output_dir = tmp_path / "runs" / "web"
    enterprise_output_dir = tmp_path / "runs" / "enterprise"
    combined_dir = tmp_path / "combined"
    official_repo = _write_official_repo(tmp_path / "official")
    _write_dataset(data_root)
    _write_official_outputs(web_output_dir, domain="web")
    _write_official_outputs(enterprise_output_dir, domain="enterprise")
    _write_combined_outputs(combined_dir)
    with (enterprise_output_dir / "provider_usage" / "reader.jsonl").open(
        "a", encoding="utf-8"
    ) as handle:
        handle.write(
            "\n"
            + json.dumps(
                {
                    "run_id": "cached-foreign-run",
                    "role": "reader",
                    "usage": {"total_tokens": 10, "cost_usd": 99.0},
                }
            )
            + "\n"
        )

    args = module.parse_args(
        [
            "--data-root",
            str(data_root),
            "--domain",
            "combined",
            "--output-dir",
            str(tmp_path / "receipt"),
            "--official-repo",
            str(official_repo),
            "--receipt-only",
            "--metric-overview",
            str(combined_dir / "metric_overview.json"),
            "--combined-metrics",
            str(combined_dir / "aggregated_metrics.json"),
            "--submission-overview",
            str(combined_dir / "submission_overview.json"),
            "--web-output-dir",
            str(web_output_dir),
            "--enterprise-output-dir",
            str(enterprise_output_dir),
        ]
    )
    args.command_args = []
    receipt = module.build_receipt_from_artifacts(
        args=args,
        data_root=data_root,
        output_dir=tmp_path / "receipt",
    )

    usage = receipt["source_runs"]["domains"]["enterprise"]["provider_usage"]["reader"]
    assert receipt["source_runs"]["integrity_complete"] is False
    assert usage["event_count"] == EXPECTED_DOMAIN_READER_REQUESTS
    assert usage["foreign_event_count"] == 1
    assert usage["run_ids"] == ["cached-foreign-run", "usage-enterprise"]
    assert receipt["accounting"]["reader"]["provider_reported_cost_usd"] == pytest.approx(0.06)
    assert receipt["accounting"]["reader"]["tracking_complete"] is False
    source_check = next(check for check in receipt["checks"] if check["name"] == "source runs")
    assert source_check["status"] == "FAIL"


def test_longmemeval_v2_receipt_marks_usage_unattributable_without_plan_run_id(
    tmp_path: Path,
) -> None:
    module = _load_runner_module()
    data_root = tmp_path / "data"
    web_output_dir = tmp_path / "runs" / "web"
    enterprise_output_dir = tmp_path / "runs" / "enterprise"
    combined_dir = tmp_path / "combined"
    official_repo = _write_official_repo(tmp_path / "official")
    _write_dataset(data_root)
    _write_official_outputs(web_output_dir, domain="web")
    _write_official_outputs(enterprise_output_dir, domain="enterprise")
    _write_combined_outputs(combined_dir)
    (enterprise_output_dir / "longmemeval_v2_official_plan.json").write_text(
        json.dumps({"domain": "enterprise"}),
        encoding="utf-8",
    )

    args = module.parse_args(
        [
            "--data-root",
            str(data_root),
            "--domain",
            "combined",
            "--output-dir",
            str(tmp_path / "receipt"),
            "--official-repo",
            str(official_repo),
            "--receipt-only",
            "--metric-overview",
            str(combined_dir / "metric_overview.json"),
            "--combined-metrics",
            str(combined_dir / "aggregated_metrics.json"),
            "--submission-overview",
            str(combined_dir / "submission_overview.json"),
            "--web-output-dir",
            str(web_output_dir),
            "--enterprise-output-dir",
            str(enterprise_output_dir),
        ]
    )
    args.command_args = []
    receipt = module.build_receipt_from_artifacts(
        args=args,
        data_root=data_root,
        output_dir=tmp_path / "receipt",
    )

    usage = receipt["source_runs"]["domains"]["enterprise"]["provider_usage"]["reader"]
    assert receipt["source_runs"]["integrity_complete"] is False
    assert usage["event_count"] == 0
    assert usage["foreign_event_count"] == EXPECTED_DOMAIN_READER_REQUESTS
    assert usage["expected_run_id"] is None
    assert receipt["accounting"]["reader"]["requests"] == EXPECTED_DOMAIN_READER_REQUESTS
    assert receipt["accounting"]["reader"]["provider_reported_cost_usd"] == pytest.approx(0.03)
    assert receipt["accounting"]["reader"]["tracking_complete"] is False

    unstamped_usage_path = enterprise_output_dir / "provider_usage" / "reader.jsonl"
    unstamped_usage_path.write_text(
        json.dumps({"role": "reader", "usage": {"cost_usd": 42.0}}) + "\n",
        encoding="utf-8",
    )
    unattributable = module._load_usage_log(
        unstamped_usage_path,
        role="reader",
        expected_run_id=None,
        filter_to_expected_run=True,
    )
    assert unattributable["events"] == []
    assert unattributable["foreign_event_count"] == 1


def test_provider_usage_run_id_is_unique_per_invocation(tmp_path: Path) -> None:
    module = _load_runner_module()
    data_root = str(tmp_path / "data")

    first = module.parse_args(
        [
            "--data-root",
            data_root,
            "--domain",
            "web",
            "--output-dir",
            str(tmp_path / "one"),
        ]
    )
    second = module.parse_args(
        [
            "--data-root",
            data_root,
            "--domain",
            "web",
            "--output-dir",
            str(tmp_path / "two"),
        ]
    )

    assert first.provider_usage_run_id.startswith("lme-v2-usage-")
    assert second.provider_usage_run_id.startswith("lme-v2-usage-")
    assert first.provider_usage_run_id != second.provider_usage_run_id


def test_provider_accounting_rejects_empty_usage_log(tmp_path: Path) -> None:
    module = _load_runner_module()
    usage_path = tmp_path / "reader.jsonl"
    usage_path.touch()

    accounting = module._provider_accounting(
        [
            {
                "reader_usage_path": usage_path,
                "reader_usage_invalid_lines": 0,
                "reader_usage_foreign_event_count": 0,
                "reader_usage_events": [],
                "expected_usage_run_id": "usage-current",
            }
        ],
        role="reader",
        fallback_input_tokens=0.0,
        fallback_output_tokens=0.0,
    )

    assert accounting["tracking_complete"] is False
    assert accounting["cost_coverage_complete"] is False


def test_planner_accounting_requires_complete_accurate_query_usage() -> None:
    module = _load_runner_module()
    source_run = {
        "memory_config": {
            "memory_params": {
                "retrieval_mode": "accurate",
            }
        },
        "per_question_rows": [
            {
                "memory_post_query_metadata": {
                    "search_metadata": {
                        "planner_status": "success",
                        "planner_usage": {
                            "provider": "openai",
                            "model": "gpt-5.4-nano",
                            "requests": 1,
                            "input_tokens": 40,
                            "output_tokens": 10,
                            "total_tokens": 50,
                            "cost_usd": 0.00001,
                            "cost_complete": True,
                        },
                    }
                }
            },
            {
                "memory_post_query_metadata": {
                    "search_metadata": {
                        "planner_status": "success",
                        "planner_usage": {
                            "provider": "openai",
                            "model": "gpt-5.4-nano",
                            "requests": 1,
                            "input_tokens": 44,
                            "output_tokens": 12,
                            "total_tokens": 56,
                            "cost_usd": 0.00002,
                            "cost_complete": True,
                        },
                    }
                }
            },
        ],
    }

    accounting = module._planner_accounting([source_run])

    assert accounting["requests"] == EXPECTED_PLANNER_REQUESTS
    assert accounting["estimated_input_tokens"] == EXPECTED_PLANNER_INPUT_TOKENS
    assert accounting["estimated_output_tokens"] == EXPECTED_PLANNER_OUTPUT_TOKENS
    assert accounting["provider_reported_cost_usd"] == pytest.approx(0.00003)
    assert accounting["recorded_question_count"] == EXPECTED_PLANNER_REQUESTS
    assert accounting["tracking_complete"] is True
    assert accounting["cost_coverage_complete"] is True


def test_planner_accounting_accepts_zero_cost_deterministic_refinement() -> None:
    module = _load_runner_module()
    source_run = {
        "memory_config": {"memory_params": {"retrieval_mode": "accurate"}},
        "per_question_rows": [
            {
                "memory_post_query_metadata": {
                    "search_metadata": {
                        "planner_status": "success",
                        "planner_usage": {
                            "provider": "deterministic",
                            "model": "pseudo_relevance_feedback_v2",
                            "requests": 0,
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "total_tokens": 0,
                            "cost_usd": 0.0,
                            "cost_complete": True,
                        },
                    }
                }
            }
        ],
    }

    accounting = module._planner_accounting([source_run])

    assert accounting["requests"] == 0
    assert accounting["providers"] == ["deterministic"]
    assert accounting["models"] == ["pseudo_relevance_feedback_v2"]
    assert accounting["recorded_question_count"] == 1
    assert accounting["tracking_complete"] is True
    assert accounting["cost_coverage_complete"] is True


def test_planner_accounting_accepts_complete_partial_refinement_usage() -> None:
    module = _load_runner_module()
    source_run = {
        "memory_config": {"memory_params": {"retrieval_mode": "accurate"}},
        "per_question_rows": [
            {
                "memory_post_query_metadata": {
                    "search_metadata": {
                        "planner_status": "partial",
                        "planner_usage": {
                            "provider": "deterministic",
                            "model": "pseudo_relevance_feedback_v2",
                            "requests": 0,
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "total_tokens": 0,
                            "cost_usd": 0.0,
                            "cost_complete": True,
                        },
                    }
                }
            }
        ],
    }

    accounting = module._planner_accounting([source_run])

    assert accounting["recorded_question_count"] == 1
    assert accounting["tracking_complete"] is True
    assert accounting["cost_coverage_complete"] is True


def test_planner_accounting_reports_missing_accurate_query_usage() -> None:
    module = _load_runner_module()
    accounting = module._planner_accounting(
        [
            {
                "memory_config": {"memory_params": {"retrieval_mode": "accurate"}},
                "per_question_rows": [
                    {
                        "memory_post_query_metadata": {
                            "search_metadata": {"planner_status": "fallback"}
                        }
                    }
                ],
            }
        ]
    )

    assert accounting["expected_question_count"] == 1
    assert accounting["recorded_question_count"] == 0
    assert accounting["tracking_complete"] is False
    assert accounting["cost_coverage_complete"] is False


def test_planner_accounting_uses_per_question_mode_after_runtime_change() -> None:
    module = _load_runner_module()
    accounting = module._planner_accounting(
        [
            {
                "memory_config": {"memory_params": {"retrieval_mode": "fast"}},
                "per_question_rows": [
                    {
                        "memory_post_query_metadata": {
                            "retrieval_mode": "accurate",
                            "search_metadata": {
                                "planner_status": "success",
                                "planner_usage": {
                                    "provider": "openai",
                                    "model": "gpt-5.4-nano",
                                    "requests": 1,
                                    "input_tokens": 40,
                                    "output_tokens": 10,
                                    "total_tokens": 50,
                                    "cost_usd": 0.00001,
                                    "cost_complete": True,
                                },
                            },
                        }
                    },
                    {
                        "memory_post_query_metadata": {
                            "retrieval_mode": "fast",
                            "search_metadata": {"planner_status": "not_requested"},
                        }
                    },
                ],
            }
        ]
    )

    assert accounting["expected_question_count"] == 1
    assert accounting["recorded_question_count"] == 1
    assert accounting["requests"] == 1
    assert accounting["tracking_complete"] is True


def test_usage_log_accounts_for_all_output_attempts(tmp_path: Path) -> None:
    module = _load_runner_module()
    path = tmp_path / "reader.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(
                {
                    "run_id": run_id,
                    "role": "reader",
                    "usage": {"total_tokens": 10},
                }
            )
            for run_id in ("attempt-one", "attempt-two")
        )
        + "\n",
        encoding="utf-8",
    )

    usage = module._load_usage_log(path, role="reader")

    assert len(usage["events"]) == EXPECTED_USAGE_ATTEMPTS
    assert usage["run_ids"] == ["attempt-one", "attempt-two"]


def test_longmemeval_v2_receipt_redacts_sensitive_command_args() -> None:
    module = _load_runner_module()

    assert module._redacted_command_args(
        [
            "--api-token",
            "sibyl-secret-token",
            "--api-credentials-file",
            "credentials.json",
            "--password=hunter2",
            "--domain",
            "web",
        ]
    ) == [
        "--api-token",
        "<redacted>",
        "--api-credentials-file",
        "<redacted>",
        "--password=<redacted>",
        "--domain",
        "web",
    ]


@pytest.mark.asyncio
async def test_provider_usage_proxies_persist_successful_responses(tmp_path: Path) -> None:
    module = _load_provider_usage_module()
    response = SimpleNamespace(
        id="response-1",
        model="provider/model-v1",
        usage=SimpleNamespace(
            prompt_tokens=11,
            completion_tokens=7,
            total_tokens=18,
            cost=0.0042,
            completion_tokens_details={"reasoning_tokens": 3},
        ),
    )

    class AsyncCompletions:
        async def create(self, **kwargs: object) -> object:
            assert kwargs["model"] == "requested/model"
            return response

    class SyncCompletions:
        def create(self, **kwargs: object) -> object:
            assert kwargs["model"] == "judge/model"
            return response

    async_path = tmp_path / "reader.jsonl"
    sync_path = tmp_path / "judge.jsonl"
    async_recorder = module.ProviderUsageRecorder(async_path, run_id="run-1", role="reader")
    sync_recorder = module.ProviderUsageRecorder(sync_path, run_id="run-1", role="judge")
    async_client = module.AsyncUsageTrackingClient(
        SimpleNamespace(chat=SimpleNamespace(completions=AsyncCompletions())),
        async_recorder,
    )
    sync_client = module.SyncUsageTrackingClient(
        SimpleNamespace(chat=SimpleNamespace(completions=SyncCompletions())),
        sync_recorder,
    )

    assert await async_client.chat.completions.create(model="requested/model") is response
    assert sync_client.chat.completions.create(model="judge/model") is response

    reader_event = json.loads(async_path.read_text(encoding="utf-8"))
    judge_event = json.loads(sync_path.read_text(encoding="utf-8"))
    assert reader_event["requested_model"] == "requested/model"
    assert reader_event["provider_model"] == "provider/model-v1"
    assert reader_event["usage"] == {
        "completion_tokens": 7,
        "completion_tokens_details": {"reasoning_tokens": 3},
        "cost": 0.0042,
        "cost_usd": 0.0042,
        "prompt_tokens": 11,
        "total_tokens": 18,
    }
    assert judge_event["role"] == "judge"


@pytest.mark.asyncio
async def test_official_runner_retries_transient_reader_parse_failure(capsys) -> None:
    module = _load_runner_module()
    harness = cast(_ReaderHarness, ModuleType("harness"))
    attempts = 0

    async def flaky_reader(
        client: object,
        args: object,
        messages: list[dict[str, object]],
    ) -> tuple[str, dict[str, int]]:
        nonlocal attempts
        del client, args, messages
        attempts += 1
        if attempts == 1:
            raise json.JSONDecodeError("Expecting value", "\n", 0)
        return "boxed answer", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

    harness.call_reader_model_async = flaky_reader
    module.install_reader_retry(
        harness,
        args=SimpleNamespace(
            reader_retry_attempts=3,
            reader_retry_base_delay_seconds=0.0,
            reader_retry_max_delay_seconds=0.0,
        ),
    )

    result = await harness.call_reader_model_async(None, SimpleNamespace(), [])

    assert result == (
        "boxed answer",
        {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    )
    assert attempts == EXPECTED_TRANSIENT_READER_ATTEMPTS
    assert "retrying attempt 2/3" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_official_runner_does_not_retry_non_transient_reader_failure() -> None:
    module = _load_runner_module()
    harness = cast(_ReaderHarness, ModuleType("harness"))
    attempts = 0

    async def broken_reader(
        client: object,
        args: object,
        messages: list[dict[str, object]],
    ) -> tuple[str, dict[str, int]]:
        nonlocal attempts
        del client, args, messages
        attempts += 1
        raise ValueError("bad request shape")

    harness.call_reader_model_async = broken_reader
    module.install_reader_retry(
        harness,
        args=SimpleNamespace(
            reader_retry_attempts=3,
            reader_retry_base_delay_seconds=0.0,
            reader_retry_max_delay_seconds=0.0,
        ),
    )

    with pytest.raises(ValueError, match="bad request shape"):
        await harness.call_reader_model_async(None, SimpleNamespace(), [])

    assert attempts == 1


@pytest.mark.parametrize(
    "error_message",
    [
        "Could not parse evaluator binary judgement: '\\boxed{0}'",
        "Empty judgement response from evaluator model.",
        "Evaluator model returned empty response content.",
    ],
)
def test_official_runner_retries_malformed_evaluator_judgement(
    capsys,
    error_message: str,
) -> None:
    module = _load_runner_module()
    metrics = cast(_EvaluatorMetrics, ModuleType("qa_eval_metrics"))
    attempts = 0

    def flaky_evaluator(*args: object, **kwargs: object) -> bool:
        nonlocal attempts
        del args, kwargs
        attempts += 1
        if attempts == 1:
            raise ValueError(error_message)
        return True

    def stable_evaluator(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        return True

    metrics.llm_abstention_checker = flaky_evaluator
    metrics.llm_gotchas_checker = stable_evaluator
    module.install_evaluator_retry(
        metrics,
        args=SimpleNamespace(evaluator_retry_attempts=EXPECTED_EVALUATOR_RETRY_ATTEMPTS),
    )

    assert metrics.llm_abstention_checker("prediction", "answer") is True
    assert attempts == EXPECTED_TRANSIENT_EVALUATOR_ATTEMPTS
    assert "retrying evaluator attempt 2/3" in capsys.readouterr().err


def test_official_runner_raises_after_malformed_evaluator_retries_exhausted() -> None:
    module = _load_runner_module()
    metrics = cast(_EvaluatorMetrics, ModuleType("qa_eval_metrics"))
    attempts = 0

    def broken_evaluator(*args: object, **kwargs: object) -> bool:
        nonlocal attempts
        del args, kwargs
        attempts += 1
        raise ValueError("Could not parse evaluator binary judgement: '\\boxed{0}'")

    metrics.llm_abstention_checker = broken_evaluator
    metrics.llm_gotchas_checker = broken_evaluator
    module.install_evaluator_retry(
        metrics,
        args=SimpleNamespace(evaluator_retry_attempts=EXPECTED_EVALUATOR_RETRY_ATTEMPTS),
    )

    with pytest.raises(ValueError, match="Could not parse evaluator binary judgement"):
        metrics.llm_abstention_checker("prediction", "answer")

    assert attempts == EXPECTED_EVALUATOR_RETRY_ATTEMPTS


def test_official_runner_does_not_retry_other_evaluator_value_error() -> None:
    module = _load_runner_module()
    metrics = cast(_EvaluatorMetrics, ModuleType("qa_eval_metrics"))
    attempts = 0

    def stable_evaluator(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        return True

    def broken_evaluator(*args: object, **kwargs: object) -> bool:
        nonlocal attempts
        del args, kwargs
        attempts += 1
        raise ValueError("bad evaluator configuration")

    metrics.llm_abstention_checker = stable_evaluator
    metrics.llm_gotchas_checker = broken_evaluator
    module.install_evaluator_retry(
        metrics,
        args=SimpleNamespace(evaluator_retry_attempts=EXPECTED_EVALUATOR_RETRY_ATTEMPTS),
    )

    with pytest.raises(ValueError, match="bad evaluator configuration"):
        metrics.llm_gotchas_checker("prediction", "answer")

    assert attempts == 1


def test_official_runner_finalizes_memory_before_prompt_building() -> None:
    module = _load_runner_module()
    calls: list[str] = []

    class FakeMemory:
        def finalize_ingest(self) -> None:
            calls.append("finalize")

    def build_prompt_row(*args: object, **kwargs: object) -> dict[str, bool]:
        del args, kwargs
        calls.append("build")
        return {"ok": True}

    harness = SimpleNamespace(build_prompt_row=build_prompt_row)
    memory = FakeMemory()
    module.install_memory_finalize(harness)

    assert harness.build_prompt_row({}, memory=memory) == {"ok": True}
    assert calls == ["finalize", "build"]


def test_sibyl_memory_request_retries_transient_timeout(capsys) -> None:
    module = _load_memory_module()
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})
    calls = 0

    class FakeClient:
        def request(
            self,
            method: str,
            path: str,
            *,
            json: dict[str, object] | None = None,
            params: dict[str, object] | None = None,
        ) -> httpx.Response:
            nonlocal calls
            del method, path, json, params
            calls += 1
            if calls == 1:
                raise httpx.ReadTimeout("timed out")
            return httpx.Response(201, json={"created": 1})

    memory.api_retry_attempts = EXPECTED_MEMORY_API_RETRY_CALLS
    memory.api_retry_base_delay_seconds = 0.0
    memory.api_retry_max_delay_seconds = 0.0
    memory._client = FakeClient()
    memory._refresh_token = ""

    assert memory._request_json("POST", "/entities/bulk", json={}) == {"created": 1}
    assert calls == EXPECTED_MEMORY_API_RETRY_CALLS
    assert "retrying attempt 2/2" in capsys.readouterr().err


def test_sibyl_memory_refresh_persists_rotated_credentials_bundle(tmp_path: Path) -> None:
    module = _load_memory_module()
    credentials_path = tmp_path / "credentials.json"
    credentials_path.write_text(
        json.dumps(
            {
                "access_token": TEST_CREDENTIAL,
                "refresh_token": TEST_CREDENTIAL,
                "organization": {"id": "org-test"},
            }
        ),
        encoding="utf-8",
    )
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})

    class FakeClient:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}

        def post(self, path: str, *, json: dict[str, object]) -> httpx.Response:
            assert path == "/auth/refresh"
            assert json == {"refresh_token": TEST_CREDENTIAL}
            return httpx.Response(
                200,
                json={
                    "access_token": ROTATED_CREDENTIAL,
                    "refresh_token": ROTATED_CREDENTIAL,
                    "expires_in": 900,
                },
            )

    memory._client = FakeClient()
    memory._refresh_token = TEST_CREDENTIAL
    memory._api_credentials_path = credentials_path
    memory._cli_auth = {}

    assert memory._refresh_access_token() is True
    assert memory._refresh_token == ROTATED_CREDENTIAL
    assert memory._client.headers["Authorization"] == f"Bearer {ROTATED_CREDENTIAL}"
    assert json.loads(credentials_path.read_text(encoding="utf-8")) == {
        "access_token": ROTATED_CREDENTIAL,
        "refresh_token": ROTATED_CREDENTIAL,
        "expires_in": 900,
        "organization": {"id": "org-test"},
    }
    assert credentials_path.stat().st_mode & 0o777 == EXPECTED_CREDENTIAL_FILE_MODE


def test_sibyl_memory_auth_loads_refreshable_credentials_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_memory_module()
    credentials_path = tmp_path / "credentials.json"
    credentials_path.write_text(
        json.dumps(
            {
                "access_token": TEST_CREDENTIAL,
                "refresh_token": TEST_CREDENTIAL,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SIBYL_API_CREDENTIALS_FILE", str(credentials_path))
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})
    memory.api_url = "http://127.0.0.1:3434/api"
    memory.allow_localhost = True
    memory._client = SimpleNamespace(headers={})

    memory._authenticate({})

    assert memory._api_credentials_path == credentials_path
    assert memory._refresh_token == TEST_CREDENTIAL
    assert memory._client.headers["Authorization"] == f"Bearer {TEST_CREDENTIAL}"


def test_sibyl_memory_payloads_chunk_trajectory_by_state() -> None:
    module = _load_memory_module()

    payloads = module.build_entity_payloads_for_trajectory(
        _trajectory("t1", tree="button " + ("Priority " * 80)),
        project_id="project_lme",
        run_id="run_lme",
        content_max_chars=TEST_CONTENT_MAX_CHARS,
        include_screenshot_refs=True,
    )

    assert len(payloads) > 1
    assert {payload["entity_type"] for payload in payloads} == {"session"}
    assert all(payload["skip_conflicts"] is True for payload in payloads)
    assert all(len(str(payload["content"])) <= TEST_CONTENT_MAX_CHARS for payload in payloads)
    assert payloads[0]["metadata"]["project_id"] == "project_lme"
    assert payloads[0]["metadata"]["source_id"] == "longmemeval-v2:run_lme:t1"
    assert payloads[0]["metadata"]["longmemeval_v2_trajectory_id"] == "t1"
    assert all(
        payload["metadata"]["longmemeval_v2_state_indices"]
        == [payload["metadata"]["longmemeval_v2_state_index"]]
        for payload in payloads
    )
    assert all(
        f"State {payload['metadata']['longmemeval_v2_state_index']}" in str(payload["content"])
        for payload in payloads
    )
    assert all(
        payload["metadata"]["entity_content_projection_policy"] == "v2-identity-state-chunks-v2"
        for payload in payloads
    )
    assert any("Screenshot:" in str(payload["content"]) for payload in payloads)


def test_sibyl_memory_trajectory_chunking_preserves_legacy_grouping() -> None:
    module = _load_memory_module()
    payloads = module.build_entity_payloads_for_trajectory(
        _trajectory("t1"),
        project_id="project_lme",
        run_id="run_lme",
        content_max_chars=2_000,
        chunking_mode="trajectory",
    )

    assert len(payloads) == 1
    assert payloads[0]["metadata"]["longmemeval_v2_state_indices"] == [0, 1]
    assert payloads[0]["metadata"]["longmemeval_v2_chunking_mode"] == "trajectory"
    assert (
        payloads[0]["metadata"]["entity_content_projection_policy"]
        == "v2-trajectory-state-chunks-v1"
    )


def test_sibyl_memory_oversized_state_parts_repeat_identity() -> None:
    module = _load_memory_module()

    payloads = module.build_entity_payloads_for_trajectory(
        _trajectory("t1", tree="button " + ("Priority " * 300)),
        project_id="project_lme",
        run_id="run_lme",
        content_max_chars=TEST_CONTENT_MAX_CHARS,
    )
    state_zero = [
        payload for payload in payloads if payload["metadata"]["longmemeval_v2_state_index"] == 0
    ]

    assert len(state_zero) > 1
    assert all("Trajectory: t1" in str(payload["content"]) for payload in state_zero)
    assert all(
        "State 0\nURL: https://example.test/start" in str(payload["content"])
        for payload in state_zero
    )
    assert [
        payload["metadata"]["longmemeval_v2_state_part_index"] for payload in state_zero
    ] == list(range(len(state_zero)))
    assert {payload["metadata"]["longmemeval_v2_state_part_count"] for payload in state_zero} == {
        len(state_zero)
    }


def test_sibyl_memory_oversized_blocks_split_on_line_boundaries() -> None:
    module = _load_memory_module()
    header = "Trajectory: t1"
    block = "alpha\nbeta\ngamma\ndelta\n"

    chunks = module._split_oversized_block(header, block, max_chars=len(header) + 14)
    prefix = f"{header}\n\n"
    bodies = [chunk.removeprefix(prefix) for chunk in chunks]

    assert "".join(bodies) == block
    assert all(body.endswith("\n") for body in bodies)


def test_sibyl_memory_context_formats_retrieved_content() -> None:
    module = _load_memory_module()
    trace_content = "State 3\nThe priority filter was selected."

    context = module.search_results_to_memory_context(
        [
            {
                "content": "The priority filter was selected before opening incidents.",
                "score": 0.875,
                "metadata": {
                    "longmemeval_v2_trajectory_id": "t1",
                    "longmemeval_v2_chunk_index": 0,
                },
            }
        ],
        max_items=1,
        max_chars_per_item=24,
    )

    assert context == [
        {
            "type": "text",
            "value": (
                "Retrieved evidence rank 1\n"
                "Retrieval: search\n"
                "Trajectory: t1\n"
                "Chunk: 0\n\n"
                "The priority filter was "
            ),
        }
    ]
    assert module.build_retrieval_trace(
        [
            {
                "id": "entity:t1-0",
                "type": "session",
                "content": trace_content,
                "score": 0.875,
                "result_origin": "graph",
                "metadata": {
                    "longmemeval_v2_trajectory_id": "t1",
                    "longmemeval_v2_chunk_index": 0,
                    "longmemeval_v2_chunk_count": 2,
                    "source_support_entity_id": "session-source",
                    "source_support_operational_source_id": "longmemeval-v2:run:t1",
                    "source_support_state_indices": [2],
                    "source_support_states": [
                        {
                            "entity_id": "session-source",
                            "operational_source_id": "longmemeval-v2:run:t1",
                            "trajectory_id": "t1",
                            "state_index": 2,
                        }
                    ],
                },
            }
        ],
        max_items=1,
        max_chars_per_item=24,
    ) == [
        {
            "rank": 1,
            "entity_id": "entity:t1-0",
            "entity_type": "session",
            "trajectory_id": "t1",
            "chunk_index": 0,
            "chunk_count": 2,
            "state_indices": [3],
            "source_support_entity_id": "session-source",
            "source_support_operational_source_id": "longmemeval-v2:run:t1",
            "source_support_state_indices": [2],
            "source_support_states": [
                {
                    "entity_id": "session-source",
                    "operational_source_id": "longmemeval-v2:run:t1",
                    "trajectory_id": "t1",
                    "state_index": 2,
                }
            ],
            "score": 0.875,
            "selection_pool": None,
            "selection_pool_rank": None,
            "selection_score": None,
            "selection_overlap": None,
            "content_chars": len(trace_content),
            "exposed_chars": 24,
            "result_origin": "graph",
            "selection_origin": "search",
            "search_rank": None,
            "trajectory_refined_from_chunk": None,
            "state_part_of_search_rank": None,
            "state_part_refined_from_chunk": None,
            "neighbor_of_search_rank": None,
            "neighbor_distance": None,
        }
    ]


def test_sibyl_memory_context_budget_fairly_preserves_selected_evidence() -> None:
    module = _load_memory_module()
    results = [
        {
            "id": f"entity-{index}",
            "content": f"evidence-{index} " + ("x" * 1_000),
            "metadata": {"longmemeval_v2_trajectory_id": f"t{index}"},
        }
        for index in range(EXPECTED_CONTEXT_BUDGET_ITEMS)
    ]

    context, metadata = module.render_memory_context(
        results,
        max_items=EXPECTED_CONTEXT_BUDGET_ITEMS,
        max_chars_per_item=1_000,
        max_total_chars=TEST_CONTEXT_TOTAL_CHARS,
    )

    assert len(context) == EXPECTED_CONTEXT_BUDGET_ITEMS
    assert sum(len(item["value"]) for item in context) <= TEST_CONTEXT_TOTAL_CHARS
    assert all(
        f"evidence-{index}" in context[index]["value"]
        for index in range(EXPECTED_CONTEXT_BUDGET_ITEMS)
    )
    assert metadata["rendered_item_count"] == EXPECTED_CONTEXT_BUDGET_ITEMS
    assert metadata["dropped_item_count"] == 0
    assert metadata["truncated_item_count"] == EXPECTED_CONTEXT_BUDGET_ITEMS
    assert metadata["binding"] is True
    assert all("Score:" not in item["value"] for item in context)
    trace = module.build_retrieval_trace(
        results,
        max_items=EXPECTED_CONTEXT_BUDGET_ITEMS,
        max_chars_per_item=1_000,
        context_budget=metadata,
    )
    assert [item["exposed_chars"] for item in trace] == [
        item["exposed_content_chars"] for item in metadata["items"]
    ]


def test_sibyl_memory_context_selects_late_query_evidence_windows() -> None:
    module = _load_memory_module()
    content = "".join(
        [
            "Trajectory: t1\n",
            "Domain: enterprise\n",
            "Goal: review policy\n",
            "\n",
            "State 7\n",
            "URL: https://example.test/policy\n",
            "Action: inspect policy\n",
            "Accessibility tree:\n",
            *(f"\tStaticText 'irrelevant row {index}'\n" for index in range(45)),
            "\tStaticText 'Refund window: 30 days'\n",
            *(f"\tStaticText 'middle row {index}'\n" for index in range(30)),
            "\tStaticText 'Shipping carrier: Northern Express'\n",
            *(f"\tStaticText 'tail row {index}'\n" for index in range(20)),
        ]
    )
    result = {
        "id": "entity-t1",
        "type": "session",
        "content": content,
        "metadata": {
            "longmemeval_v2_trajectory_id": "t1",
            "longmemeval_v2_chunk_index": 0,
        },
    }
    max_content_chars = 900
    header_chars = len(module._memory_context_header(1, result)) + 2

    context, metadata = module.render_memory_context(
        [result],
        query="Which shipping carrier handled it and how long was the refund window?",
        max_items=1,
        max_chars_per_item=max_content_chars,
        max_total_chars=header_chars + max_content_chars,
    )

    rendered = context[0]["value"]
    compaction = metadata["items"][0]["compaction"]
    assert "Refund window: 30 days" in rendered
    assert "Shipping carrier: Northern Express" in rendered
    assert "State 7" in rendered
    assert "[Source slice: lines" in rendered
    assert "[Omitted source lines" in rendered
    assert compaction["mode"] == "query_slices"
    assert compaction["ranking_applied"] is True
    assert compaction["selected_window_count"] == EXPECTED_SELECTED_WINDOW_COUNT
    assert len(rendered) <= header_chars + max_content_chars
    trace = module.build_retrieval_trace(
        [result],
        max_items=1,
        max_chars_per_item=max_content_chars,
        context_budget=metadata,
    )
    assert trace[0]["content_compaction"] == compaction


def test_sibyl_memory_context_reserves_structured_option_evidence() -> None:
    module = _load_memory_module()
    query = (
        'Open the "Filters" dropdown, excluding "Edit personal filters" and '
        '"-- None --". Which option labels contain "Incident"?'
    )
    content = "".join(
        [
            "Trajectory: t1\n",
            "Domain: enterprise\n",
            "\n",
            "State 9\n",
            "URL: https://example.test/incidents\n",
            "Accessibility tree:\n",
            "\tbutton 'Filters'\n",
            *(f"\tStaticText 'generic incident row {index}'\n" for index in range(60)),
            "\tmenuitem 'Incident Mobile'\n",
            "\tmenuitem 'Incident Portal'\n",
            "\tmenuitem 'My Open Incidents'\n",
            *(f"\tStaticText 'tail row {index}'\n" for index in range(20)),
        ]
    )

    exposed, metadata = module.compact_content_for_query(query, content, max_chars=800)

    assert module._query_focus_phrases(query) == ("Filters", "Incident")
    assert "Incident Mobile" in exposed
    assert "Incident Portal" in exposed
    assert "My Open Incidents" in exposed
    assert metadata["mode"] == "query_slices"
    assert metadata["structured_selected_window_count"] >= 1


def test_sibyl_memory_context_selects_interactive_entries_below_section_heading() -> None:
    module = _load_memory_module()
    query = (
        "On the `Data Management Delete Job` form, what are the two entries under `Related Links`?"
    )
    content = "".join(
        [
            "Trajectory: t1\n",
            "Domain: enterprise\n",
            "\n",
            "State 66\n",
            "URL: https://example.test/delete-job\n",
            "Accessibility tree:\n",
            "\tRootWebArea 'Data Management Delete Job'\n",
            "\tlink 'Back'\n",
            *(f"\tStaticText 'generic form row {index}'\n" for index in range(60)),
            "\tregion 'Related Links'\n",
            "\t\theading 'Related Links'\n",
            "\t\tlist\n",
            "\t\t\tlistitem\n",
            "\t\t\t\tbutton 'Preview Cascade'\n",
            "\t\t\tlistitem\n",
            "\t\t\t\tbutton 'Execute Now'\n",
            *(f"\tStaticText 'tail row {index}'\n" for index in range(20)),
        ]
    )

    exposed, metadata = module.compact_content_for_query(query, content, max_chars=800)

    assert "Preview Cascade" in exposed
    assert "Execute Now" in exposed
    assert metadata["mode"] == "query_slices"
    assert metadata["structured_selected_window_count"] >= 1


def test_sibyl_memory_context_infers_unquoted_choice_section_focus() -> None:
    module = _load_memory_module()
    query = (
        "Compare the `Standard Laptop` and `Sales Laptop` pages. Which optional "
        "software choices appear only on the latter?"
    )
    content = "".join(
        [
            "Trajectory: t1\n",
            "Domain: enterprise\n",
            "\n",
            "State 69\n",
            "URL: https://example.test/sales-laptop\n",
            "Accessibility tree:\n",
            "\tRootWebArea 'Sales Laptop'\n",
            *(f"\tStaticText 'generic product row {index}'\n" for index in range(60)),
            "\theading 'Optional Software'\n",
            "\t\tLayoutTable\n",
            "\t\t\tcheckbox 'Presentation Suite'\n",
            "\t\tLayoutTable\n",
            "\t\t\tcheckbox 'Project Planner'\n",
            "\t\tLayoutTable\n",
            "\t\t\tcheckbox 'Diagram Editor'\n",
            "\t\tLayoutTable\n",
            "\t\t\tcheckbox 'CRM Client'\n",
            "\theading 'Additional Requirements'\n",
            *(f"\tStaticText 'tail row {index}'\n" for index in range(20)),
        ]
    )

    exposed, metadata = module.compact_content_for_query(query, content, max_chars=800)

    assert module._query_focus_phrases(query) == (
        "Standard Laptop",
        "Sales Laptop",
        "optional software",
    )
    assert module._query_ui_roles(query) == ("option", "checkbox", "radio")
    assert "Presentation Suite" in exposed
    assert "CRM Client" in exposed
    assert metadata["structured_selected_window_count"] >= 1


def test_sibyl_memory_context_focuses_terminal_clause_over_quoted_example() -> None:
    module = _load_memory_module()
    query = (
        'The report starts from an "Incident with hashtag" example. '
        "What prefix was used for the inventory/order dashboard report link?"
    )

    assert module._query_focus_phrases(query) == (
        "Incident with hashtag",
        "prefix",
        "used",
        "inventory",
        "order",
        "dashboard",
        "report",
    )


def test_sibyl_memory_context_focuses_each_enumerated_target() -> None:
    module = _load_memory_module()
    query = (
        "In `Personalize List Columns`, look at the default `Selected` pane for "
        "the Assets list, Users list, and Catalog Items list. What is the "
        "bottom-most selected label on each page?"
    )

    assert module._query_focus_phrases(query) == (
        "Personalize List Columns",
        "Selected",
        "is the bottom-most selected",
        "asset",
        "user",
        "catalog",
        "item",
    )
    assert module._query_ui_roles(query) == ("option", "columnheader")


def test_sibyl_memory_context_keeps_labeled_listbox_options() -> None:
    module = _load_memory_module()
    query = (
        "In `Personalize List Columns`, look at the default `Selected` pane for "
        "the Assets list. What is the bottom-most selected label?"
    )
    content = "".join(
        [
            "Trajectory: t1\n",
            "Domain: enterprise\n",
            "\n",
            "State 1\n",
            "URL: https://example.test/assets\n",
            "Accessibility tree:\n",
            "\tRootWebArea 'Assets list'\n",
            *(f"\tStaticText 'generic row {index}'\n" for index in range(50)),
            "\tLabelText ''\n",
            "\t\tStaticText 'Selected'\n",
            "\tlistbox 'Selected'\n",
            "\t\toption 'Alpha'\n",
            "\t\toption 'Beta'\n",
            "\t\toption 'Gamma'\n",
            *(f"\tStaticText 'tail row {index}'\n" for index in range(20)),
        ]
    )

    exposed, metadata = module.compact_content_for_query(query, content, max_chars=800)

    assert "option 'Alpha'" in exposed
    assert "option 'Gamma'" in exposed
    assert metadata["structured_selected_window_count"] >= 1


def test_compact_content_windows_unstructured_content() -> None:
    module = _load_memory_module()
    filler = "".join(f"line {index} filler text about nothing much here\n" for index in range(40))
    tail = "".join(f"tail {index} more filler\n" for index in range(40))
    content = (
        filler + "The secret entry is Launch Dependency Assessment under Related Links\n" + tail
    )

    exposed, metadata = module.compact_content_for_query(
        "What is the entry under Related Links on the Report page?",
        content,
        max_chars=600,
    )

    assert metadata["mode"] == "query_slices"
    assert metadata["stride_window_fallback"] is True
    assert "Launch Dependency Assessment" in exposed
    assert len(exposed) <= 600


def test_compact_content_token_fallback_when_focus_phrases_miss() -> None:
    module = _load_memory_module()
    module_focus = module._query_focus_phrases("Which agent has the highest incident total?")
    content = "".join(
        [
            "State 1\n",
            "Accessibility tree:\n",
            *(f"\tStaticText 'padding row {index}'\n" for index in range(30)),
            "\tStaticText 'incident totals by agent shown below'\n",
            "\tStaticText 'agent Beth Anglin highest total 12'\n",
            "\n",
            "State 2\n",
            "Accessibility tree:\n",
            *(f"\tStaticText 'unrelated row {index}'\n" for index in range(30)),
        ]
    )

    exposed, metadata = module.compact_content_for_query(
        "Which agent has the highest incident total?",
        content,
        max_chars=700,
    )

    assert metadata["mode"] == "query_slices"
    if metadata.get("token_overlap_fallback"):
        assert metadata["ranking_applied"] is True
    assert "highest total 12" in exposed
    assert module_focus is not None


def test_compact_content_zero_overlap_still_prefixes() -> None:
    module = _load_memory_module()
    content = "".join(f"row {index} lorem ipsum dolor\n" for index in range(80))

    exposed, metadata = module.compact_content_for_query(
        "xylophone quandary zeppelin",
        content,
        max_chars=500,
    )

    assert metadata["mode"] == "prefix"
    assert exposed == content[:500]


def test_sibyl_memory_context_keeps_successor_state_after_tail_match() -> None:
    module = _load_memory_module()
    query = (
        'The report starts from an "Incident with hashtag" example. '
        "What prefix was used for the inventory/order dashboard report link?"
    )
    content = "".join(
        [
            "Trajectory: t1\n",
            "Domain: enterprise\n",
            "\n",
            "State 1\n",
            "Accessibility tree:\n",
            *(f"\tStaticText 'generic row {index}'\n" for index in range(40)),
            "\tlink 'Inventory order dashboard report'\n",
            "\n",
            "State 2\n",
            "Accessibility tree:\n",
            "\tStaticText 'Prefix value appears after navigation'\n",
            *(f"\tStaticText 'successor row {index}'\n" for index in range(20)),
        ]
    )

    exposed, metadata = module.compact_content_for_query(query, content, max_chars=900)

    assert "State 2" in exposed
    assert "Prefix value appears after navigation" in exposed
    assert metadata["version"] == "query-aware-source-windows-v5"
    assert metadata["mode"] == "query_slices"


def test_sibyl_memory_structured_section_keeps_successor_state() -> None:
    module = _load_memory_module()
    query = (
        'The report starts from an "Incident with hashtag" example. '
        "What prefix was used for the inventory/order dashboard report link?"
    )
    content = "".join(
        [
            "Trajectory: t1\n",
            "Domain: enterprise\n",
            "\n",
            "State 1\n",
            "Accessibility tree:\n",
            *(f"\tStaticText 'generic row {index}'\n" for index in range(40)),
            "\theading 'Inventory order dashboard report'\n",
            "\t\tlink 'Open report'\n",
            "\n",
            "State 2\n",
            "Accessibility tree:\n",
            "\tStaticText 'Prefix value appears after navigation'\n",
            *(f"\tStaticText 'successor row {index}'\n" for index in range(20)),
        ]
    )

    exposed, metadata = module.compact_content_for_query(query, content, max_chars=900)

    assert metadata["structured_selected_window_count"] >= 1
    assert "State 2" in exposed
    assert "Prefix value appears after navigation" in exposed


def test_sibyl_memory_near_tail_match_crosses_blank_state_separator() -> None:
    module = _load_memory_module()
    query = (
        'The report starts from an "Incident with hashtag" example. '
        "What prefix was used for the inventory/order dashboard report link?"
    )
    content = "".join(
        [
            "Trajectory: t1\n",
            "Domain: enterprise\n",
            "\n",
            "State 1\n",
            "Accessibility tree:\n",
            *(f"\tStaticText 'generic row {index}'\n" for index in range(30)),
            "\tbutton 'Inventory order dashboard report'\n",
            *(f"\tStaticText 'trailing row {index}'\n" for index in range(6)),
            "\n",
            "State 2\n",
            "Accessibility tree:\n",
            "\tStaticText 'Prefix value appears after navigation'\n",
            *(f"\tStaticText 'successor row {index}'\n" for index in range(20)),
        ]
    )

    exposed, _metadata = module.compact_content_for_query(query, content, max_chars=900)

    assert "State 2" in exposed
    assert "Prefix value appears after navigation" in exposed


def test_sibyl_memory_slice_overlap_detects_cross_state_successor_lines() -> None:
    module = _load_memory_module()

    assert module._query_slice_windows_overlap(
        {
            "state_start_line": 10,
            "window_start_line": 20,
            "window_end_line": 40,
            "window_start_char": 200,
            "window_end_char": 400,
        },
        {
            "state_start_line": 35,
            "window_start_line": 35,
            "window_end_line": 50,
            "window_start_char": 350,
            "window_end_char": 500,
        },
    )


def test_sibyl_memory_context_budget_reports_fully_dropped_rows() -> None:
    module = _load_memory_module()
    results = [
        {
            "id": f"entity-{index}",
            "content": f"evidence-{index}",
            "metadata": {"longmemeval_v2_trajectory_id": f"t{index}"},
        }
        for index in range(EXPECTED_CONTEXT_BUDGET_ITEMS)
    ]
    one_item_budget = (
        len(module._memory_context_header(1, results[0])) + 2 + len(str(results[0]["content"]))
    )

    context, metadata = module.render_memory_context(
        results,
        max_items=EXPECTED_CONTEXT_BUDGET_ITEMS,
        max_total_chars=one_item_budget,
    )
    trace = module.build_retrieval_trace(
        results,
        max_items=EXPECTED_CONTEXT_BUDGET_ITEMS,
        context_budget=metadata,
    )

    assert len(context) == 1
    assert [item["dropped"] for item in metadata["items"]] == [False, True, True]
    assert [item["exposed_content_chars"] for item in metadata["items"]] == [
        len("evidence-0"),
        0,
        0,
    ]
    assert metadata["dropped_entity_ids"] == ["entity-1", "entity-2"]
    assert [item["entity_id"] for item in trace] == ["entity-0"]


def test_sibyl_memory_context_budget_redistributes_unused_fair_share() -> None:
    module = _load_memory_module()
    max_total_chars = 600
    long = {
        "id": "long",
        "type": "event",
        "content": "x" * 1_000,
        "_selection_origin": "search",
        "metadata": {},
    }
    short = [
        {
            "id": f"short-{index}",
            "type": "event",
            "content": "x",
            "_selection_origin": "search",
            "metadata": {},
        }
        for index in range(2)
    ]

    _context, metadata = module.render_memory_context(
        [long, *short],
        max_items=3,
        max_chars_per_item=1_000,
        max_total_chars=max_total_chars,
    )
    _reordered_context, reordered_metadata = module.render_memory_context(
        [*short, long],
        max_items=3,
        max_chars_per_item=1_000,
        max_total_chars=max_total_chars,
    )

    exposed = {item["entity_id"]: item["exposed_content_chars"] for item in metadata["items"]}
    reordered_exposed = {
        item["entity_id"]: item["exposed_content_chars"] for item in reordered_metadata["items"]
    }
    assert metadata["rendered_context_chars"] == max_total_chars
    assert reordered_metadata["rendered_context_chars"] == max_total_chars
    assert exposed["long"] == reordered_exposed["long"]
    assert exposed["short-0"] == exposed["short-1"] == 1


def test_sibyl_memory_context_budget_preserves_allocated_whitespace() -> None:
    module = _load_memory_module()
    result = {
        "id": "whitespace",
        "type": "event",
        "content": "a b",
        "_selection_origin": "search",
        "metadata": {},
    }
    max_total_chars = len(module._memory_context_header(1, result)) + 2 + 2

    context, metadata = module.render_memory_context(
        [result],
        max_items=1,
        max_chars_per_item=2,
        max_total_chars=max_total_chars,
    )

    assert context[0]["value"].endswith("a ")
    assert metadata["rendered_context_chars"] == max_total_chars
    assert metadata["items"][0]["exposed_content_chars"] == EXPECTED_WHITESPACE_EXPOSURE_CHARS


def test_sibyl_memory_context_token_count_matches_official_processor_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_memory_module()
    calls: dict[str, object] = {}

    class FakeProcessor:
        def apply_chat_template(
            self,
            messages: list[dict[str, object]],
            *,
            tokenize: bool,
            add_generation_prompt: bool,
        ) -> str:
            calls["messages"] = messages
            calls["template"] = (tokenize, add_generation_prompt)
            return "rendered-context"

        def __call__(self, **kwargs: object) -> dict[str, object]:
            calls["processor"] = kwargs
            return {"input_ids": SimpleNamespace(shape=(1, EXPECTED_CONTEXT_TOKEN_COUNT))}

    processor = FakeProcessor()

    class FakeAutoProcessor:
        @staticmethod
        def from_pretrained(model: str) -> FakeProcessor:
            calls["model"] = model
            return processor

    monkeypatch.setitem(
        sys.modules,
        "transformers",
        SimpleNamespace(AutoProcessor=FakeAutoProcessor),
    )

    token_count = module.count_memory_context_tokens(
        [{"type": "text", "value": "Retrieved evidence"}]
    )

    assert token_count == EXPECTED_CONTEXT_TOKEN_COUNT
    assert calls == {
        "model": "Qwen/Qwen3.5-9B",
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "Retrieved evidence"}],
            }
        ],
        "template": (False, False),
        "processor": {
            "text": "rendered-context",
            "images": None,
            "return_tensors": "pt",
        },
    }


def test_sibyl_memory_assembles_diverse_seeds_with_neighbors() -> None:
    module = _load_memory_module()
    t1_seed = _search_result("t1", chunk_index=1, state_index=1, score=1.0)
    results = [
        t1_seed,
        _search_result("t1", chunk_index=2, state_index=2, score=0.9),
        _search_result("t2", chunk_index=0, state_index=0, score=0.8),
        _search_result("t3", chunk_index=0, state_index=0, score=0.7),
    ]
    catalog = {
        "t1": {
            0: _search_result("t1", chunk_index=0, state_index=0, score=0.0),
            1: t1_seed,
            2: results[1],
        }
    }

    assembled, metadata = module.assemble_context_results(
        results,
        chunk_catalog=catalog,
        max_items=4,
        max_chunks_per_trajectory=2,
        neighbor_stitch_items=1,
        neighbor_stitch_span=1,
    )

    assert [result["metadata"]["longmemeval_v2_trajectory_id"] for result in assembled] == [
        "t1",
        "t2",
        "t3",
        "t1",
    ]
    assert [result["_selection_origin"] for result in assembled] == [
        "search",
        "search",
        "search",
        "neighbor",
    ]
    assert assembled[-1]["metadata"]["longmemeval_v2_chunk_index"] == 0
    assert metadata["selected_search_seed_count"] == len(assembled) - 1
    assert metadata["stitched_neighbor_count"] == 1


def test_sibyl_memory_expansion_candidates_do_not_consume_seed_budget() -> None:
    module = _load_memory_module()
    query = "inventory order dashboard prefix"
    first = _search_result("t1", chunk_index=1, state_index=1, score=1.0)
    neighbor = _search_result("t1", chunk_index=0, state_index=0, score=0.0)
    neighbor["content"] = (
        "Goal: inventory order dashboard prefix\n\n"
        "State 0\nAccessibility tree:\nunrelated neighboring state"
    )
    results = [
        first,
        _search_result("t2", chunk_index=0, state_index=0, score=0.9),
        _search_result("t3", chunk_index=0, state_index=0, score=0.8),
        _search_result("target", chunk_index=0, state_index=0, score=0.7),
    ]
    results[-1]["content"] = query
    candidate_limit = module.context_assembly_candidate_limit(
        max_items=4,
        neighbor_stitch_items=1,
        state_part_completion_items=0,
        has_chunk_catalog=True,
    )

    assembled, metadata = module.assemble_context_results(
        results,
        chunk_catalog={"t1": {0: neighbor, 1: first}},
        max_items=candidate_limit,
        max_chunks_per_trajectory=2,
        neighbor_stitch_items=1,
        neighbor_stitch_span=1,
        query=query,
    )
    selected, composition = module.compile_operational_evidence_set(
        query=query,
        typed_results=[],
        raw_results=assembled,
        max_items=4,
    )

    assert candidate_limit == EXPECTED_ASSEMBLED_RESULT_COUNT
    assert metadata["selected_search_seed_count"] == EXPECTED_ASSEMBLED_SEED_COUNT
    assert metadata["stitched_neighbor_count"] == 1
    assert len(assembled) == EXPECTED_ASSEMBLED_RESULT_COUNT
    assert any(
        result["metadata"]["longmemeval_v2_trajectory_id"] == "target" for result in selected
    )
    assert composition["candidate_count"] == EXPECTED_ASSEMBLED_RESULT_COUNT
    assert composition["selected_raw_support_count"] == 0


def test_sibyl_memory_restores_transport_truncated_search_content() -> None:
    module = _load_memory_module()
    search_result = _search_result("t1", chunk_index=1, state_index=1, score=0.9)
    search_result["id"] = "entity:search-result"
    search_result["content"] = "Trajectory: t1\n\nState 1\ntruncated"
    catalog_result = _search_result("t1", chunk_index=1, state_index=1, score=0.0)
    catalog_result["content"] = (
        "Trajectory: t1\n\nState 1\nAccessibility tree:\n" + "full source evidence " * 100
    )

    assembled, metadata = module.assemble_context_results(
        [search_result],
        chunk_catalog={"t1": {1: catalog_result}},
        max_items=1,
        max_chunks_per_trajectory=1,
        neighbor_stitch_items=0,
        neighbor_stitch_span=0,
    )

    assert assembled[0]["id"] == "entity:search-result"
    assert assembled[0]["score"] == EXPECTED_RESTORED_SCORE
    assert assembled[0]["content"] == catalog_result["content"]
    assert assembled[0]["_source_content_restored"] is True
    assert assembled[0]["_transport_content_chars"] == len(search_result["content"])
    assert assembled[0]["_source_content_chars"] == len(catalog_result["content"])
    assert metadata["restored_search_result_count"] == 1
    assert metadata["restored_transport_content_chars"] == len(search_result["content"])
    assert metadata["restored_source_content_chars"] == len(catalog_result["content"])


def test_sibyl_memory_refines_retrieved_trajectory_to_structured_query_evidence() -> None:
    module = _load_memory_module()
    query = (
        'Open the "Filters" dropdown, excluding "Edit personal filters" and '
        '"-- None --". Which option labels contain "Incident"?'
    )
    seed = _search_result("t1", chunk_index=3, state_index=3, score=0.9)
    seed["content"] = "State 3\nAccessibility tree:\nbutton 'Incidents'"
    excluded = _search_result("t1", chunk_index=2, state_index=2, score=0.0)
    excluded["content"] = "State 2\nAccessibility tree:\noption 'Edit personal filters'"
    target = _search_result("t1", chunk_index=1, state_index=1, score=0.0)
    target["content"] = "\n".join(
        (
            "State 1",
            "Accessibility tree:",
            "menuitem 'Incident Mobile'",
            "menuitem 'Incident Portal'",
            "menuitem 'My Open Incidents'",
        )
    )

    assembled, metadata = module.assemble_context_results(
        [seed],
        chunk_catalog={"t1": {1: target, 2: excluded, 3: seed}},
        max_items=1,
        max_chunks_per_trajectory=1,
        neighbor_stitch_items=0,
        neighbor_stitch_span=0,
        query=query,
    )

    assert [module._result_chunk_key(result) for result in assembled] == [("t1", 1)]
    assert assembled[0]["_selection_origin"] == "trajectory_refinement"
    assert assembled[0]["_trajectory_refined_from_chunk"] == EXPECTED_REFINEMENT_SOURCE_CHUNK
    refinement = metadata["trajectory_refinement"]
    assert refinement["query_focus_phrases"] == ["Filters", "Incident"]
    assert refinement["query_ui_roles"] == ["menuitem", "option"]
    assert len(refinement["replacements"]) == 1
    replacement = refinement["replacements"][0]
    assert replacement["search_rank"] == 1
    assert replacement["trajectory_id"] == "t1"
    assert replacement["from_chunk_key"] == ["t1", 3]
    assert replacement["to_chunk_key"] == ["t1", 1]
    assert replacement["from_signal"] == [0, 0, 0, 0, 0]
    assert replacement["to_signal"][0] > 0
    assert replacement["to_signal"][1] == 1


def test_sibyl_memory_expansion_budget_drops_whole_tail_items() -> None:
    module = _load_memory_module()
    seed = _search_result("t1", chunk_index=1, state_index=1, score=1.0)
    second = _search_result("t2", chunk_index=0, state_index=0, score=0.9)
    third = _search_result("t3", chunk_index=0, state_index=0, score=0.8)
    neighbor = _search_result("t1", chunk_index=0, state_index=0, score=0.0)
    seed["_test_tokens"] = 30
    second["_test_tokens"] = 30
    third["_test_tokens"] = 30
    neighbor["_test_tokens"] = 50

    assembled, metadata = module.assemble_context_results(
        [seed, second, third],
        chunk_catalog={"t1": {0: neighbor, 1: seed}},
        max_items=4,
        max_chunks_per_trajectory=2,
        neighbor_stitch_items=1,
        neighbor_stitch_span=1,
        context_expansion_max_ratio=EXPECTED_CONTEXT_EXPANSION_MAX_RATIO,
        context_token_counter=lambda items: sum(int(item["_test_tokens"]) for item in items),
    )

    assert [module._result_chunk_key(result) for result in assembled] == [
        ("t1", 1),
        ("t2", 0),
        ("t3", 0),
    ]
    assert metadata["context_expansion_budget"] == {
        "enabled": True,
        "max_ratio": EXPECTED_CONTEXT_EXPANSION_MAX_RATIO,
        "base_item_count": 3,
        "unbounded_item_count": 4,
        "final_item_count": 3,
        "base_token_count": 90,
        "max_token_count": 108,
        "unbounded_token_count": 140,
        "final_token_count": 90,
        "dropped_item_count": 1,
        "dropped_chunk_keys": [["t1", 0]],
        "binding": True,
    }
    assert metadata["stitched_neighbor_count"] == 0


def test_sibyl_memory_expansion_budget_rejects_sub_seed_ratio() -> None:
    module = _load_memory_module()

    with pytest.raises(ValueError, match=r"zero or at least 1.0"):
        module.assemble_context_results(
            [],
            chunk_catalog={},
            max_items=1,
            max_chunks_per_trajectory=1,
            neighbor_stitch_items=0,
            neighbor_stitch_span=0,
            context_expansion_max_ratio=0.9,
            context_token_counter=lambda _items: 0,
        )


def test_sibyl_memory_query_ranks_sibling_state_parts() -> None:
    module = _load_memory_module()
    first_seed = _search_result("t1", chunk_index=1, state_index=0, score=1.0)
    second_seed = _search_result("t2", chunk_index=1, state_index=0, score=0.9)
    first_sibling = _search_result("t1", chunk_index=0, state_index=0, score=0.0)
    second_sibling = _search_result("t2", chunk_index=0, state_index=0, score=0.0)
    first_sibling["content"] = "Unrelated account and notification settings."
    second_sibling["content"] = "Deployment Ring: Canary. Pause Rollout is available."
    for result in (first_seed, second_seed, first_sibling, second_sibling):
        result["metadata"]["longmemeval_v2_state_part_count"] = 2
    first_seed["metadata"]["longmemeval_v2_state_part_index"] = 1
    second_seed["metadata"]["longmemeval_v2_state_part_index"] = 1
    first_sibling["metadata"]["longmemeval_v2_state_part_index"] = 0
    second_sibling["metadata"]["longmemeval_v2_state_part_index"] = 0

    assembled, metadata = module.assemble_context_results(
        [first_seed, second_seed],
        chunk_catalog={
            "t1": {0: first_sibling, 1: first_seed},
            "t2": {0: second_sibling, 1: second_seed},
        },
        max_items=3,
        max_chunks_per_trajectory=2,
        neighbor_stitch_items=0,
        neighbor_stitch_span=0,
        query='Which value is shown for "Deployment Ring"?',
        state_part_completion_items=1,
    )

    assert [module._result_chunk_key(result) for result in assembled] == [
        ("t1", 1),
        ("t2", 1),
        ("t2", 0),
    ]
    assert metadata["completed_state_part_count"] == 1
    assert metadata["state_part_completion"] == {
        "enabled": True,
        "candidate_count": 2,
        "ranking_applied": True,
        "admitted_chunk_keys": [["t2", 0]],
    }


def test_sibyl_memory_refines_split_state_without_spending_context_slot() -> None:
    module = _load_memory_module()
    seed = _search_result("t1", chunk_index=0, state_index=4, score=1.0)
    sibling = _search_result("t1", chunk_index=1, state_index=4, score=0.0)
    seed["content"] = "Deployment settings overview."
    sibling["content"] = "Deployment Ring: Canary. Pause Rollout is available."
    seed["metadata"]["longmemeval_v2_state_part_count"] = 2
    seed["metadata"]["longmemeval_v2_state_part_index"] = 0
    sibling["metadata"]["longmemeval_v2_state_part_count"] = 2
    sibling["metadata"]["longmemeval_v2_state_part_index"] = 1

    assembled, metadata = module.assemble_context_results(
        [seed],
        chunk_catalog={"t1": {0: seed, 1: sibling}},
        max_items=1,
        max_chunks_per_trajectory=1,
        neighbor_stitch_items=0,
        neighbor_stitch_span=0,
        query='Which value is shown for "Deployment Ring"?',
        state_part_refinement=True,
    )

    assert [module._result_chunk_key(result) for result in assembled] == [("t1", 1)]
    assert assembled[0]["_selection_origin"] == "state_part_refinement"
    assert metadata["output_result_count"] == 1
    replacements = metadata["state_part_refinement"]["replacements"]
    assert len(replacements) == 1
    assert replacements[0]["search_rank"] == 1
    assert replacements[0]["from_chunk_key"] == ["t1", 0]
    assert replacements[0]["to_chunk_key"] == ["t1", 1]
    assert replacements[0]["score_gain"] >= EXPECTED_STATE_PART_REFINEMENT_MIN_SCORE_GAIN
    assert replacements[0]["overlap_gain"] > 0.0


def test_sibyl_memory_chunk_catalog_round_trips(tmp_path: Path) -> None:
    module = _load_memory_module()
    catalog = {
        "t1": {
            0: _search_result("t1", chunk_index=0, state_index=0, score=0.0),
            1: _search_result("t1", chunk_index=1, state_index=1, score=0.0),
        }
    }
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})
    memory._chunk_catalog = catalog
    memory._pending_embedding_job_ids = set()
    memory._pending_projection_job_ids = set()
    memory._finalize_lock = threading.Lock()
    memory._ingest_finalized = True
    memory.api_url = "http://127.0.0.1:3434/api"
    memory.project_id = "project_saved"
    memory.run_id = "run-saved"
    memory.chunking_mode = "state"
    memory.content_max_chars = EXPECTED_CONTENT_MAX_CHARS
    memory.ingest_api_runtime = {"version": "test"}
    memory.ingest_embedding_usage = {
        "requests": EXPECTED_SAVED_USAGE_REQUESTS,
        "provider_reported_cost_usd": EXPECTED_SAVED_USAGE_COST_USD,
    }
    (tmp_path / "memory_config.json").write_text(
        json.dumps(memory.memory_config),
        encoding="utf-8",
    )

    memory._save_backend(tmp_path)

    restored = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(restored, {})
    restored._pending_embedding_job_ids = set()
    restored._pending_projection_job_ids = set()
    restored._ingest_finalized = False
    restored._load_backend(tmp_path)

    assert (tmp_path / module.CHUNK_CATALOG_FILENAME).is_file()
    assert (tmp_path / module.MEMORY_MANIFEST_FILENAME).is_file()
    assert restored._chunk_catalog == catalog
    assert restored.created_entities == len(catalog["t1"])
    assert restored.inserted_trajectories == len(catalog)
    assert restored._ingest_finalized is True
    assert restored.ingest_api_runtime == {"version": "test"}
    assert restored.ingest_embedding_usage == {
        "requests": EXPECTED_SAVED_USAGE_REQUESTS,
        "provider_reported_cost_usd": EXPECTED_SAVED_USAGE_COST_USD,
    }


def test_sibyl_memory_saved_config_strips_credentials() -> None:
    module = _load_memory_module()
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(
        memory,
        {
            "api_url": "http://127.0.0.1:3434/api",
            "api_token": "token-secret",
            "email": "bench@example.invalid",
            "password": "password-secret",
            "run_id": "run-saved",
        },
    )
    memory.api_url = "http://127.0.0.1:3434/api"
    memory.project_id = "project_saved"
    memory.run_id = "run-saved"

    params = memory.memory_config["memory_params"]

    assert params["project_id"] == "project_saved"
    assert params["run_id"] == "run-saved"
    assert "api_token" not in params
    assert "email" not in params
    assert "password" not in params


def test_sibyl_memory_ingest_checkpoint_resumes_completed_trajectory(tmp_path: Path) -> None:
    module = _load_memory_module()
    checkpoint_dir = tmp_path / "checkpoint"
    payloads = module.build_entity_payloads_for_trajectory(
        _trajectory("t1"),
        project_id="project_saved",
        run_id="run-saved",
    )
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})
    memory.api_url = "http://127.0.0.1:3434/api"
    memory.project_id = "project_saved"
    memory.run_id = "run-saved"
    memory.chunking_mode = "state"
    memory.content_max_chars = EXPECTED_CONTENT_MAX_CHARS
    memory.checkpoint_dir = checkpoint_dir
    memory._chunk_catalog = module._catalog_results(payloads)
    memory._completed_trajectory_ids = {"t1"}
    memory._operational_trajectory_ids = {"t1"}
    memory._pending_embedding_job_ids = {"embed-1"}
    memory._pending_projection_job_ids = {"project-1"}
    memory._pending_job_entity_ids = {
        "embed-1": ["session-one"],
        "project-1": ["session-one"],
    }
    memory._pending_job_manifest_ids = {"embed-1": "artifact-manifest-one"}
    memory.ingest_embedding_usage = {"requests": EXPECTED_SAVED_USAGE_REQUESTS}
    memory.ingest_api_runtime = {"version": "test"}

    memory._append_checkpoint(payloads)
    catalog_path = checkpoint_dir / module.CHECKPOINT_CATALOG_FILENAME
    with catalog_path.open("ab") as handle:
        handle.write(b"interrupted trailing bytes")

    restored = _reload_checkpoint(module, memory, checkpoint_dir)

    assert restored._completed_trajectory_ids == {"t1"}
    assert restored._operational_trajectory_ids == {"t1"}
    assert restored._pending_embedding_job_ids == {"embed-1"}
    assert restored._pending_projection_job_ids == {"project-1"}
    assert restored._pending_job_entity_ids == memory._pending_job_entity_ids
    assert restored._pending_job_manifest_ids == memory._pending_job_manifest_ids
    assert restored._chunk_catalog == memory._chunk_catalog
    assert restored.ingest_embedding_usage == {"requests": EXPECTED_SAVED_USAGE_REQUESTS}

    restored._request_json = lambda *args, **kwargs: pytest.fail(
        f"completed trajectory was reinserted: {args}, {kwargs}"
    )
    restored.insert(_trajectory("t1"))

    second_payloads = module.build_entity_payloads_for_trajectory(
        _trajectory("t2"),
        project_id="project_saved",
        run_id="run-saved",
    )
    restored.checkpoint_dir = checkpoint_dir
    restored._completed_trajectory_ids.add("t2")
    restored._operational_trajectory_ids.add("t2")
    restored._chunk_catalog.update(module._catalog_results(second_payloads))
    restored._append_checkpoint(second_payloads)

    reloaded = _reload_checkpoint(module, memory, checkpoint_dir)

    assert reloaded._completed_trajectory_ids == {"t1", "t2"}
    assert set(reloaded._chunk_catalog) == {"t1", "t2"}


def test_sibyl_memory_rejects_legacy_checkpoint_in_place_upgrade(
    tmp_path: Path,
) -> None:
    module = _load_memory_module()
    checkpoint_dir = tmp_path / "checkpoint"
    payloads = module.build_entity_payloads_for_trajectory(
        _trajectory("t1"),
        project_id="project_saved",
        run_id="run-saved",
    )
    source = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(source, {})
    source.api_url = "http://127.0.0.1:3434/api"
    source.project_id = "project_saved"
    source.run_id = "run-saved"
    source.chunking_mode = "state"
    source.content_max_chars = EXPECTED_CONTENT_MAX_CHARS
    source.checkpoint_dir = checkpoint_dir
    source._chunk_catalog = module._catalog_results(payloads)
    source._completed_trajectory_ids = {"t1"}
    source._operational_trajectory_ids = {"t1"}
    source._pending_embedding_job_ids = set()
    source._pending_projection_job_ids = set()
    source._pending_job_entity_ids = {}
    source.ingest_embedding_usage = {}
    source.ingest_api_runtime = {"version": "test"}
    source._append_checkpoint(payloads)

    manifest_path = checkpoint_dir / module.CHECKPOINT_MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("operational_trajectory_ids")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match="cannot be upgraded in place"):
        _reload_checkpoint(module, source, checkpoint_dir)


def test_sibyl_memory_rejects_trajectory_chunking_for_operational_ingest() -> None:
    module = _load_memory_module()

    with pytest.raises(ValueError, match="incompatible with operational experience"):
        module.SibylLiveApiMemory({"chunking_mode": "trajectory"})


def test_sibyl_memory_loaded_config_allows_only_runtime_overrides() -> None:
    module = _load_memory_module()
    saved = {
        "memory_type": "sibyl_live_api",
        "memory_params": {
            "api_url": "http://127.0.0.1:3434/api",
            "project_id": "project_saved",
            "run_id": "run-saved",
            "content_max_chars": EXPECTED_CONTENT_MAX_CHARS,
            "search_limit": 12,
            "neighbor_stitch_items": 0,
        },
    }
    requested = {
        "memory_type": "sibyl_live_api",
        "memory_params": {
            **saved["memory_params"],
            "api_token": TEST_CREDENTIAL,
            "search_limit": EXPECTED_SEARCH_LIMIT_OVERRIDE,
            "state_part_completion_items": EXPECTED_STATE_PART_COMPLETION_ITEMS,
            "state_part_refinement": True,
            "neighbor_stitch_items": 2,
            "context_expansion_max_ratio": EXPECTED_CONTEXT_EXPANSION_MAX_RATIO,
            "retrieval_mode": "accurate",
            "retrieval_max_planned_queries": 3,
        },
    }

    effective = module.SibylLiveApiMemory.reconcile_loaded_memory_config(saved, requested)
    params = effective["memory_params"]

    assert params["project_id"] == "project_saved"
    assert params["run_id"] == "run-saved"
    assert params["api_token"] == TEST_CREDENTIAL
    assert params["search_limit"] == EXPECTED_SEARCH_LIMIT_OVERRIDE
    assert params["state_part_completion_items"] == EXPECTED_STATE_PART_COMPLETION_ITEMS
    assert params["state_part_refinement"] is True
    assert params["neighbor_stitch_items"] == EXPECTED_NEIGHBOR_STITCH_ITEMS
    assert params["context_expansion_max_ratio"] == EXPECTED_CONTEXT_EXPANSION_MAX_RATIO
    assert params["retrieval_mode"] == "accurate"
    assert params["retrieval_max_planned_queries"] == EXPECTED_RETRIEVAL_MAX_PLANNED_QUERIES

    requested["memory_params"]["content_max_chars"] = 8_000
    with pytest.raises(RuntimeError, match="content_max_chars"):
        module.SibylLiveApiMemory.reconcile_loaded_memory_config(saved, requested)


def _load_note_distillation_module() -> ModuleType:
    return _load_module(
        Path(__file__).parents[2] / "benchmarks" / "longmemeval_v2_memory" / "note_distillation.py",
        "note_distillation",
    )


def test_parse_distillation_output_strips_fences_and_validates() -> None:
    module = _load_note_distillation_module()
    fenced = '```json\n{"workflow": "click New", "facts": [" Label A "], "gotchas": []}\n```'

    notes = module.parse_distillation_output(fenced)

    assert notes["workflow"] == "click New"
    assert notes["facts"] == ["Label A"]
    assert notes["gotchas"] == []

    with pytest.raises(ValueError, match="no notes"):
        module.parse_distillation_output('{"workflow": "", "facts": [], "gotchas": []}')


def test_build_trajectory_digest_bounds_length() -> None:
    module = _load_note_distillation_module()
    trajectory = {
        "id": "t1",
        "goal": "do the thing",
        "outcome": "success",
        "states": [
            {"action": f"click('{index}')", "reasoning": "x" * 400, "uri": "https://e/x"}
            for index in range(400)
        ],
    }

    digest = module.build_trajectory_digest(trajectory, max_chars=10_000)

    assert len(digest) <= 10_000
    assert digest.startswith("Goal: do the thing")
    assert "digest truncated" in digest


def test_build_trajectory_digest_includes_salient_page_content() -> None:
    module = _load_note_distillation_module()
    axtree = "\n".join(
        [
            "RootWebArea 'Forum — Most Commented'",
            "\t[a1] navigation 'Global skip links'",
            "\t[a2] link 'Skip to main content'",
            "\t[a3] heading 'Weekly discussion thread'",
            "\t[a4] gridcell 'Comments: 347'",
            "\t[a5] StaticText 'Posted by chronos_admin'",
            "\t[a6] link 'Open accessibility preferences'",
        ]
    )
    trajectory = {
        "id": "t7",
        "goal": "find the most commented post",
        "outcome": "success",
        "states": [
            {
                "action": "click('a3')",
                "uri": "https://forum.example/top",
                "evidence": [
                    {
                        "content_type": "text/plain; profile=accessibility-tree",
                        "content": axtree,
                    }
                ],
            }
        ],
    }

    digest = module.build_trajectory_digest(trajectory)

    assert "heading: Weekly discussion thread" in digest
    assert "gridcell: Comments: 347" in digest
    assert "Skip to main content" not in digest
    assert "accessibility preferences" not in digest
    content_lines = [line for line in digest.splitlines() if line.startswith("  · ")]
    assert 0 < len(content_lines) <= module.MAX_CONTENT_LINES_PER_STATE


def test_build_note_entity_payloads_shape() -> None:
    module = _load_note_distillation_module()
    payloads = module.build_note_entity_payloads(
        {"workflow": "step 1", "facts": ["fact"], "gotchas": []},
        trajectory={"id": "t9", "goal": "g", "outcome": "success"},
        project_id="project_x",
        run_id="run_y",
        model="gpt-5.4-nano",
    )

    assert [p["metadata"]["note_kind"] for p in payloads] == ["workflow", "facts"]
    for payload in payloads:
        assert payload["entity_type"] == "note"
        metadata = payload["metadata"]
        assert metadata["longmemeval_v2_trajectory_id"] == "t9"
        assert metadata["longmemeval_v2_run_id"] == "run_y"
        assert metadata["projection_kind"] == "distilled_note"
        assert str(payload["content"]).startswith("Trajectory: t9")


def test_note_distillation_insert_does_not_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_memory_module()
    monkeypatch.setattr(module.SibylLiveApiMemory, "_authenticate", lambda *args: None)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    bulk_calls: list[dict[str, object]] = []

    def fake_request(
        _self: object,
        method: str,
        path: str,
        **kwargs: object,
    ) -> dict[str, object]:
        if path == "/health":
            return {"status": "healthy"}
        if method == "GET":
            return {"id": "project_test", "entity_type": "project"}
        if path == "/entities/bulk":
            bulk_calls.append(kwargs.get("json") or {})
            return {"created": 2, "background_jobs": {}}
        return {"created": 0, "background_jobs": {}}

    monkeypatch.setattr(module.SibylLiveApiMemory, "_request_json", fake_request)

    import threading as threading_module

    gate = threading_module.Event()

    def slow_distill(*_args: object, **_kwargs: object) -> dict[str, object]:
        gate.wait(timeout=10)
        return {"workflow": "step", "facts": [], "gotchas": []}

    monkeypatch.setattr(module, "distill_trajectory_notes", slow_distill)

    memory = module.SibylLiveApiMemory(
        {
            "allow_localhost": True,
            "project_id": "project_test",
            "note_distillation": True,
            "defer_embeddings": False,
        }
    )
    try:
        memory._submit_note_distillation({"id": "t1", "goal": "g", "outcome": "success"})
        started = time.monotonic()
        memory._harvest_note_futures(block=False)
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, "non-blocking harvest must not wait on pending futures"
        assert not bulk_calls, "no notes written while distillation pending"

        gate.set()
        memory._harvest_note_futures(block=True)
        assert len(bulk_calls) == 1
        entities = bulk_calls[0]["entities"]
        assert entities[0]["entity_type"] == "note"
        assert memory.note_distillation_written == 1
    finally:
        if memory._note_executor is not None:
            memory._note_executor.shutdown(wait=False)
        memory._client.close()


def test_note_distillation_finalize_raises_on_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_memory_module()
    monkeypatch.setattr(module.SibylLiveApiMemory, "_authenticate", lambda *args: None)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def fake_request(
        _self: object,
        method: str,
        path: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        if path == "/health":
            return {"status": "healthy"}
        if method == "GET":
            return {"id": "project_test", "entity_type": "project"}
        return {"created": 0, "background_jobs": {}}

    monkeypatch.setattr(module.SibylLiveApiMemory, "_request_json", fake_request)

    def failing_distill(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(module, "distill_trajectory_notes", failing_distill)

    memory = module.SibylLiveApiMemory(
        {
            "allow_localhost": True,
            "project_id": "project_test",
            "note_distillation": True,
        }
    )
    try:
        memory._submit_note_distillation({"id": "t2", "goal": "g", "outcome": "failure"})
        with pytest.raises(RuntimeError, match="note distillation failed for 1 trajectories"):
            memory.finalize_ingest()
    finally:
        if memory._note_executor is not None:
            memory._note_executor.shutdown(wait=False)
        memory._client.close()


def test_annotate_inventory_completeness_branches() -> None:
    module = _load_memory_module()
    content = "Goal: something\nObserved UI inventory:\n- link: Home"

    complete = module.annotate_inventory_completeness(
        content, {"ui_inventory_item_count": 42, "ui_inventory_truncated": False}
    )
    assert "Complete UI element inventory" in complete
    assert "42 elements" in complete
    assert "was not present" in complete

    partial = module.annotate_inventory_completeness(
        content, {"ui_inventory_item_count": 157, "ui_inventory_truncated": True}
    )
    assert "Partial UI element inventory" in partial
    assert "cannot be inferred" in partial

    assert module.annotate_inventory_completeness(content, None) == content
    assert module.annotate_inventory_completeness(content, {}) == content
    assert (
        module.annotate_inventory_completeness("no inventory here", {"ui_inventory_item_count": 3})
        == "no inventory here"
    )


def test_merge_typed_stream_results_dedupes_by_id() -> None:
    module = _load_memory_module()
    pack = [
        {"id": "procedure_1", "type": "procedure"},
        {"id": "event_1", "type": "event"},
    ]
    stream = [
        {"id": "event_1", "type": "event"},
        {"id": "event_2", "type": "event", "_selection_origin": "context_pack:typed_stream"},
        {"id": "error_pattern_1", "type": "error_pattern"},
    ]

    merged = module.merge_typed_stream_results(pack, stream)

    assert [item["id"] for item in merged] == [
        "procedure_1",
        "event_1",
        "event_2",
        "error_pattern_1",
    ]


def test_typed_stream_results_filters_and_marks_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_memory_module()
    monkeypatch.setattr(module.SibylLiveApiMemory, "_authenticate", lambda *args: None)
    captured: list[dict[str, object]] = []

    def fake_request(
        _self: object,
        method: str,
        path: str,
        **kwargs: object,
    ) -> dict[str, object]:
        if path == "/health":
            return {"status": "healthy"}
        if method == "GET":
            return {"id": "project_test", "entity_type": "project"}
        captured.append(kwargs.get("json") or {})
        return {
            "evidence": {
                "results": [
                    {"id": "event_9", "type": "event", "content": "state changed"},
                    {"id": "session_9", "type": "session", "content": "raw slice"},
                    {"id": "procedure_9", "type": "procedure", "content": "steps"},
                ],
                "filters": {"types": ["event", "procedure", "error_pattern"]},
            }
        }

    monkeypatch.setattr(module.SibylLiveApiMemory, "_request_json", fake_request)
    memory = module.SibylLiveApiMemory(
        {
            "allow_localhost": True,
            "project_id": "project_test",
            "typed_stream_retrieval": True,
            "typed_stream_limit": 5,
        }
    )
    try:
        results, metadata = memory._typed_stream_results("what changed?")
    finally:
        memory._client.close()

    assert [item["id"] for item in results] == ["event_9", "procedure_9"]
    assert all(item["_selection_origin"] == "context_pack:typed_stream" for item in results)
    assert metadata["result_count"] == 2
    request = captured[-1]
    evidence = request["evidence"]
    assert evidence["types"] == ["note", "event", "procedure", "error_pattern"]
    assert evidence["retrieval_mode"] == "fast"
    assert evidence["limit"] == 5
    assert request["record_exposure"] is False


def test_sibyl_memory_constructor_preserves_disabled_neighbor_stitching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_memory_module()
    monkeypatch.setattr(module.SibylLiveApiMemory, "_authenticate", lambda *args: None)

    def fake_request(
        _self: object,
        method: str,
        path: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        if path == "/health":
            return {"status": "healthy"}
        assert method == "GET"
        assert path == "/entities/project_test"
        return {"id": "project_test", "entity_type": "project"}

    monkeypatch.setattr(
        module.SibylLiveApiMemory,
        "_request_json",
        fake_request,
    )

    memory = module.SibylLiveApiMemory(
        {
            "allow_localhost": True,
            "project_id": "project_test",
            "neighbor_stitch_items": 0,
            "neighbor_stitch_span": "0",
        }
    )
    try:
        assert memory.neighbor_stitch_items == 0
        assert memory.neighbor_stitch_span == 0
    finally:
        memory._client.close()


def test_sibyl_memory_attaches_existing_project_for_query_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_memory_module()
    monkeypatch.setattr(module.SibylLiveApiMemory, "_authenticate", lambda *args: None)
    trajectory = _trajectory("trajectory_test")
    payloads = module.build_entity_payloads_for_trajectory(
        trajectory,
        project_id="project_test",
        run_id="run_test",
    )
    stored_payloads = [
        {**payload, "id": f"session_{index}"} for index, payload in enumerate(payloads)
    ]

    def fake_request(
        _self: object,
        method: str,
        path: str,
        **kwargs: object,
    ) -> dict[str, object]:
        if path == "/health":
            return {"status": "healthy"}
        if path == "/entities":
            assert kwargs["params"] == {
                "entity_type": "session",
                "project_ids": "project_test",
                "page": 1,
                "page_size": 200,
            }
            return {
                "entities": stored_payloads,
                "has_more": False,
            }
        if path.startswith("/entities/session_"):
            index = int(path.removeprefix("/entities/session_"))
            return stored_payloads[index]
        assert method == "GET"
        assert path == "/entities/project_test"
        return {"id": "project_test", "entity_type": "project"}

    monkeypatch.setattr(module.SibylLiveApiMemory, "_request_json", fake_request)

    memory = module.SibylLiveApiMemory.attach_existing(
        {
            "allow_localhost": True,
            "project_id": "project_test",
            "run_id": "run_test",
        },
        expected_trajectory_ids={"trajectory_test"},
        trajectories=[trajectory],
    )
    try:
        assert memory.reuse_existing_project is True
        assert memory._ingest_finalized is True
        receipt = memory.attached_project_receipt
        assert {
            key: receipt[key]
            for key in (
                "project_id",
                "run_id",
                "session_entity_count",
                "expected_session_entity_count",
                "expected_trajectory_count",
                "observed_trajectory_count",
                "extra_trajectory_count",
                "catalog_trajectory_count",
                "catalog_entity_count",
                "missing_chunk_count",
                "unexpected_chunk_count",
                "duplicate_chunk_count",
                "catalog_mismatch_count",
                "source_metadata_mismatch_count",
                "storage_shapes",
                "pages",
            )
        } == {
            "project_id": "project_test",
            "run_id": "run_test",
            "session_entity_count": len(payloads),
            "expected_session_entity_count": len(payloads),
            "expected_trajectory_count": 1,
            "observed_trajectory_count": 1,
            "extra_trajectory_count": 0,
            "catalog_trajectory_count": 1,
            "catalog_entity_count": len(payloads),
            "missing_chunk_count": 0,
            "unexpected_chunk_count": 0,
            "duplicate_chunk_count": 0,
            "catalog_mismatch_count": 0,
            "source_metadata_mismatch_count": 0,
            "storage_shapes": ["legacy"],
            "pages": 1,
        }
        assert receipt["content_audit"]["status"] == "verified"
        assert receipt["content_audit"]["entity_count"] == len(payloads)
        memory.insert(trajectory)
        assert memory._ingest_finalized is False
    finally:
        memory._client.close()


def test_sibyl_memory_attaches_current_operational_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_memory_module()
    monkeypatch.setattr(module.SibylLiveApiMemory, "_authenticate", lambda *args: None)
    trajectory = _trajectory("trajectory_test")
    payloads = module.build_operational_session_payloads_for_trajectory(
        trajectory,
        project_id="project_test",
        run_id="run_test",
    )
    stored_by_id = {str(payload["id"]): payload for payload in payloads}

    def fake_request(
        _self: object,
        method: str,
        path: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        if path == "/health":
            return {"status": "healthy"}
        if path == "/entities":
            return {"entities": payloads, "has_more": False}
        if path.startswith("/entities/") and path.removeprefix("/entities/") in stored_by_id:
            return stored_by_id[path.removeprefix("/entities/")]
        assert method == "GET"
        assert path == "/entities/project_test"
        return {"id": "project_test", "entity_type": "project"}

    monkeypatch.setattr(module.SibylLiveApiMemory, "_request_json", fake_request)

    memory = module.SibylLiveApiMemory.attach_existing(
        {
            "allow_localhost": True,
            "project_id": "project_test",
            "run_id": "run_test",
        },
        expected_trajectory_ids={"trajectory_test"},
        trajectories=[trajectory],
    )
    try:
        assert memory.attached_project_receipt["storage_shapes"] == ["operational"]
        assert memory.attached_project_receipt["content_audit"]["status"] == "verified"
        assert memory.attached_project_receipt["content_audit"]["entity_count"] == len(payloads)
    finally:
        memory._client.close()


def test_sibyl_memory_repair_audit_rejects_content_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_memory_module()
    monkeypatch.setattr(module.SibylLiveApiMemory, "_authenticate", lambda *args: None)
    trajectory = _trajectory("trajectory_test")
    payloads = module.build_entity_payloads_for_trajectory(
        trajectory,
        project_id="project_test",
        run_id="run_test",
    )

    def fake_request(
        _self: object,
        method: str,
        path: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        if path == "/health":
            return {"status": "healthy"}
        if path == "/entities":
            return {
                "entities": [
                    {
                        "id": f"session_{index}",
                        "name": payload["name"],
                        "metadata": payload["metadata"],
                    }
                    for index, payload in enumerate(payloads)
                ],
                "has_more": False,
            }
        if path.startswith("/entities/session_"):
            index = int(path.removeprefix("/entities/session_"))
            return {**payloads[index], "id": f"session_{index}", "content": "drifted"}
        assert method == "GET"
        assert path == "/entities/project_test"
        return {"id": "project_test", "entity_type": "project"}

    monkeypatch.setattr(module.SibylLiveApiMemory, "_request_json", fake_request)

    memory = module.SibylLiveApiMemory.prepare_existing(
        {
            "allow_localhost": True,
            "project_id": "project_test",
            "run_id": "run_test",
        },
        expected_trajectory_ids={"trajectory_test"},
        trajectories=[trajectory],
    )
    try:
        dry_run = memory.repair_attached_project(apply=False)
        assert dry_run["repairable"] is False
        assert dry_run["non_repairable_reasons"] == ["content_mismatch"]
        assert dry_run["before"]["content_audit"]["status"] == "mismatch"
        with pytest.raises(RuntimeError, match="content_mismatch"):
            memory.repair_attached_project(apply=True)
    finally:
        memory._client.close()


def test_sibyl_memory_repair_dry_run_reports_structural_damage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_memory_module()
    monkeypatch.setattr(module.SibylLiveApiMemory, "_authenticate", lambda *args: None)
    trajectory = _trajectory("trajectory_test")
    payloads = module.build_entity_payloads_for_trajectory(
        trajectory,
        project_id="project_test",
        run_id="run_test",
    )
    stored = [{**payload, "id": f"session_{index}"} for index, payload in enumerate(payloads)]
    stored[0]["name"] = "damaged name"

    def fake_request(
        _self: object,
        method: str,
        path: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        if path == "/health":
            return {"status": "healthy"}
        if path == "/entities":
            return {"entities": stored, "has_more": False}
        assert method == "GET"
        assert path == "/entities/project_test"
        return {"id": "project_test", "entity_type": "project"}

    monkeypatch.setattr(module.SibylLiveApiMemory, "_request_json", fake_request)
    memory = module.SibylLiveApiMemory.prepare_existing(
        {
            "allow_localhost": True,
            "project_id": "project_test",
            "run_id": "run_test",
        },
        expected_trajectory_ids={"trajectory_test"},
        trajectories=[trajectory],
    )
    try:
        dry_run = memory.repair_attached_project(apply=False)
        assert dry_run["repairable"] is False
        assert dry_run["non_repairable_reasons"] == ["catalog_mismatches"]
        assert dry_run["before"]["content_audit"]["status"] == ("blocked_by_inventory_damage")
    finally:
        memory._client.close()


def test_sibyl_memory_attach_existing_requires_project_id() -> None:
    module = _load_memory_module()

    with pytest.raises(ValueError, match="requires project_id"):
        module.SibylLiveApiMemory.attach_existing(
            {},
            expected_trajectory_ids={"trajectory_test"},
            trajectories=[],
        )


def test_official_runner_rejects_reuse_with_checkpoint_dir(tmp_path: Path) -> None:
    module = _load_runner_module()

    with pytest.raises(SystemExit):
        module.parse_args(
            [
                "--data-root",
                str(tmp_path),
                "--domain",
                "web",
                "--output-dir",
                str(tmp_path / "output"),
                "--project-id",
                "project_test",
                "--reuse-existing-project",
                "--checkpoint-dir",
                str(tmp_path / "checkpoint"),
            ]
        )


def test_sibyl_memory_repairs_only_missing_attached_project_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_memory_module()
    monkeypatch.setattr(module.SibylLiveApiMemory, "_authenticate", lambda *args: None)
    trajectory = _trajectory("trajectory_test")
    payloads = module.build_entity_payloads_for_trajectory(
        trajectory,
        project_id="project_test",
        run_id="run_test",
    )
    existing_payload = {
        **payloads[0],
        "id": "session_existing_0",
        "metadata": {
            key: value for key, value in payloads[0]["metadata"].items() if key != "source_id"
        },
    }
    stored_payloads = [existing_payload]
    repaired_entities: dict[str, dict[str, object]] = {"session_existing_0": existing_payload}
    posted_batches: list[list[dict[str, object]]] = []

    def fake_request(
        _self: object,
        method: str,
        path: str,
        **kwargs: object,
    ) -> dict[str, object]:
        if path == "/health":
            return {"status": "healthy"}
        if path == "/entities" and method == "GET":
            return {
                "entities": [
                    {
                        "id": payload.get("id"),
                        "name": payload["name"],
                        "content": payload["content"],
                        "metadata": payload["metadata"],
                    }
                    for payload in stored_payloads
                ],
                "has_more": False,
            }
        if path == "/entities/session_existing_0" and method == "PATCH":
            request_json = cast(dict[str, object], kwargs["json"])
            metadata = cast(dict[str, object], request_json["metadata"])
            existing_payload["metadata"] = {
                **cast(dict[str, object], existing_payload["metadata"]),
                **metadata,
            }
            return existing_payload
        if path == "/entities/bulk" and method == "POST":
            request_json = cast(dict[str, object], kwargs["json"])
            batch = cast(list[dict[str, object]], request_json["entities"])
            posted_batches.append(batch)
            response_entities: list[dict[str, object]] = []
            for index, payload in enumerate(batch):
                entity_id = f"session_repaired_{index}"
                stored: dict[str, object] = {**payload, "id": entity_id}
                stored_payloads.append(stored)
                repaired_entities[entity_id] = stored
                response_entities.append({"id": entity_id})
            return {
                "created": len(batch),
                "entities": response_entities,
                "background_jobs": {},
            }
        if path.startswith("/entities/session_") and method == "GET":
            return repaired_entities[path.removeprefix("/entities/")]
        assert method == "GET"
        assert path == "/entities/project_test"
        return {"id": "project_test", "entity_type": "project"}

    monkeypatch.setattr(module.SibylLiveApiMemory, "_request_json", fake_request)
    memory = module.SibylLiveApiMemory.prepare_existing(
        {
            "allow_localhost": True,
            "project_id": "project_test",
            "run_id": "run_test",
            "defer_embeddings": False,
        },
        expected_trajectory_ids={"trajectory_test"},
        trajectories=[trajectory],
    )
    try:
        dry_run = memory.repair_attached_project(apply=False)

        assert (dry_run["applied"], dry_run["repairable"]) == (False, True)
        assert dry_run["before"]["missing_chunk_count"] == 1
        assert dry_run["before"]["source_metadata_mismatch_count"] == 1
        assert posted_batches == []

        applied = memory.repair_attached_project(apply=True)

        assert applied["applied"] is True
        assert applied["created_entity_count"] == 1
        assert applied["updated_entity_count"] == 1
        assert applied["verified_entity_count"] == 1
        assert len(applied["verified_content_sha256"]) == EXPECTED_SHA256_HEX_LENGTH
        assert len(applied["verified_source_metadata_sha256"]) == EXPECTED_SHA256_HEX_LENGTH
        assert applied["after"]["missing_chunk_count"] == 0
        assert applied["after"]["source_metadata_mismatch_count"] == 0
        assert posted_batches == [[payloads[1]]]
        assert memory._ingest_finalized is True
    finally:
        memory._client.close()


def test_sibyl_memory_rejects_invisible_saved_project() -> None:
    module = _load_memory_module()
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})
    memory.project_id = "project_saved"
    memory._request_json = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("not found"))

    with pytest.raises(RuntimeError, match="not visible to the current API credentials"):
        memory._verify_project_visibility()


def test_official_runner_load_config_preserves_saved_ingest_identity(tmp_path: Path) -> None:
    module = _load_runner_module()
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "memory_config.json").write_text(
        json.dumps(
            {
                "memory_type": "sibyl_live_api",
                "memory_params": {
                    "api_url": "http://127.0.0.1:3434/api",
                    "project_id": "project_saved",
                    "run_id": "run-saved",
                    "content_max_chars": EXPECTED_CONTENT_MAX_CHARS,
                    "search_limit": 12,
                },
            }
        ),
        encoding="utf-8",
    )
    requested = {
        "memory_type": "sibyl_live_api",
        "memory_params": {
            "api_url": "http://127.0.0.1:3334/api",
            "project_id": "",
            "run_id": "run-new",
            "content_max_chars": 8_000,
            "api_token": TEST_CREDENTIAL,
            "search_limit": EXPECTED_SEARCH_LIMIT_OVERRIDE,
            "state_part_completion_items": EXPECTED_STATE_PART_COMPLETION_ITEMS,
            "state_part_refinement": True,
        },
    }

    effective = module.build_loaded_memory_config(memory_dir, requested_config=requested)
    params = effective["memory_params"]

    assert params["api_url"] == "http://127.0.0.1:3434/api"
    assert params["project_id"] == "project_saved"
    assert params["run_id"] == "run-saved"
    assert params["content_max_chars"] == EXPECTED_CONTENT_MAX_CHARS
    assert params["api_token"] == TEST_CREDENTIAL
    assert params["search_limit"] == EXPECTED_SEARCH_LIMIT_OVERRIDE
    assert params["state_part_completion_items"] == EXPECTED_STATE_PART_COMPLETION_ITEMS
    assert params["state_part_refinement"] is True


def test_official_runner_checkpoint_restart_reuses_saved_project(tmp_path: Path) -> None:
    module = _load_runner_module()
    data_root = tmp_path / "data"
    checkpoint_dir = tmp_path / "checkpoint"
    checkpoint_dir.mkdir()
    _write_dataset(data_root)
    (checkpoint_dir / "memory_config.json").write_text(
        json.dumps(
            {
                "memory_type": "sibyl_live_api",
                "memory_params": {
                    "api_url": "http://127.0.0.1:3434/api",
                    "project_id": "project_checkpoint",
                    "run_id": "run-checkpoint",
                    "content_max_chars": EXPECTED_CONTENT_MAX_CHARS,
                    "chunking_mode": "state",
                },
            }
        ),
        encoding="utf-8",
    )
    args = module.parse_args(
        [
            "--data-root",
            str(data_root),
            "--domain",
            "enterprise",
            "--output-dir",
            str(tmp_path / "output"),
            "--checkpoint-dir",
            str(checkpoint_dir),
            "--plan-only",
            "--retrieval-mode",
            "accurate",
            "--retrieval-max-planned-queries",
            "3",
        ]
    )

    config = module.build_memory_config(args)
    params = config["memory_params"]

    assert params["api_url"] == "http://127.0.0.1:3434/api"
    assert params["project_id"] == "project_checkpoint"
    assert params["run_id"] == "run-checkpoint"
    assert params["checkpoint_dir"] == str(checkpoint_dir)
    assert params["source_evidence_bundling"] is False
    assert params["retrieval_mode"] == "accurate"
    assert params["retrieval_max_planned_queries"] == EXPECTED_RETRIEVAL_MAX_PLANNED_QUERIES


def test_sibyl_memory_query_context_exposes_only_question_and_image() -> None:
    module = _load_memory_module()
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})

    memory.set_query_context(
        question_id="q1",
        question_item={
            "id": "q1",
            "question": "Which filter was selected?",
            "question_type": "static-environment",
            "eval_function": "norm_phrase_match",
            "image": "question.png",
            "answer": "Priority",
        },
    )

    assert memory.get_query_context() == {
        "question": "Which filter was selected?",
        "image": "question.png",
    }


def test_sibyl_memory_accurate_query_rejects_planner_fallback() -> None:
    module = _load_memory_module()
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})
    request_payloads: list[dict[str, object]] = []

    def fake_request(
        _method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> dict[str, object]:
        assert path == "/context/pack"
        assert json is not None
        request_payloads.append(json)
        return {
            "sections": [],
            "evidence": {
                "results": [],
                "filters": {
                    "retrieval_mode": "accurate",
                    "planner_status": "fallback",
                },
            },
        }

    memory.project_id = "project_lme"
    memory.search_limit = 12
    memory.max_context_items = 8
    memory.max_context_chars_per_item = TEST_CONTEXT_MAX_CHARS
    memory.retrieval_mode = "accurate"
    memory.retrieval_max_planned_queries = 3
    memory._pending_embedding_job_ids = set()
    memory._pending_projection_job_ids = set()
    memory._ingest_finalized = True
    memory._request_json = fake_request

    with pytest.raises(RuntimeError, match="requires a successful query planner"):
        memory.query("Which filter was selected?")

    assert request_payloads[0]["evidence"] == {
        "types": ["session"],
        "limit": 12,
        "max_results_per_source": EXPECTED_MAX_CHUNKS_PER_TRAJECTORY,
        "content_max_chars": TEST_CONTEXT_MAX_CHARS,
        "include_retrieval_diagnostics": True,
        "retrieval_mode": "accurate",
        "max_planned_queries": 3,
    }


def test_operational_experience_payload_preserves_oversized_state_evidence() -> None:
    module = _load_memory_module()
    trajectory = _trajectory("t1", tree="Priority field\n" * 2_000)

    payload = module.build_operational_experience_payload(
        trajectory,
        project_id="project_lme",
        run_id="run_lme",
        content_max_chars=OPERATIONAL_EVIDENCE_MAX_CHARS,
    )

    experience = payload["experience"]
    observations = experience["observations"]
    parts = observations[0]["evidence"]
    assert len(parts) > 1
    assert all(len(part["content"]) <= OPERATIONAL_EVIDENCE_MAX_CHARS for part in parts)
    assert [part["metadata"]["longmemeval_v2_chunk_index"] for part in parts] == list(
        range(len(parts))
    )
    reconstructed = "".join(part["content"].split("\n\n", maxsplit=2)[-1] for part in parts)
    states = cast(list[dict[str, object]], trajectory["states"])
    tree = cast(str, states[0]["accessibility_tree"])
    assert tree in reconstructed


def test_context_pack_conversion_keeps_only_typed_operational_memory() -> None:
    module = _load_memory_module()

    results = module.context_pack_to_search_results(
        {
            "sections": [
                {
                    "facet": "procedures",
                    "items": [
                        {
                            "id": "procedure-1",
                            "type": "procedure",
                            "content": "1. click Priority",
                            "score": 0.2,
                            "metadata": {"longmemeval_v2_trajectory_id": "t1"},
                            "related": [
                                {
                                    "id": "session-source",
                                    "relationship": "DERIVED_FROM",
                                    "direction": "outgoing",
                                    "content": "hidden unless explicitly enabled",
                                    "metadata": {
                                        "operational_source_id": "longmemeval-v2:run:t1",
                                        "source_observation_id": "state-2",
                                        "observation_ordinal": 2,
                                        "evidence_part_id": "chunk-4",
                                    },
                                },
                                {
                                    "id": "session-source-2",
                                    "relationship": "DERIVED_FROM",
                                    "direction": "outgoing",
                                    "content": "another source state",
                                    "metadata": {
                                        "operational_source_id": "longmemeval-v2:run:t2",
                                        "observation_ordinal": 4,
                                    },
                                },
                                {
                                    "id": "session-invalid",
                                    "relationship": "DERIVED_FROM",
                                    "direction": "outgoing",
                                    "content": "invalid bool ordinal",
                                    "metadata": {
                                        "operational_source_id": "longmemeval-v2:run:t3",
                                        "observation_ordinal": True,
                                    },
                                },
                            ],
                        },
                        {
                            "id": "event-1",
                            "type": "event",
                            "content": "Priority changed to Critical",
                            "score": 0.8,
                            "metadata": {"longmemeval_v2_trajectory_id": "t2"},
                        },
                        {
                            "id": "tool-1",
                            "type": "tool",
                            "content": "browser",
                            "score": 0.9,
                        },
                    ],
                }
            ]
        }
    )

    assert [result["id"] for result in results] == ["procedure-1", "event-1"]
    assert all(result["_selection_origin"] == "context_pack:procedures" for result in results)
    assert results[0]["content"] == "1. click Priority"
    assert results[0]["metadata"]["source_support_entity_id"] == "session-source"
    assert results[0]["metadata"]["source_support_state_indices"] == [2]
    assert results[0]["metadata"]["source_support_states"] == [
        {
            "entity_id": "session-source",
            "operational_source_id": "longmemeval-v2:run:t1",
            "trajectory_id": "t1",
            "state_index": 2,
        },
        {
            "entity_id": "session-source-2",
            "operational_source_id": "longmemeval-v2:run:t2",
            "trajectory_id": "t2",
            "state_index": 4,
        },
    ]


def test_context_pack_conversion_bundles_query_ranked_source_evidence() -> None:
    module = _load_memory_module()

    results = module.context_pack_to_search_results(
        {
            "sections": [
                {
                    "facet": "recent_memory",
                    "items": [
                        {
                            "id": "event-1",
                            "type": "event",
                            "content": "Action: open the attribute editor",
                            "score": 0.8,
                            "metadata": {"longmemeval_v2_trajectory_id": "t1"},
                            "related": [
                                {
                                    "id": "session-unrelated",
                                    "relationship": "DERIVED_FROM",
                                    "direction": "outgoing",
                                    "content": "Account settings and profile controls",
                                },
                                {
                                    "id": "session-source",
                                    "relationship": "DERIVED_FROM",
                                    "direction": "outgoing",
                                    "content": "Catalog Input Type: Text Swatch",
                                },
                            ],
                        }
                    ],
                }
            ]
        },
        query="Which Catalog Input Type is selected?",
        include_source_support=True,
    )

    assert len(results) == 1
    assert "Typed projection:\nAction: open the attribute editor" in results[0]["content"]
    assert "Source evidence:\nCatalog Input Type: Text Swatch" in results[0]["content"]
    assert results[0]["metadata"]["source_support_entity_id"] == "session-source"


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ({"sections": []}, "missing required enhanced evidence"),
        ({"evidence": {"results": {}, "filters": {}}}, "results have an invalid shape"),
        ({"evidence": {"results": [], "filters": []}}, "filters have an invalid shape"),
    ],
)
def test_context_pack_evidence_contract_fails_closed(
    response: dict[str, object],
    message: str,
) -> None:
    module = _load_memory_module()

    with pytest.raises(RuntimeError, match=message):
        module._required_context_evidence(response)


def test_operational_evidence_set_calibrates_typed_and_raw_score_pools() -> None:
    module = _load_memory_module()
    typed = [
        {
            "id": f"procedure-{index}",
            "type": "procedure",
            "content": "Unrelated account settings",
            "score": 0.1,
            "_selection_origin": "context_pack:procedures",
            "metadata": {"longmemeval_v2_trajectory_id": f"t{index}"},
        }
        for index in range(8)
    ]
    raw = [
        {
            "id": f"session-{index}",
            "type": "session",
            "content": f"Deployment Ring value {index}",
            "score": 1.0 - (index / 100),
            "_selection_origin": "search",
        }
        for index in range(8)
    ]

    selected, metadata = module.compile_operational_evidence_set(
        query='Which value is shown for "Deployment Ring"?',
        typed_results=typed,
        raw_results=raw,
        max_items=8,
        mode="shared_relevance",
    )

    assert len(selected) == EXPECTED_OPERATIONAL_EVIDENCE_ITEMS
    assert [item["type"] for item in selected] == [
        "procedure",
        "procedure",
        "procedure",
        "session",
        "session",
        "session",
        "session",
        "session",
    ]
    assert metadata == {
        "mode": "shared_relevance",
        "candidate_count": 16,
        "typed_candidate_count": 8,
        "raw_candidate_count": 8,
        "ranking_applied": True,
        "ranking_changed": False,
        "pool_calibration": "independent_query_coverage",
        "typed_reservation": 3,
        "selected_typed_overflow_count": 0,
        "selected_raw_support_count": 0,
        "selected_typed_count": 3,
        "selected_raw_count": 5,
    }


@pytest.mark.parametrize(
    ("max_items", "raw_count", "typed_count", "selected_raw", "typed_overflow"),
    [
        (1, 8, 1, 0, 0),
        (2, 8, 1, 1, 0),
        (3, 8, 2, 1, 0),
        (8, 8, 3, 5, 0),
        (8, 1, 7, 1, 4),
    ],
)
def test_shared_relevance_reserves_typed_slots_then_fills_raw(
    max_items: int,
    raw_count: int,
    typed_count: int,
    selected_raw: int,
    typed_overflow: int,
) -> None:
    module = _load_memory_module()
    typed = [
        {
            "id": f"procedure-{index}",
            "type": "procedure",
            "content": "Typed projection",
            "_selection_origin": "context_pack:procedures",
            "metadata": {"longmemeval_v2_trajectory_id": f"typed-{index}"},
        }
        for index in range(8)
    ]
    raw = [
        {
            "id": f"session-{index}",
            "type": "session",
            "content": "Raw support",
            "_selection_origin": "search",
        }
        for index in range(raw_count)
    ]

    selected, metadata = module.compile_operational_evidence_set(
        query="anything",
        typed_results=typed,
        raw_results=raw,
        max_items=max_items,
        mode="shared_relevance",
    )

    assert len(selected) == max_items
    assert metadata["selected_typed_count"] == typed_count
    assert metadata["selected_raw_count"] == selected_raw
    assert metadata["selected_typed_overflow_count"] == typed_overflow


def test_operational_evidence_set_preserves_reserved_support_when_selected() -> None:
    module = _load_memory_module()
    typed = [
        {
            "id": f"procedure-{index}",
            "type": "procedure",
            "content": "Typed projection",
            "_selection_origin": "context_pack:procedures",
            "metadata": {"longmemeval_v2_trajectory_id": f"t{index}"},
        }
        for index in range(4)
    ]
    raw = [
        {
            "id": f"session-{index}",
            "type": "session",
            "content": "Raw support",
            "_selection_origin": "search",
        }
        for index in range(8)
    ]

    selected, metadata = module.compile_operational_evidence_set(
        query="anything",
        typed_results=typed,
        raw_results=raw,
        max_items=8,
        mode="reserved_support",
    )

    assert [item["id"] for item in selected] == [
        "procedure-0",
        "procedure-1",
        "session-0",
        "session-1",
        "session-2",
        "session-3",
        "session-4",
        "session-5",
    ]
    assert metadata["mode"] == "reserved_support"
    assert metadata["selected_typed_count"] == EXPECTED_OPERATIONAL_TYPED_ITEMS
    assert metadata["selected_raw_count"] == EXPECTED_OPERATIONAL_RAW_ITEMS


def test_operational_evidence_set_uses_shared_relevance_by_default() -> None:
    module = _load_memory_module()
    typed = [
        {
            "id": f"procedure-{index}",
            "type": "procedure",
            "content": "Typed projection",
            "_selection_origin": "context_pack:procedures",
            "metadata": {"longmemeval_v2_trajectory_id": f"t{index}"},
        }
        for index in range(4)
    ]
    raw = [
        {
            "id": f"session-{index}",
            "type": "session",
            "content": "Raw support",
            "_selection_origin": "search",
        }
        for index in range(8)
    ]

    selected, metadata = module.compile_operational_evidence_set(
        query="anything",
        typed_results=typed,
        raw_results=raw,
        max_items=8,
    )

    assert len(selected) == EXPECTED_OPERATIONAL_EVIDENCE_ITEMS
    assert metadata["mode"] == "shared_relevance"
    assert metadata["selected_typed_count"] == EXPECTED_SHARED_RELEVANCE_TYPED_ITEMS
    assert metadata["selected_raw_count"] == EXPECTED_SHARED_RELEVANCE_RAW_ITEMS


@pytest.mark.parametrize(
    ("support_origin", "parent_key"),
    [("neighbor", "_neighbor_of_search_rank"), ("state_part", "_state_part_of_search_rank")],
)
def test_shared_relevance_preserves_linked_raw_support(
    support_origin: str,
    parent_key: str,
) -> None:
    module = _load_memory_module()
    typed = [
        {
            "id": f"event-{index}",
            "type": "event",
            "content": "Typed projection",
            "_selection_origin": "context_pack:recent_memory",
            "metadata": {"longmemeval_v2_trajectory_id": f"typed-{index}"},
        }
        for index in range(2)
    ]
    raw = [
        {
            "id": f"session-{index}",
            "type": "session",
            "content": f"Raw seed {index}",
            "_selection_origin": "search",
            "_search_rank": index + 1,
        }
        for index in range(8)
    ]
    raw.append(
        {
            "id": "linked-support",
            "type": "session",
            "content": "Linked raw support",
            "_selection_origin": support_origin,
            parent_key: 1,
        }
    )

    selected, metadata = module.compile_operational_evidence_set(
        query="Which linked raw support was recorded?",
        typed_results=typed,
        raw_results=raw,
        max_items=8,
        mode="shared_relevance",
    )

    selected_ids = {item["id"] for item in selected}
    assert {"session-0", "linked-support"} <= selected_ids
    assert metadata["selected_raw_support_count"] == 1


@pytest.mark.parametrize("typed_count", [0, 1])
def test_reserved_support_does_not_duplicate_raw_when_typed_is_sparse(
    typed_count: int,
) -> None:
    module = _load_memory_module()
    typed = [
        {
            "id": "event-0",
            "type": "event",
            "content": "Typed projection",
            "_selection_origin": "context_pack:recent_memory",
            "metadata": {"longmemeval_v2_trajectory_id": "t0"},
        }
    ][:typed_count]
    raw = [
        {
            "id": f"session-{index}",
            "type": "session",
            "content": "Raw support",
            "_selection_origin": "search",
        }
        for index in range(8)
    ]

    selected, metadata = module.compile_operational_evidence_set(
        query="anything",
        typed_results=typed,
        raw_results=raw,
        max_items=8,
    )

    assert len(selected) == EXPECTED_OPERATIONAL_EVIDENCE_ITEMS
    assert len({item["id"] for item in selected}) == len(selected)
    assert metadata["selected_typed_count"] == typed_count
    assert metadata["selected_raw_count"] == 8 - typed_count


@pytest.mark.parametrize("typed_count", [0, 1])
def test_shared_relevance_falls_back_to_raw_when_typed_is_sparse(typed_count: int) -> None:
    module = _load_memory_module()
    typed = [
        {
            "id": "event-0",
            "type": "event",
            "content": "Typed projection",
            "_selection_origin": "context_pack:recent_memory",
            "metadata": {"longmemeval_v2_trajectory_id": "t0"},
        }
    ][:typed_count]
    raw = [
        {
            "id": f"session-{index}",
            "type": "session",
            "content": "Raw support",
            "_selection_origin": "search",
        }
        for index in range(8)
    ]

    selected, metadata = module.compile_operational_evidence_set(
        query="anything",
        typed_results=typed,
        raw_results=raw,
        max_items=8,
        mode="shared_relevance",
    )

    assert len(selected) == EXPECTED_OPERATIONAL_EVIDENCE_ITEMS
    assert len({item["id"] for item in selected}) == len(selected)
    assert metadata["selected_typed_count"] == typed_count
    assert metadata["selected_raw_count"] == 8 - typed_count


def test_operational_evidence_set_admits_relevant_typed_memory() -> None:
    module = _load_memory_module()
    typed = [
        {
            "id": "procedure-priority",
            "type": "procedure",
            "content": "Open the Priority menu and select Critical",
            "score": 1.2,
            "_selection_origin": "context_pack:procedures",
            "metadata": {"longmemeval_v2_trajectory_id": "t1"},
        }
    ]
    raw = [
        {
            "id": f"session-{index}",
            "type": "session",
            "content": "General settings overview",
            "score": 1.0 - (index / 10),
            "_selection_origin": "search",
        }
        for index in range(3)
    ]

    selected, metadata = module.compile_operational_evidence_set(
        query="How do I change the Priority to Critical?",
        typed_results=typed,
        raw_results=raw,
        max_items=2,
        mode="shared_relevance",
    )

    assert selected[0]["id"] == "procedure-priority"
    assert metadata["selected_typed_count"] == 1
    assert metadata["selected_raw_count"] == 1


def test_shared_relevance_preserves_upstream_raw_order() -> None:
    module = _load_memory_module()
    raw = [
        {
            "id": "header-only",
            "type": "session",
            "content": (
                "Goal: inventory order dashboard prefix\n\n"
                "State 1\nAccessibility tree:\nUnrelated incident list"
            ),
            "score": 1.0,
            "_selection_origin": "search",
            "_search_rank": 1,
        },
        {
            "id": "state-match",
            "type": "session",
            "content": (
                "Goal: unrelated task\n\n"
                "State 2\nAccessibility tree:\ninventory order dashboard prefix"
            ),
            "score": 0.5,
            "_selection_origin": "search",
            "_search_rank": 2,
        },
    ]

    selected, metadata = module.compile_operational_evidence_set(
        query="inventory order dashboard prefix",
        typed_results=[],
        raw_results=raw,
        max_items=1,
        mode="shared_relevance",
    )

    assert [item["id"] for item in selected] == ["header-only"]
    assert metadata["ranking_applied"] is True
    assert metadata["ranking_changed"] is False


def test_shared_relevance_selects_support_with_its_parent_seed() -> None:
    module = _load_memory_module()
    typed = [
        {
            "id": "event-0",
            "type": "event",
            "content": "Typed projection",
            "_selection_origin": "context_pack:recent_memory",
            "metadata": {"longmemeval_v2_trajectory_id": "typed-0"},
        }
    ]
    raw = [
        {
            "id": "support-1",
            "type": "session",
            "content": "The value shown for the Deployment Ring is Critical",
            "_selection_origin": "neighbor",
            "_neighbor_of_search_rank": 1,
        },
        {
            "id": "parent-1",
            "type": "session",
            "content": "Deployment settings overview",
            "_selection_origin": "search",
            "_search_rank": 1,
        },
        {
            "id": "parent-2",
            "type": "session",
            "content": "Deployment Ring overview",
            "_selection_origin": "search",
            "_search_rank": 2,
        },
    ]

    selected, metadata = module.compile_operational_evidence_set(
        query="Which value is shown for the Deployment Ring?",
        typed_results=typed,
        raw_results=raw,
        max_items=3,
        mode="shared_relevance",
    )

    assert [item["id"] for item in selected[1:]] == ["parent-1", "support-1"]
    assert metadata["selected_raw_support_count"] == 1


def test_shared_relevance_preserves_multiple_support_pairs() -> None:
    module = _load_memory_module()
    raw = [
        {
            "id": f"parent-{index}",
            "type": "session",
            "content": "Deployment settings",
            "_selection_origin": "search",
            "_search_rank": index,
        }
        for index in range(1, 4)
    ]
    raw.extend(
        {
            "id": f"support-{index}",
            "type": "session",
            "content": "Deployment region value settings",
            "_selection_origin": "neighbor",
            "_neighbor_of_search_rank": index,
        }
        for index in range(1, 4)
    )

    selected, metadata = module.compile_operational_evidence_set(
        query="Which deployment region value settings are shown?",
        typed_results=[],
        raw_results=raw,
        max_items=6,
        mode="shared_relevance",
    )

    assert {item["id"] for item in selected} == {
        "parent-1",
        "parent-2",
        "parent-3",
        "support-1",
        "support-2",
        "support-3",
    }
    assert metadata["selected_raw_support_count"] == EXPECTED_OPERATIONAL_SUPPORT_ITEMS


def test_shared_relevance_diversifies_grouped_support() -> None:
    module = _load_memory_module()
    raw = [
        {
            "id": f"parent-{index}",
            "type": "session",
            "content": "Deployment settings",
            "_selection_origin": "search",
            "_search_rank": index,
        }
        for index in range(1, 4)
    ]
    raw.extend(
        [
            {
                "id": "support-1a",
                "type": "session",
                "content": "Deployment region value settings",
                "_selection_origin": "neighbor",
                "_neighbor_of_search_rank": 1,
            },
            {
                "id": "support-2",
                "type": "session",
                "content": "Deployment region value settings",
                "_selection_origin": "neighbor",
                "_neighbor_of_search_rank": 2,
            },
            {
                "id": "support-3",
                "type": "session",
                "content": "Deployment region value settings",
                "_selection_origin": "neighbor",
                "_neighbor_of_search_rank": 3,
            },
            {
                "id": "support-1b",
                "type": "session",
                "content": "Deployment region value settings",
                "_selection_origin": "state_part",
                "_state_part_of_search_rank": 1,
            },
        ]
    )

    selected, metadata = module.compile_operational_evidence_set(
        query="Which deployment region value settings are shown?",
        typed_results=[],
        raw_results=raw,
        max_items=6,
        mode="shared_relevance",
    )

    assert {item["id"] for item in selected} == {
        "parent-1",
        "parent-2",
        "parent-3",
        "support-1a",
        "support-2",
        "support-3",
    }
    assert metadata["selected_raw_support_count"] == EXPECTED_OPERATIONAL_SUPPORT_ITEMS


def test_shared_relevance_rejects_orphan_support() -> None:
    module = _load_memory_module()
    raw = [
        {
            "id": f"parent-{index}",
            "type": "session",
            "content": "Deployment settings",
            "_selection_origin": "search",
            "_search_rank": index,
        }
        for index in range(1, 3)
    ]
    raw.append(
        {
            "id": "orphan-support",
            "type": "session",
            "content": "Deployment settings",
            "_selection_origin": "state_part",
            "_state_part_of_search_rank": 99,
        }
    )

    selected, metadata = module.compile_operational_evidence_set(
        query="Which deployment settings are shown?",
        typed_results=[],
        raw_results=raw,
        max_items=3,
        mode="shared_relevance",
    )

    assert [item["id"] for item in selected] == ["parent-1", "parent-2"]
    assert metadata["selected_raw_support_count"] == 0


@pytest.mark.parametrize("mode", ["reserved_support", "shared_relevance"])
def test_operational_evidence_set_one_slot_excludes_support(mode: str) -> None:
    module = _load_memory_module()
    raw = [
        {
            "id": "parent",
            "type": "session",
            "content": "Deployment settings",
            "_selection_origin": "search",
            "_search_rank": 1,
        },
        {
            "id": "support",
            "type": "session",
            "content": "Deployment settings",
            "_selection_origin": "neighbor",
            "_neighbor_of_search_rank": 1,
        },
    ]

    selected, metadata = module.compile_operational_evidence_set(
        query="Which deployment settings are shown?",
        typed_results=[],
        raw_results=raw,
        max_items=1,
        mode=mode,
    )

    assert [item["id"] for item in selected] == ["parent"]
    assert metadata["selected_raw_support_count"] == 0


@pytest.mark.parametrize("mode", ["reserved_support", "shared_relevance"])
def test_operational_evidence_set_does_not_duplicate_malformed_support(mode: str) -> None:
    module = _load_memory_module()
    raw = [
        {
            "id": "parent",
            "type": "session",
            "content": "Deployment settings",
            "_selection_origin": "search",
            "_search_rank": 1,
        },
        {
            "id": "malformed-primary",
            "type": "session",
            "content": "Deployment settings",
            "_selection_origin": "search",
            "_search_rank": 2,
            "_neighbor_of_search_rank": 1,
        },
    ]

    selected, metadata = module.compile_operational_evidence_set(
        query="Which deployment settings are shown?",
        typed_results=[],
        raw_results=raw,
        max_items=3,
        mode=mode,
    )

    assert [item["id"] for item in selected] == ["parent", "malformed-primary"]
    assert metadata["selected_raw_support_count"] == 0


def test_sibyl_memory_insert_tracks_deferred_background_jobs() -> None:
    module = _load_memory_module()
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})
    calls: list[_RequestCall] = []

    def fake_request(
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        calls.append({"method": method, "path": path, "json": json or {}, "params": params or {}})
        return {
            "written_entities": 4,
            "manifest_id": "artifact-lme-v2-1",
            "entity_ids": ["session-lme-v2-1", "event-lme-v2-1"],
            "background_jobs": {
                "embedding_backfill": {
                    "status": "queued",
                    "job_ids": ["embed-lme-v2-1"],
                },
            },
        }

    memory.project_id = "project_lme"
    memory.run_id = "run_lme"
    memory.content_max_chars = TEST_CONTENT_MAX_CHARS
    memory.bulk_max_entities = 16
    memory.bulk_max_content_chars = 200_000
    memory.embedding_backfill_max_pending_jobs = 8
    memory.include_screenshot_refs = False
    memory.defer_embeddings = True
    memory.created_entities = 0
    memory.inserted_trajectories = 0
    memory._pending_embedding_job_ids = set()
    memory._pending_projection_job_ids = set()
    memory._request_json = fake_request

    memory.insert(_trajectory("t1"))

    request_json = calls[0]["json"]
    assert isinstance(request_json, dict)
    assert calls[0]["path"] == "/memory/experience"
    assert request_json["defer_embeddings"] is True
    experience = cast(dict[str, object], request_json["experience"])
    assert experience["source_id"] == "longmemeval-v2:run_lme:t1"
    observations = cast(list[dict[str, object]], experience["observations"])
    assert observations
    assert observations[0]["evidence"]
    assert memory.created_entities == EXPECTED_OPERATIONAL_CREATED_ENTITIES
    assert memory.inserted_trajectories == 1
    assert memory._pending_embedding_job_ids == {"embed-lme-v2-1"}
    assert memory._pending_projection_job_ids == set()
    assert memory._pending_job_entity_ids == {
        "embed-lme-v2-1": ["session-lme-v2-1", "event-lme-v2-1"],
    }
    assert memory._pending_job_manifest_ids == {
        "embed-lme-v2-1": "artifact-lme-v2-1",
    }


def test_sibyl_memory_retries_write_after_pre_checkpoint_crash(tmp_path: Path) -> None:
    module = _load_memory_module()
    checkpoint_dir = tmp_path / "checkpoint"
    requests: list[dict[str, object]] = []

    def fake_request(
        _method: str,
        _path: str,
        *,
        json: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        del params
        requests.append(json or {})
        return {
            "written_entities": 2,
            "entity_ids": ["session-deterministic"],
            "background_jobs": {
                "embedding_backfill": {"job_ids": ["embed-deterministic"]},
            },
        }

    def new_memory() -> Any:
        memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
        module.Memory.__init__(memory, {})
        memory.api_url = "http://127.0.0.1:3434/api"
        memory.project_id = "project_lme"
        memory.run_id = "run_lme"
        memory.chunking_mode = "state"
        memory.content_max_chars = TEST_CONTENT_MAX_CHARS
        memory.bulk_max_entities = 16
        memory.bulk_max_content_chars = 200_000
        memory.embedding_backfill_max_pending_jobs = 8
        memory.include_screenshot_refs = False
        memory.defer_embeddings = True
        memory.checkpoint_dir = checkpoint_dir
        memory.created_entities = 0
        memory.inserted_trajectories = 0
        memory.ingest_embedding_usage = {}
        memory.ingest_api_runtime = {"version": "test"}
        memory._chunk_catalog = {}
        memory._completed_trajectory_ids = set()
        memory._pending_embedding_job_ids = set()
        memory._pending_projection_job_ids = set()
        memory._pending_job_entity_ids = {}
        memory._request_json = fake_request
        return memory

    interrupted = new_memory()
    interrupted._append_checkpoint = lambda _payloads: (_ for _ in ()).throw(
        RuntimeError("simulated crash")
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        interrupted.insert(_trajectory("t1"))
    assert not (checkpoint_dir / module.CHECKPOINT_MANIFEST_FILENAME).exists()

    resumed = new_memory()
    resumed.insert(_trajectory("t1"))

    assert len(requests) == EXPECTED_MEMORY_API_RETRY_CALLS
    assert requests[0] == requests[1]
    assert (checkpoint_dir / module.CHECKPOINT_MANIFEST_FILENAME).is_file()


def test_sibyl_memory_project_creation_defers_and_tracks_embeddings() -> None:
    module = _load_memory_module()
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})
    requests: list[dict[str, object]] = []

    def fake_request(
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        del params
        assert method == "POST"
        assert path == "/entities"
        assert isinstance(json, dict)
        requests.append(json)
        return {
            "id": "project_lme",
            "background_jobs": {
                "embedding_backfill": {
                    "status": "queued",
                    "job_ids": ["embed-project-1"],
                }
            },
        }

    memory.run_id = "run_lme"
    memory.defer_embeddings = True
    memory._pending_embedding_job_ids = set()
    memory._pending_projection_job_ids = set()
    memory._request_json = fake_request

    assert memory._create_project() == "project_lme"
    assert requests[0]["defer_embeddings"] is True
    assert memory._pending_embedding_job_ids == {"embed-project-1"}
    assert memory._pending_job_entity_ids == {"embed-project-1": ["project_lme"]}


def test_sibyl_memory_batches_payloads_by_entity_count_and_content_size() -> None:
    module = _load_memory_module()

    batches = module._payload_batches(
        [
            {"name": "a", "description": "", "content": "aaaaa"},
            {"name": "b", "description": "", "content": "bbbbb"},
            {"name": "c", "description": "", "content": "cccccccccccc"},
            {"name": "d", "description": "", "content": "ddddd"},
        ],
        max_entities=2,
        max_content_chars=14,
    )

    assert [[item["name"] for item in batch] for batch in batches] == [
        ["a", "b"],
        ["c"],
        ["d"],
    ]


def test_sibyl_memory_drains_backfills_when_pending_threshold_is_reached() -> None:
    module = _load_memory_module()
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})
    calls = 0
    embedding_drain_calls = 0
    projection_drain_calls = 0

    def fake_request(
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        nonlocal calls
        del method, path, json, params
        calls += 1
        return {
            "created": 1,
            "background_jobs": {
                "embedding_backfill": {
                    "status": "queued",
                    "job_ids": [f"embed-lme-v2-{calls}"],
                },
                "memory_projection": {
                    "status": "queued",
                    "job_ids": [f"project-lme-v2-{calls}"],
                },
            },
        }

    def fake_embedding_drain() -> None:
        nonlocal embedding_drain_calls
        embedding_drain_calls += 1
        memory._pending_embedding_job_ids.clear()

    def fake_projection_drain() -> None:
        nonlocal projection_drain_calls
        projection_drain_calls += 1
        memory._pending_projection_job_ids.clear()

    memory.project_id = "project_lme"
    memory.run_id = "run_lme"
    memory.content_max_chars = TEST_CONTENT_MAX_CHARS
    memory.bulk_max_entities = 1
    memory.bulk_max_content_chars = 200_000
    memory.embedding_backfill_max_pending_jobs = 1
    memory.include_screenshot_refs = False
    memory.defer_embeddings = True
    memory.created_entities = 0
    memory.inserted_trajectories = 0
    memory._pending_embedding_job_ids = set()
    memory._pending_projection_job_ids = set()
    memory._request_json = fake_request
    memory._drain_embedding_backfills = fake_embedding_drain
    memory._drain_memory_projections = fake_projection_drain

    memory.insert(_trajectory("t1", tree="button Priority " * 20))

    assert calls == 1
    assert embedding_drain_calls == 1
    assert projection_drain_calls == 0
    assert memory._pending_embedding_job_ids == set()
    assert memory._pending_projection_job_ids == set()


def test_sibyl_memory_insert_rejects_missing_deferred_embedding_job() -> None:
    module = _load_memory_module()
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})

    memory.defer_embeddings = True
    memory._pending_embedding_job_ids = set()

    with pytest.raises(RuntimeError, match="returned no backfill job ids"):
        memory._remember_embedding_backfill_jobs({"created": 1, "background_jobs": {}})


def test_sibyl_memory_insert_rejects_degraded_embedding_reenqueue() -> None:
    module = _load_memory_module()
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})

    memory.defer_embeddings = True
    memory._pending_embedding_job_ids = set()

    with pytest.raises(RuntimeError, match="enqueue degraded: enqueue_failed"):
        memory._remember_embedding_backfill_jobs(
            {
                "written_entities": 0,
                "background_jobs": {
                    "embedding_backfill": {
                        "status": "degraded",
                        "job_ids": [],
                        "error": "enqueue_failed",
                    }
                },
            }
        )


def test_sibyl_memory_embedding_wait_timeout_resets_on_progress(monkeypatch, capsys) -> None:
    module = _load_memory_module()
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})
    clock = [0.0]
    status_responses: list[dict[str, object]] = [
        {"status": "queued"},
        {"status": "in_progress"},
        {"status": "in_progress"},
        {"status": "complete", "error": None},
    ]

    def fake_request(
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        del method, path, params
        assert json == {"job_ids": ["embed-lme-v2-1"]}
        return {"jobs": {"embed-lme-v2-1": status_responses.pop(0)}}

    def fake_sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.time, "sleep", fake_sleep)
    memory.embedding_job_wait_timeout_seconds = 1.0
    memory.embedding_job_poll_seconds = 0.6
    memory._pending_embedding_job_ids = {"embed-lme-v2-1"}
    memory._request_json = fake_request

    memory._drain_embedding_backfills()

    assert clock[0] == pytest.approx(1.8)
    assert memory._pending_embedding_job_ids == set()
    progress = capsys.readouterr().err
    assert "pending queued=1" in progress
    assert "pending in_progress=1" in progress
    assert "1/1 complete; pending none" in progress


def test_sibyl_memory_embedding_wait_times_out_without_progress(monkeypatch) -> None:
    module = _load_memory_module()
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})
    clock = [0.0]

    def fake_request(
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        del method, path, params
        assert json == {"job_ids": ["embed-lme-v2-1"]}
        return {"jobs": {"embed-lme-v2-1": {"status": "queued"}}}

    def fake_sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.time, "sleep", fake_sleep)
    memory.embedding_job_wait_timeout_seconds = 1.0
    memory.embedding_job_poll_seconds = 0.6
    memory._pending_embedding_job_ids = {"embed-lme-v2-1"}
    memory._request_json = fake_request

    with pytest.raises(RuntimeError, match="without embedding backfill progress"):
        memory._drain_embedding_backfills()

    assert clock[0] == pytest.approx(1.2)
    assert memory._pending_embedding_job_ids == {"embed-lme-v2-1"}


def test_sibyl_memory_projection_rejects_partial_job_result() -> None:
    module = _load_memory_module()
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})
    memory.embedding_job_wait_timeout_seconds = 1.0
    memory.embedding_job_poll_seconds = 0.1
    memory._pending_projection_job_ids = {"project-lme-v2-1"}
    memory._request_json = lambda *_args, **_kwargs: {
        "jobs": {
            "project-lme-v2-1": {
                "status": "complete",
                "error": None,
                "result": {
                    "projection_state": "partial",
                    "errors": ["Transaction conflict: Resource busy"],
                },
            },
        },
    }

    with pytest.raises(RuntimeError, match=r"completed partially.*Resource busy"):
        memory._drain_memory_projections()

    assert memory._pending_projection_job_ids == {"project-lme-v2-1"}


def test_sibyl_memory_query_rejects_unfinalized_background_jobs() -> None:
    module = _load_memory_module()
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})
    calls: list[str] = []

    def fake_request(
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        del method, params
        calls.append(path)
        raise AssertionError(f"unexpected path: {path}")

    memory.project_id = "project_lme"
    memory.search_limit = 12
    memory.max_context_items = 8
    memory.max_context_chars_per_item = TEST_CONTEXT_MAX_CHARS
    memory.embedding_job_wait_timeout_seconds = 5.0
    memory.embedding_job_poll_seconds = 0.0
    memory._pending_embedding_job_ids = {"embed-lme-v2-1"}
    memory._pending_projection_job_ids = {"project-lme-v2-1"}
    memory._ingest_finalized = False
    memory._request_json = fake_request

    with pytest.raises(RuntimeError, match="call finalize_ingest first"):
        memory.query("Which filter was selected?")

    assert calls == []
    assert memory._pending_embedding_job_ids == {"embed-lme-v2-1"}
    assert memory._pending_projection_job_ids == {"project-lme-v2-1"}


def test_sibyl_memory_finalize_drains_jobs_before_search() -> None:
    module = _load_memory_module()
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})
    calls: list[str] = []

    memory.project_id = "project_lme"
    memory.api_url = "http://localhost:3434/api"
    memory.api_runtime = {
        "status": "healthy",
        "version": "1.1.0",
        "runtime": {"commit": "abc123", "git_dirty": False},
    }
    memory.run_id = "run_lme"
    memory.search_limit = 12
    memory.max_context_items = 8
    memory.max_context_chars_per_item = TEST_CONTEXT_MAX_CHARS
    memory.inserted_trajectories = 2
    memory.created_entities = EXPECTED_OPERATIONAL_CREATED_ENTITIES
    memory.defer_embeddings = True
    memory.ingest_embedding_usage = {}
    memory.embedding_job_wait_timeout_seconds = 5.0
    memory.embedding_job_poll_seconds = 0.0
    memory._pending_embedding_job_ids = {"embed-lme-v2-1"}
    memory._pending_projection_job_ids = {"project-lme-v2-1"}
    memory._finalize_lock = threading.Lock()
    memory._query_local = threading.local()
    memory._ingest_finalized = False
    memory._request_json = _finalize_request_handler(calls)

    memory.finalize_ingest()
    assert memory.query("Which filter was selected?") == []
    metadata = memory.post_query_hook(
        query="Which filter was selected?",
        query_image=None,
        memory_context=[],
    )
    memory.finalize_ingest()

    assert calls == [
        "/jobs/status",
        "/jobs/status",
        "/context/pack",
    ]
    assert metadata is not None
    assert metadata["search_metadata"] == {
        "retrieval_mode": "native",
        "stage_timings_ms": {"total": 12.5},
        "adapter_assembly": {
            "input_result_count": 0,
            "restored_search_result_count": 0,
            "restored_transport_content_chars": 0,
            "restored_source_content_chars": 0,
            "selected_search_seed_count": 0,
            "completed_state_part_count": 0,
            "stitched_neighbor_count": 0,
            "output_result_count": 0,
            "max_chunks_per_trajectory": EXPECTED_MAX_CHUNKS_PER_TRAJECTORY,
            "neighbor_stitch_items": EXPECTED_NEIGHBOR_STITCH_ITEMS,
            "neighbor_stitch_span": EXPECTED_NEIGHBOR_STITCH_SPAN,
            "state_part_completion": {
                "enabled": False,
                "candidate_count": 0,
                "ranking_applied": False,
                "admitted_chunk_keys": [],
            },
            "trajectory_refinement": {
                "enabled": False,
                "query_focus_phrases": [],
                "query_ui_roles": [],
                "inspected_trajectory_count": 0,
                "candidate_count": 0,
                "replacements": [],
            },
            "state_part_refinement": {
                "enabled": False,
                "inspected_state_count": 0,
                "candidate_count": 0,
                "ranking_applied_count": 0,
                "replacements": [],
                "min_score_gain": 0.05,
            },
            "context_expansion_budget": {
                "enabled": False,
                "max_ratio": None,
                "base_item_count": 0,
                "unbounded_item_count": 0,
                "final_item_count": 0,
                "base_token_count": None,
                "max_token_count": None,
                "unbounded_token_count": None,
                "final_token_count": None,
                "dropped_item_count": 0,
                "dropped_chunk_keys": [],
                "binding": False,
            },
            "typed_context_candidate_count": 0,
            "typed_context_selected_count": 0,
            "evidence_composition": {
                "mode": "shared_relevance",
                "candidate_count": 0,
                "typed_candidate_count": 0,
                "raw_candidate_count": 0,
                "ranking_applied": False,
                "ranking_changed": False,
                "pool_calibration": "independent_query_coverage",
                "typed_reservation": 0,
                "selected_typed_overflow_count": 0,
                "selected_raw_support_count": 0,
                "selected_typed_count": 0,
                "selected_raw_count": 0,
            },
            "context_budget": {
                "enabled": True,
                "max_total_chars": EXPECTED_CONTEXT_TOTAL_CHARS,
                "max_chars_per_item": TEST_CONTEXT_MAX_CHARS,
                "candidate_item_count": 0,
                "rendered_item_count": 0,
                "dropped_item_count": 0,
                "dropped_entity_ids": [],
                "per_item_limited_chars": 0,
                "rendered_context_chars": 0,
                "truncated_item_count": 0,
                "binding": False,
                "items": [],
            },
        },
    }
    assert metadata["retrieval_trace"] == []
    assert metadata["api_runtime"]["runtime"] == {
        "commit": "abc123",
        "git_dirty": False,
    }
    assert metadata["ingest_embedding_usage"] == {
        "provider": "openai",
        "model": "text-embedding-3-small",
        "requests": 1,
        "inputs": 4,
        "prompt_tokens": 100,
        "total_tokens": 100,
        "cost_reported_requests": 0,
        "cost_usd": 0.0,
    }


def test_sibyl_memory_polls_pending_jobs_in_one_batch() -> None:
    module = _load_memory_module()
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})
    requests: list[dict[str, object]] = []

    def fake_request(
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        del params
        assert method == "POST"
        assert path == "/jobs/status"
        assert isinstance(json, dict)
        requests.append(json)
        job_ids = json["job_ids"]
        assert isinstance(job_ids, list)
        return {"jobs": {job_id: {"status": "complete", "error": None} for job_id in job_ids}}

    memory.embedding_job_wait_timeout_seconds = 1.0
    memory.embedding_job_poll_seconds = 0.1
    memory._pending_embedding_job_ids = {"embed-1", "embed-2", "embed-3"}
    memory._request_json = fake_request

    memory._drain_embedding_backfills()

    assert requests == [{"job_ids": ["embed-1", "embed-2", "embed-3"]}]


def test_sibyl_memory_requeues_job_lost_after_broker_restart() -> None:
    module = _load_memory_module()
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})
    paths: list[str] = []
    status_calls = 0

    def fake_request(
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        nonlocal status_calls
        del params
        paths.append(path)
        if path == "/jobs/status":
            assert method == "POST"
            assert json == {"job_ids": ["embed-lost"]}
            status_calls += 1
            status = "not_found" if status_calls == 1 else "complete"
            return {
                "jobs": {
                    "embed-lost": {
                        "status": status,
                        "error": None,
                        "result": {},
                    }
                }
            }
        assert path == "/entities/bulk/requeue-background-jobs"
        assert json == {
            "entity_ids": ["session-one"],
            "jobs": ["embedding_backfill"],
        }
        return {
            "entity_ids": ["session-one"],
            "background_jobs": {
                "embedding_backfill": {
                    "status": "queued",
                    "job_ids": ["embed-lost"],
                }
            },
        }

    memory.defer_embeddings = True
    memory.checkpoint_dir = None
    memory.embedding_job_wait_timeout_seconds = 1.0
    memory.embedding_job_poll_seconds = 0.0
    memory.ingest_embedding_usage = {}
    memory._pending_embedding_job_ids = {"embed-lost"}
    memory._pending_projection_job_ids = set()
    memory._pending_job_entity_ids = {"embed-lost": ["session-one"]}
    memory._request_json = fake_request

    memory._drain_embedding_backfills()

    assert paths == [
        "/jobs/status",
        "/entities/bulk/requeue-background-jobs",
        "/jobs/status",
    ]
    assert memory._pending_embedding_job_ids == set()
    assert memory._pending_job_entity_ids == {}


def test_sibyl_memory_requeues_failed_job_once() -> None:
    module = _load_memory_module()
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})
    paths: list[str] = []
    status_calls = 0

    def fake_request(
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        nonlocal status_calls
        del params
        paths.append(path)
        if path == "/jobs/status":
            assert method == "POST"
            status_calls += 1
            return {
                "jobs": {
                    "embed-failed": {
                        "status": "complete",
                        "error": "provider unavailable" if status_calls == 1 else None,
                        "result": {},
                    }
                }
            }
        assert path == "/entities/bulk/requeue-background-jobs"
        return {
            "entity_ids": ["session-one"],
            "background_jobs": {
                "embedding_backfill": {
                    "status": "queued",
                    "job_ids": ["embed-failed"],
                }
            },
        }

    memory.defer_embeddings = True
    memory.checkpoint_dir = None
    memory.embedding_job_wait_timeout_seconds = 1.0
    memory.embedding_job_poll_seconds = 0.0
    memory.ingest_embedding_usage = {}
    memory._pending_embedding_job_ids = {"embed-failed"}
    memory._pending_projection_job_ids = set()
    memory._pending_job_entity_ids = {"embed-failed": ["session-one"]}
    memory._request_json = fake_request

    memory._drain_embedding_backfills()

    assert paths == [
        "/jobs/status",
        "/entities/bulk/requeue-background-jobs",
        "/jobs/status",
    ]
    assert memory._pending_embedding_job_ids == set()
    assert memory._pending_job_entity_ids == {}


def test_sibyl_memory_requeues_large_operational_job_by_manifest() -> None:
    module = _load_memory_module()
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})
    manifest_id = "artifact-operational-manifest"
    entity_ids = [*(f"event-{index}" for index in range(129)), manifest_id]
    requests: list[dict[str, object]] = []

    def fake_request(
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        del params
        assert method == "POST"
        assert path == "/entities/bulk/requeue-background-jobs"
        assert isinstance(json, dict)
        requests.append(json)
        return {
            "manifest_id": manifest_id,
            "entity_ids": entity_ids,
            "background_jobs": {
                "embedding_backfill": {
                    "status": "queued",
                    "job_ids": ["embed-replacement"],
                }
            },
        }

    memory.defer_embeddings = True
    memory.checkpoint_dir = None
    memory._pending_embedding_job_ids = {"embed-lost"}
    memory._pending_projection_job_ids = set()
    memory._pending_job_entity_ids = {"embed-lost": entity_ids}
    memory._pending_job_manifest_ids = {"embed-lost": manifest_id}
    memory._request_json = fake_request

    replacements = memory._recover_background_job(
        "embed-lost",
        job_kind="embedding_backfill",
    )

    assert requests == [{"manifest_id": manifest_id, "jobs": ["embedding_backfill"]}]
    assert replacements == {"embed-replacement"}
    assert memory._pending_embedding_job_ids == {"embed-replacement"}
    assert memory._pending_job_manifest_ids == {
        "embed-replacement": manifest_id,
    }


def test_sibyl_memory_treats_completed_manifest_recovery_as_done() -> None:
    module = _load_memory_module()
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})
    manifest_id = "artifact-complete-manifest"
    paths: list[str] = []

    def fake_request(
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        del params
        assert method == "POST"
        paths.append(path)
        if path == "/jobs/status":
            return {"jobs": {"embed-lost": {"status": "not_found"}}}
        assert path == "/entities/bulk/requeue-background-jobs"
        assert json == {"manifest_id": manifest_id, "jobs": ["embedding_backfill"]}
        return {
            "manifest_id": manifest_id,
            "entity_ids": [manifest_id],
            "background_jobs": {
                "embedding_backfill": {
                    "status": "skipped",
                    "job_ids": [],
                    "reason": "manifest_complete",
                }
            },
        }

    memory.defer_embeddings = True
    memory.checkpoint_dir = None
    memory.embedding_job_wait_timeout_seconds = 1.0
    memory.embedding_job_poll_seconds = 0.0
    memory._pending_embedding_job_ids = {"embed-lost"}
    memory._pending_projection_job_ids = set()
    memory._pending_job_entity_ids = {"embed-lost": ["event-0", manifest_id]}
    memory._pending_job_manifest_ids = {"embed-lost": manifest_id}
    memory._request_json = fake_request

    memory._drain_embedding_backfills()

    assert paths == ["/jobs/status", "/entities/bulk/requeue-background-jobs"]
    assert memory._pending_embedding_job_ids == set()
    assert memory._pending_job_entity_ids == {}
    assert memory._pending_job_manifest_ids == {}


def test_sibyl_memory_projection_recovery_ignores_manifest_mapping() -> None:
    module = _load_memory_module()
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})
    requests: list[dict[str, object]] = []

    def fake_request(
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        del params
        assert method == "POST"
        assert path == "/entities/bulk/requeue-background-jobs"
        assert isinstance(json, dict)
        requests.append(json)
        return {
            "entity_ids": ["session-one"],
            "background_jobs": {
                "memory_projection": {
                    "status": "queued",
                    "job_ids": ["projection-replacement"],
                }
            },
        }

    memory.defer_embeddings = True
    memory.checkpoint_dir = None
    memory._pending_embedding_job_ids = set()
    memory._pending_projection_job_ids = {"projection-lost"}
    memory._pending_job_entity_ids = {"projection-lost": ["session-one"]}
    memory._pending_job_manifest_ids = {"projection-lost": "artifact-unrelated"}
    memory._request_json = fake_request

    replacements = memory._recover_background_job(
        "projection-lost",
        job_kind="memory_projection",
    )

    assert requests == [{"entity_ids": ["session-one"], "jobs": ["memory_projection"]}]
    assert replacements == {"projection-replacement"}
    assert memory._pending_job_manifest_ids == {}


def test_sibyl_memory_recovers_large_job_from_legacy_checkpoint_inventory() -> None:
    module = _load_memory_module()
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})
    manifest_id = "artifact-legacy-manifest"
    requests: list[dict[str, object]] = []

    def fake_request(
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        del params
        assert method == "POST"
        assert path == "/entities/bulk/requeue-background-jobs"
        assert isinstance(json, dict)
        requests.append(json)
        return {
            "manifest_id": manifest_id,
            "entity_ids": ["event-0", manifest_id],
            "background_jobs": {
                "embedding_backfill": {
                    "status": "queued",
                    "job_ids": ["embed-replacement"],
                }
            },
        }

    memory.defer_embeddings = True
    memory.checkpoint_dir = None
    memory._pending_embedding_job_ids = {"embed-lost"}
    memory._pending_projection_job_ids = set()
    memory._pending_job_entity_ids = {
        "embed-lost": [*(f"event-{index}" for index in range(129)), manifest_id]
    }
    memory._pending_job_manifest_ids = {}
    memory._request_json = fake_request

    replacements = memory._recover_background_job(
        "embed-lost",
        job_kind="embedding_backfill",
    )

    assert requests == [{"manifest_id": manifest_id, "jobs": ["embedding_backfill"]}]
    assert replacements == {"embed-replacement"}


def test_sibyl_memory_chunks_large_job_status_batches() -> None:
    module = _load_memory_module()
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})
    requested_batches: list[list[str]] = []

    def fake_request(
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        del params
        assert method == "POST"
        assert path == "/jobs/status"
        assert isinstance(json, dict)
        job_ids = json["job_ids"]
        assert isinstance(job_ids, list)
        job_id_batch = [str(job_id) for job_id in job_ids]
        requested_batches.append(job_id_batch)
        return {"jobs": {job_id: {"status": "complete", "error": None} for job_id in job_id_batch}}

    memory.embedding_job_wait_timeout_seconds = 1.0
    memory.embedding_job_poll_seconds = 0.1
    memory._pending_embedding_job_ids = {f"embed-{index:02d}" for index in range(65)}
    memory._request_json = fake_request

    memory._drain_embedding_backfills()

    assert [len(batch) for batch in requested_batches] == [64, 1]


def _write_dataset(root: Path) -> None:
    (root / "haystacks").mkdir(parents=True)
    (root / "questions.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "id": "q-enterprise",
                        "domain": "enterprise",
                        "environment": "workarena",
                        "question_type": "dynamic-environment",
                        "question": "Which filter was selected?",
                        "image": None,
                        "answer": "The priority filter.",
                        "eval_function": "norm_phrase_set_match",
                    }
                ),
                json.dumps(
                    {
                        "id": "q-web",
                        "domain": "web",
                        "environment": "visualwebarena",
                        "question_type": "procedure",
                        "question": "How did checkout finish?",
                        "image": None,
                        "answer": "It confirmed the order.",
                        "eval_function": "llm_gotchas_checker",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    (root / "haystacks" / "lme_v2_small.json").write_text(
        json.dumps({"q-enterprise": ["t1", "t2"], "q-web": ["t3"]}),
        encoding="utf-8",
    )
    (root / "trajectories.jsonl").write_text(
        "\n".join(json.dumps(_trajectory(trajectory_id)) for trajectory_id in ["t1", "t2", "t3"]),
        encoding="utf-8",
    )


def _assert_credentials_stay_process_local(memory_config: dict[str, object]) -> None:
    params = memory_config["memory_params"]
    assert isinstance(params, dict)
    assert not {"api_token", "email", "password"} & params.keys()
    serialized = json.dumps(memory_config)
    assert TEST_CREDENTIAL not in serialized
    assert TEST_EMAIL not in serialized
    assert os.environ["SIBYL_API_TOKEN"] == TEST_CREDENTIAL
    assert os.environ["LME_SIBYL_EMAIL"] == TEST_EMAIL
    assert os.environ["LME_SIBYL_PASSWORD"] == TEST_CREDENTIAL


def _reload_checkpoint(module: ModuleType, source: Any, checkpoint_dir: Path) -> Any:
    restored = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(restored, {})
    for attribute in (
        "api_url",
        "project_id",
        "run_id",
        "chunking_mode",
        "content_max_chars",
    ):
        setattr(restored, attribute, getattr(source, attribute))
    restored._load_checkpoint(checkpoint_dir)
    return restored


def _write_official_repo(root: Path) -> Path:
    git = shutil.which("git")
    if git is None:
        msg = "git is required for official-repo provenance tests"
        raise RuntimeError(msg)
    (root / "evaluation").mkdir(parents=True)
    (root / "evaluation" / "harness.py").write_text(
        "def main():\n    return None\n", encoding="utf-8"
    )
    subprocess.run([git, "init"], cwd=root, check=True, capture_output=True)  # noqa: S603
    subprocess.run([git, "config", "user.email", "test@example.test"], cwd=root, check=True)  # noqa: S603
    subprocess.run([git, "config", "user.name", "Test"], cwd=root, check=True)  # noqa: S603
    subprocess.run([git, "add", "evaluation/harness.py"], cwd=root, check=True)  # noqa: S603
    subprocess.run(  # noqa: S603
        [git, "commit", "-m", "add harness"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return root


def _write_official_outputs(
    output_dir: Path,
    *,
    domain: str = "enterprise",
    legacy_usage_identity: bool = False,
) -> None:
    output_dir.mkdir(parents=True)
    run_id = f"run-{domain}"
    usage_run_id = run_id if legacy_usage_identity else f"usage-{domain}"
    runtime_dir = output_dir / "runtime_inputs"
    runtime_dir.mkdir()
    plan = {"run_id": run_id, "domain": domain}
    if not legacy_usage_identity:
        plan["provider_usage_run_id"] = usage_run_id
    (output_dir / "longmemeval_v2_official_plan.json").write_text(
        json.dumps(plan),
        encoding="utf-8",
    )
    (runtime_dir / "questions.json").write_text(
        json.dumps([{"id": f"q-{domain}", "question": "Which filter was selected?"}]),
        encoding="utf-8",
    )
    (runtime_dir / "haystack.json").write_text(
        json.dumps({f"q-{domain}": ["t1", "t2"]}),
        encoding="utf-8",
    )
    (runtime_dir / "memory_config.json").write_text(
        json.dumps(
            {
                "memory_type": "sibyl_live_api",
                "memory_params": {
                    "run_id": run_id,
                    "api_url": "http://localhost:3434/api",
                    "api_token": "secret-token",
                    "email": "eval@example.test",
                    "password": "secret-password",
                    "search_limit": 12,
                    "max_context_items": 8,
                },
            }
        ),
        encoding="utf-8",
    )
    (output_dir / "run_args.json").write_text(
        json.dumps(
            {
                "domain": domain,
                "tier": "small",
                "model": "Qwen/Qwen3.5-9B",
                "base_url": "http://localhost:8023/v1",
                "evaluator_model": "gpt-5.2",
                "evaluator_reasoning_effort": "medium",
                "method": "sibyl_live_api",
            }
        ),
        encoding="utf-8",
    )
    (output_dir / "metric_overview.json").write_text(
        json.dumps(
            {
                "overall_full_set": 0.44,
                "gotchas_accuracy": 0.5,
                "static_accuracy": 0.4,
                "dynamic_accuracy": 0.45,
                "procedure_accuracy": 0.55,
                "memory_query_avg_seconds": 2.5,
            }
        ),
        encoding="utf-8",
    )
    (output_dir / "aggregated_metrics.json").write_text(
        json.dumps(
            {
                "overall": {
                    "overall_full_set": 0.44,
                    "count_all_questions": 2,
                    "count_non_abstention": 2,
                    "count_abstention": 0,
                },
                "non_abstention_by_category": {
                    "gotchas": {"pct_correct": 0.5, "count": 1},
                },
                "combined_abstention_by_category": {
                    "static": {"pct_correct": 0.4, "count": 1},
                    "dynamic": {"pct_correct": 0.45, "count": 1},
                    "procedure": {"pct_correct": 0.55, "count": 1},
                },
                "memory_query": {"avg_seconds": 2.5, "max_seconds": 4.0},
                "tokens": {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200},
            }
        ),
        encoding="utf-8",
    )
    (output_dir / "per_question.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "id": question_id,
                    "memory_query_duration_seconds": latency,
                    "memory_post_query_metadata": {
                        "api_runtime": {
                            "status": "healthy",
                            "version": "1.1.0",
                            "runtime": {"commit": "api-commit", "git_dirty": False},
                        },
                        "ingest_embedding_usage": {
                            "provider": "openai",
                            "model": "text-embedding-3-small",
                            "requests": 2,
                            "inputs": 4,
                            "prompt_tokens": 50,
                            "total_tokens": 50,
                            "cost_reported_requests": 2,
                            "cost_usd": 0.001,
                        },
                        "search_metadata": {
                            "embedding_usage": {
                                "provider": "openai",
                                "model": "text-embedding-3-small",
                                "requests": 1,
                                "inputs": 1,
                                "prompt_tokens": 5,
                                "total_tokens": 5,
                                "cost_reported_requests": 1,
                                "cost_usd": 0.0001,
                            }
                        },
                    },
                }
            )
            for question_id, latency in (("q1", 1.0), ("q2", 2.0), ("q3", 4.0))
        ),
        encoding="utf-8",
    )
    usage_dir = output_dir / "provider_usage"
    usage_dir.mkdir()
    (usage_dir / "reader.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "run_id": usage_run_id,
                    "role": "reader",
                    "requested_model": "Qwen/Qwen3.5-9B",
                    "provider_model": "qwen/qwen3.5-9b",
                    "response_id": f"reader-{domain}-{index}",
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "total_tokens": 120,
                        "cost_usd": 0.01,
                    },
                }
            )
            for index in range(3)
        ),
        encoding="utf-8",
    )
    (usage_dir / "judge.jsonl").write_text(
        json.dumps(
            {
                "run_id": usage_run_id,
                "role": "judge",
                "requested_model": "gpt-5.2",
                "provider_model": "gpt-5.2-2026-06-01",
                "response_id": f"judge-{domain}",
                "usage": {
                    "prompt_tokens": 50,
                    "completion_tokens": 10,
                    "total_tokens": 60,
                    "cost_usd": 0.02,
                },
            }
        ),
        encoding="utf-8",
    )


def _write_combined_outputs(
    output_dir: Path,
    *,
    include_submission_overview: bool = True,
    include_metric_overview_latency: bool = False,
) -> None:
    output_dir.mkdir(parents=True)
    metric_overview = {
        "overall_full_set": 0.44,
        "gotchas_accuracy": 0.5,
        "static_accuracy": 0.4,
        "dynamic_accuracy": 0.45,
        "procedure_accuracy": 0.55,
    }
    if include_metric_overview_latency:
        metric_overview["memory_query_avg_seconds"] = 2.5
    (output_dir / "metric_overview.json").write_text(
        json.dumps(metric_overview),
        encoding="utf-8",
    )
    (output_dir / "aggregated_metrics.json").write_text(
        json.dumps(
            {
                "overall": {
                    "overall_full_set": 0.44,
                    "count_all_questions": 4.0,
                    "count_non_abstention": 4,
                    "count_abstention": 0,
                },
                "non_abstention_by_category": {
                    "gotchas": {"pct_correct": 0.5, "count": 2},
                },
                "combined_abstention_by_category": {
                    "static": {"pct_correct": 0.4, "count": 2},
                    "dynamic": {"pct_correct": 0.45, "count": 2},
                    "procedure": {"pct_correct": 0.55, "count": 2},
                },
                "memory_query": {
                    "avg_seconds": 2.5,
                    "max_seconds": 4.0,
                    "total_seconds": 10.0,
                },
                "tokens": {"prompt_tokens": 2000, "completion_tokens": 400, "total_tokens": 2400},
            }
        ),
        encoding="utf-8",
    )
    if include_submission_overview:
        (output_dir / "submission_overview.json").write_text(
            json.dumps({"lafs_gain": EXPECTED_LAFS_GAIN}),
            encoding="utf-8",
        )


def _trajectory(trajectory_id: str, *, tree: str = "button Priority") -> dict[str, object]:
    return {
        "id": trajectory_id,
        "domain": "enterprise",
        "environment": "workarena",
        "goal": "Resolve the assigned incident.",
        "outcome": "success",
        "start_url": "https://example.test/start",
        "states": [
            {
                "state_index": 0,
                "step": 0,
                "url": "https://example.test/start",
                "action": "click filter",
                "thought": "Need incidents",
                "accessibility_tree": tree,
                "screenshot": f"screenshots/{trajectory_id}/0.png",
            },
            {
                "state_index": 1,
                "step": 1,
                "url": "https://example.test/incidents",
                "action": None,
                "thought": None,
                "accessibility_tree": "list Incidents",
                "screenshot": f"screenshots/{trajectory_id}/1.png",
            },
        ],
    }


def _search_result(
    trajectory_id: str,
    *,
    chunk_index: int,
    state_index: int,
    score: float,
) -> dict[str, Any]:
    return {
        "id": f"entity:{trajectory_id}:{chunk_index}",
        "type": "session",
        "name": f"Trajectory {trajectory_id} chunk {chunk_index}",
        "content": f"State {state_index}\nEvidence",
        "score": score,
        "result_origin": "graph",
        "metadata": {
            "longmemeval_v2_trajectory_id": trajectory_id,
            "longmemeval_v2_chunk_index": chunk_index,
            "longmemeval_v2_state_index": state_index,
            "longmemeval_v2_state_indices": [state_index],
        },
    }
