from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

OPAQUE_INVOCATION_ID_HEX_LENGTH = 32


def test_script_help_without_repository_on_pythonpath(tmp_path: Path) -> None:
    script = Path(__file__).parents[2] / "benchmarks" / "longmemeval_v2_live_retrieval.py"
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-E", str(script), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--project-id" in result.stdout


def _load_module() -> ModuleType:
    path = Path(__file__).parents[2] / "benchmarks" / "longmemeval_v2_live_retrieval.py"
    spec = importlib.util.spec_from_file_location("longmemeval_v2_live_retrieval", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_load_question_ids_rejects_duplicates(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "ids.json"
    path.write_text('["q1", "q1"]', encoding="utf-8")

    with pytest.raises(ValueError, match="unique"):
        module.load_question_ids(path)


def test_select_questions_preserves_requested_order() -> None:
    module = _load_module()
    questions = {
        "q1": {"id": "q1", "domain": "enterprise"},
        "q2": {"id": "q2", "domain": "enterprise"},
    }

    selected = module.select_questions(questions, ["q2", "q1"], domain="enterprise")

    assert [question["id"] for question in selected] == ["q2", "q1"]


def test_load_trajectory_subset_requires_every_expected_id(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "trajectories.jsonl"
    path.write_text(json.dumps({"id": "t1"}) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="t2"):
        module.load_trajectory_subset(path, {"t1", "t2"})


def test_load_api_credentials_file_tracks_json_bundle_for_rotation(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps({"access_token": "access", "refresh_token": "refresh"}),
        encoding="utf-8",
    )

    assert module.load_api_credentials_file(path) == {
        "api_token": "access",
        "api_credentials_path": str(path),
        "refresh_token": "refresh",
    }


def test_prepare_output_resumes_matching_run(tmp_path: Path) -> None:
    module = _load_module()
    output_dir = tmp_path / "run"
    config = {"schema_version": module.RUN_SCHEMA_VERSION, "project_id": "project_test"}
    questions = [{"id": "q1"}, {"id": "q2"}]
    haystack = {"q1": ["t1"], "q2": ["t1"]}

    assert (
        module.prepare_output(
            output_dir,
            run_config=config,
            questions=questions,
            haystack=haystack,
            resume=False,
        )
        == set()
    )
    (output_dir / "per_question.jsonl").write_text(
        json.dumps({"question_id": "q1"}) + "\n",
        encoding="utf-8",
    )

    assert module.prepare_output(
        output_dir,
        run_config=config,
        questions=questions,
        haystack=haystack,
        resume=True,
    ) == {"q1"}


def test_prepare_output_rejects_resume_config_drift(tmp_path: Path) -> None:
    module = _load_module()
    output_dir = tmp_path / "run"
    questions = [{"id": "q1"}]
    haystack = {"q1": ["t1"]}
    module.prepare_output(
        output_dir,
        run_config={"project_id": "first"},
        questions=questions,
        haystack=haystack,
        resume=False,
    )

    with pytest.raises(ValueError, match="does not match"):
        module.prepare_output(
            output_dir,
            run_config={"project_id": "second"},
            questions=questions,
            haystack=haystack,
            resume=True,
        )


def test_prepare_output_quarantines_torn_final_record(tmp_path: Path) -> None:
    module = _load_module()
    output_dir = tmp_path / "run"
    config = {"project_id": "project_test"}
    questions = [{"id": "q1"}, {"id": "q2"}]
    haystack = {"q1": ["t1"], "q2": ["t2"]}
    module.prepare_output(
        output_dir,
        run_config=config,
        questions=questions,
        haystack=haystack,
        resume=False,
    )
    results = output_dir / "per_question.jsonl"
    results.write_bytes(json.dumps({"question_id": "q1"}).encode() + b'\n{"question_id":')

    completed = module.prepare_output(
        output_dir,
        run_config=config,
        questions=questions,
        haystack=haystack,
        resume=True,
    )

    assert completed == {"q1"}
    assert module.load_jsonl(results) == [{"question_id": "q1"}]
    assert (output_dir / "per_question.jsonl.torn-tail").read_bytes() == b'{"question_id":'


def test_run_queries_flushes_resumable_official_rows(tmp_path: Path) -> None:
    module = _load_module()

    class FakeMemory:
        project_id = "project_test"
        run_id = "run_test"

        def __init__(self) -> None:
            self.query_invocation_ids: list[str] = []

        def set_query_context(self, *, query_invocation_id: str) -> None:
            self.query_invocation_ids.append(query_invocation_id)

        def clear_query_context(self) -> None:
            self.question = None

        def query(self, query: str) -> list[dict[str, str]]:
            return [{"type": "text", "value": f"context for {query}"}]

        def post_query_hook(self, **kwargs: object) -> dict[str, Any]:
            return {
                "retrieval_trace": [{"rank": 1}],
                "query": kwargs["query"],
                "search_metadata": {
                    "embedding_usage": {
                        "provider": "openai",
                        "model": "embedding-test",
                        "requests": 1,
                        "inputs": 1,
                        "prompt_tokens": 3,
                        "total_tokens": 3,
                        "cost_reported_requests": 1,
                        "cost_usd": 0.001,
                    },
                    "planner_usage": {
                        "requests": 0,
                        "cost_usd": 0.0,
                        "cost_complete": True,
                    },
                },
            }

    questions = [
        {
            "id": "q1",
            "domain": "enterprise",
            "question": "first?",
            "question_type": "procedure",
            "eval_function": "qa",
        },
        {
            "id": "q2",
            "domain": "enterprise",
            "question": "second?",
            "question_type": "static-environment",
            "eval_function": "qa",
        },
    ]
    memory = FakeMemory()
    summary = module.run_queries(
        memory,
        questions=questions,
        haystack={"q1": ["t1"], "q2": ["t1"]},
        output_dir=tmp_path,
        completed_question_ids={"q1"},
    )

    rows = module.load_jsonl(tmp_path / "per_question.jsonl")
    assert [row["question_id"] for row in rows] == ["q2"]
    assert rows[0]["memory_context"][0]["value"] == "context for second?"
    assert rows[0]["score_bool"] is None
    assert summary["completed_question_count"] == len(questions)
    assert summary["resumed_question_count"] == 1
    assert summary["query_embedding_usage_this_invocation"]["requests"] == 1
    assert summary["query_cost_usd_this_invocation"] == pytest.approx(0.001)
    assert summary["query_cost_complete"] is True
    assert len(memory.query_invocation_ids) == 1
    assert len(memory.query_invocation_ids[0]) == OPAQUE_INVOCATION_ID_HEX_LENGTH
    assert "second" not in memory.query_invocation_ids[0]


def test_run_queries_persists_failed_row_and_resume_retries_it(tmp_path: Path) -> None:
    module = _load_module()

    class FailingMemory:
        project_id = "project_test"
        run_id = "run_test"

        def __init__(self) -> None:
            self.runner_provenance = {"sibyl_commit": "a" * 40, "git_status": "clean"}
            self.api_runtime = {"runtime": {"commit": "api"}}
            self.official_source = {"commit": "official"}

        def set_query_context(self, *, query_invocation_id: str) -> None:
            self.query_invocation_id = query_invocation_id

        def clear_query_context(self) -> None:
            self.query_invocation_id = None

        def query(self, query: str) -> list[dict[str, str]]:
            raise RuntimeError(f"fault injected for {query}")

    question = {
        "id": "q1",
        "domain": "enterprise",
        "question": "first?",
        "question_type": "procedure",
        "eval_function": "qa",
    }
    with pytest.raises(RuntimeError, match="fault injected"):
        module.run_queries(
            FailingMemory(),
            questions=[question],
            haystack={"q1": ["t1"]},
            output_dir=tmp_path,
            completed_question_ids=set(),
        )

    rows = module.load_jsonl(tmp_path / "per_question.jsonl")
    assert rows == [
        {
            **rows[0],
            "row_status": "failed",
            "context_status": "failed",
            "memory_context": None,
            "memory_post_query_metadata": None,
            "failure": {"stage": "retrieval", "error_type": "RuntimeError"},
            "score_bool": None,
        }
    ]
    run_config = {"schema_version": module.RUN_SCHEMA_VERSION, "api_runtime": {}}
    (tmp_path / "run_config.json").write_text(json.dumps(run_config), encoding="utf-8")
    completed = module.prepare_output(
        tmp_path,
        run_config=run_config,
        questions=[question],
        haystack={"q1": ["t1"]},
        resume=True,
    )
    assert completed == set()


def test_prepare_output_resumes_across_a_server_restart(tmp_path: Path) -> None:
    """A new api_runtime fingerprint is provenance, not measurement drift.

    Refusing it bricked every multi-hour run that outlived its API process.
    The receipt keeps the original runtime and appends each runtime a resume
    continued under, so provenance stays honest without blocking the run.
    """
    module = _load_module()
    output_dir = tmp_path / "run"
    base = {
        "schema_version": module.RUN_SCHEMA_VERSION,
        "project_id": "project_test",
        "api_runtime": {"runtime": {"commit": "aaa"}},
    }
    module.prepare_output(
        output_dir, run_config=dict(base), questions=[], haystack={}, resume=False
    )

    module.prepare_output(
        output_dir,
        run_config={**base, "api_runtime": {"runtime": {"commit": "bbb"}}},
        questions=[],
        haystack={},
        resume=True,
    )
    module.prepare_output(
        output_dir,
        run_config={**base, "api_runtime": {"runtime": {"commit": "ccc"}}},
        questions=[],
        haystack={},
        resume=True,
    )

    config = json.loads((output_dir / "run_config.json").read_text(encoding="utf-8"))
    assert config["api_runtime"] == {"runtime": {"commit": "aaa"}}
    assert [entry["runtime"]["commit"] for entry in config["resume_api_runtimes"]] == [
        "bbb",
        "ccc",
    ]

    with pytest.raises(ValueError, match="does not match"):
        module.prepare_output(
            output_dir,
            run_config={**base, "project_id": "project_other"},
            questions=[],
            haystack={},
            resume=True,
        )


def test_evidence_geometry_flags_thread_into_the_run_config(tmp_path: Path) -> None:
    module = _load_module()
    armed_char_budget = 16000

    questions = tmp_path / "questions.json"
    haystack = tmp_path / "haystack.json"
    ids = tmp_path / "ids.json"
    for f in (questions, haystack, ids):
        f.write_text("[]", encoding="utf-8")
    trajectories = tmp_path / "trajectories.jsonl"
    trajectories.write_text("", encoding="utf-8")

    base = [
        "--api-token-file",
        str(tmp_path / "tok"),
        "--project-id",
        "project_x",
        "--run-id",
        "run_x",
        "--questions",
        str(questions),
        "--haystack",
        str(haystack),
        "--trajectories",
        str(trajectories),
        "--question-ids-file",
        str(ids),
        "--output-dir",
        str(tmp_path / "out"),
    ]

    defaults = module.parse_args(base)
    default_config = module.build_run_config(
        defaults,
        question_ids=[],
        questions_path=questions,
        haystack_path=haystack,
        api_runtime={},
    )
    # The shipped geometry is the default: whole-state substrate, item-bounded.
    assert default_config["evidence_types"] == ["session"]
    assert default_config["evidence_char_budget"] is None

    armed = module.parse_args(
        [
            *base,
            "--evidence-types",
            "passage",
            "session",
            "--evidence-char-budget",
            str(armed_char_budget),
        ]
    )
    armed_config = module.build_run_config(
        armed,
        question_ids=[],
        questions_path=questions,
        haystack_path=haystack,
        api_runtime={},
    )
    assert armed_config["evidence_types"] == ["passage", "session"]
    assert armed_config["evidence_char_budget"] == armed_char_budget


def test_live_retrieval_defaults_to_supported_fast_mode() -> None:
    module = _load_module()
    required = [
        "--api-token-file",
        "credentials.json",
        "--project-id",
        "project_test",
        "--run-id",
        "run_test",
        "--questions",
        "questions.jsonl",
        "--haystack",
        "haystack.json",
        "--trajectories",
        "trajectories.jsonl",
        "--question-ids-file",
        "ids.json",
        "--output-dir",
        "results",
    ]

    assert module.parse_args(required).retrieval_mode == "fast"
    assert module.parse_args([*required, "--retrieval-mode", "naive"]).retrieval_mode == "naive"
    # Old experiment configurations remain parseable for a pinned old server.
    historical = module.parse_args([*required, "--retrieval-mode", "accurate"])
    assert historical.retrieval_mode == "accurate"
