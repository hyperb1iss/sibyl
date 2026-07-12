from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

EXPECTED_EXACT_CONTEXT_RECALL = 0.5


def _load_module() -> ModuleType:
    path = Path(__file__).parents[2] / "benchmarks" / "longmemeval_v2_diagnostics.py"
    spec = importlib.util.spec_from_file_location("longmemeval_v2_diagnostics", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_diagnostics_separate_selection_exposure_and_reader_failures(tmp_path: Path) -> None:
    module = _load_module()
    data_root = tmp_path / "data"
    run_dir = tmp_path / "run"
    runtime_dir = run_dir / "runtime_inputs"
    data_root.mkdir()
    runtime_dir.mkdir(parents=True)
    questions = [
        _question("q-reader"),
        _question("q-selection"),
    ]
    trajectories = [
        _trajectory("t-evidence", tree="The priority filter is selected."),
        _trajectory("t-noise", tree="The status filter is selected."),
    ]
    _write_jsonl(data_root / "questions.jsonl", questions)
    _write_jsonl(data_root / "trajectories.jsonl", trajectories)
    (runtime_dir / "haystack.json").write_text(
        json.dumps(
            {
                "q-reader": ["t-evidence", "t-noise"],
                "q-selection": ["t-evidence", "t-noise"],
            }
        ),
        encoding="utf-8",
    )
    _write_jsonl(
        run_dir / "per_question.jsonl",
        [
            _result(
                "q-reader",
                trajectory_id="t-evidence",
                content="The priority filter is selected.",
            ),
            _result(
                "q-selection",
                trajectory_id="t-noise",
                content="The status filter is selected.",
            ),
        ],
    )

    rows, sources = module.build_trace_rows(
        runs={"web": run_dir},
        questions={row["id"]: row for row in questions},
        trajectories={row["id"]: row for row in trajectories},
        max_rank=10,
    )
    by_id = {row["question_id"]: row for row in rows}
    slice_record = module.build_diagnostic_slice(
        rows,
        source_artifacts=sources,
        size_per_domain=2,
        max_rank=10,
    )
    report = module.build_diagnostic_report(
        rows,
        source_artifacts=sources,
        max_rank=10,
        slice_record=slice_record,
    )

    assert by_id["q-reader"]["failure_class"] == "reader_or_scoring_miss"
    assert by_id["q-reader"]["metrics"]["trajectory_recall_at_k"] is True
    assert by_id["q-reader"]["metrics"]["state_recall_at_k"] is True
    assert by_id["q-reader"]["metrics"]["exact_context_recall_at_k"] is True
    assert by_id["q-selection"]["failure_class"] == "trajectory_selection_miss"
    assert report["metrics"]["exact_context_recall_at_10"] == EXPECTED_EXACT_CONTEXT_RECALL
    assert {case["question_id"] for case in slice_record["cases"]} == {
        "q-reader",
        "q-selection",
    }
    assert "priority filter" not in json.dumps(slice_record).lower()

    mismatched_slice = {
        **slice_record,
        "source_artifacts": {
            "web": {"haystack_sha256": "sha256:mismatch"},
        },
    }
    with pytest.raises(ValueError, match="haystack hash mismatch"):
        module.validate_slice_sources(mismatched_slice, sources)


def _question(question_id: str) -> dict[str, object]:
    return {
        "id": question_id,
        "domain": "web",
        "question_type": "dynamic-environment",
        "question": "Which filter is selected?",
        "answer": "priority filter",
        "eval_function": "norm_phrase_set_match",
    }


def _trajectory(trajectory_id: str, *, tree: str) -> dict[str, object]:
    return {
        "id": trajectory_id,
        "domain": "web",
        "environment": "test",
        "goal": "Inspect the filters",
        "outcome": "success",
        "states": [
            {
                "state_index": 0,
                "url": "https://example.test",
                "action": None,
                "thought": "Inspect the selected filter.",
                "accessibility_tree": tree,
            }
        ],
    }


def _result(
    question_id: str,
    *,
    trajectory_id: str,
    content: str,
) -> dict[str, object]:
    return {
        "question_id": question_id,
        "score_bool": False,
        "memory_query_duration_seconds": 1.25,
        "memory_context": [
            {
                "type": "text",
                "value": (
                    "Retrieved evidence rank 1\n"
                    f"Trajectory: {trajectory_id}\n"
                    "Chunk: 0\n"
                    "Score: 0.9\n\n"
                    "State 0\n"
                    f"Accessibility tree:\n{content}"
                ),
            }
        ],
    }


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
