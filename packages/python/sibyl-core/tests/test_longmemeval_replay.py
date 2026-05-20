from __future__ import annotations

import json
from pathlib import Path

import pytest

from sibyl_core.evals.longmemeval_replay import (
    load_longmemeval_replay_inputs,
    replay_longmemeval_report,
    rerank_longmemeval_case,
    summary_to_dict,
)


def _entry(
    *,
    question: str = "Can you suggest a hotel for Miami?",
    question_type: str = "single-session-preference",
) -> dict:
    return {
        "question_id": "q1",
        "question_type": question_type,
        "question": question,
        "question_date": "2026/01/20 12:00",
        "answer_session_ids": ["answer-session"],
        "haystack_session_ids": ["generic-session", "answer-session", "older-session"],
        "haystack_dates": ["2026/01/19", "2026/01/18", "2026/01/10"],
        "haystack_sessions": [
            [
                {
                    "role": "assistant",
                    "content": "Here are some hotels in Miami with useful booking details.",
                }
            ],
            [
                {
                    "role": "user",
                    "content": "I prefer boutique hotels with quiet rooms and small pools.",
                }
            ],
            [{"role": "user", "content": "I booked a conference venue last year."}],
        ],
    }


def _case_result() -> dict:
    return {
        "case_index": 0,
        "question_id": "q1",
        "question_type": "single-session-preference",
        "question": "Can you suggest a hotel for Miami?",
        "question_date": "2026/01/20 12:00",
        "answer_session_ids": ["answer-session"],
        "ranked_session_ids": ["generic-session", "older-session", "answer-session"],
        "ranked_results": [
            {
                "longmemeval_session_id": "generic-session",
                "score": 1.0,
                "type": "session",
            },
            {
                "longmemeval_session_id": "older-session",
                "score": 0.9,
                "type": "session",
            },
            {
                "longmemeval_session_id": "answer-session",
                "score": 0.8,
                "type": "session",
            },
        ],
    }


def test_heuristic_rerank_preserves_candidate_set() -> None:
    reranked = rerank_longmemeval_case(
        _case_result(),
        _entry(),
        strategy="heuristic",
        corpus_text_policy="user-and-assistant-turns-v1",
    )

    assert sorted(reranked) == sorted(_case_result()["ranked_session_ids"])


def test_coverage_rerank_uses_query_terms_without_answer_oracle() -> None:
    haystack_ids = [
        *(f"distractor-{index}" for index in range(5)),
        "answer-session",
        *(f"tail-{index}" for index in range(4)),
    ]
    haystack_sessions = [
        [{"role": "user", "content": "I need generic travel planning help."}]
        for _ in range(5)
    ]
    haystack_sessions.append(
        [
            {
                "role": "user",
                "content": "The bike service receipt is part of my total maintenance expense.",
            }
        ]
    )
    haystack_sessions.extend(
        [{"role": "user", "content": "I saved an unrelated cookbook note."}]
        for _ in range(4)
    )
    entry = {
        "question_id": "q1",
        "question_type": "multi-session",
        "question": "How much was my bike service expense total?",
        "question_date": "2026/01/20 12:00",
        "answer_session_ids": ["answer-session"],
        "haystack_session_ids": haystack_ids,
        "haystack_dates": ["2026/01/19"] * len(haystack_ids),
        "haystack_sessions": haystack_sessions,
    }
    case = {
        "case_index": 0,
        "question_id": "q1",
        "question_type": "multi-session",
        "question": entry["question"],
        "question_date": entry["question_date"],
        "answer_session_ids": ["answer-session"],
        "ranked_session_ids": haystack_ids,
        "ranked_results": [
            {"longmemeval_session_id": session_id, "score": 1.0 - (index * 0.01)}
            for index, session_id in enumerate(haystack_ids)
        ],
    }

    reranked = rerank_longmemeval_case(
        case,
        entry,
        strategy="coverage",
        corpus_text_policy="user-and-assistant-turns-v1",
    )

    assert reranked.index("answer-session") < 5


def test_oracle_rerank_is_explicit_upper_bound() -> None:
    reranked = rerank_longmemeval_case(
        _case_result(),
        _entry(),
        strategy="oracle",
        corpus_text_policy="user-and-assistant-turns-v1",
    )

    assert reranked[0] == "answer-session"


def test_replay_report_returns_delta_and_case_changes() -> None:
    report = {
        "k_values": [1, 2],
        "dataset": {"corpus_text_policy": "user-and-assistant-turns-v1"},
        "case_results": [_case_result()],
    }

    summary = replay_longmemeval_report(report, [_entry()], strategy="oracle")
    payload = summary_to_dict(summary, include_cases=True)

    assert payload["baseline_overall"]["recall@1"] == 0.0
    assert payload["overall"]["recall@1"] == 1.0
    assert payload["delta"]["recall@1"] == 1.0
    assert payload["improved_cases"] == 1
    assert payload["case_results"][0]["reranked_answer_ranks"] == [
        {"session_id": "answer-session", "rank": 1}
    ]


def test_loader_uses_dataset_path_from_report(tmp_path: Path) -> None:
    dataset_path = tmp_path / "longmemeval.json"
    report_path = tmp_path / "report.json"
    dataset_path.write_text(json.dumps([_entry()]), encoding="utf-8")
    report_path.write_text(
        json.dumps({"dataset": {"path": str(dataset_path)}, "case_results": []}),
        encoding="utf-8",
    )

    report, dataset = load_longmemeval_replay_inputs(report_path)

    assert report["dataset"]["path"] == str(dataset_path)
    assert dataset[0]["question_id"] == "q1"


def test_loader_requires_dataset_path_when_missing(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({"dataset": {}, "case_results": []}), encoding="utf-8")

    with pytest.raises(ValueError, match=r"dataset\.path"):
        load_longmemeval_replay_inputs(report_path)
