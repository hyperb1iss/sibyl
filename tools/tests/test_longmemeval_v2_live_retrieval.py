from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


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

        def set_query_context(self, **kwargs: object) -> None:
            self.question = kwargs["question_item"]

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
    summary = module.run_queries(
        FakeMemory(),
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
    from benchmarks.longmemeval_v2_live_retrieval import build_run_config, parse_args

    questions = tmp_path / "questions.json"
    haystack = tmp_path / "haystack.json"
    ids = tmp_path / "ids.json"
    for f in (questions, haystack, ids):
        f.write_text("[]", encoding="utf-8")
    trajectories = tmp_path / "trajectories.jsonl"
    trajectories.write_text("", encoding="utf-8")

    base = [
        "--api-token-file", str(tmp_path / "tok"),
        "--project-id", "project_x",
        "--run-id", "run_x",
        "--questions", str(questions),
        "--haystack", str(haystack),
        "--trajectories", str(trajectories),
        "--question-ids-file", str(ids),
        "--output-dir", str(tmp_path / "out"),
    ]

    defaults = parse_args(base)
    default_config = build_run_config(
        defaults,
        question_ids=[],
        questions_path=questions,
        haystack_path=haystack,
        api_runtime={},
    )
    # The shipped geometry is the default: whole-state substrate, item-bounded.
    assert default_config["evidence_types"] == ["session"]
    assert default_config["evidence_char_budget"] is None

    armed = parse_args(
        [*base, "--evidence-types", "passage", "session", "--evidence-char-budget", "16000"]
    )
    armed_config = build_run_config(
        armed,
        question_ids=[],
        questions_path=questions,
        haystack_path=haystack,
        api_runtime={},
    )
    assert armed_config["evidence_types"] == ["passage", "session"]
    assert armed_config["evidence_char_budget"] == 16000
