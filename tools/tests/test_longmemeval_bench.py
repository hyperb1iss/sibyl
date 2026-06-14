from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

QUESTION_COUNT = 2
EXPECTED_RECALL_AT_1 = 0.5
EXPECTED_NDCG_AT_2 = 0.8154648767857288
EXPECTED_MULTI_ANSWER_RECALL = 0.5
EXPECTED_MULTI_ANSWER_NDCG_AT_2 = 0.6131471927654584


def _load_bench_module() -> ModuleType:
    path = Path(__file__).parents[2] / "benchmarks" / "longmemeval_bench.py"
    spec = importlib.util.spec_from_file_location("longmemeval_bench", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_longmemeval_corpus_uses_user_turns_and_keeps_timestamps() -> None:
    module = _load_bench_module()

    documents = module.build_longmemeval_corpus(
        {
            "haystack_session_ids": ["s1", "s2", "s3"],
            "haystack_dates": ["2026/01/01", "2026/01/02", "2026/01/03"],
            "haystack_sessions": [
                [{"role": "assistant", "content": "No user text."}],
                [
                    {"role": "user", "content": "First user turn."},
                    {"role": "assistant", "content": "Assistant reply."},
                    {"role": "user", "content": "Second user turn."},
                ],
                [{"role": "user", "content": "Another session."}],
            ],
        }
    )

    assert [(document.session_id, document.text, document.timestamp) for document in documents] == [
        ("s2", "First user turn.\nSecond user turn.", "2026/01/02"),
        ("s3", "Another session.", "2026/01/03"),
    ]


def test_longmemeval_corpus_can_include_assistant_turns() -> None:
    module = _load_bench_module()

    documents = module.build_longmemeval_corpus(
        {
            "haystack_session_ids": ["s1"],
            "haystack_dates": ["2026/01/01"],
            "haystack_sessions": [
                [
                    {"role": "user", "content": "What term did you suggest?"},
                    {"role": "assistant", "content": "I suggested retrieval planning."},
                ]
            ],
        },
        text_policy="user-and-assistant-turns-v1",
    )

    assert [(document.session_id, document.text, document.timestamp) for document in documents] == [
        (
            "s1",
            "User: What term did you suggest?\nAssistant: I suggested retrieval planning.",
            "2026/01/01",
        )
    ]


def test_longmemeval_report_includes_full_case_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_bench_module()
    data_path = tmp_path / "longmemeval.json"
    data_path.write_text(
        json.dumps(
            [
                {
                    "question_id": "q1",
                    "question_type": "single-session-user",
                    "question": "What did I buy?",
                    "question_date": "2026/01/03 12:00",
                    "answer_session_ids": ["s2"],
                    "haystack_session_ids": ["s1", "s2"],
                    "haystack_sessions": [
                        [{"role": "user", "content": "I bought pencils."}],
                        [{"role": "user", "content": "I bought markers."}],
                    ],
                },
                {
                    "question_id": "q2",
                    "question_type": "temporal-reasoning",
                    "question": "What happened first?",
                    "question_date": "2026/01/03 12:00",
                    "answer_session_ids": ["s1"],
                    "haystack_session_ids": ["s1", "s2"],
                    "haystack_sessions": [
                        [{"role": "user", "content": "First event."}],
                        [{"role": "user", "content": "Second event."}],
                    ],
                },
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        module,
        "git_provenance",
        lambda _root: {
            "sibyl_commit": "abc123",
            "git_dirty": True,
            "git_status": "dirty",
        },
    )
    monkeypatch.setattr(module, "retrieve_raw", lambda _entry: ([1, 0], ["s1", "s2"]))

    report = module.run_benchmark(
        str(data_path),
        mode="raw",
        k_values=[1, 2],
        command=["longmemeval_bench.py", "fixture.json"],
    )

    assert report["schema_version"] == "longmemeval-offline-v2"
    assert report["sibyl_commit"] == "abc123"
    assert report["git_dirty"] is True
    assert report["git_status"] == "dirty"
    assert report["command"] == ["longmemeval_bench.py", "fixture.json"]
    assert report["runtime"] == {
        "runtime_mode": "offline",
        "graph_engine": "none",
        "store": "chromadb_ephemeral",
        "retrieval_mode": "raw",
        "embedding_provider": "chromadb",
        "embedding_model": "chromadb_default",
        "embedding_dimensions": 384,
        "tokenizer_estimate_method": "chromadb_default",
    }
    assert report["dataset"]["name"] == "longmemeval"
    assert report["dataset"]["corpus_hash"].startswith("sha256:")
    assert report["dataset"]["corpus_text_policy"] == module.CORPUS_TEXT_POLICY
    assert report["dataset"]["evaluated_entries"] == QUESTION_COUNT
    assert report["repeat_count"] == 1
    assert report["auth_manifest_id"] == "not-applicable:offline"
    assert report["overall"]["hit@1"] == pytest.approx(EXPECTED_RECALL_AT_1)
    assert report["overall"]["legacy_recall@1"] == pytest.approx(EXPECTED_RECALL_AT_1)
    assert report["overall"]["recall@1"] == pytest.approx(EXPECTED_RECALL_AT_1)
    assert report["overall"]["ndcg@2"] == pytest.approx(EXPECTED_NDCG_AT_2)
    assert len(report["case_results"]) == QUESTION_COUNT
    assert report["case_results"][0] == {
        "question_id": "q1",
        "question_type": "single-session-user",
        "question": "What did I buy?",
        "question_date": "2026/01/03 12:00",
        "answer_session_ids": ["s2"],
        "ranked_session_ids": ["s2", "s1"],
        "hit@1": 1.0,
        "legacy_recall@1": 1.0,
        "recall@1": 1.0,
        "ndcg@1": 1.0,
        "hit@2": 1.0,
        "legacy_recall@2": 1.0,
        "recall@2": 1.0,
        "ndcg@2": 1.0,
    }


def test_longmemeval_scoring_uses_true_multi_answer_recall() -> None:
    module = _load_bench_module()

    metrics = module.score_longmemeval_ranking(["s2", "s3"], ["s1", "s2"], [1, 2])

    assert metrics["hit@1"] == 1.0
    assert metrics["legacy_recall@1"] == 1.0
    assert metrics["recall@1"] == EXPECTED_MULTI_ANSWER_RECALL
    assert metrics["ndcg@1"] == 1.0
    assert metrics["hit@2"] == 1.0
    assert metrics["legacy_recall@2"] == 1.0
    assert metrics["recall@2"] == EXPECTED_MULTI_ANSWER_RECALL
    assert metrics["ndcg@2"] == pytest.approx(EXPECTED_MULTI_ANSWER_NDCG_AT_2)
