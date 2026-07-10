from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
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
EXPECTED_COMBINED_QUESTION_COUNT = 4
TEST_CONTENT_MAX_CHARS = 420
TEST_CONTEXT_MAX_CHARS = 800


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
    assert plan["memory_api_timeout_seconds"] == EXPECTED_MEMORY_API_TIMEOUT_SECONDS
    assert plan["memory_api_retry_attempts"] == EXPECTED_MEMORY_API_RETRY_ATTEMPTS
    assert plan["honesty_contract"]["answer_gold_visible_to_memory"] is False
    assert plan["required_trajectory_count"] == EXPECTED_REQUIRED_TRAJECTORIES
    assert plan["requirements"]["trajectories_jsonl_exists"] is True
    assert plan["requirements"]["official_repo_configured"] is False
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
    assert set(receipt["source_runs"]["domains"]) == {"web", "enterprise"}
    assert receipt["metrics"]["lafs_gain"] == EXPECTED_LAFS_GAIN
    assert receipt["metrics"]["memory_query_avg_seconds"] == EXPECTED_MEMORY_QUERY_AVG_SECONDS
    assert receipt["metrics"]["latency_p95_ms"] > 0
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
    assert "checks[4] status must be 'PASS'" in failures


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
    assert any("Screenshot:" in str(payload["content"]) for payload in payloads)


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
                "Trajectory: t1\n"
                "Chunk: 0\n"
                "Score: 0.875\n\n"
                "The priority filter was"
            ),
        }
    ]


def test_sibyl_memory_query_context_strips_gold_answer() -> None:
    module = _load_memory_module()
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})

    memory.set_query_context(
        question_id="q1",
        question_item={
            "id": "q1",
            "question": "Which filter was selected?",
            "answer": "Priority",
        },
    )

    context = memory.get_query_context()
    assert context["question_item"] == {
        "id": "q1",
        "question": "Which filter was selected?",
    }


def test_sibyl_memory_insert_defers_embeddings_and_tracks_backfill_job() -> None:
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
                }
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
    memory._request_json = fake_request

    memory.insert(_trajectory("t1"))

    request_json = calls[0]["json"]
    assert isinstance(request_json, dict)
    assert calls[0]["path"] == "/entities/bulk"
    assert request_json["defer_embeddings"] is True
    assert memory.created_entities == 1
    assert memory.inserted_trajectories == 1
    assert memory._pending_embedding_job_ids == {"embed-lme-v2-1"}


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
    drain_calls = 0

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
                }
            },
        }

    def fake_drain() -> None:
        nonlocal drain_calls
        drain_calls += 1
        memory._pending_embedding_job_ids.clear()

    memory.project_id = "project_lme"
    memory.run_id = "run_lme"
    memory.content_max_chars = 20
    memory.bulk_max_entities = 1
    memory.bulk_max_content_chars = 200_000
    memory.embedding_backfill_max_pending_jobs = 1
    memory.include_screenshot_refs = False
    memory.defer_embeddings = True
    memory.created_entities = 0
    memory.inserted_trajectories = 0
    memory._pending_embedding_job_ids = set()
    memory._request_json = fake_request
    memory._drain_embedding_backfills = fake_drain

    memory.insert(_trajectory("t1", tree="button Priority " * 20))

    assert calls > 1
    assert drain_calls == calls
    assert memory._pending_embedding_job_ids == set()


def test_sibyl_memory_insert_rejects_missing_deferred_embedding_job() -> None:
    module = _load_memory_module()
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})

    memory.defer_embeddings = True
    memory._pending_embedding_job_ids = set()

    with pytest.raises(RuntimeError, match="returned no backfill job ids"):
        memory._remember_embedding_backfill_jobs({"created": 1, "background_jobs": {}})


def test_sibyl_memory_query_waits_for_embedding_backfill_before_search() -> None:
    module = _load_memory_module()
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})
    status_responses: list[dict[str, object]] = [
        {"status": "queued"},
        {"status": "complete", "error": None},
    ]
    calls: list[str] = []
    search_payloads: list[dict[str, object]] = []

    def fake_request(
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        del method, params
        calls.append(path)
        if path == "/jobs/embed-lme-v2-1":
            return status_responses.pop(0)
        if path == "/search":
            assert isinstance(json, dict)
            search_payloads.append(json)
            return {
                "results": [
                    {
                        "content": "x" * 900,
                        "score": 0.9,
                        "metadata": {
                            "longmemeval_v2_trajectory_id": "t1",
                            "longmemeval_v2_chunk_index": 0,
                        },
                    }
                ]
            }
        raise AssertionError(f"unexpected path: {path}")

    memory.project_id = "project_lme"
    memory.search_limit = 12
    memory.max_context_items = 8
    memory.max_context_chars_per_item = TEST_CONTEXT_MAX_CHARS
    memory.embedding_job_wait_timeout_seconds = 5.0
    memory.embedding_job_poll_seconds = 0.0
    memory._pending_embedding_job_ids = {"embed-lme-v2-1"}
    memory._request_json = fake_request

    context = memory.query("Which filter was selected?")

    assert len(context) == 1
    assert context[0]["value"].endswith("x" * TEST_CONTEXT_MAX_CHARS)
    assert search_payloads[0]["content_max_chars"] == TEST_CONTEXT_MAX_CHARS
    assert calls == ["/jobs/embed-lme-v2-1", "/jobs/embed-lme-v2-1", "/search"]
    assert memory._pending_embedding_job_ids == set()


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
            json.dumps({"id": question_id, "memory_query_seconds": latency})
            for question_id, latency in (("q1", 1.0), ("q2", 2.0), ("q3", 4.0))
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
