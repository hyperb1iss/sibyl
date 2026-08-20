from __future__ import annotations

import json
from pathlib import Path

import pytest
from tools.bench import longmemeval_v2_pack_compare as compare


def _write_rows(path: Path, traces: dict[str, list[dict[str, str]]]) -> None:
    path.write_text(
        "\n".join(
            json.dumps(
                {
                    "question_id": question_id,
                    "row_status": "valid",
                    "memory_query_duration_seconds": index + 1.0,
                    "memory_post_query_metadata": {"retrieval_trace": trace},
                }
            )
            for index, (question_id, trace) in enumerate(traces.items())
        )
        + "\n",
        encoding="utf-8",
    )


def test_pack_comparison_is_entity_id_and_order_strict(tmp_path: Path) -> None:
    left = tmp_path / "left.jsonl"
    right = tmp_path / "right.jsonl"
    _write_rows(
        left,
        {"q1": [{"entity_id": "a"}, {"entity_id": "b"}]},
    )
    _write_rows(
        right,
        {"q1": [{"entity_id": "b"}, {"entity_id": "a"}]},
    )

    receipt = compare.build_receipt(left, right)

    assert receipt["status"] == "DIVERGENT"
    assert receipt["comparison"]["membership_identical_count"] == 1
    assert receipt["comparison"]["ordered_identical_count"] == 0


def test_pack_comparison_rejects_empty_or_legacy_identity(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.jsonl"
    _write_rows(malformed, {"q1": [{"uuid": "legacy-is-not-canonical"}]})

    with pytest.raises(compare.PackInputError, match="without entity_id"):
        compare.load_packs(malformed)


def test_pack_comparison_rejects_failed_rows_and_question_drift(tmp_path: Path) -> None:
    left = tmp_path / "left.jsonl"
    right = tmp_path / "right.jsonl"
    _write_rows(left, {"q1": [{"entity_id": "a"}]})
    _write_rows(right, {"q2": [{"entity_id": "a"}]})

    with pytest.raises(compare.PackInputError, match="different question sets"):
        compare.compare_packs(compare.load_packs(left), compare.load_packs(right))

    failed = json.loads(left.read_text(encoding="utf-8"))
    failed["row_status"] = "failed"
    left.write_text(json.dumps(failed) + "\n", encoding="utf-8")
    with pytest.raises(compare.PackInputError, match="not a valid row"):
        compare.load_packs(left)
