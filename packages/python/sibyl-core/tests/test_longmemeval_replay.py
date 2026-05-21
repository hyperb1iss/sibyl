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


def test_coverage_rerank_uses_question_date_for_temporal_evidence() -> None:
    haystack_ids = [
        "phone-session",
        "workshop-session",
        "shoes-session",
        "sushi-session",
        "closet-session",
        "answer-session",
    ]
    entry = {
        "question_id": "q1",
        "question_type": "temporal-reasoning",
        "question": "What kitchen appliance did I buy 10 days ago?",
        "question_date": "2026/01/20 12:00",
        "answer_session_ids": ["answer-session"],
        "haystack_session_ids": haystack_ids,
        "haystack_dates": ["2026/01/10 12:00"] * len(haystack_ids),
        "haystack_sessions": [
            [{"role": "user", "content": "My Samsung phone battery has been unreliable."}],
            [{"role": "user", "content": "I organized a sustainable living workshop."}],
            [{"role": "user", "content": "I bought running shoes for a picnic."}],
            [{"role": "user", "content": "I learned about uramaki sushi rolls."}],
            [{"role": "user", "content": "I organized my closet and made a shoe list."}],
            [
                {
                    "role": "user",
                    "content": "I got a smoker today and want BBQ sauce recipes.",
                }
            ],
        ],
    }
    case = {
        "case_index": 0,
        "question_id": "q1",
        "question_type": "temporal-reasoning",
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


def test_coverage_rerank_demotes_generic_assistant_advice() -> None:
    haystack_ids = [
        "generic-session",
        "answer-session",
        *(f"distractor-{index}" for index in range(4)),
    ]
    entry = {
        "question_id": "q1",
        "question_type": "single-session-preference",
        "question": "Can you recommend a movie tonight?",
        "question_date": "2026/01/20 12:00",
        "answer_session_ids": ["answer-session"],
        "haystack_session_ids": haystack_ids,
        "haystack_dates": ["2026/01/19"] * len(haystack_ids),
        "haystack_sessions": [
            [
                {
                    "role": "assistant",
                    "content": "As an AI, here are some movie ideas for tonight.",
                }
            ],
            [{"role": "user", "content": "I recommend a comedy movie tonight."}],
            *(
                [{"role": "user", "content": "I saved an unrelated travel note."}]
                for _index in range(4)
            ),
        ],
    }
    case = {
        "case_index": 0,
        "question_id": "q1",
        "question_type": entry["question_type"],
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

    assert reranked[0] == "answer-session"


def test_coverage_rerank_uses_best_local_segment() -> None:
    filler = " ".join(f"filler{index}" for index in range(24))
    haystack_ids = [
        *(f"distractor-{index}" for index in range(5)),
        "answer-session",
        *(f"tail-{index}" for index in range(4)),
    ]
    haystack_sessions = [
        [
            {
                "role": "user",
                "content": f"favorite {filler} short {filler} grain {filler} rice",
            }
        ]
        for _index in range(5)
    ]
    haystack_sessions.append(
        [
            {
                "role": "user",
                "content": (
                    f"{filler} {filler} I was making dinner with my favorite "
                    "Japanese short grain rice."
                ),
            }
        ]
    )
    haystack_sessions.extend(
        [{"role": "user", "content": "I saved an unrelated cookbook note."}]
        for _index in range(4)
    )
    entry = {
        "question_id": "q1",
        "question_type": "single-session-user",
        "question": "What is my favorite short grain rice?",
        "question_date": "2026/01/20 12:00",
        "answer_session_ids": ["answer-session"],
        "haystack_session_ids": haystack_ids,
        "haystack_dates": ["2026/01/19"] * len(haystack_ids),
        "haystack_sessions": haystack_sessions,
    }
    case = {
        "case_index": 0,
        "question_id": "q1",
        "question_type": entry["question_type"],
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


def test_coverage_rerank_uses_primary_user_turn_signal() -> None:
    haystack_ids = [
        *(f"distractor-{index}" for index in range(5)),
        "answer-session",
        *(f"tail-{index}" for index in range(5)),
    ]
    haystack_sessions = [
        [
            {"role": "user", "content": "I need generic calendar help."},
            {
                "role": "assistant",
                "content": (
                    "Here are tips about what time people usually get home from "
                    "work on weeknights."
                ),
            },
        ]
        for _index in range(5)
    ]
    haystack_sessions.append(
        [
            {
                "role": "user",
                "content": "I usually get home from work around 6:30 pm on weeknights.",
            },
            {"role": "assistant", "content": "Here are some easy dinner ideas."},
        ]
    )
    haystack_sessions.extend(
        [{"role": "user", "content": "I saved an unrelated cookbook note."}]
        for _index in range(5)
    )
    entry = {
        "question_id": "q1",
        "question_type": "single-session-user",
        "question": "What time do I usually get home from work on weeknights?",
        "question_date": "2026/01/20 12:00",
        "answer_session_ids": ["answer-session"],
        "haystack_session_ids": haystack_ids,
        "haystack_dates": ["2026/01/19"] * len(haystack_ids),
        "haystack_sessions": haystack_sessions,
    }
    case = {
        "case_index": 0,
        "question_id": "q1",
        "question_type": entry["question_type"],
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


def test_coverage_rerank_uses_assistant_turn_for_retrospective_answer() -> None:
    haystack_ids = [
        *(f"distractor-{index}" for index in range(5)),
        "answer-session",
        *(f"tail-{index}" for index in range(5)),
    ]
    haystack_sessions = [
        [
            {
                "role": "user",
                "content": "I'm looking for online music resources and free lessons.",
            },
            {"role": "assistant", "content": "Here are broad learning websites."},
        ]
        for _index in range(5)
    ]
    haystack_sessions.append(
        [
            {"role": "user", "content": "Do you have any recommendations for resources?"},
            {
                "role": "assistant",
                "content": (
                    "MusicTheory.net offers free lessons and exercises for music theory."
                ),
            },
        ]
    )
    haystack_sessions.extend(
        [{"role": "user", "content": "I saved an unrelated note."}]
        for _index in range(5)
    )
    entry = {
        "question_id": "q1",
        "question_type": "single-session-assistant",
        "question": (
            "Can you remind me of the website you recommended for free lessons and exercises?"
        ),
        "question_date": "2026/01/20 12:00",
        "answer_session_ids": ["answer-session"],
        "haystack_session_ids": haystack_ids,
        "haystack_dates": ["2026/01/19"] * len(haystack_ids),
        "haystack_sessions": haystack_sessions,
    }
    case = {
        "case_index": 0,
        "question_id": "q1",
        "question_type": entry["question_type"],
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


def test_coverage_rerank_keeps_partial_segment_from_evicting_count_evidence() -> None:
    haystack_ids = [
        *(f"answer-{index}" for index in range(5)),
        "tail-session",
    ]
    entry = {
        "question_id": "q1",
        "question_type": "multi-session",
        "question": "How many museum visits did I make in February?",
        "question_date": "2026/01/20 12:00",
        "answer_session_ids": [f"answer-{index}" for index in range(5)],
        "haystack_session_ids": haystack_ids,
        "haystack_dates": ["2026/01/19"] * len(haystack_ids),
        "haystack_sessions": [
            [
                {
                    "role": "user",
                    "content": f"museum visit February evidence {index}",
                }
            ]
            for index in range(5)
        ]
        + [
            [
                {
                    "role": "user",
                    "content": "museum clustered with otherwise unrelated notes",
                }
            ]
        ],
    }
    case = {
        "case_index": 0,
        "question_id": "q1",
        "question_type": entry["question_type"],
        "question": entry["question"],
        "question_date": entry["question_date"],
        "answer_session_ids": entry["answer_session_ids"],
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

    assert set(reranked[:5]) == set(entry["answer_session_ids"])


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
