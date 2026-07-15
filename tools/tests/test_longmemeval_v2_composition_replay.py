from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pytest
from benchmarks.longmemeval_v2_composition_replay import (
    legacy_result_candidates,
    replay_composition,
)


def test_composition_replay_restores_relevant_neighbor_without_model_calls(
    tmp_path: Path,
) -> None:
    run_path = tmp_path / "per_question.jsonl"
    catalog_path = tmp_path / "chunk_catalog.jsonl.gz"
    manifest_path = tmp_path / "memory_manifest.json"
    run_path.write_text(json.dumps(_run_row()) + "\n", encoding="utf-8")
    with gzip.open(catalog_path, "wt", encoding="utf-8") as handle:
        for item in _catalog_rows():
            handle.write(json.dumps(item) + "\n")
    _write_manifest(manifest_path, catalog_path)

    report = replay_composition(
        runs={"web": run_path},
        catalogs={"web": catalog_path},
        manifests={"web": manifest_path},
        max_items=3,
        max_chunks_per_trajectory=2,
        neighbor_stitch_items=1,
        neighbor_stitch_span=1,
    )

    assert report["metrics"]["baseline_full_phrase_exposure_rate"] == 0.0
    assert report["metrics"]["candidate_full_phrase_exposure_rate"] == 1.0
    assert report["metrics"]["questions_losing_full_phrase_exposure"] == 0
    assert report["metrics"]["candidate_neighbor_item_count"] == 1
    assert report["metrics"]["raw_assembly_parity_rate"] == 1.0
    assert report["gate"]["pass"] is True


def test_composition_replay_rejects_catalog_content_drift(tmp_path: Path) -> None:
    run_path = tmp_path / "per_question.jsonl"
    catalog_path = tmp_path / "chunk_catalog.jsonl.gz"
    manifest_path = tmp_path / "memory_manifest.json"
    run_path.write_text(json.dumps(_run_row()) + "\n", encoding="utf-8")
    rows = _catalog_rows()
    rows[1] = _catalog_item("t1", 1, "Drifted deployment content")
    with gzip.open(catalog_path, "wt", encoding="utf-8") as handle:
        for item in rows:
            handle.write(json.dumps(item) + "\n")
    _write_manifest(manifest_path, catalog_path)

    with pytest.raises(ValueError, match="content disagrees"):
        replay_composition(
            runs={"web": run_path},
            catalogs={"web": catalog_path},
            manifests={"web": manifest_path},
            max_items=3,
            max_chunks_per_trajectory=2,
            neighbor_stitch_items=1,
            neighbor_stitch_span=1,
        )


def test_legacy_result_candidates_recover_sealed_context_headers() -> None:
    candidates = legacy_result_candidates(
        [
            {
                "type": "text",
                "value": (
                    "Retrieved evidence rank 2\n"
                    "Trajectory: trajectory-1\n"
                    "Chunk: 3\n"
                    "Score: 1.25\n\n"
                    "State evidence"
                ),
            }
        ]
    )

    assert candidates == [
        {
            "id": "legacy:trajectory-1:3",
            "type": "session",
            "content": "State evidence",
            "score": 1.25,
            "metadata": {
                "longmemeval_v2_trajectory_id": "trajectory-1",
                "longmemeval_v2_chunk_index": 3,
                "longmemeval_v2_state_index": 3,
                "longmemeval_v2_state_indices": [3],
            },
            "_selection_origin": "search",
            "_search_rank": 2,
        }
    ]


def _run_row() -> dict[str, object]:
    entries = [
        (
            "procedure-1",
            "context_pack:procedures",
            None,
            None,
            0.1,
            "Unrelated account settings",
        ),
        ("session-t1", "search", "t1", 1, 1.0, "Deployment settings overview"),
        ("session-t2", "search", "t2", 0, 0.9, "Notification settings overview"),
    ]
    return {
        "question_id": "question-1",
        "question_text": "Which value is shown for the Deployment Ring?",
        "answer_gold": "Critical",
        "eval_function": "norm_phrase_set_match|lower=true|separators=,;",
        "memory_context": [
            {
                "type": "text",
                "value": f"Retrieved evidence rank {rank}\n\n{entry[5]}",
            }
            for rank, entry in enumerate(entries, start=1)
        ],
        "memory_post_query_metadata": {
            "run_id": "run-1",
            "project_id": "project-1",
            "retrieval_trace": [
                {
                    "rank": rank,
                    "entity_id": entry[0],
                    "entity_type": "procedure"
                    if str(entry[1]).startswith("context_pack:")
                    else "session",
                    "trajectory_id": entry[2],
                    "chunk_index": entry[3],
                    "state_indices": [entry[3]] if entry[3] is not None else [],
                    "score": entry[4],
                    "selection_origin": entry[1],
                    "search_rank": rank - 1 if entry[1] == "search" else None,
                    "state_part_of_search_rank": None,
                    "state_part_refined_from_chunk": None,
                    "neighbor_of_search_rank": None,
                    "neighbor_distance": None,
                }
                for rank, entry in enumerate(entries, start=1)
            ],
            "search_metadata": {
                "adapter_assembly": {"input_result_count": 2},
                "graph_retrieval": {
                    "ranking_trace": [
                        _ranking_trace_item("session-t1", "t1", 1, 1, 1.0),
                        _ranking_trace_item("session-t2", "t2", 0, 2, 0.9),
                    ]
                },
            },
        },
    }


def _ranking_trace_item(
    entity_id: str,
    trajectory_id: str,
    chunk_index: int,
    rank: int,
    score: float,
) -> dict[str, object]:
    return {
        "entity_id": entity_id,
        "rank": rank,
        "score": score,
        "metadata": {
            "longmemeval_v2_trajectory_id": trajectory_id,
            "evidence_part_id": f"chunk-{chunk_index}",
        },
    }


def _catalog_rows() -> list[dict[str, object]]:
    return [
        _catalog_item("t1", 0, "Deployment Ring: Critical"),
        _catalog_item("t1", 1, "Deployment settings overview"),
        _catalog_item("t2", 0, "Notification settings overview"),
    ]


def _catalog_item(trajectory_id: str, chunk_index: int, content: str) -> dict[str, object]:
    return {
        "id": f"catalog:{trajectory_id}:{chunk_index}",
        "type": "session",
        "content": content,
        "score": 0.0,
        "metadata": {
            "longmemeval_v2_trajectory_id": trajectory_id,
            "longmemeval_v2_chunk_index": chunk_index,
            "longmemeval_v2_state_index": chunk_index,
            "longmemeval_v2_state_indices": [chunk_index],
            "longmemeval_v2_state_part_count": 1,
            "longmemeval_v2_state_part_index": 0,
        },
    }


def _write_manifest(path: Path, catalog_path: Path) -> None:
    digest = hashlib.sha256(catalog_path.read_bytes()).hexdigest()
    path.write_text(
        json.dumps(
            {
                "chunk_catalog_sha256": f"sha256:{digest}",
                "run_id": "run-1",
                "project_id": "project-1",
            }
        ),
        encoding="utf-8",
    )
