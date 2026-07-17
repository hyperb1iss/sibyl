from __future__ import annotations

import argparse
import base64
import builtins
import hashlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from benchmarks import longmemeval_v2_reader_replay as replay
from benchmarks import longmemeval_v2_reader_replay_replication as replication

EXPECTED_READER_CONCURRENCY = 3
EXPECTED_READER_INPUT_COUNT = 2
EXPECTED_REPLICATION_RUN_COUNT = 12
EXPECTED_NEW_REPLICATION_RUN_COUNT = 8


def _prompt_row(
    question_id: str,
    *,
    answer: str = "gold",
    image_bytes: bytes | None = None,
) -> dict[str, Any]:
    row = {
        "index": 0,
        "stream_index": 0,
        "question_id": question_id,
        "question_type": "static-environment",
        "category": "static",
        "eval_function": "norm_phrase_set_match",
        "eval_name": "norm_phrase_set_match",
        "question_text": "question",
        "question_image": None,
        "question_item": {
            "id": question_id,
            "answer": answer,
            "eval_function": "norm_phrase_set_match",
            "question_type": "static-environment",
        },
        "answer_gold": answer,
        "haystack_ids": ["trajectory"],
        "memory_context": [{"type": "text", "value": "evidence"}],
        "memory_query_duration_seconds": 4.0,
        "memory_post_query_duration_seconds": 0.5,
        "memory_post_query_metadata": {},
        "memory_context_original_token_count": 20,
        "memory_context_token_count": 20,
        "memory_context_was_truncated": False,
        "prompt_messages": [{"role": "user", "content": "frozen evidence and question"}],
        "messages": [{"role": "user", "content": "frozen evidence and question"}],
        "is_abstention_problem": False,
    }
    if image_bytes is not None:
        image_path = f"/frozen/question_screenshots/{question_id}.png"
        content = [
            {"type": "text", "text": "frozen evidence and question"},
            {"type": "image_path", "image_path": image_path},
        ]
        reader_content = [
            {"type": "text", "text": "frozen evidence and question"},
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:image/png;base64," + base64.b64encode(image_bytes).decode()
                },
            },
        ]
        row["question_image"] = image_path
        row["prompt_messages"] = [{"role": "user", "content": content}]
        row["messages"] = [{"role": "user", "content": reader_content}]
    return row


def _receipt(question_ids: list[str]) -> dict[str, Any]:
    return {
        "official_repo": {"commit": "official-commit"},
        "dataset": {
            "selected_question_ids_sha256": replay.sha256_question_ids(question_ids),
        },
    }


def _source_score_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **{
            key: row[key]
            for key in (
                "answer_gold",
                "category",
                "eval_function",
                "is_abstention_problem",
                "prompt_messages",
                "question_id",
                "question_image",
                "question_text",
                "question_type",
            )
        },
        "score": 0.0,
    }


def _replay_args(source_dir: Path, output_dir: Path, official_repo: Path) -> argparse.Namespace:
    return argparse.Namespace(
        source_run_dir=str(source_dir),
        question_set_from_run=None,
        output_dir=str(output_dir),
        official_repo=str(official_repo),
        data_root=None,
        reader_api_key_env="READER_KEY",
        reader_api_key_file=None,
        evaluator_api_key_env="JUDGE_KEY",
        evaluator_api_key_file=None,
        reader_max_concurrent_requests=3,
        timeout_seconds=100.0,
        evaluator_timeout_seconds=200.0,
        question_order_seed=1729,
        preflight_only=False,
        provider_usage_run_id="replay-run",
        reader_retry_attempts=4,
        reader_retry_base_delay_seconds=0.0,
        reader_retry_max_delay_seconds=0.0,
        evaluator_retry_attempts=3,
    )


def _write_source(source_dir: Path, rows: list[dict[str, Any]]) -> None:
    source_dir.mkdir()
    (source_dir / "prompt_rows.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    source_scores = [_source_score_row(row) for row in rows]
    (source_dir / "per_question.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in source_scores),
        encoding="utf-8",
    )
    (source_dir / "aggregated_metrics.json").write_text("{}\n", encoding="utf-8")
    (source_dir / "run_args.json").write_text(
        json.dumps(
            {
                "model": "qwen/qwen3.5-9b",
                "base_url": "https://openrouter.ai/api/v1",
                "temperature": 0.6,
                "top_p": 0.95,
                "max_completion_tokens": 20000,
                "api_key_env": "SOURCE_READER_KEY",
                "api_key_file": None,
                "reader_max_concurrent_requests": 16,
                "timeout_seconds": 43200.0,
                "evaluator_model": "gpt-5.2",
                "evaluator_base_url": None,
                "evaluator_api_key_env": "SOURCE_JUDGE_KEY",
                "evaluator_api_key_file": None,
                "evaluator_reasoning_effort": "medium",
                "evaluator_max_completion_tokens": 4096,
                "evaluator_timeout_seconds": 43200.0,
                "started_at_utc": "2026-07-16T00:00:00+00:00",
                "future_harness_argument": "must-survive",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    receipt = _receipt([row["question_id"] for row in rows])
    receipt["accounting"] = {
        "reader": {"provider_reported_cost_usd": 0.1},
        "judge": {"provider_reported_cost_usd": 0.0},
    }
    receipt["artifacts"] = {
        receipt_key: {"sha256": replay.sha256_file(source_dir / filename)}
        for receipt_key, filename in replay.SOURCE_RECEIPT_BINDINGS.items()
    }
    (source_dir / "longmemeval_v2_official_receipt.json").write_text(
        json.dumps(receipt) + "\n",
        encoding="utf-8",
    )


def _write_dataset(
    data_root: Path,
    *,
    question_id: str,
    image_bytes: bytes,
) -> tuple[str, str]:
    image_relative_path = f"question_screenshots/{question_id}.png"
    image_path = data_root / image_relative_path
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(image_bytes)
    questions_path = data_root / "questions.jsonl"
    questions_path.write_text(
        json.dumps({"id": question_id, "image": image_relative_path}) + "\n",
        encoding="utf-8",
    )
    image_sha = hashlib.sha256(image_bytes).hexdigest()
    questions_sha = hashlib.sha256(questions_path.read_bytes()).hexdigest()
    manifest_path = data_root / "checksums.sha256"
    manifest_path.write_text(
        f"{questions_sha}  questions.jsonl\n{image_sha}  {image_relative_path}\n",
        encoding="utf-8",
    )
    return f"sha256:{questions_sha}", replay.sha256_file(manifest_path)


def test_validate_prompt_rows_projects_exact_reader_fields() -> None:
    rows = [_prompt_row("q1"), _prompt_row("q2")]

    reader_rows = replay.validate_prompt_rows(
        rows,
        source_per_question_rows=[_source_score_row(rows[1]), _source_score_row(rows[0])],
        source_receipt=_receipt(["q1", "q2"]),
    )

    assert all(set(row) == {"question_id", "messages"} for row in reader_rows)
    assert all("answer_gold" not in row for row in reader_rows)


@pytest.mark.parametrize(
    ("rows", "error"),
    [
        ([_prompt_row("q1"), _prompt_row("q1")], "duplicate question IDs"),
        ([{"question_id": "q1", "messages": []}], "missing fields"),
    ],
)
def test_validate_prompt_rows_fails_before_reader_use(
    rows: list[dict[str, Any]],
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        replay.validate_prompt_rows(
            rows,
            source_per_question_rows=[{"question_id": "q1"}],
            source_receipt=_receipt(["q1"]),
        )


def test_build_harness_args_preserves_unknown_frozen_fields() -> None:
    source_args = {
        "model": "qwen/qwen3.5-9b",
        "base_url": "https://openrouter.ai/api/v1",
        "temperature": 0.6,
        "api_key_env": "SOURCE_READER_KEY",
        "reader_max_concurrent_requests": 16,
        "future_harness_argument": "must-survive",
        "started_at_utc": "ignored",
    }
    args = _replay_args(Path("source"), Path("output"), Path("official"))

    frozen = replay.build_harness_args(source_args, args)

    assert frozen.model == source_args["model"]
    assert frozen.base_url == source_args["base_url"]
    assert frozen.temperature == source_args["temperature"]
    assert frozen.future_harness_argument == "must-survive"
    assert frozen.api_key_env == "READER_KEY"
    assert frozen.reader_max_concurrent_requests == EXPECTED_READER_CONCURRENCY
    assert not hasattr(frozen, "started_at_utc")


def test_merge_and_score_rows_joins_outputs_by_question_id(tmp_path: Path) -> None:
    rows = [_prompt_row("q1", answer="one"), _prompt_row("q2", answer="two")]
    observed = []

    def score_prediction(row: dict[str, Any], _config: dict[str, Any]) -> tuple[bool, str, bool]:
        observed.append((row["question_id"], row["answer_gold"], row["response_raw"]))
        return row["answer_gold"] in row["response_raw"], row["eval_name"], False

    outputs = {
        "q2": {
            "response_raw": "answer two",
            "response_parsed_boxed": "two",
            "is_unknown": False,
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        },
        "q1": {
            "response_raw": "answer one",
            "response_parsed_boxed": "one",
            "is_unknown": False,
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        },
    }

    records = replay.merge_and_score_rows(
        prompt_rows=rows,
        outputs_by_question_id=outputs,
        official_harness=SimpleNamespace(score_prediction=score_prediction),
        eval_config={},
        output_path=tmp_path / "per_question.jsonl",
    )

    assert [row["question_id"] for row in records] == ["q1", "q2"]
    assert observed == [("q1", "one", "answer one"), ("q2", "two", "answer two")]
    assert all(row["score_bool"] for row in records)


def test_replay_writes_distinct_hash_bound_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [_prompt_row("q1", answer="one"), _prompt_row("q2", answer="two")]
    source_dir = tmp_path / "source"
    output_dir = tmp_path / "output"
    official_repo = tmp_path / "official"
    official_repo.mkdir()
    _write_source(source_dir, rows)
    received_reader_rows: list[dict[str, Any]] = []

    async def generate_all_reader_outputs(
        _args: argparse.Namespace,
        reader_rows: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        received_reader_rows.extend(reader_rows)
        assert all(set(row) == {"question_id", "messages"} for row in reader_rows)
        return {
            row["question_id"]: {
                "response_raw": f"answer {row['question_id'][-1]}",
                "response_parsed_boxed": row["question_id"][-1],
                "is_unknown": False,
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            }
            for row in reversed(reader_rows)
        }

    def score_prediction(row: dict[str, Any], _config: dict[str, Any]) -> tuple[bool, str, bool]:
        return True, row["eval_name"], False

    def aggregate_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "overall": {
                "overall_full_set": sum(float(row["score"]) for row in records) / len(records),
                "count_all_questions": len(records),
            }
        }

    harness = SimpleNamespace(
        LLM_EVAL_FUNCTIONS=set(),
        generate_all_reader_outputs=generate_all_reader_outputs,
        make_eval_config=lambda _args: {},
        score_prediction=score_prediction,
        aggregate_metrics=aggregate_metrics,
    )
    monkeypatch.setattr(replay, "verify_official_repo", lambda *_args: "official-commit")
    monkeypatch.setattr(replay, "install_provider_usage_tracking", lambda *_args, **_kw: None)
    monkeypatch.setattr(replay, "install_reader_retry", lambda *_args, **_kw: None)
    monkeypatch.setattr(replay, "install_evaluator_retry", lambda *_args, **_kw: None)
    monkeypatch.setattr(
        replay,
        "summarize_usage",
        lambda path, **_kw: {
            "requests": 2 if path.name == "reader.jsonl" else 0,
            "priced_requests": 2 if path.name == "reader.jsonl" else 0,
            "provider_reported_cost_usd": 0.1 if path.name == "reader.jsonl" else 0.0,
            "cost_coverage_complete": path.name == "reader.jsonl",
            "tracking_complete": path.name == "reader.jsonl",
            "invalid_or_foreign_lines": 0,
            "requested_models": [],
            "provider_models": [],
        },
    )
    monkeypatch.setattr(replay, "git_provenance", lambda _path: {"commit": "sibyl"})

    receipt = replay.replay(
        _replay_args(source_dir, output_dir, official_repo),
        official_harness=harness,
        official_metrics=SimpleNamespace(),
    )

    assert receipt["status"] == "PASS"
    assert receipt["claim_boundary"]["retrieval_executed"] is False
    assert receipt["claim_boundary"]["reader_received_exact_fields"] == [
        "messages",
        "question_id",
    ]
    assert len(receipt["reader_inputs"]["records"]) == EXPECTED_READER_INPUT_COUNT
    assert received_reader_rows
    assert (output_dir / "longmemeval_v2_reader_replay_receipt.json").is_file()
    assert not (output_dir / "longmemeval_v2_official_receipt.json").exists()
    assert json.loads((output_dir / "aggregated_metrics.json").read_text())["retrieval"] == {
        "executed": False,
        "frozen_prompt_source": str(source_dir / "prompt_rows.jsonl"),
    }


def test_preflight_validates_without_creating_output_or_calling_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_dir = tmp_path / "source"
    output_dir = tmp_path / "output"
    official_repo = tmp_path / "official"
    official_repo.mkdir()
    _write_source(source_dir, [_prompt_row("q1")])
    args = _replay_args(source_dir, output_dir, official_repo)
    args.preflight_only = True
    monkeypatch.setattr(replay, "verify_official_repo", lambda *_args: "official-commit")

    receipt = replay.replay(
        args,
        official_harness=SimpleNamespace(),
        official_metrics=SimpleNamespace(),
    )

    assert receipt["status"] == "PASS"
    assert receipt["claim_boundary"]["provider_calls_executed"] is False
    assert not output_dir.exists()


def test_prompt_images_match_pinned_dataset_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_bytes = b"official-image-bytes"
    row = _prompt_row("q1", image_bytes=image_bytes)
    data_root = tmp_path / "dataset"
    questions_sha, manifest_sha = _write_dataset(
        data_root,
        question_id="q1",
        image_bytes=image_bytes,
    )
    monkeypatch.setitem(
        replay.DATASET_MANIFEST_SHA256_BY_QUESTIONS_SHA256,
        questions_sha,
        manifest_sha,
    )
    receipt = _receipt(["q1"])
    receipt["dataset"]["questions_sha256"] = questions_sha

    integrity = replay.validate_prompt_image_bytes(
        [row],
        data_root=data_root,
        source_receipt=receipt,
    )

    assert integrity["embedded_image_count"] == 1
    assert integrity["verified_image_count"] == 1
    assert integrity["records"][0]["sha256"] == (
        f"sha256:{hashlib.sha256(image_bytes).hexdigest()}"
    )


def test_prompt_image_tampering_fails_against_pinned_dataset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    official_bytes = b"official-image-bytes"
    row = _prompt_row("q1", image_bytes=b"tampered-image-bytes")
    data_root = tmp_path / "dataset"
    questions_sha, manifest_sha = _write_dataset(
        data_root,
        question_id="q1",
        image_bytes=official_bytes,
    )
    monkeypatch.setitem(
        replay.DATASET_MANIFEST_SHA256_BY_QUESTIONS_SHA256,
        questions_sha,
        manifest_sha,
    )
    receipt = _receipt(["q1"])
    receipt["dataset"]["questions_sha256"] = questions_sha

    with pytest.raises(ValueError, match="Embedded image bytes do not match"):
        replay.validate_prompt_image_bytes(
            [row],
            data_root=data_root,
            source_receipt=receipt,
        )


def test_embedded_image_without_bound_metadata_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_bytes = b"official-image-bytes"
    row = _prompt_row("q1", image_bytes=image_bytes)
    row["question_image"] = None
    data_root = tmp_path / "dataset"
    questions_sha, manifest_sha = _write_dataset(
        data_root,
        question_id="q1",
        image_bytes=image_bytes,
    )
    monkeypatch.setitem(
        replay.DATASET_MANIFEST_SHA256_BY_QUESTIONS_SHA256,
        questions_sha,
        manifest_sha,
    )
    receipt = _receipt(["q1"])
    receipt["dataset"]["questions_sha256"] = questions_sha

    with pytest.raises(ValueError, match="Prompt image path does not match"):
        replay.validate_prompt_image_bytes(
            [row],
            data_root=data_root,
            source_receipt=receipt,
        )


def test_missing_expected_judge_usage_fails_closed() -> None:
    reader_complete, judge_complete = replay.usage_tracking_complete(
        {
            "tracking_complete": True,
            "requests": 1,
        },
        {
            "tracking_complete": False,
            "requests": 0,
            "invalid_or_foreign_lines": 0,
        },
        reader_request_count=1,
        expected_judge_requests=1,
    )

    assert reader_complete is True
    assert judge_complete is False


def test_source_artifact_sha_mismatch_fails_closed(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    _write_source(source_dir, [_prompt_row("q1")])
    with (source_dir / "run_args.json").open("a", encoding="utf-8") as handle:
        handle.write("\n")

    with pytest.raises(ValueError, match=r"does not match its receipt: run_args\.json"):
        replay.load_source_run(source_dir)


def test_official_commit_mismatch_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(replay, "ensure_official_harness", lambda _path: None)
    monkeypatch.setattr(replay, "git_commit", lambda _path: "different-commit")

    with pytest.raises(ValueError, match="Official harness commit mismatch"):
        replay.verify_official_repo(tmp_path, _receipt(["q1"]))


def test_main_verifies_official_commit_before_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_dir = tmp_path / "source"
    output_dir = tmp_path / "output"
    official_repo = tmp_path / "official"
    official_repo.mkdir()
    _write_source(source_dir, [_prompt_row("q1")])
    events = []

    monkeypatch.setattr(
        replay,
        "verify_official_repo",
        lambda *_args: events.append("verify") or "official-commit",
    )
    monkeypatch.setattr(
        replay,
        "replay",
        lambda *_args, **_kwargs: events.append("replay") or {"status": "PASS"},
    )
    evaluation = ModuleType("evaluation")
    evaluation.__path__ = []
    harness_module = ModuleType("evaluation.harness")
    metrics_module = ModuleType("evaluation.qa_eval_metrics")
    evaluation.__dict__["harness"] = harness_module
    evaluation.__dict__["qa_eval_metrics"] = metrics_module
    monkeypatch.setitem(sys.modules, "evaluation", evaluation)
    monkeypatch.setitem(sys.modules, "evaluation.harness", harness_module)
    monkeypatch.setitem(
        sys.modules,
        "evaluation.qa_eval_metrics",
        metrics_module,
    )
    original_import = builtins.__import__

    def tracked_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("evaluation."):
            events.append("import")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", tracked_import)
    monkeypatch.setattr(sys, "path", sys.path.copy())

    result = replay.main(
        [
            "--source-run-dir",
            str(source_dir),
            "--output-dir",
            str(output_dir),
            "--official-repo",
            str(official_repo),
        ]
    )

    assert result == 0
    assert events == ["verify", "import", "import", "replay"]


def test_empty_reader_response_writes_invalid_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_dir = tmp_path / "source"
    output_dir = tmp_path / "output"
    official_repo = tmp_path / "official"
    official_repo.mkdir()
    _write_source(source_dir, [_prompt_row("q1")])

    async def generate_all_reader_outputs(
        _args: argparse.Namespace,
        _reader_rows: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        return {
            "q1": {
                "response_raw": "",
                "response_parsed_boxed": "",
                "is_unknown": False,
                "usage": {"prompt_tokens": 2, "completion_tokens": 0},
            }
        }

    harness = SimpleNamespace(
        LLM_EVAL_FUNCTIONS=set(),
        generate_all_reader_outputs=generate_all_reader_outputs,
        make_eval_config=lambda _args: {},
        score_prediction=lambda row, _config: (False, row["eval_name"], False),
        aggregate_metrics=lambda records: {"overall": {"count_all_questions": len(records)}},
    )
    monkeypatch.setattr(replay, "verify_official_repo", lambda *_args: "official-commit")
    monkeypatch.setattr(replay, "install_provider_usage_tracking", lambda *_args, **_kw: None)
    monkeypatch.setattr(replay, "install_reader_retry", lambda *_args, **_kw: None)
    monkeypatch.setattr(replay, "install_evaluator_retry", lambda *_args, **_kw: None)
    monkeypatch.setattr(replay, "git_provenance", lambda _path: {"commit": "sibyl"})
    monkeypatch.setattr(
        replay,
        "summarize_usage",
        lambda path, **_kw: {
            "requests": 1 if path.name == "reader.jsonl" else 0,
            "priced_requests": 0,
            "provider_reported_cost_usd": 0.0,
            "cost_coverage_complete": False,
            "tracking_complete": path.name == "reader.jsonl",
            "invalid_or_foreign_lines": 0,
            "requested_models": [],
            "provider_models": [],
        },
    )

    with pytest.raises(RuntimeError, match="Reader replay is invalid"):
        replay.replay(
            _replay_args(source_dir, output_dir, official_repo),
            official_harness=harness,
            official_metrics=SimpleNamespace(),
        )

    receipt = json.loads((output_dir / "longmemeval_v2_reader_replay_receipt.json").read_text())
    assert receipt["status"] == "INVALID"
    assert receipt["outputs"]["empty_response_question_ids"] == ["q1"]


def test_source_prompt_tampering_fails_against_scored_artifact(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    _write_source(source_dir, [_prompt_row("q1")])
    prompt_path = source_dir / "prompt_rows.jsonl"
    prompt_row = json.loads(prompt_path.read_text())
    prompt_row["messages"][0]["content"] = "tampered"
    prompt_path.write_text(json.dumps(prompt_row) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Prompt messages changed shape"):
        replay.validate_prompt_rows(
            replay.load_jsonl(prompt_path),
            source_per_question_rows=replay.load_jsonl(source_dir / "per_question.jsonl"),
            source_receipt=replay.load_json(source_dir / "longmemeval_v2_official_receipt.json"),
        )


def test_question_set_selection_uses_reference_order() -> None:
    source_rows = [_prompt_row("q1"), _prompt_row("q2"), _prompt_row("q3")]
    source = {
        "prompt_rows": source_rows,
        "score_rows": [_source_score_row(row) for row in source_rows],
    }
    selection = {"prompt_rows": [_prompt_row("q3"), _prompt_row("q1")]}

    prompts, reader_rows = replay.select_question_set(source, selection)

    assert [row["question_id"] for row in prompts] == ["q3", "q1"]
    assert [row["question_id"] for row in reader_rows] == ["q3", "q1"]
    assert all(set(row) == {"question_id", "messages"} for row in reader_rows)


def _replication_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[argparse.Namespace, dict[str, dict[str, Path]]]:
    paths: dict[str, dict[str, Path]] = {
        configuration: {} for configuration in replication.CONFIGURATIONS
    }
    rows = [_prompt_row("q1"), _prompt_row("q2")]
    for configuration in replication.CONFIGURATIONS:
        for domain in replication.DOMAINS:
            path = tmp_path / "sources" / configuration / domain
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_source(path, rows)
            _set_source_scores(path, score=configuration == "candidate")
            paths[configuration][domain] = path
    key_file = tmp_path / "openrouter.key"
    key_file.write_text("test-key\n", encoding="utf-8")
    official_repo = tmp_path / "official"
    official_repo.mkdir()
    monkeypatch.setattr(
        replication,
        "verify_official_repo",
        lambda *_args: "official-commit",
    )
    args = argparse.Namespace(
        command="plan",
        frozen_baseline_web=str(paths["frozen_baseline"]["web"]),
        frozen_baseline_enterprise=str(paths["frozen_baseline"]["enterprise"]),
        candidate_web=str(paths["candidate"]["web"]),
        candidate_enterprise=str(paths["candidate"]["enterprise"]),
        official_repo=str(official_repo),
        data_root=str(tmp_path / "dataset"),
        reader_api_key_file=str(key_file),
        evaluator_api_key_env="TEST_OPENAI_API_KEY",
        output_root=str(tmp_path / "replication"),
        plan_path=str(tmp_path / "replication" / "plan.json"),
    )
    return args, paths


def _set_source_scores(source_dir: Path, *, score: bool) -> None:
    score_path = source_dir / "per_question.jsonl"
    rows = replay.load_jsonl(score_path)
    for row in rows:
        row["score_bool"] = score
        row["score"] = float(score)
    score_path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    receipt_path = source_dir / "longmemeval_v2_official_receipt.json"
    receipt = replay.load_json(receipt_path)
    receipt["artifacts"]["per_question"]["sha256"] = replay.sha256_file(score_path)
    receipt_path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")


def _write_replication_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], Path]:
    args, _ = _replication_fixture(tmp_path, monkeypatch)
    plan = replication.build_plan(args)
    plan_path = Path(args.plan_path)
    plan_path.parent.mkdir(parents=True)
    replication.write_json(plan_path, plan)
    return plan, plan_path


def _write_completed_replay(
    plan: dict[str, Any],
    run: dict[str, Any],
    *,
    score: bool,
    cost: float = 0.01,
) -> None:
    output_dir = Path(run["output_dir"])
    output_dir.mkdir(parents=True)
    question_ids = replication.selected_question_ids(plan, run["domain"])
    per_question_path = output_dir / "per_question.jsonl"
    per_question_path.write_text(
        "".join(
            json.dumps(
                {
                    "question_id": question_id,
                    "question_type": "static-environment",
                    "score_bool": score,
                    "score": float(score),
                }
            )
            + "\n"
            for question_id in question_ids
        ),
        encoding="utf-8",
    )
    metrics_path = output_dir / "aggregated_metrics.json"
    metrics_path.write_text("{}\n", encoding="utf-8")
    source = plan["sources"][run["configuration"]][run["domain"]]
    receipt = {
        "schema_version": replay.SCHEMA_VERSION,
        "status": "PASS",
        "source": {
            "artifacts": source["artifacts"],
            "official_commit": source["official_commit"],
        },
        "reader_inputs": {
            "question_count": len(question_ids),
            "question_ids_sha256": replay.sha256_question_ids(question_ids),
            "image_integrity": {
                "embedded_image_count": 0,
                "verified_image_count": 0,
            },
        },
        "outputs": {
            "per_question": {
                "path": str(per_question_path),
                "sha256": replay.sha256_file(per_question_path),
            },
            "aggregated_metrics": {
                "path": str(metrics_path),
                "sha256": replay.sha256_file(metrics_path),
            },
        },
        "accounting": {
            "provider_reported_cost_usd": cost,
            "cost_coverage_complete": True,
        },
    }
    replication.write_json(
        output_dir / "longmemeval_v2_reader_replay_receipt.json",
        receipt,
    )
    replication.write_json(
        output_dir / "run_args.json",
        {"question_order_seed": run["question_order_seed"]},
    )


def test_replication_plan_freezes_paired_matrix_and_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, plan_path = _write_replication_plan(tmp_path, monkeypatch)

    runs = replication.require_plan(plan, plan_path=plan_path)

    assert len(runs) == EXPECTED_REPLICATION_RUN_COUNT
    assert sum(not run["existing"] for run in runs) == EXPECTED_NEW_REPLICATION_RUN_COUNT
    assert plan["cost_budget"]["estimated_incremental_cost_usd"] == pytest.approx(0.8)
    assert all(
        run["command"][:4] == list(replication.COMMAND_PREFIX)
        for run in runs
        if not run["existing"]
    )


def test_replication_plan_rejects_command_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, plan_path = _write_replication_plan(tmp_path, monkeypatch)
    plan["runs"][4]["command"].append("--temperature")
    plan["runs"][4]["command"].append("0")
    replication.write_json(plan_path, plan)

    with pytest.raises(ValueError, match="command changed"):
        replication.require_plan(plan, plan_path=plan_path)


def test_replication_runner_resumes_valid_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, plan_path = _write_replication_plan(tmp_path, monkeypatch)
    for run in plan["runs"]:
        if not run["existing"]:
            _write_completed_replay(plan, run, score=run["configuration"] == "candidate")
    monkeypatch.setenv("TEST_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        replication,
        "execute_wave",
        lambda *_args, **_kwargs: pytest.fail("valid receipts should be resumed"),
    )

    receipt = replication.run_plan(
        plan,
        plan_path=plan_path,
        max_workers=replication.DEFAULT_MAX_WORKERS,
    )

    assert receipt["status"] == "PASS"
    assert len(receipt["skipped"]) == EXPECTED_NEW_REPLICATION_RUN_COUNT
    assert receipt["incremental_provider_reported_cost_usd"] == pytest.approx(0.08)


def test_replication_report_uses_paired_question_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, plan_path = _write_replication_plan(tmp_path, monkeypatch)
    for run in plan["runs"]:
        if not run["existing"]:
            _write_completed_replay(plan, run, score=run["configuration"] == "candidate")

    report = replication.build_report(plan, plan_path=plan_path)

    assert report["status"] == "PASS"
    assert report["comparison"]["cluster_bootstrap"]["mean_accuracy_delta"] == 1.0
    assert report["comparison"]["decision"]["outcome"] == "GO"
    assert report["comparison"]["domain_mean_accuracy_deltas"] == {
        "web": 1.0,
        "enterprise": 1.0,
    }
    assert report["costs"]["incremental_provider_reported_cost_usd"] == pytest.approx(0.08)


def test_replication_scores_reject_duplicate_questions(tmp_path: Path) -> None:
    path = tmp_path / "scores.jsonl"
    path.write_text(
        '{"question_id":"q1","score_bool":true}\n{"question_id":"q1","score_bool":false}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Duplicate scored replay question"):
        replication.load_scores(path)


def test_replication_existing_scores_require_boolean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, plan_path = _write_replication_plan(tmp_path, monkeypatch)
    run = next(
        run
        for run in replication.require_plan(plan, plan_path=plan_path)
        if run["existing"] and run["configuration"] == "candidate"
    )
    score_path = Path(run["output_dir"]) / "per_question.jsonl"
    rows = replay.load_jsonl(score_path)
    rows[0]["score_bool"] = "false"
    score_path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="Invalid scored replay row"):
        replication.existing_summary(plan, run)


def test_replication_receipt_rejects_output_path_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, _ = _write_replication_plan(tmp_path, monkeypatch)
    run = next(run for run in plan["runs"] if not run["existing"])
    _write_completed_replay(plan, run, score=True)
    output_dir = Path(run["output_dir"])
    receipt_path = output_dir / "longmemeval_v2_reader_replay_receipt.json"
    receipt = replay.load_json(receipt_path)
    escaped_path = tmp_path / "escaped.jsonl"
    escaped_path.write_text(
        (output_dir / "per_question.jsonl").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    receipt["outputs"]["per_question"] = {
        "path": str(escaped_path),
        "sha256": replay.sha256_file(escaped_path),
    }
    replication.write_json(receipt_path, receipt)

    assert replication.completed_replay_summary(plan, run) is None


def test_replication_plan_rejects_source_metadata_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, plan_path = _write_replication_plan(tmp_path, monkeypatch)
    plan["sources"]["candidate"]["web"]["official_commit"] = "other-commit"
    replication.write_json(plan_path, plan)

    with pytest.raises(ValueError, match="source metadata changed"):
        replication.require_plan(plan, plan_path=plan_path)


def test_replication_runner_refuses_more_spend_after_cost_overrun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, plan_path = _write_replication_plan(tmp_path, monkeypatch)
    pass_two = [run for run in plan["runs"] if run["pass_id"] == replication.PASS_IDS[1]]
    for run in pass_two:
        _write_completed_replay(plan, run, score=True, cost=0.4)
    monkeypatch.setenv("TEST_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        replication,
        "execute_wave",
        lambda *_args, **_kwargs: pytest.fail("cost guard must stop before another wave"),
    )

    receipt = replication.run_plan(
        plan,
        plan_path=plan_path,
        max_workers=replication.DEFAULT_MAX_WORKERS,
    )

    assert receipt["status"] == "FAIL"
    assert receipt["failures"] == [
        {
            "cost_budget_preflight_failed": True,
            "projected_incremental_cost_usd": pytest.approx(1.6),
        }
    ]


def test_replication_runner_archives_partial_attempt_and_counts_its_spend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, plan_path = _write_replication_plan(tmp_path, monkeypatch)
    partial_run = next(run for run in plan["runs"] if not run["existing"])
    partial_dir = Path(partial_run["output_dir"])
    partial_dir.mkdir(parents=True)
    replication.write_json(
        partial_dir / "run_args.json",
        {"provider_usage_run_id": "partial-run"},
    )
    usage_dir = partial_dir / "provider_usage"
    usage_dir.mkdir()
    usage_dir.joinpath("reader.jsonl").write_text(
        json.dumps(
            {
                "run_id": "partial-run",
                "requested_model": "reader",
                "usage": {"cost_usd": 0.2},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_OPENAI_API_KEY", "test-key")

    def complete_wave(
        _plan: dict[str, Any],
        runs: list[dict[str, Any]],
        *,
        max_workers: int,
    ) -> list[tuple[dict[str, Any], int]]:
        assert max_workers == replication.DEFAULT_MAX_WORKERS
        for run in runs:
            _write_completed_replay(_plan, run, score=True)
        return [(run, 0) for run in runs]

    monkeypatch.setattr(replication, "execute_wave", complete_wave)

    receipt = replication.run_plan(
        plan,
        plan_path=plan_path,
        max_workers=replication.DEFAULT_MAX_WORKERS,
    )

    assert receipt["status"] == "PASS"
    assert receipt["incremental_provider_reported_cost_usd"] == pytest.approx(0.28)
    assert receipt["archived_attempts"] == [
        {
            "run": replication.run_key(partial_run),
            "path": str(partial_dir.with_name(f"{partial_dir.name}.attempt-01")),
            "receipt_sha256": None,
            "requests": 1,
            "priced_requests": 1,
            "cost_usd": pytest.approx(0.2),
            "cost_coverage_complete": True,
        }
    ]
    assert Path(receipt["archived_attempts"][0]["path"]).is_dir()
    assert replication.completed_replay_summary(plan, partial_run) is not None


def test_replication_decision_uses_only_fresh_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, plan_path = _write_replication_plan(tmp_path, monkeypatch)
    for run in plan["runs"]:
        if not run["existing"]:
            _write_completed_replay(plan, run, score=False)

    report = replication.build_report(plan, plan_path=plan_path)

    comparison = report["comparison"]
    assert comparison["per_pass_accuracy_deltas"]["pass_01"] == 1.0
    assert comparison["all_passes_mean_accuracy_delta"] == pytest.approx(1 / 3)
    assert comparison["cluster_bootstrap"]["mean_accuracy_delta"] == 0.0
    assert comparison["decision_passes"] == ["pass_02", "pass_03"]
    assert comparison["decision"]["outcome"] == "RESEARCH-MORE"


def test_replication_source_cost_requires_provider_accounting() -> None:
    with pytest.raises(TypeError, match="no provider accounting"):
        replication.official_source_cost({})


def test_replication_plan_rejects_estimate_over_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args, _ = _replication_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(replication, "MAX_INCREMENTAL_COST_USD", 0.5)

    with pytest.raises(ValueError, match="Estimated replay cost exceeds"):
        replication.build_plan(args)


def test_replication_runner_detects_post_wave_cost_overrun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, plan_path = _write_replication_plan(tmp_path, monkeypatch)
    monkeypatch.setenv("TEST_OPENAI_API_KEY", "test-key")

    def expensive_wave(
        _plan: dict[str, Any],
        runs: list[dict[str, Any]],
        *,
        max_workers: int,
    ) -> list[tuple[dict[str, Any], int]]:
        assert max_workers == replication.DEFAULT_MAX_WORKERS
        for run in runs:
            _write_completed_replay(_plan, run, score=True, cost=0.4)
        return [(run, 0) for run in runs]

    monkeypatch.setattr(replication, "execute_wave", expensive_wave)

    receipt = replication.run_plan(
        plan,
        plan_path=plan_path,
        max_workers=replication.DEFAULT_MAX_WORKERS,
    )

    assert receipt["status"] == "FAIL"
    assert receipt["incremental_provider_reported_cost_usd"] == pytest.approx(1.6)
    assert receipt["failures"] == [
        {"cost_budget_exceeded": True, "incremental_cost_usd": pytest.approx(1.6)}
    ]


def test_replication_report_fails_when_completed_cost_exceeds_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, plan_path = _write_replication_plan(tmp_path, monkeypatch)
    for run in plan["runs"]:
        if not run["existing"]:
            _write_completed_replay(plan, run, score=True, cost=0.2)

    report = replication.build_report(plan, plan_path=plan_path)

    assert report["status"] == "FAIL"
    assert report["costs"]["within_budget"] is False
    assert report["costs"]["incremental_provider_reported_cost_usd"] == pytest.approx(1.6)


@pytest.mark.parametrize(
    ("mean_delta", "lower", "upper", "domain_delta", "outcome"),
    [
        (0.03, 0.001, 0.06, -0.02, "GO"),
        (-0.03, -0.06, 0.0, 0.0, "NO-GO"),
        (-0.01, -0.02, -0.001, 0.0, "NO-GO"),
        (0.02, -0.01, 0.05, 0.0, "RESEARCH-MORE"),
    ],
)
def test_replication_decision_boundaries(
    mean_delta: float,
    lower: float,
    upper: float,
    domain_delta: float,
    outcome: str,
) -> None:
    decision = replication.replication_decision(
        mean_delta=mean_delta,
        confidence_interval={"lower": lower, "upper": upper},
        domain_deltas=dict.fromkeys(replication.DOMAINS, domain_delta),
    )

    assert decision["outcome"] == outcome
