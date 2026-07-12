from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Protocol, TypedDict, cast

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
EXPECTED_JUDGE_REQUESTS = 2
EXPECTED_MAX_CHUNKS_PER_TRAJECTORY = 2
EXPECTED_NEIGHBOR_STITCH_ITEMS = 2
EXPECTED_NEIGHBOR_STITCH_SPAN = 1
EXPECTED_SEARCH_LIMIT_OVERRIDE = 24
EXPECTED_SAVED_USAGE_REQUESTS = 2
EXPECTED_SAVED_USAGE_COST_USD = 0.25
TEST_CONTENT_MAX_CHARS = 420
TEST_CONTEXT_MAX_CHARS = 800
TEST_CREDENTIAL = "fresh-credential"


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


def test_official_runner_plan_materializes_honest_runtime_inputs(tmp_path: Path) -> None:
    module = _load_runner_module()
    data_root = tmp_path / "data"
    output_dir = tmp_path / "out"
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
    assert memory_config["memory_params"]["allow_localhost"] is True
    assert memory_config["memory_params"]["defer_embeddings"] is True
    assert memory_config["memory_params"]["content_max_chars"] == EXPECTED_CONTENT_MAX_CHARS
    assert memory_config["memory_params"]["chunking_mode"] == "state"
    assert (
        memory_config["memory_params"]["max_chunks_per_trajectory"]
        == EXPECTED_MAX_CHUNKS_PER_TRAJECTORY
    )
    assert memory_config["memory_params"]["neighbor_stitch_items"] == EXPECTED_NEIGHBOR_STITCH_ITEMS
    assert memory_config["memory_params"]["neighbor_stitch_span"] == EXPECTED_NEIGHBOR_STITCH_SPAN
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
    assert plan["max_chunks_per_trajectory"] == EXPECTED_MAX_CHUNKS_PER_TRAJECTORY
    assert plan["neighbor_stitch_items"] == EXPECTED_NEIGHBOR_STITCH_ITEMS
    assert plan["neighbor_stitch_span"] == EXPECTED_NEIGHBOR_STITCH_SPAN
    assert plan["honesty_contract"]["answer_gold_visible_to_memory"] is False
    assert plan["required_trajectory_count"] == EXPECTED_REQUIRED_TRAJECTORIES
    assert plan["requirements"]["trajectories_jsonl_exists"] is True
    assert plan["requirements"]["official_repo_configured"] is False
    assert plan["provider_usage"] == {
        "reader": str(output_dir / "provider_usage" / "reader.jsonl"),
        "judge": str(output_dir / "provider_usage" / "judge.jsonl"),
    }
    assert plan["checkpoint_dir"] is None
    assert "reader_endpoint_reachable" in plan["requirements"]
    assert "torch_available" in plan["requirements"]


def test_official_runner_receipt_only_emits_citable_contract(tmp_path: Path) -> None:
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


def test_longmemeval_v2_receipt_redacts_sensitive_command_args() -> None:
    module = _load_runner_module()

    assert module._redacted_command_args(
        [
            "--api-token",
            "sibyl-secret-token",
            "--password=hunter2",
            "--domain",
            "web",
        ]
    ) == [
        "--api-token",
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
                "Chunk: 0\n"
                "Score: 0.875\n\n"
                "The priority filter was"
            ),
        }
    ]
    assert module.build_retrieval_trace(
        [
            {
                "id": "entity:t1-0",
                "content": trace_content,
                "score": 0.875,
                "result_origin": "graph",
                "metadata": {
                    "longmemeval_v2_trajectory_id": "t1",
                    "longmemeval_v2_chunk_index": 0,
                    "longmemeval_v2_chunk_count": 2,
                },
            }
        ],
        max_items=1,
        max_chars_per_item=24,
    ) == [
        {
            "rank": 1,
            "entity_id": "entity:t1-0",
            "trajectory_id": "t1",
            "chunk_index": 0,
            "chunk_count": 2,
            "state_indices": [3],
            "score": 0.875,
            "content_chars": len(trace_content),
            "exposed_chars": 24,
            "result_origin": "graph",
            "selection_origin": "search",
            "search_rank": None,
            "neighbor_of_search_rank": None,
            "neighbor_distance": None,
        }
    ]


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
    memory._pending_embedding_job_ids = {"embed-1"}
    memory._pending_projection_job_ids = {"project-1"}
    memory.ingest_embedding_usage = {"requests": EXPECTED_SAVED_USAGE_REQUESTS}
    memory.ingest_api_runtime = {"version": "test"}

    memory._append_checkpoint(payloads)
    catalog_path = checkpoint_dir / module.CHECKPOINT_CATALOG_FILENAME
    with catalog_path.open("ab") as handle:
        handle.write(b"interrupted trailing bytes")

    restored = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(restored, {})
    restored.api_url = memory.api_url
    restored.project_id = memory.project_id
    restored.run_id = memory.run_id
    restored.chunking_mode = memory.chunking_mode
    restored.content_max_chars = memory.content_max_chars
    restored._load_checkpoint(checkpoint_dir)

    assert restored._completed_trajectory_ids == {"t1"}
    assert restored._pending_embedding_job_ids == {"embed-1"}
    assert restored._pending_projection_job_ids == {"project-1"}
    assert restored._chunk_catalog == memory._chunk_catalog
    assert restored.ingest_embedding_usage == {"requests": EXPECTED_SAVED_USAGE_REQUESTS}

    restored._request_json = lambda *args, **kwargs: pytest.fail(
        f"completed trajectory was reinserted: {args}, {kwargs}"
    )
    restored.insert(_trajectory("t1"))


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
            "neighbor_stitch_items": 2,
        },
    }

    effective = module.SibylLiveApiMemory.reconcile_loaded_memory_config(saved, requested)
    params = effective["memory_params"]

    assert params["project_id"] == "project_saved"
    assert params["run_id"] == "run-saved"
    assert params["api_token"] == TEST_CREDENTIAL
    assert params["search_limit"] == EXPECTED_SEARCH_LIMIT_OVERRIDE
    assert params["neighbor_stitch_items"] == EXPECTED_NEIGHBOR_STITCH_ITEMS

    requested["memory_params"]["content_max_chars"] = 8_000
    with pytest.raises(RuntimeError, match="content_max_chars"):
        module.SibylLiveApiMemory.reconcile_loaded_memory_config(saved, requested)


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
        ]
    )

    config = module.build_memory_config(args)
    params = config["memory_params"]

    assert params["api_url"] == "http://127.0.0.1:3434/api"
    assert params["project_id"] == "project_checkpoint"
    assert params["run_id"] == "run-checkpoint"
    assert params["checkpoint_dir"] == str(checkpoint_dir)


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
            "created": 1,
            "background_jobs": {
                "embedding_backfill": {
                    "status": "queued",
                    "job_ids": ["embed-lme-v2-1"],
                },
                "memory_projection": {
                    "status": "queued",
                    "job_ids": ["project-lme-v2-1"],
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
    assert calls[0]["path"] == "/entities/bulk"
    assert request_json["defer_embeddings"] is True
    assert memory.created_entities == 1
    assert memory.inserted_trajectories == 1
    assert memory._pending_embedding_job_ids == {"embed-lme-v2-1"}
    assert memory._pending_projection_job_ids == {"project-lme-v2-1"}


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

    assert calls > 1
    assert embedding_drain_calls == calls
    assert projection_drain_calls == calls
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
                        "inputs": 4,
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
        if path == "/search":
            return {
                "results": [],
                "filters": {
                    "retrieval_mode": "native",
                    "stage_timings_ms": {"total": 12.5},
                },
            }
        raise AssertionError(f"unexpected path: {path}")

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
    memory.created_entities = 4
    memory.defer_embeddings = True
    memory.ingest_embedding_usage = {}
    memory.embedding_job_wait_timeout_seconds = 5.0
    memory.embedding_job_poll_seconds = 0.0
    memory._pending_embedding_job_ids = {"embed-lme-v2-1"}
    memory._pending_projection_job_ids = {"project-lme-v2-1"}
    memory._finalize_lock = threading.Lock()
    memory._query_local = threading.local()
    memory._ingest_finalized = False
    memory._request_json = fake_request

    memory.finalize_ingest()
    assert memory.query("Which filter was selected?") == []
    metadata = memory.post_query_hook(
        query="Which filter was selected?",
        query_image=None,
        memory_context=[],
    )
    memory.finalize_ingest()

    assert calls == ["/jobs/status", "/jobs/status", "/search"]
    assert metadata is not None
    assert metadata["search_metadata"] == {
        "retrieval_mode": "native",
        "stage_timings_ms": {"total": 12.5},
        "adapter_assembly": {
            "input_result_count": 0,
            "selected_search_seed_count": 0,
            "stitched_neighbor_count": 0,
            "output_result_count": 0,
            "max_chunks_per_trajectory": EXPECTED_MAX_CHUNKS_PER_TRAJECTORY,
            "neighbor_stitch_items": EXPECTED_NEIGHBOR_STITCH_ITEMS,
            "neighbor_stitch_span": EXPECTED_NEIGHBOR_STITCH_SPAN,
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


def _write_official_outputs(output_dir: Path, *, domain: str = "enterprise") -> None:
    output_dir.mkdir(parents=True)
    run_id = f"run-{domain}"
    runtime_dir = output_dir / "runtime_inputs"
    runtime_dir.mkdir()
    (output_dir / "longmemeval_v2_official_plan.json").write_text(
        json.dumps({"run_id": run_id, "domain": domain}),
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
                    "run_id": run_id,
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
                "run_id": run_id,
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
) -> dict[str, object]:
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
