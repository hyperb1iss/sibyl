from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

QUESTION_COUNT = 2
EXPECTED_RECALL_AT_1 = 0.5


def _load_bench_module() -> ModuleType:
    path = Path(__file__).parents[2] / "benchmarks" / "longmemeval_bench.py"
    spec = importlib.util.spec_from_file_location("longmemeval_bench", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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

    monkeypatch.setattr(module, "_git_commit", lambda: "abc123")
    monkeypatch.setattr(module, "retrieve_raw", lambda _entry: ([1, 0], ["s1", "s2"]))

    report = module.run_benchmark(
        str(data_path),
        mode="raw",
        k_values=[1, 2],
        command=["longmemeval_bench.py", "fixture.json"],
    )

    assert report["schema_version"] == "longmemeval-offline-v2"
    assert report["sibyl_commit"] == "abc123"
    assert report["command"] == ["longmemeval_bench.py", "fixture.json"]
    assert report["runtime"] == {
        "runtime_mode": "offline",
        "graph_engine": "none",
        "store": "chromadb_ephemeral",
        "retrieval_mode": "raw",
        "embedding_model": "chromadb_default",
    }
    assert report["dataset"]["evaluated_entries"] == QUESTION_COUNT
    assert report["overall"]["recall@1"] == pytest.approx(EXPECTED_RECALL_AT_1)
    assert len(report["case_results"]) == QUESTION_COUNT
    assert report["case_results"][0] == {
        "question_id": "q1",
        "question_type": "single-session-user",
        "question": "What did I buy?",
        "question_date": "2026/01/03 12:00",
        "answer_session_ids": ["s2"],
        "ranked_session_ids": ["s2", "s1"],
        "recall@1": 1.0,
        "ndcg@1": 1.0,
        "recall@2": 1.0,
        "ndcg@2": 1.0,
    }
