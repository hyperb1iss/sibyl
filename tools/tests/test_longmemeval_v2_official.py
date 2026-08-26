from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import tracemalloc
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Protocol, TypedDict, cast

import httpx
import pytest
from benchmarks import local_execution_identity as local_identity
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
EXPECTED_TYPED_NOTE_RESERVATION = 3
EXPECTED_BUDGETED_RAW_ITEMS = 10
EXPECTED_OPERATIONAL_RAW_ITEMS = 6
EXPECTED_OPERATIONAL_TYPED_ITEMS = 2
EXPECTED_OPERATIONAL_SUPPORT_ITEMS = 3
EXPECTED_SHARED_RELEVANCE_TYPED_ITEMS = 3
EXPECTED_SHARED_RELEVANCE_RAW_ITEMS = 5
EXPECTED_PLANNER_REQUESTS = 2
EXPECTED_PLANNER_INPUT_TOKENS = 84
EXPECTED_PLANNER_OUTPUT_TOKENS = 22
EXPECTED_DISTILLATION_REQUESTS = 3
EXPECTED_DISTILLATION_INPUT_TOKENS = 300
EXPECTED_DISTILLATION_OUTPUT_TOKENS = 90
EXPECTED_DISTILLATION_TOTAL_TOKENS = 390
EXPECTED_LOADED_QUERY_TOKENS = 10
EXPECTED_MISMATCHED_INGEST_QUERY_TOKENS = 2
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


def _local_embedding_job_result() -> dict[str, object]:
    return {
        "embedding_usage": {
            "provider": "local",
            "model": "sentence-transformers/all-MiniLM-L6-v2",
            "requests": 1,
            "inputs": 1,
            "prompt_tokens": 0,
            "total_tokens": 0,
            "cost_reported_requests": 0,
            "cost_usd": 0.0,
        }
    }


def _set_github_execution_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": "hyperb1iss/sibyl",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_RUN_ID": "1234",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_WORKFLOW_REF": (
            "hyperb1iss/sibyl/.github/workflows/longmemeval-v2.yml@refs/heads/main"
        ),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _local_git_checkout(tmp_path: Path) -> tuple[Path, str, str]:
    checkout = tmp_path / "sibyl"
    checkout.mkdir()
    git = shutil.which("git")
    assert git is not None
    commands = [
        [git, "init", "-b", "main"],
        [git, "config", "user.email", "eval@example.test"],
        [git, "config", "user.name", "Eval Test"],
        [git, "remote", "add", "origin", "git@github.com:hyperb1iss/sibyl.git"],
    ]
    for command in commands:
        subprocess.run(  # noqa: S603 - resolved git with test-owned arguments.
            command,
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        )
    marker = checkout / "README.md"
    marker.write_text("sealed\n", encoding="utf-8")
    subprocess.run(  # noqa: S603 - resolved git with test-owned arguments.
        [git, "add", "README.md"],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(  # noqa: S603 - resolved git with test-owned arguments.
        [git, "commit", "-m", "test: seal checkout"],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    )
    sha = subprocess.run(  # noqa: S603 - resolved git with test-owned arguments.
        [git, "rev-parse", "HEAD"],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(  # noqa: S603 - resolved git with test-owned arguments.
        [git, "update-ref", "refs/remotes/origin/main", sha],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    )
    return checkout, "refs/heads/main", sha


def _stub_published_origin_ref(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    ref: str,
    result: str | ValueError,
) -> list[tuple[str, ...]]:
    original = local_identity._required_git_output
    calls: list[tuple[str, ...]] = []

    def required_git_output(root: Path, *args: str) -> str:
        calls.append(args)
        if args == ("ls-remote", "--exit-code", "--refs", "origin", ref):
            if isinstance(result, ValueError):
                raise result
            return result
        return original(root, *args)

    monkeypatch.setattr(local_identity, "_required_git_output", required_git_output)
    return calls


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
            else:
                result = {
                    "embedding_usage": {
                        "provider": "openai",
                        "model": "text-embedding-3-small",
                        "requests": 0,
                        "inputs": 0,
                        "prompt_tokens": 0,
                        "total_tokens": 0,
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


def test_official_runner_rebinds_adapter_to_official_registry(
    tmp_path: Path,
) -> None:
    fallback_repo = tmp_path / "fallback"
    official_repo = tmp_path / "official"
    for registry_root, origin in (
        (fallback_repo, "fallback"),
        (official_repo, "official"),
    ):
        memory_package = registry_root / "memory_modules"
        memory_package.mkdir(parents=True)
        (memory_package / "__init__.py").write_text("", encoding="utf-8")
        (memory_package / "memory.py").write_text(
            f'''ORIGIN = "{origin}"
MEMORY_TYPES = {{}}


class Memory:
    memory_type = ""

    def __init__(self, memory_params):
        self.memory_params = memory_params


MemoryContextItem = dict[str, str]


def register_memory(memory_cls):
    MEMORY_TYPES[memory_cls.memory_type] = memory_cls
    return memory_cls
''',
            encoding="utf-8",
        )
    project_root = Path(__file__).parents[2]
    probe = """
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[2])
import benchmarks.longmemeval_v2_official as runner
from benchmarks.longmemeval_v2_memory import sibyl_memory as fallback_adapter
from memory_modules import memory as fallback_registry

assert fallback_registry.ORIGIN == "fallback"
assert fallback_registry.MEMORY_TYPES["sibyl_live_api"] is fallback_adapter.SibylLiveApiMemory
installed = runner.install_official_memory_adapter(Path(sys.argv[1]))
from memory_modules import memory as official_registry

assert sys.path[0] == sys.argv[1]
assert official_registry.ORIGIN == "official"
assert official_registry is not fallback_registry
assert official_registry.MEMORY_TYPES["sibyl_live_api"] is installed
assert runner.SibylLiveApiMemory is installed
print("real_registry_rebind=PASS")
"""

    result = subprocess.run(  # noqa: S603 - current interpreter with test-owned input.
        [sys.executable, "-c", probe, str(official_repo), str(fallback_repo)],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "real_registry_rebind=PASS"


def test_official_runner_releases_shared_trajectories_after_final_insert(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_runner_module()
    trajectory_path = tmp_path / "trajectories.jsonl"
    trajectory_path.write_text(
        "\n".join(
            [
                json.dumps({"id": "kept-once", "payload": "once"}),
                json.dumps({"id": "unselected", "payload": "unused"}),
                json.dumps({"id": "kept-twice", "payload": "twice"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    def original_load(_path: str) -> dict[str, dict[str, str]]:
        raise AssertionError("shared trajectory loading must bypass the full-file loader")

    def forbidden_read_text(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("shared trajectory loading must stream the JSONL source")

    harness = SimpleNamespace(load_trajectories=original_load)

    release = module.install_shared_trajectory_release(
        harness,
        selected_haystack={
            "question-a": ["kept-once", "kept-twice", "kept-twice"],
            "question-b": ["kept-once", "kept-twice", "kept-twice"],
        },
    )
    assert release is not None
    monkeypatch.setattr(Path, "read_text", forbidden_read_text)
    trajectories = harness.load_trajectories(str(trajectory_path))

    assert set(trajectories) == {"kept-once", "kept-twice"}
    assert trajectories["kept-once"]["payload"] == "once"
    assert "kept-once" not in trajectories
    assert trajectories["kept-twice"]["payload"] == "twice"
    assert "kept-twice" in trajectories
    assert trajectories["kept-twice"]["payload"] == "twice"
    assert trajectories == {}
    release.assert_complete()
    assert "streaming consume-on-insert (2 selected)" in capsys.readouterr().out


def test_official_runner_bounds_shared_trajectory_heap(tmp_path: Path) -> None:
    module = _load_runner_module()
    trajectory_path = tmp_path / "trajectories.jsonl"
    payload = "x" * 65_536
    rows = [{"id": f"unselected-{index}", "payload": payload} for index in range(64)]
    rows.insert(32, {"id": "kept", "payload": payload})
    trajectory_path.write_text(
        "".join(f"{json.dumps(row)}\n" for row in rows),
        encoding="utf-8",
    )
    harness = SimpleNamespace(load_trajectories=lambda _path: {})
    release = module.install_shared_trajectory_release(
        harness,
        selected_haystack={"question": ["kept"]},
    )
    assert release is not None

    tracemalloc.start()
    try:
        trajectories = harness.load_trajectories(str(trajectory_path))
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert trajectories["kept"]["payload"] == payload
    assert peak < trajectory_path.stat().st_size // 4
    release.assert_complete()


@pytest.mark.parametrize(
    ("rows", "error"),
    [
        ([{"id": "kept"}, {"id": "duplicate"}, {"id": "duplicate"}], "Duplicate"),
        ([{"id": "kept"}, {"payload": "missing id"}], "Invalid trajectory id"),
        ([{"id": "kept"}, ["not", "an", "object"]], "Invalid trajectory"),
    ],
)
def test_official_runner_validates_unselected_shared_trajectories(
    rows: list[object],
    error: str,
    tmp_path: Path,
) -> None:
    module = _load_runner_module()
    trajectory_path = tmp_path / "trajectories.jsonl"
    trajectory_path.write_text(
        "".join(f"{json.dumps(row)}\n" for row in rows),
        encoding="utf-8",
    )
    harness = SimpleNamespace(load_trajectories=lambda _path: {})
    release = module.install_shared_trajectory_release(
        harness,
        selected_haystack={"question": ["kept"]},
    )
    assert release is not None

    with pytest.raises(RuntimeError, match=error) as exc_info:
        harness.load_trajectories(str(trajectory_path))

    if error == "Duplicate":
        assert str(trajectory_path) in str(exc_info.value)
        assert str(exc_info.value).endswith(":3")


def test_official_runner_rejects_missing_shared_trajectory(tmp_path: Path) -> None:
    module = _load_runner_module()
    trajectory_path = tmp_path / "trajectories.jsonl"
    trajectory_path.write_text('{"id": "present"}\n', encoding="utf-8")
    harness = SimpleNamespace(load_trajectories=lambda _path: {})
    release = module.install_shared_trajectory_release(
        harness,
        selected_haystack={"question": ["missing"]},
    )
    assert release is not None

    with pytest.raises(RuntimeError, match=r"missing trajectories.*missing"):
        harness.load_trajectories(str(trajectory_path))


def test_official_runner_reports_invalid_shared_trajectory_json(tmp_path: Path) -> None:
    module = _load_runner_module()
    trajectory_path = tmp_path / "trajectories.jsonl"
    trajectory_path.write_text(
        '{"id": "kept"}\n\nnot-json\n',
        encoding="utf-8",
    )
    harness = SimpleNamespace(load_trajectories=lambda _path: {})
    release = module.install_shared_trajectory_release(
        harness,
        selected_haystack={"question": ["kept"]},
    )
    assert release is not None

    with pytest.raises(RuntimeError, match=rf"{trajectory_path}:3"):
        harness.load_trajectories(str(trajectory_path))


def test_official_runner_rejects_non_jsonl_shared_trajectories(tmp_path: Path) -> None:
    module = _load_runner_module()
    trajectory_path = tmp_path / "trajectories.json"
    trajectory_path.write_text("[]\n", encoding="utf-8")
    harness = SimpleNamespace(load_trajectories=lambda _path: {})
    release = module.install_shared_trajectory_release(
        harness,
        selected_haystack={"question": ["trajectory"]},
    )
    assert release is not None

    with pytest.raises(RuntimeError, match="requires a JSONL source"):
        harness.load_trajectories(str(trajectory_path))


def test_official_runner_keeps_nonshared_trajectory_loading_unchanged() -> None:
    module = _load_runner_module()

    def original_load(_path: str) -> dict[str, dict[str, str]]:
        return {"trajectory": {"id": "trajectory"}}

    harness = SimpleNamespace(load_trajectories=original_load)

    assert (
        module.install_shared_trajectory_release(
            harness,
            selected_haystack={
                "question-a": ["trajectory-a"],
                "question-b": ["trajectory-b"],
            },
        )
        is None
    )
    assert harness.load_trajectories is original_load


def test_official_runner_rejects_missing_shared_trajectory_loader() -> None:
    module = _load_runner_module()

    with pytest.raises(RuntimeError, match="does not expose callable load_trajectories"):
        module.install_shared_trajectory_release(
            SimpleNamespace(),
            selected_haystack={"question": ["trajectory"]},
        )


def test_official_runner_rejects_shared_trajectory_loader_drift(tmp_path: Path) -> None:
    module = _load_runner_module()
    harness = SimpleNamespace(load_trajectories=lambda _path: {})
    release = module.install_shared_trajectory_release(
        harness,
        selected_haystack={"question": ["trajectory"]},
    )
    assert release is not None

    with pytest.raises(RuntimeError, match="did not load"):
        release.assert_complete()

    trajectory_path = tmp_path / "trajectories.jsonl"
    trajectory_path.write_text(
        '{"id": "trajectory", "payload": "retained"}\n',
        encoding="utf-8",
    )
    trajectories = harness.load_trajectories(str(trajectory_path))
    with pytest.raises(RuntimeError, match="1 remaining"):
        release.assert_complete()

    assert trajectories["trajectory"]["payload"] == "retained"
    release.assert_complete()


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
    assert plan["official_question_count"] == 1
    assert plan["official_question_ids_sha256"] == expected_question_ids_sha256
    assert plan["selection_complete"] is True
    dataset_receipt = module.build_dataset_receipt(
        data_root=data_root,
        domain="enterprise",
        tier="small",
        plan=plan,
        aggregated_metrics={},
    )
    assert dataset_receipt["selected_question_ids_sha256"] == expected_question_ids_sha256
    assert dataset_receipt["official_question_count"] == 1
    assert dataset_receipt["official_question_ids_sha256"] == expected_question_ids_sha256
    assert dataset_receipt["selection_complete"] is True


def _assert_source_prompt_artifacts(
    receipt: dict[str, Any],
    module: ModuleType,
    output_dirs: dict[str, Path],
) -> None:
    assert "prompt_build_summary" not in receipt["artifacts"]
    assert "prompt_rows" not in receipt["artifacts"]
    for domain, output_dir in output_dirs.items():
        source = receipt["source_runs"]["domains"][domain]
        assert source["prompt_build_summary"] == module.artifact_path_record(
            output_dir / "prompt_build_summary.json"
        )
        assert source["prompt_rows"] == module.artifact_path_record(
            output_dir / "prompt_rows.jsonl"
        )


@pytest.mark.parametrize(
    ("run_arg_method", "run_arg_tier"),
    [(None, None), ("unsealed_method", "medium")],
    ids=["fallbacks-absent", "fallbacks-conflict"],
)
def test_official_runner_receipt_only_emits_citable_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_arg_method: str | None,
    run_arg_tier: str | None,
) -> None:
    module = _load_runner_module()
    data_root = tmp_path / "data"
    receipt_dir = tmp_path / "receipt"
    web_output_dir = tmp_path / "runs" / "web"
    enterprise_output_dir = tmp_path / "runs" / "enterprise"
    combined_dir = tmp_path / "combined"
    official_repo = _write_official_repo(tmp_path / "official")
    fixture_commit = module.git_commit(official_repo)
    source_record = module.official_source_record
    monkeypatch.setattr(
        module,
        "official_source_record",
        lambda repo: source_record(repo, expected_commit=fixture_commit),
    )
    _write_dataset(data_root)
    _write_official_outputs(
        web_output_dir,
        domain="web",
        legacy_usage_identity=True,
        run_arg_method=run_arg_method,
        run_arg_tier=run_arg_tier,
    )
    _write_official_outputs(
        enterprise_output_dir,
        domain="enterprise",
        run_arg_method=run_arg_method,
        run_arg_tier=run_arg_tier,
    )
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
    assert all(
        source["method"] == "sibyl_live_api" and source["tier"] == "small"
        for source in receipt["source_runs"]["domains"].values()
    )
    assert receipt["source_runs"]["domains"]["web"]["runtime_inputs"]["questions"][
        "sha256"
    ].startswith("sha256:")
    assert receipt["source_runs"]["domains"]["web"]["official_receipt"]["sha256"].startswith(
        "sha256:"
    )
    _assert_source_prompt_artifacts(
        receipt,
        module,
        {"web": web_output_dir, "enterprise": enterprise_output_dir},
    )
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
    assert receipt["accounting"]["distillation"]["calls"] == 0
    assert receipt["accounting"]["reader"]["requests"] == EXPECTED_READER_REQUESTS
    assert receipt["accounting"]["judge"]["requests"] == EXPECTED_JUDGE_REQUESTS
    assert receipt["accounting"]["cost"]["provider_reported_total_usd"] == pytest.approx(0.1026)
    assert receipt["accounting"]["cost"]["coverage_complete"] is True
    assert {check["status"] for check in receipt["checks"]} == {"PASS"}
    assert eval_gate.evaluate_report(receipt, profile="longmemeval-v2") == []


def test_combined_source_runs_require_each_domain_receipt(tmp_path: Path) -> None:
    module = _load_runner_module()
    web_output_dir = tmp_path / "runs" / "web"
    enterprise_output_dir = tmp_path / "runs" / "enterprise"
    _write_official_outputs(web_output_dir, domain="web")
    _write_official_outputs(enterprise_output_dir, domain="enterprise")
    (web_output_dir / "longmemeval_v2_official_receipt.json").unlink()
    args = module.parse_args(
        [
            "--data-root",
            str(tmp_path / "data"),
            "--domain",
            "combined",
            "--output-dir",
            str(tmp_path / "combined"),
            "--receipt-only",
            "--web-output-dir",
            str(web_output_dir),
            "--enterprise-output-dir",
            str(enterprise_output_dir),
        ]
    )

    source_runs = module.load_receipt_source_runs(
        args=args,
        output_dir=tmp_path / "combined",
    )
    receipt = module.build_source_runs_receipt(args=args, source_runs=source_runs)

    assert receipt["integrity_complete"] is False
    assert receipt["domains"]["web"]["official_receipt"]["exists"] is False
    assert receipt["domains"]["enterprise"]["official_receipt"]["exists"] is True


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


def test_longmemeval_v2_receipt_binds_prompt_artifacts(tmp_path: Path) -> None:
    module = _load_runner_module()
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    summary_path = output_dir / "prompt_build_summary.json"
    rows_path = output_dir / "prompt_rows.jsonl"
    summary_path.write_text("{}\n", encoding="utf-8")
    rows_path.write_text("{}\n", encoding="utf-8")

    artifacts = module.build_artifact_receipt(
        output_dir=output_dir,
        prompt_artifact_dir=output_dir,
        plan_path=output_dir / "longmemeval_v2_official_plan.json",
        aggregated_path=output_dir / "aggregated_metrics.json",
        per_question_path=output_dir / "per_question.jsonl",
        run_args_path=output_dir / "run_args.json",
        metric_overview_path=output_dir / "metric_overview.json",
        combined_metrics_path=None,
        submission_overview_path=None,
        submission_archive_path=None,
    )

    assert artifacts["prompt_build_summary"] == module.artifact_path_record(summary_path)
    assert artifacts["prompt_rows"] == module.artifact_path_record(rows_path)


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


def test_official_runner_binds_complete_experiment_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_runner_module()
    _set_github_execution_environment(monkeypatch)
    args = module.parse_args(
        [
            "--data-root",
            str(tmp_path / "data"),
            "--domain",
            "web",
            "--output-dir",
            str(tmp_path / "out"),
            "--plan-only",
            "--experiment-id",
            "eval-1.3",
            "--experiment-phase",
            "aa",
            "--pass-id",
            "aa-01",
            "--pass-seed",
            "1701",
            "--arm-role",
            "machine",
            "--substrate",
            "machine",
        ]
    )

    assert module.experiment_identity(args) == {
        "experiment_identity_schema_version": "sibyl-longmemeval-v2-experiment-identity-v2",
        "experiment_id": "eval-1.3",
        "experiment_phase": "aa",
        "pass_id": "aa-01",
        "pass_seed": 1701,
        "arm_role": "machine",
        "substrate": "machine",
        "preregistration_sha256": None,
        "max_spend_usd": None,
        "execution": {
            "schema_version": "sibyl-longmemeval-v2-execution-identity-v1",
            "kind": "github",
            "repository": "hyperb1iss/sibyl",
            "ref": "refs/heads/main",
            "sha": "a" * 40,
            "run_id": "1234",
            "workflow_ref": (
                "hyperb1iss/sibyl/.github/workflows/longmemeval-v2.yml@refs/heads/main"
            ),
            "run_attempt": 2,
        },
    }


def test_official_runner_rejects_spoofed_github_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_runner_module()
    _set_github_execution_environment(monkeypatch)
    args = SimpleNamespace(
        execution_kind="github",
        local_repository="",
        local_ref="",
        local_sha="",
        local_run_id="",
        local_run_attempt=0,
        github_repository="attacker/fork",
        github_ref="refs/heads/main",
        github_workflow_ref=os.environ["GITHUB_WORKFLOW_REF"],
        github_workflow_sha=os.environ["GITHUB_SHA"],
        github_run_id=os.environ["GITHUB_RUN_ID"],
        github_run_attempt=2,
    )

    with pytest.raises(ValueError, match="differ from the Actions environment"):
        module.resolve_execution_identity(args, root=tmp_path)


@pytest.mark.parametrize(
    ("repository", "ref", "run_id", "workflow_filename", "message"),
    [
        (
            "git@github.com:hyperb1iss/sibyl",
            "refs/heads/main",
            "1234",
            "longmemeval-v2.yml",
            "canonical owner/repository slug",
        ),
        ("hyperb1iss/sibyl", "refs/heads/", "1234", "eval.yml", "valid full refs/heads"),
        (
            "hyperb1iss/sibyl",
            "refs/heads/bad..branch",
            "1234",
            "eval.yml",
            "valid full refs/heads",
        ),
        (
            "hyperb1iss/sibyl",
            "refs/heads/main",
            "run-1234",
            "eval.yml",
            "canonical positive decimal",
        ),
        (
            "hyperb1iss/sibyl",
            "refs/heads/main",
            "01234",
            "eval.yml",
            "canonical positive decimal",
        ),
        ("hyperb1iss/sibyl", "refs/heads/main", "1234", "", "canonical YAML"),
        ("hyperb1iss/sibyl", "refs/heads/main", "1234", "eval.json", "canonical YAML"),
        (
            "hyperb1iss/sibyl",
            "refs/heads/main",
            "1234",
            "nested/eval.yml",
            "canonical YAML",
        ),
    ],
)
def test_official_runner_rejects_noncanonical_github_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repository: str,
    ref: str,
    run_id: str,
    workflow_filename: str,
    message: str,
) -> None:
    module = _load_runner_module()
    workflow_ref = f"{repository}/.github/workflows/{workflow_filename}@{ref}"
    environment = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": repository,
        "GITHUB_REF": ref,
        "GITHUB_SHA": "a" * 40,
        "GITHUB_RUN_ID": run_id,
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_WORKFLOW_REF": workflow_ref,
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    args = SimpleNamespace(
        execution_kind="github",
        local_repository="",
        local_ref="",
        local_sha="",
        local_run_id="",
        local_run_attempt=0,
        github_repository=repository,
        github_ref=ref,
        github_workflow_ref=workflow_ref,
        github_workflow_sha="a" * 40,
        github_run_id=run_id,
        github_run_attempt=1,
    )

    with pytest.raises(ValueError, match=message):
        module.resolve_execution_identity(args, root=tmp_path)


def _local_execution_args(*, ref: str, sha: str, run_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        execution_kind="local",
        local_repository="hyperb1iss/sibyl",
        local_ref=ref,
        local_sha=sha,
        local_run_id=run_id,
        local_run_attempt=1,
        github_repository="",
        github_ref="",
        github_workflow_ref="",
        github_workflow_sha="",
        github_run_id="",
        github_run_attempt=0,
    )


def test_official_runner_seals_exact_clean_local_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_runner_module()
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    checkout, ref, sha = _local_git_checkout(tmp_path)
    run_id = "d6cf4d36-606f-44d6-b386-c723e6b756e8"
    git_calls = _stub_published_origin_ref(
        module,
        monkeypatch,
        ref=ref,
        result=f"{sha}\t{ref}",
    )

    execution = module.resolve_execution_identity(
        _local_execution_args(ref=ref, sha=sha, run_id=run_id),
        root=checkout,
    )

    assert execution == {
        "schema_version": "sibyl-longmemeval-v2-execution-identity-v1",
        "kind": "local",
        "repository": "hyperb1iss/sibyl",
        "ref": "refs/heads/main",
        "sha": sha,
        "run_id": run_id,
        "run_attempt": 1,
    }
    assert not ({"hostname", "path", "workflow_ref"} & execution.keys())
    assert ("status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none") in (
        git_calls
    )
    assert ("ls-remote", "--exit-code", "--refs", "origin", ref) in git_calls


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("local_sha", "b" * 40, "differs from the checkout"),
        ("local_ref", "main", "full refs/heads"),
        ("local_run_id", "not-unique", "canonical UUID"),
    ],
)
def test_official_runner_rejects_fake_local_execution_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
    message: str,
) -> None:
    module = _load_runner_module()
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    checkout, ref, sha = _local_git_checkout(tmp_path)
    args = _local_execution_args(
        ref=ref,
        sha=sha,
        run_id="d6cf4d36-606f-44d6-b386-c723e6b756e8",
    )
    setattr(args, field, value)
    _stub_published_origin_ref(
        module,
        monkeypatch,
        ref=ref,
        result=f"{sha}\t{ref}",
    )

    with pytest.raises(ValueError, match=message):
        module.resolve_execution_identity(args, root=checkout)


def test_official_runner_rejects_dirty_local_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_runner_module()
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    checkout, ref, sha = _local_git_checkout(tmp_path)
    (checkout / "README.md").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(ValueError, match="clean Sibyl checkout"):
        module.resolve_execution_identity(
            _local_execution_args(
                ref=ref,
                sha=sha,
                run_id="d6cf4d36-606f-44d6-b386-c723e6b756e8",
            ),
            root=checkout,
        )


def test_official_runner_rejects_unpushed_local_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_runner_module()
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    checkout, ref, _sha = _local_git_checkout(tmp_path)
    git = shutil.which("git")
    assert git is not None
    marker = checkout / "README.md"
    marker.write_text("new commit\n", encoding="utf-8")
    subprocess.run(  # noqa: S603 - resolved git with test-owned arguments.
        [git, "commit", "-am", "test: unpushed commit"],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    )
    head = subprocess.run(  # noqa: S603 - resolved git with test-owned arguments.
        [git, "rev-parse", "HEAD"],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    with pytest.raises(ValueError, match="differs from its origin tracking ref"):
        module.resolve_execution_identity(
            _local_execution_args(
                ref=ref,
                sha=head,
                run_id="d6cf4d36-606f-44d6-b386-c723e6b756e8",
            ),
            root=checkout,
        )


def test_official_runner_rejects_hidden_untracked_local_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_runner_module()
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    checkout, ref, sha = _local_git_checkout(tmp_path)
    git = shutil.which("git")
    assert git is not None
    subprocess.run(  # noqa: S603 - resolved git with test-owned arguments.
        [git, "config", "status.showUntrackedFiles", "no"],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    )
    (checkout / "hidden-by-config.txt").write_text("untracked\n", encoding="utf-8")

    with pytest.raises(ValueError, match="clean Sibyl checkout"):
        module.resolve_execution_identity(
            _local_execution_args(
                ref=ref,
                sha=sha,
                run_id="d6cf4d36-606f-44d6-b386-c723e6b756e8",
            ),
            root=checkout,
        )


def test_official_runner_rejects_deleted_origin_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_runner_module()
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    checkout, ref, sha = _local_git_checkout(tmp_path)
    _stub_published_origin_ref(
        module,
        monkeypatch,
        ref=ref,
        result=ValueError("ls-remote found no exact ref"),
    )

    with pytest.raises(ValueError, match="could not verify its exact ref on origin"):
        module.resolve_execution_identity(
            _local_execution_args(
                ref=ref,
                sha=sha,
                run_id="d6cf4d36-606f-44d6-b386-c723e6b756e8",
            ),
            root=checkout,
        )


def test_official_runner_rejects_force_moved_origin_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_runner_module()
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    checkout, ref, sha = _local_git_checkout(tmp_path)
    _stub_published_origin_ref(
        module,
        monkeypatch,
        ref=ref,
        result=f"{'b' * 40}\t{ref}",
    )

    with pytest.raises(ValueError, match="differs from the exact ref on origin"):
        module.resolve_execution_identity(
            _local_execution_args(
                ref=ref,
                sha=sha,
                run_id="d6cf4d36-606f-44d6-b386-c723e6b756e8",
            ),
            root=checkout,
        )


@pytest.mark.parametrize(
    "published_ref",
    [
        "",
        f"{'a' * 40}\trefs/heads/main\n{'a' * 40}\trefs/heads/main",
        f"{'a' * 40} refs/heads/main",
        f"{'a' * 40}\trefs/heads/other",
    ],
)
def test_official_runner_rejects_nonunique_or_malformed_origin_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    published_ref: str,
) -> None:
    module = _load_runner_module()
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    checkout, ref, sha = _local_git_checkout(tmp_path)
    _stub_published_origin_ref(
        module,
        monkeypatch,
        ref=ref,
        result=published_ref,
    )

    with pytest.raises(ValueError, match="origin returned"):
        module.resolve_execution_identity(
            _local_execution_args(
                ref=ref,
                sha=sha,
                run_id="d6cf4d36-606f-44d6-b386-c723e6b756e8",
            ),
            root=checkout,
        )


def test_official_runner_rejects_partial_experiment_identity(tmp_path: Path) -> None:
    module = _load_runner_module()

    with pytest.raises(SystemExit, match="2"):
        module.parse_args(
            [
                "--data-root",
                str(tmp_path / "data"),
                "--domain",
                "web",
                "--output-dir",
                str(tmp_path / "out"),
                "--experiment-id",
                "eval-1.3",
            ]
        )


@pytest.mark.parametrize(
    ("extra_args", "message"),
    [
        (["--limit", "1"], "--limit and --question-ids are forbidden"),
        (["--question-ids", "q-web"], "--limit and --question-ids are forbidden"),
        (["--tier", "medium"], "require the complete Small corpus"),
    ],
)
def test_official_runner_rejects_incomplete_paid_experiment_corpus(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    extra_args: list[str],
    message: str,
) -> None:
    module = _load_runner_module()
    argv = [
        "--data-root",
        str(tmp_path / "data"),
        "--domain",
        "web",
        "--output-dir",
        str(tmp_path / "out"),
        "--plan-only",
        "--experiment-id",
        "eval-1.3",
        "--experiment-phase",
        "aa",
        "--pass-id",
        "aa-01",
        "--pass-seed",
        "1701",
        "--arm-role",
        "machine",
        "--substrate",
        "machine",
        "--github-repository",
        "hyperb1iss/sibyl",
        "--github-workflow-ref",
        "hyperb1iss/sibyl/.github/workflows/longmemeval-v2.yml@refs/heads/main",
        "--github-workflow-sha",
        "a" * 40,
        "--github-run-id",
        "1234",
        *extra_args,
    ]

    with pytest.raises(SystemExit, match="2"):
        module.parse_args(argv)

    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    ("phase", "digest", "message"),
    [
        ("race", "", "require --preregistration-sha256"),
        ("render", "bogus", "lowercase SHA-256"),
        ("anchor", "a" * 64, "must precede preregistration"),
    ],
)
def test_official_runner_binds_preregistration_to_experiment_phase(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    phase: str,
    digest: str,
    message: str,
) -> None:
    module = _load_runner_module()
    argv = [
        "--data-root",
        str(tmp_path / "data"),
        "--domain",
        "web",
        "--output-dir",
        str(tmp_path / "out"),
        "--plan-only",
        "--experiment-id",
        "eval-1.3",
        "--experiment-phase",
        phase,
        "--pass-id",
        "pass-01",
        "--pass-seed",
        "1701",
        "--arm-role",
        "machine",
        "--substrate",
        "machine",
        "--github-repository",
        "hyperb1iss/sibyl",
        "--github-workflow-ref",
        "hyperb1iss/sibyl/.github/workflows/longmemeval-v2.yml@refs/heads/main",
        "--github-workflow-sha",
        "a" * 40,
        "--github-run-id",
        "1234",
        "--github-run-attempt",
        "1",
    ]
    if digest:
        argv.extend(["--preregistration-sha256", digest])

    with pytest.raises(SystemExit, match="2"):
        module.parse_args(argv)

    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("llm_abstention_checker|answerable=false", "llm_abstention_checker"),
        ("llm_gotchas_checker(strict=true)", "llm_gotchas_checker"),
        ("norm_phrase_set_match|separators=,;", "norm_phrase_set_match"),
    ],
)
def test_evaluator_function_name_handles_official_parameter_syntax(
    raw: str,
    expected: str,
) -> None:
    module = _load_runner_module()

    assert module.evaluator_function_name(raw) == expected


def test_spend_reservation_counts_reader_judge_and_operations(tmp_path: Path) -> None:
    module = _load_runner_module()
    question_count = 4
    llm_eval_count = 3
    args = module.parse_args(
        [
            "--data-root",
            str(tmp_path / "data"),
            "--domain",
            "web",
            "--output-dir",
            str(tmp_path / "out"),
            "--plan-only",
            "--max-spend-usd",
            "10",
            "--retrieval-mode",
            "accurate",
            "--retrieval-max-planned-queries",
            "2",
            "--note-distillation",
        ]
    )

    reservation = module.build_spend_reservation(
        args=args,
        question_count=question_count,
        llm_eval_count=llm_eval_count,
        required_trajectory_count=5,
    )

    assert reservation["status"] == "PASS"
    assert reservation["within_cap"] is True
    assert reservation["sections"]["reader"]["requests"] == question_count
    assert reservation["sections"]["judge"]["requests"] == llm_eval_count
    assert reservation["sections"]["operations"] == {
        "requests": 13,
        "planner_requests": 8,
        "distillation_requests": 5,
        "input_tokens": 130_000,
        "output_tokens": 13_312,
        "estimated_usd": pytest.approx(0.04264),
    }


def test_spend_reservation_and_actual_accounting_fail_closed(tmp_path: Path) -> None:
    module = _load_runner_module()
    args = module.parse_args(
        [
            "--data-root",
            str(tmp_path / "data"),
            "--domain",
            "web",
            "--output-dir",
            str(tmp_path / "out"),
            "--plan-only",
            "--max-spend-usd",
            "0.01",
        ]
    )
    blocked = module.build_spend_reservation(
        args=args,
        question_count=1,
        llm_eval_count=1,
        required_trajectory_count=1,
    )

    with pytest.raises(RuntimeError, match="exceeds its fixed cap"):
        module.enforce_spend_reservation({"spend_reservation": blocked})
    with pytest.raises(RuntimeError, match="accounting is incomplete"):
        module.enforce_actual_spend_cap(
            {"accounting": {"cost": {"coverage_complete": False}}},
            max_spend_usd=1.0,
        )


def test_official_runner_derives_bound_rig_rows(tmp_path: Path) -> None:
    module = _load_runner_module()
    data_root = tmp_path / "data"
    output_dir = tmp_path / "out"
    _write_dataset(data_root)
    (data_root / "trajectories.jsonl").write_text(
        "\n".join(
            json.dumps(
                _trajectory(
                    trajectory_id,
                    tree=("The priority filter." if trajectory_id == "t1" else "button Other"),
                )
            )
            for trajectory_id in ["t1", "t2", "t3"]
        ),
        encoding="utf-8",
    )
    runtime_dir = output_dir / "runtime_inputs"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "haystack.json").write_text(
        json.dumps({"q-enterprise": ["t1", "t2"]}),
        encoding="utf-8",
    )
    (output_dir / "per_question.jsonl").write_text(
        json.dumps(
            {
                "question_id": "q-enterprise",
                "score_bool": True,
                "memory_context": [
                    {
                        "type": "text",
                        "value": (
                            "Retrieved evidence rank 1\nTrajectory: t1\n"
                            "State 0\nThe priority filter."
                        ),
                    }
                ],
                "memory_post_query_metadata": {
                    "context_status": "complete",
                    "rig_activity": {
                        "mode": "fast",
                        "activity_events": 1,
                        "lever_activity": {},
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    path = module.write_rig_rows(
        data_root=data_root,
        output_dir=output_dir,
        domain="enterprise",
    )

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "question_id": "q-enterprise",
        "status": "valid",
        "context_status": "complete",
        "evidence_exposure_eligible": True,
        "evidence_exposed": True,
        "activity": {"mode": "fast", "activity_events": 1, "lever_activity": {}},
    }


def test_official_runner_refuses_to_infer_missing_rig_activity(tmp_path: Path) -> None:
    module = _load_runner_module()
    data_root = tmp_path / "data"
    output_dir = tmp_path / "out"
    _write_dataset(data_root)
    runtime_dir = output_dir / "runtime_inputs"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "haystack.json").write_text(
        json.dumps({"q-enterprise": ["t1", "t2"]}),
        encoding="utf-8",
    )
    (output_dir / "per_question.jsonl").write_text(
        json.dumps(
            {
                "question_id": "q-enterprise",
                "score_bool": True,
                "memory_context": [],
                "memory_post_query_metadata": {"context_status": "empty"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="no explicit activity receipt"):
        module.write_rig_rows(
            data_root=data_root,
            output_dir=output_dir,
            domain="enterprise",
        )
    with pytest.raises(RuntimeError, match="exceeds fixed cap"):
        module.enforce_actual_spend_cap(
            {
                "accounting": {
                    "cost": {
                        "coverage_complete": True,
                        "provider_reported_total_usd": 1.01,
                    }
                }
            },
            max_spend_usd=1.0,
        )


def test_official_runner_carries_explicit_render_profile_into_memory(tmp_path: Path) -> None:
    module = _load_runner_module()
    total_chars = 400_000
    args = module.parse_args(
        [
            "--data-root",
            str(tmp_path / "data"),
            "--domain",
            "web",
            "--output-dir",
            str(tmp_path / "out"),
            "--max-context-total-chars",
            str(total_chars),
            "--operational-note-dedupe-mode",
            "source_kind",
            "--operational-note-lane-mode",
            "additive",
            "--operational-note-distillation-profile",
            "render_v1",
            "--render-char-total-treatment",
            "--render-group-lanes",
            "--render-action-spines",
        ]
    )

    params = module.build_memory_config(args)["memory_params"]

    assert params["max_context_total_chars"] == total_chars
    assert params["operational_note_dedupe_mode"] == "source_kind"
    assert params["operational_note_lane_mode"] == "additive"
    assert params["operational_note_distillation_profile"] == "render_v1"
    assert params["render_char_total_treatment"] is True
    assert params["render_group_lanes"] is True
    assert params["render_action_spines"] is True


def test_official_runner_omits_baseline_render_profile_from_memory(tmp_path: Path) -> None:
    module = _load_runner_module()
    args = module.parse_args(
        [
            "--data-root",
            str(tmp_path / "data"),
            "--domain",
            "web",
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )

    params = module.build_memory_config(args)["memory_params"]

    assert params["longmemeval_v2_domain"] == "web"
    assert (
        not {
            "operational_note_dedupe_mode",
            "operational_note_lane_mode",
            "operational_note_distillation_profile",
            "render_char_total_treatment",
            "render_group_lanes",
            "render_action_spines",
        }
        & params.keys()
    )


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


def test_provider_accounting_prices_pinned_openai_judge_usage(tmp_path: Path) -> None:
    module = _load_runner_module()
    usage_path = tmp_path / "judge.jsonl"
    usage_path.touch()
    accounting = module._provider_accounting(
        [
            {
                "judge_usage_path": usage_path,
                "judge_usage_invalid_lines": 0,
                "judge_usage_foreign_event_count": 0,
                "judge_usage_events": [
                    {
                        "requested_model": "gpt-5.2",
                        "provider_model": "gpt-5.2-2025-12-11",
                        "usage": {
                            "prompt_tokens": 1000,
                            "completion_tokens": 200,
                            "total_tokens": 1200,
                            "prompt_tokens_details": {"cached_tokens": 400},
                        },
                    }
                ],
                "expected_usage_run_id": "usage-current",
            }
        ],
        role="judge",
        fallback_input_tokens=0.0,
        fallback_output_tokens=0.0,
    )

    assert accounting["provider_reported_cost_usd"] == 0.0
    assert accounting["official_pricing_cost_usd"] == pytest.approx(0.00392)
    assert accounting["settled_cost_usd"] == pytest.approx(0.00392)
    assert accounting["estimated_cost_usd"] == pytest.approx(0.00392)
    assert accounting["cost_sources"] == ["openai_official_model_pricing"]
    assert accounting["official_price_snapshot"] == module.EVAL_PRICE_SNAPSHOT["judge"]
    assert accounting["cost_coverage_complete"] is True


def test_provider_accounting_rejects_negative_judge_cost(tmp_path: Path) -> None:
    module = _load_runner_module()
    usage_path = tmp_path / "judge.jsonl"
    usage_path.touch()
    accounting = module._provider_accounting(
        [
            {
                "judge_usage_path": usage_path,
                "judge_usage_invalid_lines": 0,
                "judge_usage_foreign_event_count": 0,
                "judge_usage_events": [
                    {
                        "requested_model": "gpt-5.2",
                        "provider_model": "gpt-5.2-2025-12-11",
                        "usage": {
                            "cost_usd": -0.01,
                            "prompt_tokens": 1000,
                            "completion_tokens": 200,
                            "total_tokens": 1200,
                            "prompt_tokens_details": {"cached_tokens": 400},
                        },
                    }
                ],
                "expected_usage_run_id": "usage-current",
            }
        ],
        role="judge",
        fallback_input_tokens=0.0,
        fallback_output_tokens=0.0,
    )

    assert accounting["settled_cost_usd"] == 0.0
    assert accounting["cost_coverage_complete"] is False


@pytest.mark.parametrize("invalid_cost", ["garbage", True, float("nan")])
def test_provider_accounting_rejects_malformed_explicit_judge_cost(
    invalid_cost: object,
) -> None:
    module = _load_runner_module()
    event = {
        "requested_model": "gpt-5.2",
        "provider_model": "gpt-5.2-2025-12-11",
        "usage": {
            "cost_usd": invalid_cost,
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
    }

    assert module._provider_event_cost(event, role="judge") is None


def test_provider_accounting_rejects_zero_token_judge_fallback() -> None:
    module = _load_runner_module()
    event = {
        "requested_model": "gpt-5.2",
        "provider_model": "gpt-5.2-2025-12-11",
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
    }

    assert module._provider_event_cost(event, role="judge") is None


@pytest.mark.parametrize(
    ("provider_model", "prompt_details"),
    [
        ("gpt-5.2-2099-01-01", {"cached_tokens": 0}),
        ("gpt-5.2-2025-12-11", {}),
    ],
)
def test_provider_accounting_rejects_unpinned_or_incomplete_judge_usage(
    tmp_path: Path,
    provider_model: str,
    prompt_details: dict[str, int],
) -> None:
    module = _load_runner_module()
    usage_path = tmp_path / "judge.jsonl"
    usage_path.touch()
    accounting = module._provider_accounting(
        [
            {
                "judge_usage_path": usage_path,
                "judge_usage_invalid_lines": 0,
                "judge_usage_foreign_event_count": 0,
                "judge_usage_events": [
                    {
                        "requested_model": "gpt-5.2",
                        "provider_model": provider_model,
                        "usage": {
                            "prompt_tokens": 1000,
                            "completion_tokens": 200,
                            "total_tokens": 1200,
                            "prompt_tokens_details": prompt_details,
                        },
                    }
                ],
                "expected_usage_run_id": "usage-current",
            }
        ],
        role="judge",
        fallback_input_tokens=0.0,
        fallback_output_tokens=0.0,
    )

    assert accounting["settled_cost_usd"] == 0.0
    assert accounting["cost_coverage_complete"] is False


def test_actual_spend_cap_uses_settled_total() -> None:
    module = _load_runner_module()

    with pytest.raises(RuntimeError, match="exceeds fixed cap"):
        module.enforce_actual_spend_cap(
            {
                "accounting": {
                    "cost": {
                        "coverage_complete": True,
                        "provider_reported_total_usd": 0.5,
                        "settled_total_usd": 1.01,
                    }
                }
            },
            max_spend_usd=1.0,
        )


def test_actual_spend_cap_rejects_negative_settlement() -> None:
    module = _load_runner_module()

    with pytest.raises(RuntimeError, match="provider spend is missing"):
        module.enforce_actual_spend_cap(
            {
                "accounting": {
                    "cost": {
                        "coverage_complete": True,
                        "settled_total_usd": -999.0,
                    }
                }
            },
            max_spend_usd=0.0,
        )


def test_embedding_accounting_accepts_loaded_local_memory() -> None:
    module = _load_runner_module()
    historical_ingest_usage = {
        "provider": "openai",
        "model": "text-embedding-3-small",
        "requests": 1,
        "inputs": 1,
        "prompt_tokens": 1000,
        "total_tokens": 1000,
        "cost_reported_requests": 0,
        "cost_usd": 0.0,
    }
    local_usage = {
        "provider": "local",
        "model": "sentence-transformers/all-MiniLM-L6-v2",
        "requests": 1,
        "inputs": 1,
        "prompt_tokens": 10,
        "total_tokens": 10,
        "cost_reported_requests": 0,
        "cost_usd": 0.0,
    }
    accounting = module._embedding_accounting(
        [
            {
                "plan": {"load_memory_dir": "/sealed/memory_state"},
                "per_question_rows": [
                    {
                        "memory_post_query_metadata": {
                            "ingest_embedding_usage": historical_ingest_usage,
                            "search_metadata": {"embedding_usage": local_usage},
                        }
                    }
                ],
            }
        ]
    )

    assert accounting["providers"] == ["local"]
    assert accounting["requests"] == 1
    assert accounting["estimated_input_tokens"] == EXPECTED_LOADED_QUERY_TOKENS
    assert accounting["settled_cost_usd"] == 0.0
    assert accounting["cost_sources"] == ["local_runtime_zero_cost"]
    assert accounting["tracking_complete"] is True
    assert accounting["cost_coverage_complete"] is True


def test_embedding_accounting_requires_one_consistent_build_ingest_receipt() -> None:
    module = _load_runner_module()
    first = {
        "provider": "openai",
        "model": "text-embedding-3-small",
        "requests": 1,
        "inputs": 1,
        "prompt_tokens": 1,
        "total_tokens": 1,
        "cost_reported_requests": 0,
        "cost_usd": 0.0,
    }
    second = {**first, "prompt_tokens": 1_000_000, "total_tokens": 1_000_000}
    query = {**first, "prompt_tokens": 1, "total_tokens": 1}
    accounting = module._embedding_accounting(
        [
            {
                "plan": {"load_memory_dir": None},
                "per_question_rows": [
                    {
                        "memory_post_query_metadata": {
                            "ingest_embedding_usage": first,
                            "search_metadata": {"embedding_usage": query},
                        }
                    },
                    {
                        "memory_post_query_metadata": {
                            "ingest_embedding_usage": second,
                            "search_metadata": {"embedding_usage": query},
                        }
                    },
                ],
            }
        ]
    )

    assert accounting["tracking_complete"] is False
    assert accounting["cost_coverage_complete"] is False
    assert accounting["estimated_input_tokens"] == EXPECTED_MISMATCHED_INGEST_QUERY_TOKENS


@pytest.mark.parametrize(
    "override",
    [
        {"requests": 0.5},
        {"cost_reported_requests": 0.5},
        {"prompt_tokens": -1, "total_tokens": -1},
        {"total_tokens": 999},
        {"cost_usd": -0.01},
    ],
)
def test_embedding_accounting_rejects_malformed_usage_counters(
    override: dict[str, float | int],
) -> None:
    module = _load_runner_module()
    usage = {
        "provider": "openai",
        "model": "text-embedding-3-small",
        "requests": 1,
        "inputs": 1,
        "prompt_tokens": 1000,
        "total_tokens": 1000,
        "cost_reported_requests": 0,
        "cost_usd": 0.0,
        **override,
    }
    accounting = module._embedding_accounting(
        [
            {
                "plan": {"load_memory_dir": None},
                "per_question_rows": [
                    {
                        "memory_post_query_metadata": {
                            "ingest_embedding_usage": usage,
                            "search_metadata": {"embedding_usage": usage},
                        }
                    }
                ],
            }
        ]
    )

    assert accounting["tracking_complete"] is False
    assert accounting["cost_coverage_complete"] is False
    assert accounting["official_pricing_cost_usd"] == 0.0
    assert accounting["settled_cost_usd"] >= 0.0


def test_embedding_accounting_prices_unreported_openai_usage() -> None:
    module = _load_runner_module()
    expected_requests = 2
    usage = {
        "provider": "openai",
        "model": "text-embedding-3-small",
        "requests": 1,
        "inputs": 2,
        "prompt_tokens": 1000,
        "total_tokens": 1000,
        "cost_reported_requests": 0,
        "cost_usd": 0.0,
    }
    accounting = module._embedding_accounting(
        [
            {
                "plan": {"load_memory_dir": None},
                "per_question_rows": [
                    {
                        "memory_post_query_metadata": {
                            "ingest_embedding_usage": usage,
                            "search_metadata": {"embedding_usage": usage},
                        }
                    }
                ],
            }
        ]
    )

    assert accounting["requests"] == expected_requests
    assert accounting["provider_reported_cost_usd"] == 0.0
    assert accounting["official_pricing_cost_usd"] == pytest.approx(0.00004)
    assert accounting["settled_cost_usd"] == pytest.approx(0.00004)
    assert accounting["official_price_snapshot"] == module.EVAL_PRICE_SNAPSHOT["embedding"]
    assert accounting["cost_coverage_complete"] is True


def test_embedding_accounting_rejects_untracked_reported_cost() -> None:
    module = _load_runner_module()
    usage = {
        "provider": "openai",
        "model": "text-embedding-3-small",
        "requests": 1,
        "inputs": 1,
        "prompt_tokens": 1000,
        "total_tokens": 1000,
        "cost_reported_requests": 0,
        "cost_usd": 0.25,
    }
    accounting = module._embedding_accounting(
        [
            {
                "plan": {"load_memory_dir": None},
                "per_question_rows": [
                    {
                        "memory_post_query_metadata": {
                            "ingest_embedding_usage": usage,
                            "search_metadata": {"embedding_usage": usage},
                        }
                    }
                ],
            }
        ]
    )

    assert accounting["provider_reported_cost_usd"] == pytest.approx(0.5)
    assert accounting["settled_cost_usd"] == pytest.approx(0.5)
    assert accounting["official_pricing_cost_usd"] == 0.0
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


def test_planner_accounting_rejects_negative_provider_cost() -> None:
    module = _load_runner_module()
    usage = {
        "provider": "openai",
        "model": "gpt-5.4-nano",
        "requests": 1,
        "input_tokens": 40,
        "output_tokens": 10,
        "total_tokens": 50,
        "cost_usd": -1.0,
        "cost_complete": True,
    }
    accounting = module._planner_accounting(
        [
            {
                "memory_config": {"memory_params": {"retrieval_mode": "accurate"}},
                "per_question_rows": [
                    {
                        "memory_post_query_metadata": {
                            "retrieval_mode": "accurate",
                            "search_metadata": {
                                "planner_status": "success",
                                "planner_usage": usage,
                            },
                        }
                    }
                ],
            }
        ]
    )

    assert accounting["provider_reported_cost_usd"] == 0.0
    assert accounting["tracking_complete"] is True
    assert accounting["cost_coverage_complete"] is False


@pytest.mark.parametrize(
    "override",
    [
        {"requests": 0.5},
        {"input_tokens": -1},
        {"total_tokens": 49},
        {"cost_usd": -1.0},
    ],
)
def test_structured_provider_usage_rejects_malformed_values(
    override: dict[str, float | int],
) -> None:
    module = _load_runner_module()
    usage = {
        "provider": "openai",
        "model": "gpt-5.4-nano",
        "requests": 1,
        "input_tokens": 40,
        "output_tokens": 10,
        "total_tokens": 50,
        "cost_usd": 0.001,
        "cost_complete": True,
        **override,
    }

    assert module._structured_provider_usage_valid(usage) is False


@pytest.mark.parametrize(
    "override",
    [
        {"input_tokens": 100, "total_tokens": 100},
        {"output_tokens": 100, "total_tokens": 100},
        {"cost_usd": 0.01},
        {"requests": 1},
    ],
)
def test_structured_provider_usage_rejects_zero_request_work(
    override: dict[str, float | int],
) -> None:
    module = _load_runner_module()
    usage = {
        "provider": "deterministic",
        "model": "pseudo_relevance_feedback_v2",
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "cost_complete": True,
        **override,
    }

    assert module._structured_provider_usage_valid(usage) is False


@pytest.mark.parametrize(
    "override",
    [
        {"inputs": 1},
        {"prompt_tokens": 100, "total_tokens": 100},
        {"cost_reported_requests": 1, "cost_usd": 0.01},
        {"requests": 1},
    ],
)
def test_embedding_usage_rejects_zero_request_work(
    override: dict[str, float | int],
) -> None:
    module = _load_runner_module()
    usage = {
        "provider": "local",
        "model": "sentence-transformers/all-MiniLM-L6-v2",
        "requests": 0,
        "inputs": 0,
        "prompt_tokens": 0,
        "total_tokens": 0,
        "cost_reported_requests": 0,
        "cost_usd": 0.0,
        **override,
    }

    assert module._embedding_usage_record_valid(usage) is False


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


def test_distillation_accounting_counts_each_ingest_once() -> None:
    module = _load_runner_module()
    usage = {
        "provider": "openai",
        "model": "gpt-5.4-nano",
        "requests": 3,
        "input_tokens": 300,
        "output_tokens": 90,
        "total_tokens": 390,
        "cost_usd": 0.0012,
        "cost_complete": True,
    }
    accounting = module._distillation_accounting(
        [
            {
                "plan": {"note_distillation": True},
                "per_question_rows": [
                    {"memory_post_query_metadata": {"ingest_note_distillation_usage": usage}},
                    {"memory_post_query_metadata": {"ingest_note_distillation_usage": usage}},
                ],
            }
        ]
    )

    assert accounting["requests"] == EXPECTED_DISTILLATION_REQUESTS
    assert accounting["estimated_input_tokens"] == EXPECTED_DISTILLATION_INPUT_TOKENS
    assert accounting["estimated_output_tokens"] == EXPECTED_DISTILLATION_OUTPUT_TOKENS
    assert accounting["total_tokens"] == EXPECTED_DISTILLATION_TOTAL_TOKENS
    assert accounting["provider_reported_cost_usd"] == pytest.approx(0.0012)
    assert accounting["recorded_source_run_count"] == 1
    assert accounting["tracking_complete"] is True
    assert accounting["cost_coverage_complete"] is True


def test_distillation_accounting_rejects_negative_provider_cost() -> None:
    module = _load_runner_module()
    usage = {
        "provider": "openai",
        "model": "gpt-5.4-nano",
        "requests": 1,
        "input_tokens": 100,
        "output_tokens": 30,
        "total_tokens": 130,
        "cost_usd": -1.0,
        "cost_complete": True,
    }
    accounting = module._distillation_accounting(
        [
            {
                "plan": {"note_distillation": True},
                "per_question_rows": [
                    {"memory_post_query_metadata": {"ingest_note_distillation_usage": usage}}
                ],
            }
        ]
    )

    assert accounting["provider_reported_cost_usd"] == 0.0
    assert accounting["tracking_complete"] is True
    assert accounting["cost_coverage_complete"] is False


@pytest.mark.parametrize("failure", ["missing", "drift", "unpriced"])
def test_distillation_accounting_fails_closed_on_incomplete_usage(failure: str) -> None:
    module = _load_runner_module()
    first = {
        "provider": "openai",
        "model": "gpt-5.4-nano",
        "requests": 1,
        "input_tokens": 100,
        "output_tokens": 30,
        "total_tokens": 130,
        "cost_usd": 0.0004,
        "cost_complete": True,
    }
    second = dict(first)
    if failure == "missing":
        second = {}
    elif failure == "drift":
        second["requests"] = 2
    else:
        first["cost_complete"] = False
        first["cost_usd"] = None
        second = dict(first)
    accounting = module._distillation_accounting(
        [
            {
                "plan": {"note_distillation": True},
                "per_question_rows": [
                    {"memory_post_query_metadata": {"ingest_note_distillation_usage": first}},
                    {"memory_post_query_metadata": {"ingest_note_distillation_usage": second}},
                ],
            }
        ]
    )

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
    memory._auth_refresh_lock = threading.Lock()
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


def test_sibyl_memory_refresh_is_single_flight_after_stale_401() -> None:
    module = _load_memory_module()
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})

    class FakeClient:
        def __init__(self) -> None:
            self.headers = {"Authorization": f"Bearer {TEST_CREDENTIAL}"}
            self.request_count = 0
            self.refresh_count = 0

        def request(self, *_args: object, **_kwargs: object) -> httpx.Response:
            self.request_count += 1
            if self.request_count == 1:
                self.headers["Authorization"] = f"Bearer {ROTATED_CREDENTIAL}"
                return httpx.Response(401)
            return httpx.Response(200, json={"ok": True})

        def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
            self.refresh_count += 1
            return httpx.Response(500)

    client = FakeClient()
    memory._client = client
    memory._auth_refresh_lock = threading.Lock()
    memory._refresh_token = ROTATED_CREDENTIAL
    memory._api_credentials_path = None
    memory._cli_auth = {}
    memory.api_retry_attempts = 1

    assert memory._request_json("GET", "/entities") == {"ok": True}
    assert client.request_count == EXPECTED_MEMORY_API_RETRY_CALLS
    assert client.refresh_count == 0


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


def test_sibyl_memory_bodies_respect_the_declared_content_max_chars() -> None:
    """Every body the adapter authors stays inside the cap it declares.

    The adapter threads one ``content_max_chars`` into three emission
    surfaces, and each reaches the graph as an entity body: the session
    payloads under both chunking modes, and the evidence parts of the
    operational experience the server projects into sessions and passages.
    A surface that skips the cap sends the server a body it will refuse,
    and the refusal lands mid-ingest after earlier trajectories have
    already spent embedding credits.
    """
    module = _load_memory_module()
    trajectory = _trajectory("t1", tree="button " + ("Priority " * 400))
    bodies: list[str] = []

    for mode in ("state", "trajectory"):
        payloads = module.build_entity_payloads_for_trajectory(
            trajectory,
            project_id="project_lme",
            run_id="run_lme",
            content_max_chars=TEST_CONTENT_MAX_CHARS,
            chunking_mode=mode,
            include_screenshot_refs=True,
        )
        assert len(payloads) > 1, mode
        bodies.extend(str(payload["content"]) for payload in payloads)

    experience = module.build_operational_experience_payload(
        trajectory,
        project_id="project_lme",
        run_id="run_lme",
        content_max_chars=TEST_CONTENT_MAX_CHARS,
        include_screenshot_refs=True,
    )["experience"]
    evidence_bodies = [
        str(part["content"])
        for observation in experience["observations"]
        for part in observation["evidence"]
    ]
    assert len(evidence_bodies) > len(experience["observations"])
    bodies.extend(evidence_bodies)

    oversized = [len(body) for body in bodies if len(body) > TEST_CONTENT_MAX_CHARS]
    assert oversized == [], oversized
    assert max(len(body) for body in bodies) > TEST_CONTENT_MAX_CHARS // 2


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
    max_chars = 600
    filler = "".join(f"line {index} filler text about nothing much here\n" for index in range(40))
    tail = "".join(f"tail {index} more filler\n" for index in range(40))
    content = (
        filler + "The secret entry is Launch Dependency Assessment under Related Links\n" + tail
    )

    exposed, metadata = module.compact_content_for_query(
        "What is the entry under Related Links on the Report page?",
        content,
        max_chars=max_chars,
    )

    assert metadata["mode"] == "query_slices"
    assert metadata["stride_window_fallback"] is True
    assert "Launch Dependency Assessment" in exposed
    assert len(exposed) <= max_chars


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


def test_passages_of_one_trajectory_are_distinct_assembly_candidates() -> None:
    """Sub-state rows must not dedupe each other down to one row per trajectory.

    Passages carry the trajectory id but neither a chunk index nor a state
    index, so keying their fallback on the trajectory made every passage of a
    trajectory the same candidate. Assembly then admitted the first and
    discarded its siblings as duplicates, which is a slice substrate that
    returns one slice per trajectory however many the search found.
    """
    module = _load_memory_module()
    passages = [
        _passage_result("t1", observation_ordinal=3, passage_index=index, score=1.0 - index / 10)
        for index in range(4)
    ]

    chunk_keys = {module._result_chunk_key(passage) for passage in passages}
    diversity_keys = {module._result_diversity_key(passage) for passage in passages}
    assembled, _metadata = module.assemble_context_results(
        passages,
        chunk_catalog={},
        max_items=len(passages),
        max_chunks_per_trajectory=len(passages),
        neighbor_stitch_items=0,
        neighbor_stitch_span=0,
    )

    assert len(chunk_keys) == len(passages)
    assert len(diversity_keys) == len(passages)
    assert [result["id"] for result in assembled] == [passage["id"] for passage in passages]


def test_chunk_indexed_rows_keep_their_existing_identity() -> None:
    """The fat-state substrate keys exactly as it did; only the fallback moved.

    Every frozen campaign number came from rows that carry an integer chunk
    index and an integer state index, and both keys short-circuit on those
    before reaching the new fallback.
    """
    module = _load_memory_module()
    results = [
        _search_result("t1", chunk_index=0, state_index=0, score=1.0),
        _search_result("t1", chunk_index=1, state_index=1, score=0.9),
        _search_result("t2", chunk_index=0, state_index=0, score=0.8),
    ]

    assert [module._result_chunk_key(result) for result in results] == [
        ("t1", 0),
        ("t1", 1),
        ("t2", 0),
    ]
    assert [module._result_diversity_key(result) for result in results] == [
        ("t1", 0),
        ("t1", 1),
        ("t2", 0),
    ]

    untagged = {"id": "entity:loose", "type": "session", "content": "no trajectory", "metadata": {}}
    assert module._result_chunk_key(untagged) == ("entity:loose", "entity:loose")


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


def test_sibyl_memory_exempt_neighbors_survive_composition_next_to_their_seed() -> None:
    """The exempt arm admits stitched neighbors regardless of query coverage.

    The default gate requires a neighbor to beat its parent on query-term
    overlap, which structurally rejects answer-bearing neighbors (the answer
    is rarely phrased in the query). With the exemption on, the same
    no-coverage-gain neighbor must survive composition adjacent to its seed,
    and the displacement cost is explicit: the support slot comes out of the
    tail of the primary lane.
    """
    module = _load_memory_module()
    query = "inventory order dashboard prefix"
    first = _search_result("t1", chunk_index=1, state_index=1, score=1.0)
    neighbor = _search_result("t1", chunk_index=0, state_index=0, score=0.0)
    neighbor["content"] = (
        "Goal: inventory order dashboard prefix\n\n"
        "State 0\nAccessibility tree:\nanswer-bearing neighboring state"
    )
    results = [
        first,
        _search_result("t2", chunk_index=0, state_index=0, score=0.9),
        _search_result("t3", chunk_index=0, state_index=0, score=0.8),
        _search_result("target", chunk_index=0, state_index=0, score=0.7),
    ]
    results[-1]["content"] = query
    max_items = 4
    candidate_limit = module.context_assembly_candidate_limit(
        max_items=max_items,
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
        max_items=max_items,
        neighbor_support_exempt=True,
    )

    assert metadata["stitched_neighbor_count"] == 1
    assert composition["neighbor_support_exempt"] is True
    assert composition["selected_raw_support_count"] == 1
    assert len(selected) == max_items
    origins_and_chunks = [
        (
            module._stripped_str(item.get("_selection_origin")),
            item["metadata"]["longmemeval_v2_trajectory_id"],
            item["metadata"]["longmemeval_v2_chunk_index"],
        )
        for item in selected
    ]
    seed_position = origins_and_chunks.index(("search", "t1", 1))
    assert origins_and_chunks[seed_position + 1] == ("neighbor", "t1", 0)
    assert not any(
        item["metadata"]["longmemeval_v2_trajectory_id"] == "target" for item in selected
    )


def test_sibyl_memory_trajectory_preserving_refuses_a_sole_carrier_eviction() -> None:
    """A neighbor is refused when the seed it displaces is its trajectory's last.

    Exempting neighbors buys state coverage by spending tail seeds, and a
    stitched neighbor carries its parent's trajectory, so the trade can cost
    the pack a trajectory outright. Here every seed is the only carrier of its
    own trajectory, so the preserving arm must refuse the neighbor and hand
    back exactly the default pack.
    """
    module = _load_memory_module()
    query = "inventory order dashboard prefix"
    first = _search_result("t1", chunk_index=1, state_index=1, score=1.0)
    neighbor = _search_result("t1", chunk_index=0, state_index=0, score=0.0)
    neighbor["content"] = (
        "Goal: inventory order dashboard prefix\n\n"
        "State 0\nAccessibility tree:\nanswer-bearing neighboring state"
    )
    results = [
        first,
        _search_result("t2", chunk_index=0, state_index=0, score=0.9),
        _search_result("t3", chunk_index=0, state_index=0, score=0.8),
        _search_result("target", chunk_index=0, state_index=0, score=0.7),
    ]
    results[-1]["content"] = query
    max_items = 4
    assembled = _assemble_for_neighbor_arms(module, results, {"t1": {0: neighbor, 1: first}}, query)

    selected, composition = module.compile_operational_evidence_set(
        query=query,
        typed_results=[],
        raw_results=assembled,
        max_items=max_items,
        neighbor_support_exempt=True,
        neighbor_trajectory_preserving=True,
    )

    assert composition["neighbor_trajectory_preserving"] is True
    assert composition["selected_raw_support_count"] == 0
    assert len(selected) == max_items
    assert any(item["metadata"]["longmemeval_v2_trajectory_id"] == "target" for item in selected)
    assert not any(
        module._stripped_str(item.get("_selection_origin")) == "neighbor" for item in selected
    )


def test_sibyl_memory_trajectory_preserving_admits_a_replaceable_eviction() -> None:
    """A neighbor still lands when its displaced seed's trajectory survives.

    The refusal is scoped to trajectory coverage, not to displacement itself.
    When the evicted tail seed shares its trajectory with a seed that stays in
    the pack, nothing is lost by admitting the neighbor, so the preserving arm
    composes the same pack as the plain exempt arm.
    """
    module = _load_memory_module()
    query = "inventory order dashboard prefix"
    first = _search_result("t1", chunk_index=1, state_index=1, score=1.0)
    neighbor = _search_result("t1", chunk_index=0, state_index=0, score=0.0)
    neighbor["content"] = (
        "Goal: inventory order dashboard prefix\n\n"
        "State 0\nAccessibility tree:\nanswer-bearing neighboring state"
    )
    results = [
        first,
        _search_result("t2", chunk_index=0, state_index=0, score=0.9),
        _search_result("t3", chunk_index=0, state_index=0, score=0.8),
        _search_result("t3", chunk_index=1, state_index=1, score=0.7),
    ]
    max_items = 4
    assembled = _assemble_for_neighbor_arms(module, results, {"t1": {0: neighbor, 1: first}}, query)

    selected, composition = module.compile_operational_evidence_set(
        query=query,
        typed_results=[],
        raw_results=assembled,
        max_items=max_items,
        neighbor_support_exempt=True,
        neighbor_trajectory_preserving=True,
    )

    assert composition["selected_raw_support_count"] == 1
    assert len(selected) == max_items
    origins_and_chunks = [
        (
            module._stripped_str(item.get("_selection_origin")),
            item["metadata"]["longmemeval_v2_trajectory_id"],
            item["metadata"]["longmemeval_v2_chunk_index"],
        )
        for item in selected
    ]
    seed_position = origins_and_chunks.index(("search", "t1", 1))
    assert origins_and_chunks[seed_position + 1] == ("neighbor", "t1", 0)
    assert ("search", "t3", 0) in origins_and_chunks
    assert ("search", "t3", 1) not in origins_and_chunks


def test_sibyl_memory_additive_support_slots_spare_the_displaced_seed() -> None:
    """Overflow slots let a support join the pack without evicting a seed.

    Supports and seeds compete only because the pack is bounded by an item
    count, and the character budget that count stands in for runs about a third
    spent. With an overflow slot granted, the same fixture that forces a
    sole-carrier eviction under the plain exempt arm keeps every baseline seed
    and gains the neighbor, so the pack is the union of both arms.
    """
    module = _load_memory_module()
    query = "inventory order dashboard prefix"
    first = _search_result("t1", chunk_index=1, state_index=1, score=1.0)
    neighbor = _search_result("t1", chunk_index=0, state_index=0, score=0.0)
    neighbor["content"] = (
        "Goal: inventory order dashboard prefix\n\n"
        "State 0\nAccessibility tree:\nanswer-bearing neighboring state"
    )
    results = [
        first,
        _search_result("t2", chunk_index=0, state_index=0, score=0.9),
        _search_result("t3", chunk_index=0, state_index=0, score=0.8),
        _search_result("target", chunk_index=0, state_index=0, score=0.7),
    ]
    results[-1]["content"] = query
    max_items = 4
    assembled = _assemble_for_neighbor_arms(module, results, {"t1": {0: neighbor, 1: first}}, query)

    selected, composition = module.compile_operational_evidence_set(
        query=query,
        typed_results=[],
        raw_results=assembled,
        max_items=max_items,
        neighbor_support_exempt=True,
        neighbor_support_overflow_items=2,
    )

    assert composition["support_overflow_items"] == 1
    assert composition["selected_raw_support_count"] == 1
    assert len(selected) == max_items + 1
    origins_and_chunks = [
        (
            module._stripped_str(item.get("_selection_origin")),
            item["metadata"]["longmemeval_v2_trajectory_id"],
            item["metadata"]["longmemeval_v2_chunk_index"],
        )
        for item in selected
    ]
    seed_position = origins_and_chunks.index(("search", "t1", 1))
    assert origins_and_chunks[seed_position + 1] == ("neighbor", "t1", 0)
    assert ("search", "target", 0) in origins_and_chunks


def test_sibyl_memory_render_ceiling_admits_the_granted_overflow() -> None:
    """The item ceiling downstream of the composer has to follow it.

    Without the overflow the render stage would clip an additive pack back to
    the old geometry, so the extra items would never reach the reader and the
    arm would be unmeasurable.
    """
    module = _load_memory_module()
    max_items = 8
    overflow_items = 2
    candidate_count = 12
    assert (
        module.context_pack_item_ceiling(
            max_items=max_items,
            char_budget=None,
            candidate_count=candidate_count,
        )
        == max_items
    )
    assert (
        module.context_pack_item_ceiling(
            max_items=max_items,
            char_budget=None,
            candidate_count=candidate_count,
            overflow_items=overflow_items,
        )
        == max_items + overflow_items
    )


def test_sibyl_memory_stitch_spread_reaches_every_seed_before_the_second_ring() -> None:
    """Ring-major stitch order spends the budget across seeds, not on the first.

    Seed-major order gives the top seed both of its adjacent chunks and the
    budget is gone, so a two-item stitch never reaches the second seed however
    many seeds were retrieved. Ring-major order takes distance one from every
    seed before distance two from any of them.
    """
    module = _load_memory_module()
    query = "inventory order dashboard prefix"
    seeds = [
        _search_result("t1", chunk_index=2, state_index=2, score=1.0),
        _search_result("t2", chunk_index=2, state_index=2, score=0.9),
    ]
    catalog = {
        trajectory: {
            index: _search_result(trajectory, chunk_index=index, state_index=index, score=0.0)
            for index in (0, 1, 2, 3, 4)
        }
        for trajectory in ("t1", "t2")
    }
    stitch_items = 2

    def stitched(*, spread: bool) -> list[tuple[str, int]]:
        assembled, _metadata = module.assemble_context_results(
            [dict(seed) for seed in seeds],
            chunk_catalog=catalog,
            max_items=module.context_assembly_candidate_limit(
                max_items=8,
                neighbor_stitch_items=stitch_items,
                state_part_completion_items=0,
                has_chunk_catalog=True,
            ),
            max_chunks_per_trajectory=8,
            neighbor_stitch_items=stitch_items,
            neighbor_stitch_span=2,
            neighbor_stitch_spread=spread,
            query=query,
        )
        return [
            (
                item["metadata"]["longmemeval_v2_trajectory_id"],
                item["metadata"]["longmemeval_v2_chunk_index"],
            )
            for item in assembled
            if module._stripped_str(item.get("_selection_origin")) == "neighbor"
        ]

    assert stitched(spread=False) == [("t1", 1), ("t1", 3)]
    assert stitched(spread=True) == [("t1", 1), ("t2", 1)]


def _assemble_for_neighbor_arms(
    module: ModuleType,
    results: list[dict[str, Any]],
    chunk_catalog: dict[str, dict[int, dict[str, Any]]],
    query: str,
) -> list[dict[str, Any]]:
    candidate_limit = module.context_assembly_candidate_limit(
        max_items=4,
        neighbor_stitch_items=1,
        state_part_completion_items=0,
        has_chunk_catalog=True,
    )
    assembled, metadata = module.assemble_context_results(
        results,
        chunk_catalog=chunk_catalog,
        max_items=candidate_limit,
        max_chunks_per_trajectory=2,
        neighbor_stitch_items=1,
        neighbor_stitch_span=1,
        query=query,
    )
    assert metadata["stitched_neighbor_count"] == 1
    return assembled


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
    operational_created_entities = 17
    catalog = {
        "t1": {
            0: _search_result("t1", chunk_index=0, state_index=0, score=0.0),
            1: _search_result("t1", chunk_index=1, state_index=1, score=0.0),
        },
        "t2": {0: _search_result("t2", chunk_index=0, state_index=0, score=0.0)},
    }
    catalog_entity_count = sum(len(chunks) for chunks in catalog.values())
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})
    memory._chunk_catalog = catalog
    memory._pending_embedding_job_ids = set()
    memory._pending_projection_job_ids = set()
    memory._finalize_lock = threading.Lock()
    memory._ingest_finalized = True
    memory.api_url = "http://127.0.0.1:3434/api"
    memory.longmemeval_v2_domain = "web"
    memory.project_id = "project_saved"
    memory.run_id = "run-saved"
    memory.chunking_mode = "state"
    memory.content_max_chars = EXPECTED_CONTENT_MAX_CHARS
    memory.ingest_api_runtime = {"version": "test"}
    memory.ingest_embedding_usage = {
        "requests": EXPECTED_SAVED_USAGE_REQUESTS,
        "provider_reported_cost_usd": EXPECTED_SAVED_USAGE_COST_USD,
    }
    memory.created_entities = operational_created_entities
    (tmp_path / "memory_config.json").write_text(
        json.dumps(memory.memory_config),
        encoding="utf-8",
    )

    memory._save_backend(tmp_path)

    restored = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(restored, {})
    restored.longmemeval_v2_domain = "web"
    restored._pending_embedding_job_ids = set()
    restored._pending_projection_job_ids = set()
    restored._ingest_finalized = False
    restored._load_backend(tmp_path)

    assert (tmp_path / module.CHUNK_CATALOG_FILENAME).is_file()
    assert (tmp_path / module.MEMORY_MANIFEST_FILENAME).is_file()
    manifest = json.loads((tmp_path / module.MEMORY_MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["longmemeval_v2_domain"] == "web"
    assert manifest["created_entities"] == catalog_entity_count
    assert memory.created_entities == operational_created_entities
    assert restored._chunk_catalog == catalog
    assert restored.created_entities == catalog_entity_count
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
            "required_embedding_provider": "local",
            "required_embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
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
    assert params["required_embedding_provider"] == "local"
    assert params["required_embedding_model"] == "sentence-transformers/all-MiniLM-L6-v2"

    requested["memory_params"]["content_max_chars"] = 8_000
    with pytest.raises(RuntimeError, match="content_max_chars"):
        module.SibylLiveApiMemory.reconcile_loaded_memory_config(saved, requested)


def test_sibyl_memory_requires_the_sealed_embedding_profile() -> None:
    module = _load_memory_module()
    expected_model = "sentence-transformers/all-MiniLM-L6-v2"

    module.require_embedding_profile(
        {
            "embedding_usage": {
                "provider": "local",
                "model": expected_model,
                "requests": 1,
                "inputs": 1,
            }
        },
        provider="local",
        model=expected_model,
    )

    with pytest.raises(RuntimeError, match="embedding profile mismatch"):
        module.require_embedding_profile(
            {
                "embedding_usage": {
                    "provider": "openai",
                    "model": "text-embedding-3-small",
                    "requests": 1,
                    "inputs": 1,
                }
            },
            provider="local",
            model=expected_model,
        )

    with pytest.raises(RuntimeError, match="ingest worker embedding profile mismatch"):
        module.require_embedding_usage_profile(
            {},
            provider="local",
            model=expected_model,
            surface="ingest worker",
        )

    with pytest.raises(RuntimeError, match="no observed usage"):
        module.require_embedding_usage_profile(
            {
                "provider": "local",
                "model": expected_model,
                "requests": 0,
                "inputs": 0,
            },
            provider="local",
            model=expected_model,
            surface="ingest worker",
        )

    module.require_embedding_usage_profile(
        {
            "provider": "local",
            "model": expected_model,
            "requests": 0,
            "inputs": 0,
        },
        provider="local",
        model=expected_model,
        surface="serving API",
        require_observed_usage=False,
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_sibyl_memory_marks_cross_job_embedding_provenance_mixed(reverse: bool) -> None:
    module = _load_memory_module()
    expected_model = "sentence-transformers/all-MiniLM-L6-v2"
    openai_usage = {
        "provider": "openai",
        "model": "text-embedding-3-small",
        "requests": 1,
        "inputs": 1,
        "prompt_tokens": 10,
        "total_tokens": 10,
        "cost_reported_requests": 0,
        "cost_usd": 0.0,
    }
    local_usage = {
        "provider": "local",
        "model": expected_model,
        "requests": 0,
        "inputs": 0,
        "prompt_tokens": 0,
        "total_tokens": 0,
        "cost_reported_requests": 0,
        "cost_usd": 0.0,
    }
    usages = [openai_usage, local_usage]
    if reverse:
        usages.reverse()
    total: dict[str, object] = {}

    for usage in usages:
        module._merge_usage_totals(total, usage)

    assert total["provider"] == "mixed"
    assert total["model"] == "mixed"
    with pytest.raises(RuntimeError, match="profile mismatch"):
        module.require_embedding_usage_profile(
            total,
            provider="local",
            model=expected_model,
            surface="ingest worker",
        )


def test_sibyl_memory_rejects_malformed_child_usage_before_merge() -> None:
    module = _load_memory_module()
    total: dict[str, object] = {}
    malformed_embedding = {
        "provider": "openai",
        "model": "text-embedding-3-small",
        "requests": -1,
        "inputs": 1,
        "prompt_tokens": 10,
        "total_tokens": 10,
        "cost_reported_requests": 0,
        "cost_usd": 0.0,
    }

    with pytest.raises(RuntimeError, match="invalid usage accounting"):
        module._merge_usage_totals(total, malformed_embedding)
    assert total == {}

    malformed_distillation = {
        "provider": "openai",
        "model": "gpt-5.4-nano",
        "requests": -1,
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "cost_usd": -0.1,
        "cost_complete": True,
    }
    with pytest.raises(RuntimeError, match="invalid usage accounting"):
        module._merge_note_distillation_usage(total, malformed_distillation)
    assert total == {}


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


def test_compile_evidence_honors_typed_reservation_override() -> None:
    module = _load_memory_module()
    default_reservation = 3
    boosted_reservation = 5
    capped_reservation = 6
    max_items = 8
    typed = [
        {
            "id": f"note_{i}",
            "type": "note",
            "content": f"note {i}",
            "_selection_origin": "context_pack:typed_stream",
            "metadata": {"longmemeval_v2_trajectory_id": f"t{i}"},
        }
        for i in range(6)
    ]
    raw = [{"id": f"session_{i}", "type": "session", "content": f"raw slice {i}"} for i in range(8)]

    _, default_meta = module.compile_operational_evidence_set(
        query="find the field",
        typed_results=typed,
        raw_results=raw,
        max_items=max_items,
        mode="shared_relevance",
    )
    boosted_set, boosted_meta = module.compile_operational_evidence_set(
        query="find the field",
        typed_results=typed,
        raw_results=raw,
        max_items=max_items,
        mode="shared_relevance",
        typed_reservation_items=boosted_reservation,
    )

    assert default_meta["typed_reservation"] == default_reservation
    assert boosted_meta["typed_reservation"] == boosted_reservation
    assert boosted_meta["selected_typed_count"] >= boosted_reservation
    assert len(boosted_set) == max_items
    capped_set, capped_meta = module.compile_operational_evidence_set(
        query="find the field",
        typed_results=typed,
        raw_results=raw,
        max_items=max_items,
        mode="shared_relevance",
        typed_reservation_items=99,
    )
    assert capped_meta["typed_reservation"] == capped_reservation
    assert len(capped_set) == max_items


@pytest.mark.parametrize("max_items", [8, 28])
def test_eval_reserved_lane_is_an_absolute_count_a_wider_pack_cannot_widen(
    max_items: int,
) -> None:
    """A slice-granular pack raises `max_items`; it must not raise the note lane.

    Mirrors the production pin in `sibyl_core.retrieval.operational_evidence`.
    The proportional law this replaced reserved eleven of twenty-eight slots
    at slice granularity, and the tuning kill measured that widening as a
    total loss of the note gain. Both the shipped pack size and the slice
    pack size are pinned so neither can drift.
    """
    module = _load_memory_module()
    pinned_reservation = 3
    override_reservation = 5
    typed = [
        {
            "id": f"note_{i}",
            "type": "note",
            "content": f"note {i} about the field",
            "_selection_origin": "context_pack:typed_stream",
            "metadata": {"longmemeval_v2_trajectory_id": f"t{i}"},
        }
        for i in range(12)
    ]
    raw = [
        {"id": f"session_{i}", "type": "session", "content": f"raw slice {i} of the field"}
        for i in range(40)
    ]

    selected, meta = module.compile_operational_evidence_set(
        query="find the field",
        typed_results=typed,
        raw_results=raw,
        max_items=max_items,
        mode="shared_relevance",
    )

    assert meta["typed_reservation"] == pinned_reservation
    assert meta["selected_typed_count"] == pinned_reservation
    assert meta["selected_raw_count"] == max_items - pinned_reservation
    assert len(selected) == max_items

    override_selected, override_meta = module.compile_operational_evidence_set(
        query="find the field",
        typed_results=typed,
        raw_results=raw,
        max_items=max_items,
        mode="shared_relevance",
        typed_reservation_items=override_reservation,
    )

    assert override_meta["typed_reservation"] == override_reservation
    assert override_meta["selected_typed_count"] == override_reservation
    assert len(override_selected) == max_items


def _budget_pools(
    *,
    note_chars: int,
    raw_chars: int,
    note_count: int = 12,
    raw_count: int = 40,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    typed = [
        {
            "id": f"note_{index}",
            "type": "note",
            "content": f"note {index} about the field ".ljust(note_chars, "x"),
            "_selection_origin": "context_pack:typed_stream",
            "metadata": {"longmemeval_v2_trajectory_id": f"t{index}"},
        }
        for index in range(note_count)
    ]
    raw = [
        {
            "id": f"session_{index}",
            "type": "session",
            "content": f"raw slice {index} of the field ".ljust(raw_chars, "x"),
            "metadata": {"longmemeval_v2_trajectory_id": f"t{index}"},
        }
        for index in range(raw_count)
    ]
    return typed, raw


@pytest.mark.parametrize("max_items", [8, 28])
def test_char_budget_bounds_the_pack_and_item_count_stops_binding(max_items: int) -> None:
    """With a budget in force the pack is bounded by characters, not by `max_items`.

    The same budget must produce the same pack at both pack sizes, otherwise
    `max_items` is still the real bound and the budget is decoration.
    """
    module = _load_memory_module()
    note_chars = 200
    raw_chars = 1_000
    budget = 3 * note_chars + 10 * raw_chars
    typed, raw = _budget_pools(note_chars=note_chars, raw_chars=raw_chars)

    selected, meta = module.compile_operational_evidence_set(
        query="find the field",
        typed_results=typed,
        raw_results=raw,
        max_items=max_items,
        mode="shared_relevance",
        char_budget=budget,
    )

    assert meta["budget_mode"] == "characters"
    assert meta["char_budget"] == budget
    assert meta["selected_chars"] <= budget
    assert meta["selected_chars"] == sum(len(str(item["content"])) for item in selected)
    assert meta["selected_chars"] == budget
    assert len(selected) == EXPECTED_TYPED_NOTE_RESERVATION + EXPECTED_BUDGETED_RAW_ITEMS
    assert meta["selected_typed_count"] == EXPECTED_TYPED_NOTE_RESERVATION
    assert meta["selected_raw_count"] == EXPECTED_BUDGETED_RAW_ITEMS


@pytest.mark.parametrize("max_items", [8, 28])
def test_char_budget_keeps_the_note_lane_pinned_at_its_absolute_count(max_items: int) -> None:
    """The parent branch's pin survives the budget at both pack sizes.

    The note lane is the campaign's one proven lever and it was tuned as an
    absolute count. A budget changes what bounds the pack, not how many
    distilled notes are worth reading.
    """
    module = _load_memory_module()
    typed, raw = _budget_pools(note_chars=200, raw_chars=1_000)

    _selected, meta = module.compile_operational_evidence_set(
        query="find the field",
        typed_results=typed,
        raw_results=raw,
        max_items=max_items,
        mode="shared_relevance",
        char_budget=200 * 3 + 1_000 * 10,
    )

    assert meta["typed_reservation"] == EXPECTED_TYPED_NOTE_RESERVATION
    assert meta["selected_typed_count"] == EXPECTED_TYPED_NOTE_RESERVATION


def test_char_budget_outranks_the_note_pin_when_it_cannot_hold_three_notes() -> None:
    """A lane allowed to overrun the budget is not a budget."""
    module = _load_memory_module()
    note_chars = 500
    typed, raw = _budget_pools(note_chars=note_chars, raw_chars=1_000)

    selected, meta = module.compile_operational_evidence_set(
        query="find the field",
        typed_results=typed,
        raw_results=raw,
        max_items=8,
        mode="shared_relevance",
        char_budget=note_chars,
    )

    assert meta["typed_reservation"] == 1
    assert meta["selected_chars"] <= note_chars
    assert len(selected) == 1


def test_char_budget_admits_a_rank_prefix_rather_than_packing_by_size() -> None:
    """Admission stops at the first candidate that does not fit.

    Skipping a large candidate for a smaller one further down would reorder
    relevance against length and cost the guarantee that a pack is a prefix of
    its ranking.
    """
    module = _load_memory_module()
    raw = [
        {"id": "session_big", "type": "session", "content": "the field " * 400},
        {"id": "session_small", "type": "session", "content": "the field again"},
    ]

    selected, meta = module.compile_operational_evidence_set(
        query="find the field",
        typed_results=[],
        raw_results=raw,
        max_items=8,
        mode="shared_relevance",
        char_budget=100,
    )

    assert selected == []
    assert meta["raw_candidate_count"] == len(raw)
    assert meta["selected_raw_count"] == 0


def test_composition_without_a_char_budget_reproduces_the_item_bounded_pack() -> None:
    """The frozen geometry is what a run gets when it does not ask for a budget."""
    module = _load_memory_module()
    typed, raw = _budget_pools(note_chars=200, raw_chars=1_000)

    selected, meta = module.compile_operational_evidence_set(
        query="find the field",
        typed_results=typed,
        raw_results=raw,
        max_items=8,
        mode="shared_relevance",
    )

    assert meta["budget_mode"] == "items"
    assert meta["char_budget"] is None
    assert len(selected) == EXPECTED_OPERATIONAL_EVIDENCE_ITEMS
    assert meta["typed_reservation"] == EXPECTED_TYPED_NOTE_RESERVATION
    assert meta["selected_raw_count"] == EXPECTED_OPERATIONAL_EVIDENCE_ITEMS - (
        EXPECTED_TYPED_NOTE_RESERVATION
    )


def test_char_budget_is_rejected_for_composition_modes_that_cannot_honor_it() -> None:
    module = _load_memory_module()
    typed, raw = _budget_pools(note_chars=200, raw_chars=1_000)

    with pytest.raises(ValueError, match="only defined for shared_relevance"):
        module.compile_operational_evidence_set(
            query="find the field",
            typed_results=typed,
            raw_results=raw,
            max_items=8,
            mode="reserved_support",
            char_budget=5_000,
        )


def test_entity_overlap_downranks_mismatched_notes() -> None:
    module = _load_memory_module()
    query = "Find the warranty expiration for Chelsea-Cynthia Tran-Dyer's laptop"
    matching = {
        "id": "note_match",
        "type": "note",
        "content": "Trajectory: t1\nGoal: warranty lookup\n- Chelsea-Cynthia Tran-Dyer laptop warranty shown in Hardware list",
        "metadata": {"longmemeval_v2_trajectory_id": "t1"},
    }
    mismatched = {
        "id": "note_miss",
        "type": "note",
        "content": "Trajectory: t2\nGoal: warranty lookup\n- Kelly-Ronald Schwartz-King laptop warranty shown in Hardware list",
        "metadata": {"longmemeval_v2_trajectory_id": "t2"},
    }
    neutral = {
        "id": "note_neutral",
        "type": "note",
        "content": "list header search uses a default comparison operator",
        "metadata": {"longmemeval_v2_trajectory_id": "t3"},
    }

    ranked, _ranking = module._rank_operational_evidence_pool(
        query, [mismatched, matching, neutral], pool="typed_entity_overlap"
    )
    ids = [item["id"] for item in ranked]

    assert ids.index("note_miss") > ids.index("note_match")
    assert ids.index("note_miss") > ids.index("note_neutral")


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
    typed_stream_limit = 5

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
        payload = kwargs.get("json")
        assert isinstance(payload, dict)
        captured.append(cast(dict[str, object], payload))
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
            "typed_stream_limit": typed_stream_limit,
        }
    )
    try:
        results, metadata = memory._typed_stream_results("what changed?")
    finally:
        memory._client.close()

    expected_result_ids = ["event_9", "procedure_9"]
    assert [item["id"] for item in results] == expected_result_ids
    assert all(item["_selection_origin"] == "context_pack:typed_stream" for item in results)
    assert metadata["result_count"] == len(expected_result_ids)
    request = captured[-1]
    evidence = request["evidence"]
    assert isinstance(evidence, dict)
    evidence_payload = cast(dict[str, object], evidence)
    assert evidence_payload["types"] == ["note", "event", "procedure", "error_pattern"]
    assert evidence_payload["retrieval_mode"] == "fast"
    assert evidence_payload["limit"] == typed_stream_limit
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


def test_sibyl_memory_constructor_closes_client_when_authentication_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_memory_module()

    class Client:
        closed = False

        def close(self) -> None:
            self.closed = True

    client = Client()
    monkeypatch.setattr(module, "_new_http_client", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(
        module.SibylLiveApiMemory,
        "_authenticate",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("auth failed")),
    )

    with pytest.raises(RuntimeError, match="auth failed"):
        module.SibylLiveApiMemory({"allow_localhost": True})
    assert client.closed is True


@pytest.mark.parametrize("failure_point", ["insert", "finalize"])
def test_sibyl_memory_existing_attachment_closes_client_on_failure(
    failure_point: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_memory_module()

    class Client:
        closed = False

        def close(self) -> None:
            self.closed = True

    client = Client()
    monkeypatch.setattr(module, "_new_http_client", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(module.SibylLiveApiMemory, "_authenticate", lambda *_args: None)

    def request(
        _self: object,
        _method: str,
        path: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        if path == "/health":
            return {"status": "healthy"}
        return {"id": "project_test", "entity_type": "project"}

    monkeypatch.setattr(module.SibylLiveApiMemory, "_request_json", request)
    if failure_point == "insert":
        monkeypatch.setattr(
            module.SibylLiveApiMemory,
            "insert",
            lambda *_args: (_ for _ in ()).throw(RuntimeError("insert failed")),
        )
        invoke = module.SibylLiveApiMemory.prepare_existing
        trajectories = [{"id": "trajectory_test"}]
    else:
        monkeypatch.setattr(
            module.SibylLiveApiMemory,
            "finalize_ingest",
            lambda *_args: (_ for _ in ()).throw(RuntimeError("finalize failed")),
        )
        invoke = module.SibylLiveApiMemory.attach_existing
        trajectories = []

    with pytest.raises(RuntimeError, match=f"{failure_point} failed"):
        invoke(
            {"allow_localhost": True, "project_id": "project_test"},
            expected_trajectory_ids={"trajectory_test"},
            trajectories=trajectories,
        )
    assert client.closed is True


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


def test_official_runner_carries_substrate_and_budget_arms_into_memory_params(
    tmp_path: Path,
) -> None:
    """A slice arm has to be expressible on the command line, and reproducibly absent.

    Both knobs default to the shipped configuration, so a run that names
    neither builds the same memory params the frozen baseline did.
    """
    module = _load_runner_module()
    data_root = tmp_path / "data"
    _write_dataset(data_root)
    base_argv = [
        "--data-root",
        str(data_root),
        "--domain",
        "enterprise",
        "--output-dir",
        str(tmp_path / "output"),
        "--plan-only",
    ]

    default_params = module.build_memory_config(module.parse_args(base_argv))["memory_params"]
    slice_params = module.build_memory_config(
        module.parse_args(
            [
                *base_argv,
                "--evidence-types",
                "session",
                "passage",
                "--evidence-char-budget",
                "60000",
            ]
        )
    )["memory_params"]

    assert default_params["evidence_types"] == ["session"]
    assert default_params["evidence_char_budget"] is None
    assert slice_params["evidence_types"] == ["session", "passage"]
    assert slice_params["evidence_char_budget"] == EXPECTED_CONTEXT_TOTAL_CHARS


def test_sibyl_memory_query_context_exposes_only_opaque_invocation_id() -> None:
    module = _load_memory_module()
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})

    memory.set_query_context(query_invocation_id="opaque-run-local-id")

    assert memory.get_query_context() == {"query_invocation_id": "opaque-run-local-id"}
    with pytest.raises(TypeError):
        memory.set_query_context(question_item={"question": "must not cross"})


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


def _passage_query_memory(
    module: ModuleType,
    *,
    results: list[dict[str, Any]],
    request_payloads: list[dict[str, Any]],
) -> Any:
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})

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
                "results": results,
                "filters": {"retrieval_mode": "native"},
            },
        }

    memory.project_id = "project_lme"
    memory.search_limit = 12
    memory.max_context_items = 8
    memory.max_context_chars_per_item = TEST_CONTEXT_MAX_CHARS
    memory.retrieval_mode = "fast"
    memory.retrieval_max_planned_queries = 3
    memory._pending_embedding_job_ids = set()
    memory._pending_projection_job_ids = set()
    memory._ingest_finalized = True
    memory._query_local = threading.local()
    memory._request_json = fake_request
    return memory


def test_query_reaches_passage_evidence_when_the_type_is_requested() -> None:
    """`types` is a hard node-type filter, so passages need naming to exist.

    The composer never filtered by entity type, which is why passages looked
    reachable. The search that feeds it does: the requested list becomes a
    `node_types` filter, so a slice substrate that is not named here never
    enters the candidate pool and a gate run would score an empty slice lane.
    """
    module = _load_memory_module()
    passages = [
        _passage_result("t1", observation_ordinal=2, passage_index=index, score=1.0 - index / 10)
        for index in range(3)
    ]
    request_payloads: list[dict[str, Any]] = []
    memory = _passage_query_memory(
        module,
        results=passages,
        request_payloads=request_payloads,
    )
    memory.evidence_types = ("session", "passage")
    memory.max_chunks_per_trajectory = len(passages)

    context = memory.query("Which filter was selected?")

    assert request_payloads[0]["evidence"]["types"] == ["session", "passage"]
    assert len(context) == len(passages)
    rendered = "\n".join(str(item["value"]) for item in context)
    for index in range(len(passages)):
        assert f"passage-body-2-{index}" in rendered


def _wide_passage_pool(count: int, *, content_chars: int) -> list[dict[str, Any]]:
    return [
        _passage_result(
            f"t{index}",
            observation_ordinal=1,
            passage_index=0,
            score=1.0 - index / 100,
            content_chars=content_chars,
        )
        for index in range(count)
    ]


def test_query_with_a_char_budget_is_not_re_clipped_to_the_item_count() -> None:
    """The adapter's own item budget must stop binding once a budget is in force.

    A budget the server honors is defeated silently if the adapter then clips
    the pack back to `max_context_items` on the way to the reader, which is
    what every post-API stage did unconditionally.
    """
    module = _load_memory_module()
    passage_chars = 1_000
    passages = _wide_passage_pool(20, content_chars=passage_chars)
    request_payloads: list[dict[str, Any]] = []
    memory = _passage_query_memory(
        module,
        results=passages,
        request_payloads=request_payloads,
    )
    memory.evidence_types = ("session", "passage")
    memory.max_context_chars_per_item = passage_chars
    memory.max_context_total_chars = 400_000
    memory.evidence_char_budget = len(passages) * passage_chars

    context = memory.query("Which filter was selected?")

    assert request_payloads[0]["evidence"]["char_budget"] == len(passages) * passage_chars
    assert memory.max_context_items == EXPECTED_OPERATIONAL_EVIDENCE_ITEMS
    assert len(context) == len(passages)


def test_query_without_a_char_budget_keeps_the_item_bounded_geometry() -> None:
    """The frozen baseline arm still sends the same request and gets the same pack."""
    module = _load_memory_module()
    passages = _wide_passage_pool(20, content_chars=1_000)
    request_payloads: list[dict[str, Any]] = []
    memory = _passage_query_memory(
        module,
        results=passages,
        request_payloads=request_payloads,
    )
    memory.evidence_types = ("session", "passage")

    context = memory.query("Which filter was selected?")

    assert "char_budget" not in request_payloads[0]["evidence"]
    assert len(context) == EXPECTED_OPERATIONAL_EVIDENCE_ITEMS


def test_per_trajectory_chunk_cap_still_bounds_passages_from_one_trajectory() -> None:
    """`max_chunks_per_trajectory` is load-bearing once the unit is a passage.

    It defaults to two, which was a sane source-diversity floor when a chunk was
    a whole state. At slice granularity it is a hard cap on how much of any one
    trajectory the reader can ever see, so a passage arm has to raise it
    deliberately. Pinned here so the cap is a stated configuration rather than a
    surprise in a scored run.
    """
    module = _load_memory_module()
    passages = [
        _passage_result("t1", observation_ordinal=2, passage_index=index, score=1.0 - index / 10)
        for index in range(5)
    ]
    request_payloads: list[dict[str, Any]] = []
    memory = _passage_query_memory(
        module,
        results=passages,
        request_payloads=request_payloads,
    )
    memory.evidence_types = ("session", "passage")

    context = memory.query("Which filter was selected?")

    assert module.DEFAULT_MAX_CHUNKS_PER_TRAJECTORY == EXPECTED_MAX_CHUNKS_PER_TRAJECTORY
    assert len(context) == EXPECTED_MAX_CHUNKS_PER_TRAJECTORY


def test_query_requests_the_whole_state_substrate_by_default() -> None:
    """The shipped arm must still put exactly `["session"]` on the wire.

    Every frozen campaign number came from the whole-state substrate, and the
    gate is a paired comparison against it, so opting in to passages has to be
    something a run states rather than something it inherits.
    """
    module = _load_memory_module()
    request_payloads: list[dict[str, Any]] = []
    memory = _passage_query_memory(
        module,
        results=[_search_result("t1", chunk_index=0, state_index=0, score=1.0)],
        request_payloads=request_payloads,
    )

    memory.query("Which filter was selected?")

    assert request_payloads[0]["evidence"]["types"] == ["session"]


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        (None, ("session",)),
        (["session", "passage"], ("session", "passage")),
        ("session,passage", ("session", "passage")),
        (["passage", "passage", "session"], ("passage", "session")),
    ],
)
def test_evidence_types_param_accepts_explicit_substrates(
    requested: object,
    expected: tuple[str, ...],
) -> None:
    module = _load_memory_module()
    params = {} if requested is None else {"evidence_types": requested}

    assert (
        module._param_evidence_types(params, "evidence_types", module.DEFAULT_EVIDENCE_TYPES)
        == expected
    )


def test_char_budget_accepts_the_shipped_per_item_and_total_caps() -> None:
    """The eval's own defaults must not trip the coupling check.

    Ingest and retrieval both cap at 18,000 today, so nothing truncates before
    the budget does and the budget spends what items actually cost.
    """
    module = _load_memory_module()

    module.validate_evidence_char_budget(
        char_budget=60_000,
        content_max_chars=module.DEFAULT_CONTENT_MAX_CHARS,
        max_context_chars_per_item=module.DEFAULT_CONTEXT_CHARS_PER_ITEM,
        max_context_total_chars=module.DEFAULT_CONTEXT_TOTAL_CHARS,
    )
    module.validate_evidence_char_budget(
        char_budget=None,
        content_max_chars=module.DEFAULT_CONTENT_MAX_CHARS,
        max_context_chars_per_item=500,
        max_context_total_chars=module.DEFAULT_CONTEXT_TOTAL_CHARS,
    )


def test_char_budget_rejects_a_per_item_cap_that_flattens_every_item() -> None:
    """A per-item cap below the stored unit size turns the budget into an item budget.

    A 1K passage and a 12K state both spend the cap once truncation binds, so
    the budget stops measuring size and starts counting items again. This is a
    hard error because the failure mode is a paid run reporting a plausible
    number that argues against the substrate.
    """
    module = _load_memory_module()

    with pytest.raises(ValueError, match="degenerates into an item budget"):
        module.validate_evidence_char_budget(
            char_budget=60_000,
            content_max_chars=18_000,
            max_context_chars_per_item=500,
            max_context_total_chars=module.DEFAULT_CONTEXT_TOTAL_CHARS,
        )


def test_char_budget_rejects_a_budget_the_render_total_cannot_carry() -> None:
    module = _load_memory_module()

    with pytest.raises(ValueError, match="exceeds max_context_total_chars"):
        module.validate_evidence_char_budget(
            char_budget=135_000,
            content_max_chars=18_000,
            max_context_chars_per_item=18_000,
            max_context_total_chars=module.DEFAULT_CONTEXT_TOTAL_CHARS,
        )


@pytest.mark.parametrize("requested", [["slice"], [""], 7])
def test_evidence_types_param_rejects_types_the_substrate_cannot_serve(requested: object) -> None:
    """A typo here costs a paid run and returns an empty lane, so it fails loudly."""
    module = _load_memory_module()

    with pytest.raises(ValueError, match=r"evidence type|at least one entity type|must be a list"):
        module._param_evidence_types(
            {"evidence_types": requested},
            "evidence_types",
            module.DEFAULT_EVIDENCE_TYPES,
        )


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
    assert observations[0]["metadata"]["accessibility_inventory"] == {
        "schema_version": "sibyl-accessibility-inventory-v1",
        "source": "longmemeval-v2-official",
        "complete": True,
        "truncated": False,
        "evidence_part_count": len(parts),
    }
    assert all(len(part["content"]) <= OPERATIONAL_EVIDENCE_MAX_CHARS for part in parts)
    assert [part["metadata"]["longmemeval_v2_chunk_index"] for part in parts] == list(
        range(len(parts))
    )
    reconstructed = "".join(part["content"].split("\n\n", maxsplit=2)[-1] for part in parts)
    states = cast(list[dict[str, object]], trajectory["states"])
    tree = cast(str, states[0]["accessibility_tree"])
    assert tree in reconstructed


def test_retrieval_lane_activity_fails_faulted_hybrid_and_typed_lanes() -> None:
    module = _load_memory_module()

    with pytest.raises(RuntimeError, match="hybrid vector lane failed"):
        module.retrieval_lane_activity(
            {"graph_retrieval": {"entity_manager_search_completed": False}},
            retrieval_mode="fast",
        )

    with pytest.raises(RuntimeError, match="typed-evidence lane failed"):
        module.retrieval_lane_activity(
            {
                "graph_retrieval": {"entity_manager_search_completed": True},
                "evidence_composition": {"typed_search_status": "degraded"},
            },
            retrieval_mode="fast",
        )


def test_retrieval_lane_activity_is_mode_aware_for_naive() -> None:
    module = _load_memory_module()

    activity = module.retrieval_lane_activity(
        {
            "vector_requested": True,
            "vector_attempted": True,
            "vector_degraded": False,
            "vector_status": "ok",
        },
        retrieval_mode="naive",
    )

    assert activity["naive_vector_attempts"] == 1
    assert activity["typed_evidence_applicable"] is False
    assert activity["activity_events"] > 0


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
        "neighbor_support_exempt": False,
        "neighbor_trajectory_preserving": False,
        "neighbor_support_overflow_items": 0,
        "support_overflow_items": 0,
        "traversal_candidate_count": 0,
        "traversal_overflow_items": 0,
        "traversal_admitted_items": 0,
        "semantic_prior_rescue_weight": 0.0,
        "typed_pool": "typed",
        "budget_mode": "items",
        "char_budget": None,
        "char_budget_raw_reserve": None,
        "selected_chars": sum(len(str(item["content"])) for item in selected),
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
    assert request_json["note_distillation"] is False
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


def test_sibyl_memory_requests_and_profiles_operational_note_distillation() -> None:
    module = _load_memory_module()
    memory = module.SibylLiveApiMemory.__new__(module.SibylLiveApiMemory)
    module.Memory.__init__(memory, {})
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
            "written_entities": 1,
            "background_jobs": {
                "note_distillation": {
                    "status": "queued",
                    "job_ids": ["distill-lme-v2-1"],
                },
            },
        }

    memory.project_id = "project_lme"
    memory.run_id = "run_lme"
    memory.content_max_chars = TEST_CONTENT_MAX_CHARS
    memory.embedding_backfill_max_pending_jobs = 8
    memory.include_screenshot_refs = False
    memory.defer_embeddings = False
    memory.note_distillation = True
    memory.operational_note_distillation_profile = "render_v1"
    memory.created_entities = 0
    memory.inserted_trajectories = 0
    memory._pending_embedding_job_ids = set()
    memory._pending_projection_job_ids = set()
    memory._pending_note_distillation_job_ids = set()
    memory._request_json = fake_request

    memory.insert(_trajectory("t1"))

    payload = requests[0]
    assert payload["note_distillation"] is True
    experience = cast(dict[str, object], payload["experience"])
    metadata = cast(dict[str, object], experience["metadata"])
    assert metadata["operational_note_distillation_profile"] == "render_v1"
    assert memory._pending_note_distillation_job_ids == {"distill-lme-v2-1"}


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
        {
            "status": "complete",
            "error": None,
            "result": _local_embedding_job_result(),
        },
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
                "neighbor_support_exempt": False,
                "neighbor_trajectory_preserving": False,
                "neighbor_support_overflow_items": 0,
                "support_overflow_items": 0,
                "traversal_candidate_count": 0,
                "traversal_overflow_items": 0,
                "traversal_admitted_items": 0,
                "semantic_prior_rescue_weight": 0.0,
                "typed_pool": "typed",
                "budget_mode": "items",
                "char_budget": None,
                "char_budget_raw_reserve": None,
                "selected_chars": 0,
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
        return {
            "jobs": {
                job_id: {
                    "status": "complete",
                    "error": None,
                    "result": _local_embedding_job_result(),
                }
                for job_id in job_ids
            }
        }

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
                        "result": ({} if status == "not_found" else _local_embedding_job_result()),
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
                        "result": ({} if status_calls == 1 else _local_embedding_job_result()),
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
        return {
            "jobs": {
                job_id: {
                    "status": "complete",
                    "error": None,
                    "result": _local_embedding_job_result(),
                }
                for job_id in job_id_batch
            }
        }

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
    run_arg_method: str | None = None,
    run_arg_tier: str | None = None,
) -> None:
    output_dir.mkdir(parents=True)
    run_id = f"run-{domain}"
    usage_run_id = run_id if legacy_usage_identity else f"usage-{domain}"
    runtime_dir = output_dir / "runtime_inputs"
    runtime_dir.mkdir()
    plan = {
        "run_id": run_id,
        "domain": domain,
        "tier": "small",
        "method": "sibyl_live_api",
    }
    if not legacy_usage_identity:
        plan["provider_usage_run_id"] = usage_run_id
    (output_dir / "longmemeval_v2_official_plan.json").write_text(
        json.dumps(plan),
        encoding="utf-8",
    )
    (output_dir / "longmemeval_v2_official_receipt.json").write_text(
        json.dumps({"domain": domain, "schema_version": "fixture"}),
        encoding="utf-8",
    )
    (output_dir / "prompt_build_summary.json").write_text(
        json.dumps({"prompt_row_count": 1, "question_ids": [f"q-{domain}"]}),
        encoding="utf-8",
    )
    (output_dir / "prompt_rows.jsonl").write_text(
        json.dumps({"question_id": f"q-{domain}", "messages": []}) + "\n",
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
    run_args = {
        "domain": domain,
        "model": "Qwen/Qwen3.5-9B",
        "base_url": "http://localhost:8023/v1",
        "evaluator_model": "gpt-5.2",
        "evaluator_reasoning_effort": "medium",
    }
    if run_arg_method is not None:
        run_args["method"] = run_arg_method
    if run_arg_tier is not None:
        run_args["tier"] = run_arg_tier
    (output_dir / "run_args.json").write_text(
        json.dumps(run_args),
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


def _passage_result(
    trajectory_id: str,
    *,
    observation_ordinal: int,
    passage_index: int,
    score: float,
    content_chars: int = 1_000,
) -> dict[str, Any]:
    """A passage row as the operational projection actually mints it.

    The metadata bag is the projection's shared `common` block plus the
    passage's own fields, so it carries the trajectory id but neither the
    chunk index nor the state index the fat-state substrate keys on.
    """
    return {
        "id": f"entity:{trajectory_id}:state-{observation_ordinal}:passage-{passage_index}",
        "type": "passage",
        "name": f"Observation {observation_ordinal} passage {passage_index + 1}",
        "content": f"passage-body-{observation_ordinal}-{passage_index} ".ljust(content_chars, "x"),
        "score": score,
        "result_origin": "graph",
        "metadata": {
            "longmemeval_v2_trajectory_id": trajectory_id,
            "projection_kind": "passage",
            "observation_ordinal": observation_ordinal,
            "passage_index": passage_index,
        },
    }


def test_raw_reserve_cures_fat_head_starvation() -> None:
    """A fat top-ranked raw candidate must not zero out the evidence lane.

    Without a reserve, prefix-stop admission lets the reserved notes spend
    first and the fat head then exceeds the remainder, so the raw lane admits
    nothing (the observed stage-2 slice-screen geometry). With the reserve,
    the raw lane spends first and the same pool keeps its evidence.
    """
    module = _load_memory_module()
    typed = [
        {
            "id": f"note_{index}",
            "type": "note",
            "content": "distilled note ".ljust(200, "n"),
            "metadata": {"longmemeval_v2_trajectory_id": f"nt{index}"},
        }
        for index in range(3)
    ]
    raw = [
        {
            "id": "session_fat",
            "type": "session",
            "content": "fat state ".ljust(5_000, "f"),
            "metadata": {"longmemeval_v2_trajectory_id": "tf"},
        },
        {
            "id": "passage_small",
            "type": "passage",
            "content": "small slice ".ljust(400, "p"),
            "metadata": {"longmemeval_v2_trajectory_id": "tp"},
        },
    ]

    char_budget = 5_500
    raw_reserve = 5_200
    starved, _starved_meta = module.compile_operational_evidence_set(
        query="find the field",
        typed_results=typed,
        raw_results=raw,
        max_items=8,
        mode="shared_relevance",
        char_budget=char_budget,
    )
    starved_ids = {item["id"] for item in starved}
    assert "session_fat" not in starved_ids
    assert "passage_small" not in starved_ids

    cured, cured_meta = module.compile_operational_evidence_set(
        query="find the field",
        typed_results=typed,
        raw_results=raw,
        max_items=8,
        mode="shared_relevance",
        char_budget=char_budget,
        char_budget_raw_reserve=raw_reserve,
    )
    assert cured_meta["char_budget_raw_reserve"] == raw_reserve
    assert cured_meta["selected_chars"] <= char_budget
    assert any(item["id"] == "session_fat" for item in cured)


def test_raw_reserve_returns_unspent_budget_to_the_shared_pool() -> None:
    module = _load_memory_module()
    typed = [
        {
            "id": f"note_{index}",
            "type": "note",
            "content": "typed item ".ljust(300, "t"),
            "metadata": {"longmemeval_v2_trajectory_id": f"nt{index}"},
        }
        for index in range(12)
    ]
    raw = [
        {
            "id": "passage_only",
            "type": "passage",
            "content": "tiny slice ".ljust(500, "p"),
            "metadata": {"longmemeval_v2_trajectory_id": "tp"},
        }
    ]

    char_budget = 6_000
    raw_reserve = 3_000
    note_chars = 600
    selected, meta = module.compile_operational_evidence_set(
        query="find the field",
        typed_results=typed,
        raw_results=raw,
        max_items=8,
        mode="shared_relevance",
        char_budget=char_budget,
        char_budget_raw_reserve=raw_reserve,
    )
    assert any(item["id"] == "passage_only" for item in selected)
    # The raw lane spent 500 of its 3000 reserve; typed overflow must reach
    # past reserve + notes or the unspent reserve was burned.
    assert meta["selected_chars"] > raw_reserve + note_chars
    assert meta["selected_chars"] <= char_budget
    assert meta["selected_chars"] == sum(len(str(item["content"])) for item in selected)


def test_raw_reserve_validation() -> None:
    module = _load_memory_module()
    typed, raw = _budget_pools(note_chars=200, raw_chars=1_000)
    with pytest.raises(ValueError, match="requires char_budget"):
        module.compile_operational_evidence_set(
            query="q",
            typed_results=typed,
            raw_results=raw,
            max_items=8,
            mode="shared_relevance",
            char_budget_raw_reserve=1_000,
        )
    with pytest.raises(ValueError, match="below char_budget"):
        module.compile_operational_evidence_set(
            query="q",
            typed_results=typed,
            raw_results=raw,
            max_items=8,
            mode="shared_relevance",
            char_budget=5_000,
            char_budget_raw_reserve=5_000,
        )


def test_raw_reserve_none_is_byte_identical_to_the_shipped_budget_path() -> None:
    module = _load_memory_module()
    typed, raw = _budget_pools(note_chars=200, raw_chars=1_000)
    kwargs = {
        "query": "find the field",
        "typed_results": typed,
        "raw_results": raw,
        "max_items": 8,
        "mode": "shared_relevance",
        "char_budget": 3 * 200 + 10 * 1_000,
    }
    base_selected, base_meta = module.compile_operational_evidence_set(**kwargs)
    none_selected, none_meta = module.compile_operational_evidence_set(
        **kwargs, char_budget_raw_reserve=None
    )
    assert [item["id"] for item in base_selected] == [item["id"] for item in none_selected]
    base_meta.pop("char_budget_raw_reserve")
    none_meta.pop("char_budget_raw_reserve")
    assert base_meta == none_meta


def _load_adapter_module_for_keys() -> ModuleType:
    return _load_module(
        Path(__file__).parents[2] / "benchmarks" / "longmemeval_v2_memory" / "sibyl_memory.py",
        "sibyl_memory_for_runtime_keys",
    )


def test_loaded_runtime_keys_stay_in_parity_with_the_adapter() -> None:
    """The runner and the adapter each merge saved configs against their own
    copy of LOADED_MEMORY_RUNTIME_KEYS. A key present in one copy and absent
    from the other is silently dropped by that side's merge, which turns an
    armed run into a baseline replica under the arm's name (the A3 traversal
    screen failed exactly this way). The only tolerated divergence is the
    adapter-side secret aliases the runner never produces."""
    runner = _load_runner_module()
    adapter = _load_adapter_module_for_keys()
    runner_keys = set(runner.LOADED_MEMORY_RUNTIME_KEYS)
    adapter_keys = set(adapter.LOADED_MEMORY_RUNTIME_KEYS)
    assert runner_keys <= adapter_keys
    assert adapter_keys - runner_keys == {"api_credentials_path", "refresh_token"}


def test_loaded_memory_merge_threads_every_behavioral_request_key(tmp_path: Path) -> None:
    module = _load_runner_module()
    memory_dir = tmp_path / "memory_state"
    memory_dir.mkdir()
    max_actions = 3
    rescue_weight = 0.5
    (memory_dir / "memory_config.json").write_text(
        json.dumps(
            {
                "memory_type": "sibyl_live_api",
                "memory_params": {
                    "api_url": "http://127.0.0.1:3434/api",
                    "project_id": "project_saved",
                    "run_id": "run-saved",
                },
            }
        ),
        encoding="utf-8",
    )
    args = module.parse_args(
        [
            "--data-root",
            str(tmp_path / "data"),
            "--domain",
            "enterprise",
            "--output-dir",
            str(tmp_path / "output"),
            "--api-url",
            "http://127.0.0.1:3434/api",
            "--load-memory-dir",
            str(memory_dir),
            "--agentic-traversal",
            "--traversal-max-actions",
            str(max_actions),
            "--semantic-prior-rescue-weight",
            str(rescue_weight),
        ]
    )
    params = module.build_memory_config(args)["memory_params"]
    assert params["agentic_traversal"] is True
    assert params["traversal_max_actions"] == max_actions
    assert params["traversal_model"] == args.traversal_model
    assert params["semantic_prior_rescue_weight"] == pytest.approx(rescue_weight)


def test_loaded_memory_merge_rejects_unclassified_request_keys(tmp_path: Path) -> None:
    module = _load_runner_module()
    memory_dir = tmp_path / "memory_state"
    memory_dir.mkdir()
    (memory_dir / "memory_config.json").write_text(
        json.dumps({"memory_type": "sibyl_live_api", "memory_params": {"run_id": "run-saved"}}),
        encoding="utf-8",
    )
    requested = {
        "memory_type": "sibyl_live_api",
        "memory_params": {"run_id": "run-new", "future_arm_flag": True},
    }
    with pytest.raises(RuntimeError, match="future_arm_flag"):
        module.build_loaded_memory_config(memory_dir, requested_config=requested)


# ---------------------------------------------------------------------------
# The naive-strong control arm (1.3 Phase 0)
# ---------------------------------------------------------------------------


def test_naive_is_a_selectable_retrieval_mode() -> None:
    module = _load_memory_module()

    assert "naive" in module.RETRIEVAL_MODES
    # The arm must stay opt-in: a run that does not name it gets the machine.
    assert module.DEFAULT_RETRIEVAL_MODE == "fast"


def test_official_runner_carries_the_naive_arm_into_memory_params(tmp_path: Path) -> None:
    """The arm is only screenable if a command line can select it end to end."""

    module = _load_runner_module()
    data_root = tmp_path / "data"
    _write_dataset(data_root)

    args = module.parse_args(
        [
            "--data-root",
            str(data_root),
            "--domain",
            "enterprise",
            "--output-dir",
            str(tmp_path / "output"),
            "--plan-only",
            "--retrieval-mode",
            "naive",
        ]
    )

    config = module.build_memory_config(args)

    assert config["memory_params"]["retrieval_mode"] == "naive"


def test_the_arm_refuses_to_run_beside_retrieval_that_bypasses_it() -> None:
    """Both flags retrieve outside the arm, so the arm's name would be a lie."""

    module = _load_memory_module()

    for conflicting in ("typed_stream_retrieval", "agentic_traversal"):
        with pytest.raises(ValueError, match="control arm"):
            module.SibylLiveApiMemory(
                {
                    "allow_localhost": True,
                    "project_id": "project_test",
                    "retrieval_mode": "naive",
                    conflicting: True,
                }
            )


def test_the_arm_is_replayable_onto_a_loaded_corpus() -> None:
    """Arms are screened against an existing corpus, so the key must be runtime."""

    module = _load_runner_module()

    assert "retrieval_mode" in module.LOADED_MEMORY_RUNTIME_KEYS


def _naive_render_stub(module: ModuleType, *, max_items: int = 8) -> Any:
    """A memory adapter positioned for the render path, with no network in __init__."""

    memory = object.__new__(module.SibylLiveApiMemory)
    memory.max_context_items = max_items
    memory.max_context_chars_per_item = 4_000
    memory.retrieval_mode = "naive"
    memory._query_local = SimpleNamespace(search_metadata={}, retrieval_trace=None)
    return memory


def _chunk_result(trajectory: str, index: int) -> dict[str, object]:
    # The stitcher keys on these exact metadata names; anything else silently
    # fails to match the catalog and the expansion this guards against never
    # fires, which would leave the assertion below passing over nothing.
    return {
        "id": f"{trajectory}-{index}",
        "name": f"chunk {index}",
        "content": f"content for chunk {index}",
        "score": 0.5,
        "metadata": {
            "longmemeval_v2_trajectory_id": trajectory,
            "longmemeval_v2_chunk_index": index,
        },
    }


def test_the_arm_renders_exactly_the_candidates_the_server_returned() -> None:
    """Membership and order must survive the client untouched.

    Client-side assembly expands a hit into its catalog neighbours, with
    stitching on by default, so one returned row can render as three. A screen
    reading that render would be scoring a client-side expansion of the arm
    rather than the arm.
    """

    module = _load_memory_module()
    memory = _naive_render_stub(module)
    memory._chunk_catalog = {
        "traj-a": {index: _chunk_result("traj-a", index) for index in (0, 1, 2)}
    }
    server_results = [_chunk_result("traj-a", 1)]

    rendered = memory._render_naive_verbatim_context(query="q", results=server_results)

    assembly = memory._query_local.search_metadata["adapter_assembly"]
    assert assembly["naive_rendered_ids"] == ["traj-a-1"]
    assert len(rendered) == 1
    assert assembly["naive_verbatim_render"] is True
    assert assembly["naive_server_candidate_count"] == 1
    assert assembly["naive_rendered_count"] == 1
    assert assembly["naive_bypassed_stages"] == [
        "assemble_context_results",
        "compile_operational_evidence_set",
        "render_memory_context",
    ]

    # The contrast that makes the assertion above mean something: handed the
    # same single result and the same catalog, the client-side stage the arm
    # bypasses turns one row into three.
    expanded, _metadata = module.assemble_context_results(
        server_results,
        chunk_catalog=memory._chunk_catalog,
        max_items=8,
        max_chunks_per_trajectory=module.DEFAULT_MAX_CHUNKS_PER_TRAJECTORY,
        neighbor_stitch_items=module.DEFAULT_NEIGHBOR_STITCH_ITEMS,
        neighbor_stitch_span=module.DEFAULT_NEIGHBOR_STITCH_SPAN,
        query="q",
    )
    assert [row.get("id") for row in expanded] == ["traj-a-1", "traj-a-0", "traj-a-2"]


def test_the_arm_renders_every_candidate_not_just_max_items() -> None:
    """max_items is a machine selection stage, so it must not cut the arm's pack."""

    module = _load_memory_module()
    memory = _naive_render_stub(module, max_items=2)
    memory._chunk_catalog = {}
    server_results = [_chunk_result("traj-a", index) for index in (5, 3, 9, 1)]

    rendered = memory._render_naive_verbatim_context(query="q", results=server_results)

    # The arm already ranked these; the client neither reorders, reselects, nor
    # drops the tail to satisfy an item count.
    assembly = memory._query_local.search_metadata["adapter_assembly"]
    assert assembly["naive_rendered_ids"] == [
        "traj-a-5",
        "traj-a-3",
        "traj-a-9",
        "traj-a-1",
    ]
    assert len(rendered) == len(server_results)
    assert assembly["naive_dropped_count"] == 0


def test_the_arm_refuses_every_client_side_reshaping_flag() -> None:
    """A flag that reads as applied while doing nothing misdescribes the run."""

    module = _load_memory_module()
    reshaping = (
        ("typed_stream_retrieval", True),
        ("agentic_traversal", True),
        ("state_part_refinement", True),
        ("neighbor_stitch_spread", True),
        ("neighbor_support_exempt", True),
        ("neighbor_trajectory_preserving", True),
        ("source_evidence_bundling", True),
        ("state_part_completion_items", 2),
        ("neighbor_support_overflow_items", 2),
        ("semantic_prior_rescue_weight", 0.5),
    )
    for name, value in reshaping:
        with pytest.raises(ValueError, match="control arm"):
            module.SibylLiveApiMemory(
                {
                    "allow_localhost": True,
                    "project_id": "project_test",
                    "retrieval_mode": "naive",
                    name: value,
                }
            )


def test_the_arm_conflict_guard_does_not_depend_on_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configuration conflict is a property of the configuration.

    agentic_traversal is separately gated on an OpenAI key being exported. With
    the guard sitting beside the flags it names, that key check ran first, so
    naive-plus-traversal refused with a "control arm" error only on a machine
    that happened to export a key and reported an unrelated missing-key error
    everywhere else. CI has no key, so the guard was untested exactly where it
    mattered.
    """

    module = _load_memory_module()
    for variable in ("OPENAI_API_KEY", "SIBYL_OPENAI_API_KEY"):
        monkeypatch.delenv(variable, raising=False)

    with pytest.raises(ValueError, match="control arm"):
        module.SibylLiveApiMemory(
            {
                "allow_localhost": True,
                "project_id": "project_test",
                "retrieval_mode": "naive",
                "agentic_traversal": True,
            }
        )


def test_the_arm_conflict_message_names_every_offending_setting() -> None:
    module = _load_memory_module()

    conflicts = module._naive_arm_conflicts(
        {
            "agentic_traversal": True,
            "typed_stream_retrieval": True,
            "semantic_prior_rescue_weight": 0.5,
            "neighbor_stitch_items": 2,
        }
    )

    # Sorted so the message is stable, and neighbor_stitch_items is absent
    # because its shipped default is already non-zero: listing it would refuse
    # every plain naive run.
    assert conflicts == [
        "agentic_traversal",
        "semantic_prior_rescue_weight",
        "typed_stream_retrieval",
    ]


def test_the_arm_renders_bodies_whole_rather_than_slicing_them() -> None:
    """Query-aware slicing rewrites the evidence the arm chose to show."""

    module = _load_memory_module()
    memory = _naive_render_stub(module)
    memory._chunk_catalog = {}
    body = ("unique-token " + ("filler sentence about something else. " * 200)).strip()
    result = _chunk_result("traj-a", 1)
    result["content"] = body

    rendered = memory._render_naive_verbatim_context(query="unique-token", results=[result])

    assert len(rendered) == 1
    # The whole body survives, not a query-selected excerpt of it.
    assert body in rendered[0]["value"]


def test_the_arm_keeps_empty_bodied_candidates_in_the_pack() -> None:
    """Dropping a row is a selection decision the arm did not make."""

    module = _load_memory_module()
    memory = _naive_render_stub(module)
    memory._chunk_catalog = {}
    empty = _chunk_result("traj-a", 2)
    empty["content"] = ""
    results = [_chunk_result("traj-a", 1), empty, _chunk_result("traj-a", 3)]

    rendered = memory._render_naive_verbatim_context(query="q", results=results)

    assembly = memory._query_local.search_metadata["adapter_assembly"]
    assert assembly["naive_rendered_ids"] == ["traj-a-1", "traj-a-2", "traj-a-3"]
    assert len(rendered) == len(results)


def test_the_arm_truncates_whole_candidates_from_the_tail_and_stamps_it() -> None:
    """A short pack has to be auditable, and it drops rows rather than slicing."""

    module = _load_memory_module()
    memory = _naive_render_stub(module)
    memory._chunk_catalog = {}
    memory.max_context_total_chars = 1_200
    results = []
    for index in range(6):
        row = _chunk_result("traj-a", index)
        row["content"] = f"body {index} " + ("x" * 400)
        results.append(row)

    rendered = memory._render_naive_verbatim_context(query="q", results=results)

    assembly = memory._query_local.search_metadata["adapter_assembly"]
    assert 0 < len(rendered) < len(results)
    # The survivors are a prefix of the server's order, never a re-selection.
    assert assembly["naive_rendered_ids"] == [f"traj-a-{index}" for index in range(len(rendered))]
    assert assembly["naive_tail_truncated_from_rank"] == len(rendered) + 1
    assert assembly["naive_dropped_count"] == len(results) - len(rendered)
    # Every rendered body is whole; truncation removed rows, not text.
    for index, item in enumerate(rendered):
        assert ("x" * 400) in item["value"], f"item {index} was sliced"


def test_the_arm_refuses_settings_for_stages_it_bypasses() -> None:
    module = _load_memory_module()
    runner_defaults = {
        "neighbor_stitch_items": module.DEFAULT_NEIGHBOR_STITCH_ITEMS,
        "neighbor_stitch_span": module.DEFAULT_NEIGHBOR_STITCH_SPAN,
        "max_chunks_per_trajectory": module.DEFAULT_MAX_CHUNKS_PER_TRAJECTORY,
        "context_expansion_max_ratio": module.DEFAULT_CONTEXT_EXPANSION_MAX_RATIO,
        "evidence_composition_mode": module.DEFAULT_EVIDENCE_COMPOSITION_MODE,
        "typed_reservation_items": None,
        "knn_type_overfetch": module.DEFAULT_KNN_TYPE_OVERFETCH,
    }

    # The runner writes every key on every run, so the defaults it ships must
    # not refuse or no naive screen could start.
    assert module._naive_arm_conflicts(dict(runner_defaults)) == []

    for name, value in (
        ("neighbor_stitch_items", 3),
        ("neighbor_stitch_span", 2),
        ("max_chunks_per_trajectory", 4),
        ("context_expansion_max_ratio", 2.0),
        ("evidence_composition_mode", "reserved_support"),
        ("typed_reservation_items", 3),
        ("knn_type_overfetch", 17),
    ):
        assert module._naive_arm_conflicts({**runner_defaults, name: value}) == [name]
