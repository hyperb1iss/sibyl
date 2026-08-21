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
from benchmarks.longmemeval_v2_memory import render_bundle


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
    assert set(report["replay_survivors"]) == set(render_bundle.RENDER_BUNDLE_LEVERS)
    assert report["bundle_eligible"] is False


def test_render_bundle_replay_requires_and_accepts_all_seven_score_blind_screens(
    tmp_path: Path,
) -> None:
    run_path = tmp_path / "per_question.jsonl"
    catalog_path = tmp_path / "chunk_catalog.jsonl.gz"
    manifest_path = tmp_path / "memory_manifest.json"
    action_path = tmp_path / render_bundle.ACTION_SPINE_FILENAME
    distillation_path = tmp_path / render_bundle.DISTILLATION_RECEIPT_FILENAME
    source_content = "Deployment Ring: Critical\n" + "x" * 70_000
    distillation_receipt = _render_v1_distillation_receipt()
    distillation_receipts = {"t1": distillation_receipt}
    receipt_set_sha256 = render_bundle.canonical_sha256(distillation_receipts)
    run_path.write_text(
        json.dumps(_bundle_run_row(source_content, receipt_set_sha256)) + "\n",
        encoding="utf-8",
    )
    with gzip.open(catalog_path, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps(_catalog_item("t1", 0, source_content)) + "\n")
    action_spine = render_bundle.build_action_spine(
        {
            "id": "t1",
            "states": [
                {
                    "action": "click('a1')",
                    "accessibility_tree": "[a1] button 'Deployment Ring'",
                }
            ],
        }
    )
    assert action_spine is not None
    render_bundle.write_action_spines(
        action_path,
        {"t1": action_spine},
        compressed=True,
    )
    render_bundle.write_distillation_receipts(
        distillation_path,
        distillation_receipts,
        compressed=True,
    )
    manifest_path.write_text(
        json.dumps(
            {
                "chunk_catalog_sha256": _sha256(catalog_path),
                "run_id": "run-1",
                "project_id": "project-1",
                "action_spine_count": 1,
                "action_spines_sha256": render_bundle.file_sha256(action_path),
                "distillation_receipt_count": 1,
                "distillation_receipts_sha256": render_bundle.file_sha256(distillation_path),
                "ingest_note_distillation_receipt_set_sha256": receipt_set_sha256,
            }
        ),
        encoding="utf-8",
    )

    report = replay_composition(
        runs={"web": run_path},
        catalogs={"web": catalog_path},
        manifests={"web": manifest_path},
        max_items=1,
        max_chunks_per_trajectory=1,
        neighbor_stitch_items=0,
        neighbor_stitch_span=0,
        render_max_chars_per_item=100_000,
        replay_cost_budget_usd=0.0,
    )

    assert report["replay_survivors"] == dict.fromkeys(
        render_bundle.RENDER_BUNDLE_LEVERS,
        True,
    )
    assert report["bundle_eligible"] is True
    assert report["render_screen"]["hard_total_within"] is True
    assert report["cost"] == {
        "budget_usd": 0.0,
        "actual_usd": 0.0,
        "provider_calls": 0,
        "within_budget": True,
    }


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
    path.write_text(
        json.dumps(
            {
                "chunk_catalog_sha256": _sha256(catalog_path),
                "run_id": "run-1",
                "project_id": "project-1",
            }
        ),
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _bundle_run_row(content: str, receipt_set_sha256: str) -> dict[str, object]:
    return {
        "question_id": "bundle-question-1",
        "question_text": "Which deployment ring is configured?",
        "answer_gold": "Critical",
        "eval_function": "norm_phrase_set_match|lower=true|separators=,;",
        "memory_context": [
            {
                "type": "text",
                "value": f"Retrieved evidence rank 1\n\n{content}",
            }
        ],
        "memory_post_query_metadata": {
            "run_id": "run-1",
            "project_id": "project-1",
            "ingest_note_distillation_receipt_count": 1,
            "ingest_note_distillation_receipt_set_sha256": receipt_set_sha256,
            "retrieval_trace": [
                {
                    "rank": 1,
                    "entity_id": "session-t1",
                    "entity_type": "session",
                    "trajectory_id": "t1",
                    "chunk_index": 0,
                    "state_indices": [0],
                    "score": 1.0,
                    "selection_origin": "search",
                    "search_rank": 1,
                    "state_part_of_search_rank": None,
                    "state_part_refined_from_chunk": None,
                    "neighbor_of_search_rank": None,
                    "neighbor_distance": None,
                }
            ],
            "search_metadata": {
                "evidence_composition": {
                    "operational_note_dedupe_mode": "source_kind",
                    "operational_note_lane_mode": "additive",
                    "activity_receipt": {
                        "note_dedupe": {
                            "mode": "source_kind",
                            "duplicate_source_count": 2,
                            "duplicate_source_kind_count": 0,
                        },
                        "additive_note_lane": {"admitted_note_ids": ["note-1"]},
                        "raw_parity": {
                            "reference_ids": ["session-t1"],
                            "selected_ids": ["session-t1"],
                            "membership_equal": True,
                            "order_equal": True,
                        },
                        "hard_budget": {
                            "mode": "characters",
                            "limit": 400_000,
                            "selected": len(content),
                            "within": True,
                        },
                    },
                },
                "adapter_assembly": {"input_result_count": 1},
            },
        },
    }


def _render_v1_distillation_receipt() -> dict[str, object]:
    return {
        "profile": "render_v1",
        "digest": {
            "roles": ["heading", "gridcell"],
            "candidate_line_count": 3,
            "admitted_line_count": 2,
            "digest_chars": 500,
            "configured_budget": {
                "digest_chars": 40_000,
                "lines_per_observation": 8,
                "lines_total": 160,
                "line_chars": 140,
            },
            "within_digest_char_budget": True,
            "within_line_budget": True,
        },
        "observed_absence": {
            "proposed_count": 1,
            "admitted_count": 1,
            "rejected_count": 0,
            "proposals": [
                {
                    "status": "admitted",
                    "reason": "complete_inventory",
                    "inventory_complete": True,
                    "inventory_rejection_reasons": [],
                }
            ],
        },
        "render": {
            "max_note_chars": 1_600,
            "within_note_char_budget": True,
            "lines": 4,
            "chars": 300,
            "truncated": False,
            "notes": [
                {
                    "note_kind": "observed_absence",
                    "lines": 1,
                    "chars": 80,
                    "unbounded_chars": 80,
                    "truncated": False,
                }
            ],
        },
        "usage": {
            "provider": "openai",
            "model": "gpt-test",
            "requests": 1,
            "input_tokens": 120,
            "output_tokens": 30,
            "total_tokens": 150,
            "cost_usd": 0.001,
            "cost_complete": True,
        },
    }
