from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType


def _load_replay_module() -> ModuleType:
    path = Path(__file__).parents[2] / "benchmarks" / "longmemeval_replay.py"
    spec = importlib.util.spec_from_file_location("longmemeval_replay", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_longmemeval_replay_cli_prints_json_summary(
    tmp_path: Path,
    capsys,
) -> None:
    module = _load_replay_module()
    dataset_path = tmp_path / "longmemeval.json"
    report_path = tmp_path / "report.json"
    dataset_path.write_text(
        json.dumps(
            [
                {
                    "question_id": "q1",
                    "question_type": "single-session-user",
                    "question": "What did I buy?",
                    "question_date": "2026/01/03 12:00",
                    "answer_session_ids": ["s2"],
                    "haystack_session_ids": ["s1", "s2"],
                    "haystack_dates": ["2026/01/01", "2026/01/02"],
                    "haystack_sessions": [
                        [{"role": "user", "content": "I bought pencils."}],
                        [{"role": "user", "content": "I bought markers."}],
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    report_path.write_text(
        json.dumps(
            {
                "k_values": [1, 2],
                "dataset": {
                    "path": str(dataset_path),
                    "corpus_text_policy": "user-and-assistant-turns-v1",
                },
                "case_results": [
                    {
                        "case_index": 0,
                        "question_id": "q1",
                        "question_type": "single-session-user",
                        "question": "What did I buy?",
                        "question_date": "2026/01/03 12:00",
                        "answer_session_ids": ["s2"],
                        "ranked_session_ids": ["s1", "s2"],
                        "ranked_results": [
                            {"longmemeval_session_id": "s1", "score": 1.0},
                            {"longmemeval_session_id": "s2", "score": 0.9},
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert module.main([str(report_path), "--strategy", "oracle", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["strategy"] == "oracle"
    assert payload["baseline_overall"]["recall@1"] == 0.0
    assert payload["overall"]["recall@1"] == 1.0
