from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

EXPECTED_EXACT_CONTEXT_RECALL = 0.5
EXPECTED_LEXICAL_SOURCE_REACHABILITY = 0.5
EXPECTED_ANSWER_PHRASE_COUNT = 3


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
    assert by_id["q-reader"]["metrics"]["lexical_source_reachability_at_k"] is True
    assert by_id["q-reader"]["metrics"]["exact_context_recall_at_k"] is True
    assert by_id["q-selection"]["failure_class"] == "trajectory_selection_miss"
    assert report["metrics"]["exact_context_recall_at_10"] == EXPECTED_EXACT_CONTEXT_RECALL
    assert (
        report["metrics"]["lexical_source_reachability_at_10"]
        == EXPECTED_LEXICAL_SOURCE_REACHABILITY
    )
    assert report["evidence_reference"] == {
        "source": "exact_answer_phrase_occurrence_proxy",
        "official_state_labels_available": False,
        "semantic_evidence_recall_supported": False,
        "legacy_multi_state_metric": (
            "normalized answer-phrase occurrence coverage; not semantic evidence coverage"
        ),
    }
    assert {case["question_id"] for case in slice_record["cases"]} == {
        "q-reader",
        "q-selection",
    }
    assert "priority filter" not in json.dumps(slice_record).lower()

    mismatched_slice = {
        **slice_record,
        "source_artifacts": {
            "web": {"slice_haystack_sha256": "sha256:mismatch"},
        },
    }
    with pytest.raises(ValueError, match="haystack hash mismatch"):
        module.validate_slice_sources(mismatched_slice, sources, rows)


def test_frozen_slice_accepts_matching_candidate_haystack_subset(tmp_path: Path) -> None:
    module = _load_module()
    questions = [_question(question_id) for question_id in ("q1", "q2", "q3")]
    trajectories = [_trajectory("t-evidence", tree="The priority filter is selected.")]
    full_run = tmp_path / "full"
    full_runtime = full_run / "runtime_inputs"
    full_runtime.mkdir(parents=True)
    full_haystack = {question["id"]: ["t-evidence"] for question in questions}
    (full_runtime / "haystack.json").write_text(
        json.dumps(full_haystack),
        encoding="utf-8",
    )
    _write_jsonl(
        full_run / "per_question.jsonl",
        [
            _result(
                str(question["id"]),
                trajectory_id="t-evidence",
                content="The priority filter is selected.",
            )
            for question in questions
        ],
    )
    question_index = {str(row["id"]): row for row in questions}
    trajectory_index = {str(row["id"]): row for row in trajectories}
    rows, sources = module.build_trace_rows(
        runs={"web": full_run},
        questions=question_index,
        trajectories=trajectory_index,
        max_rank=10,
    )
    slice_record = module.build_diagnostic_slice(
        rows,
        source_artifacts=sources,
        size_per_domain=2,
        max_rank=10,
    )
    selected_ids = {case["question_id"] for case in slice_record["cases"]}

    candidate_run = tmp_path / "candidate"
    candidate_runtime = candidate_run / "runtime_inputs"
    candidate_runtime.mkdir(parents=True)
    (candidate_runtime / "haystack.json").write_text(
        json.dumps({question_id: full_haystack[question_id] for question_id in selected_ids}),
        encoding="utf-8",
    )
    _write_jsonl(
        candidate_run / "per_question.jsonl",
        [
            _result(
                question_id,
                trajectory_id="t-evidence",
                content="The priority filter is selected.",
            )
            for question_id in selected_ids
        ],
    )
    candidate_rows, candidate_sources = module.build_trace_rows(
        runs={"web": candidate_run},
        questions=question_index,
        trajectories=trajectory_index,
        max_rank=10,
    )

    module.validate_slice_sources(slice_record, candidate_sources, candidate_rows)
    module.validate_slice_sources(slice_record, sources, rows)

    missing_rows = [row for row in candidate_rows if row["question_id"] != next(iter(selected_ids))]
    with pytest.raises(ValueError, match="questions missing"):
        module.validate_slice_sources(slice_record, candidate_sources, missing_rows)


def test_diagnostics_counts_typed_source_support_without_direct_state_hit() -> None:
    module = _load_module()
    question = _question("q-support")
    trajectories = {
        "t-evidence": _trajectory("t-evidence", tree="The priority filter is selected."),
        "t-typed": _trajectory("t-typed", tree="No matching answer here."),
    }
    result = {
        "question_id": "q-support",
        "score_bool": False,
        "memory_context": [{"type": "text", "value": "Typed operational evidence"}],
        "memory_post_query_metadata": {
            "retrieval_trace": [
                {
                    "rank": 1,
                    "entity_id": "procedure-1",
                    "trajectory_id": "t-typed",
                    "state_indices": [],
                    "source_support_states": [
                        {
                            "entity_id": "session-source",
                            "operational_source_id": "longmemeval-v2:run:t-evidence",
                            "trajectory_id": "t-evidence",
                            "state_index": 0,
                        },
                        {
                            "trajectory_id": "t-typed",
                            "state_index": True,
                        },
                    ],
                    "content_chars": 28,
                }
            ]
        },
    }

    row = module.build_question_trace(
        domain="web",
        result=result,
        question=question,
        haystack_ids=["t-evidence"],
        state_text_index=module.build_state_text_index(trajectories),
        max_rank=10,
    )

    assert row["metrics"]["state_recall_at_k"] is False
    assert row["metrics"]["lexical_source_reachability_at_k"] is True
    assert row["retrieved"][0]["source_support_states"] == [
        {
            "entity_id": "session-source",
            "operational_source_id": "longmemeval-v2:run:t-evidence",
            "trajectory_id": "t-evidence",
            "state_index": 0,
        }
    ]


def test_diagnostics_does_not_assign_bare_support_ordinal_to_typed_trajectory() -> None:
    module = _load_module()
    question = _question("q-foreign-support")
    trajectories = {
        "t-evidence": _trajectory("t-evidence", tree="The priority filter is selected."),
        "t-other": _trajectory("t-other", tree="No matching answer here."),
    }
    result = {
        "question_id": "q-foreign-support",
        "score_bool": False,
        "memory_context": [{"type": "text", "value": "Typed operational evidence"}],
        "memory_post_query_metadata": {
            "retrieval_trace": [
                {
                    "rank": 1,
                    "entity_id": "procedure-foreign",
                    "trajectory_id": "t-evidence",
                    "state_indices": [],
                    "source_support_state_indices": [0],
                    "source_support_states": [
                        {
                            "operational_source_id": "longmemeval-v2:run:t-other",
                            "trajectory_id": "t-other",
                            "state_index": 0,
                        }
                    ],
                    "content_chars": 28,
                }
            ]
        },
    }

    row = module.build_question_trace(
        domain="web",
        result=result,
        question=question,
        haystack_ids=["t-evidence", "t-other"],
        state_text_index=module.build_state_text_index(trajectories),
        max_rank=10,
    )

    assert row["metrics"]["state_recall_at_k"] is False
    assert row["metrics"]["lexical_source_reachability_at_k"] is False


def test_diagnostics_requires_all_phrase_set_answers_in_rendered_context() -> None:
    module = _load_module()
    question = {
        **_question("q-phrase-set"),
        "answer": "Incident Mobile, Incident Portal, My Open Incidents",
        "eval_function": (
            "norm_phrase_set_match|lower=true|normalize_hyphen=true|"
            "strip_punct=true|separators=,;|require_non_empty=true"
        ),
    }
    trajectories = {
        "t-evidence": _trajectory(
            "t-evidence",
            tree="Incident Mobile\nIncident Portal\nMy Open Incidents",
        )
    }
    full_result = _result(
        "q-phrase-set",
        trajectory_id="t-evidence",
        content="Incident Mobile\nIncident Portal\nMy Open Incidents",
    )
    partial_result = _result(
        "q-phrase-set",
        trajectory_id="t-evidence",
        content="Incident Mobile\nIncident Portal",
    )

    full_row = module.build_question_trace(
        domain="enterprise",
        result=full_result,
        question=question,
        haystack_ids=["t-evidence"],
        state_text_index=module.build_state_text_index(trajectories),
        max_rank=10,
    )
    partial_row = module.build_question_trace(
        domain="enterprise",
        result=partial_result,
        question=question,
        haystack_ids=["t-evidence"],
        state_text_index=module.build_state_text_index(trajectories),
        max_rank=10,
    )

    assert full_row["exact_evidence_eligible"] is True
    assert full_row["exact_evidence_source_complete"] is True
    assert full_row["metrics"]["answer_phrase_count"] == EXPECTED_ANSWER_PHRASE_COUNT
    assert full_row["metrics"]["exact_context_recall_at_k"] is True
    assert full_row["metrics"]["context_answer_phrase_coverage_at_k"] == 1.0
    assert partial_row["failure_class"] == "evidence_exposure_miss"
    assert partial_row["metrics"]["exact_context_recall_at_k"] is False
    assert partial_row["metrics"]["context_answer_phrase_coverage_at_k"] == pytest.approx(2 / 3)
    assert "Incident Mobile" not in json.dumps(full_row)
    assert "My Open Incidents" not in json.dumps(full_row)


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
