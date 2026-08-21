from __future__ import annotations

import threading
from pathlib import Path

import pytest
from benchmarks.longmemeval_v2_memory import render_bundle, sibyl_memory

EXPECTED_ACTION_COUNT = 2
EXPECTED_LANE_COUNT = 3
EXPECTED_SPINE_COUNT = 2
EXPECTED_ADDED_RENDER_CHARS = 15_000
EXPECTED_TREATMENT_ACTIVITY_EVENTS = 8
TREATMENT_CONTEXT_TOTAL_CHARS = 200_000
EXPECTED_NOTE_INPUT_TOKENS = 120
EXPECTED_NOTE_OUTPUT_TOKENS = 30


def _result(result_id: str, *, origin: str, trajectory_id: str = "t1") -> dict[str, object]:
    return {
        "id": result_id,
        "type": "note" if origin.startswith("context_pack:") else "session",
        "content": result_id,
        "metadata": {"longmemeval_v2_trajectory_id": trajectory_id},
        "_selection_origin": origin,
    }


def test_action_spine_annotates_targets_without_reading_question_data() -> None:
    sidecar = render_bundle.build_action_spine(
        {
            "id": "trajectory-1",
            "goal": "filter incidents",
            "states": [
                {
                    "action": "click('a790')",
                    "accessibility_tree": "[a790] menuitem 'Filters'\n[a791] link 'Home'",
                },
                {
                    "action": "fill('missing', 'open')",
                    "accessibility_tree": "[a900] textbox 'Status'",
                },
            ],
        }
    )

    assert sidecar is not None
    assert "click('a790')  # [a790] menuitem 'Filters'" in str(sidecar["content"])
    assert "question" not in sidecar
    assert sidecar["action_count"] == EXPECTED_ACTION_COUNT
    assert sidecar["annotated_action_count"] == 1
    assert sidecar["unresolved_target_count"] == 1


def test_lane_grouping_preserves_membership_and_within_lane_order() -> None:
    results = [
        _result("raw-1", origin="search"),
        _result("note-1", origin="context_pack:typed_stream"),
        _result("support-1", origin="neighbor"),
        _result("raw-2", origin="search", trajectory_id="t2"),
        _result("note-2", origin="context_pack:typed_stream", trajectory_id="t2"),
    ]

    grouped, receipt = render_bundle.group_results_by_lane(results)

    assert [item["id"] for item in grouped] == [
        "note-1",
        "note-2",
        "raw-1",
        "raw-2",
        "support-1",
    ]
    assert receipt["membership_preserved"] is True
    assert receipt["within_lane_order_preserved"] is True
    assert receipt["nonempty_lane_count"] == EXPECTED_LANE_COUNT


def test_action_spines_append_once_in_selected_trajectory_order() -> None:
    first = render_bundle.build_action_spine(
        {"id": "t1", "states": [{"action": "click('a1')", "accessibility_tree": ""}]}
    )
    second = render_bundle.build_action_spine(
        {"id": "t2", "states": [{"action": "click('a2')", "accessibility_tree": ""}]}
    )
    assert first is not None
    assert second is not None

    appended, receipt = render_bundle.append_action_spines(
        [
            _result("raw-1", origin="search", trajectory_id="t1"),
            _result("raw-2", origin="neighbor", trajectory_id="t1"),
            _result("raw-3", origin="search", trajectory_id="t2"),
        ],
        sidecars={"t1": first, "t2": second},
    )

    assert [item["id"] for item in appended[-2:]] == ["action-spine:t1", "action-spine:t2"]
    assert receipt["appended_spine_count"] == EXPECTED_SPINE_COUNT


def test_action_spine_file_is_deterministic_and_tamper_evident(tmp_path: Path) -> None:
    sidecar = render_bundle.build_action_spine(
        {"id": "t1", "states": [{"action": "click('a1')", "accessibility_tree": ""}]}
    )
    assert sidecar is not None
    first = tmp_path / "first.jsonl.gz"
    second = tmp_path / "second.jsonl.gz"
    render_bundle.write_action_spines(first, {"t1": sidecar}, compressed=True)
    render_bundle.write_action_spines(second, {"t1": sidecar}, compressed=True)

    assert first.read_bytes() == second.read_bytes()
    assert render_bundle.read_action_spines(first, compressed=True) == {"t1": sidecar}

    tampered = dict(sidecar)
    tampered["content"] = "changed"
    with pytest.raises(ValueError, match="digest"):
        render_bundle.write_action_spines(
            tmp_path / "tampered.jsonl",
            {"t1": tampered},
            compressed=False,
        )


def test_distillation_receipt_sidecar_is_deterministic_and_screenable(
    tmp_path: Path,
) -> None:
    receipt = _render_v1_distillation_receipt()
    first = tmp_path / "first-receipts.jsonl.gz"
    second = tmp_path / "second-receipts.jsonl.gz"

    render_bundle.write_distillation_receipts(
        first,
        {"trajectory-1": receipt},
        compressed=True,
    )
    render_bundle.write_distillation_receipts(
        second,
        {"trajectory-1": receipt},
        compressed=True,
    )

    assert first.read_bytes() == second.read_bytes()
    loaded = render_bundle.read_distillation_receipts(first, compressed=True)
    assert loaded == {"trajectory-1": receipt}
    screens = render_bundle.screen_distillation_receipts(loaded)
    assert screens["observed_absence"]["survives"] is True
    assert screens["digest_roles_budget"]["survives"] is True


def test_plain_english_lane_header_is_treatment_only() -> None:
    result = _result("raw-1", origin="search")
    baseline, _baseline_receipt = sibyl_memory.render_memory_context([result])
    grouped, _group_receipt = render_bundle.group_results_by_lane([result])
    treatment, _treatment_receipt = sibyl_memory.render_memory_context(grouped)

    assert "Retrieval: search" in str(baseline[0]["value"])
    assert "Evidence lane: Retrieved source evidence" in str(treatment[0]["value"])
    assert "Retrieval: search" not in str(treatment[0]["value"])


def test_reader_char_total_activity_counts_only_new_whole_items() -> None:
    control: dict[str, object] = {
        "max_total_chars": 60_000,
        "rendered_context_chars": 60_000,
        "items": [
            {"entity_id": "a", "dropped": False, "truncated": True},
            {"entity_id": "b", "dropped": False, "truncated": False},
        ],
    }
    treatment: dict[str, object] = {
        "max_total_chars": 400_000,
        "rendered_context_chars": 75_000,
        "items": [
            {"entity_id": "a", "dropped": False, "truncated": False},
            {"entity_id": "b", "dropped": False, "truncated": False},
        ],
    }

    receipt = sibyl_memory.reader_char_total_activity(
        control_receipt=control,
        treatment_receipt=treatment,
    )

    assert receipt["promoted_to_full_count"] == 1
    assert receipt["promoted_entity_ids"] == ["a"]
    assert receipt["added_rendered_chars"] == EXPECTED_ADDED_RENDER_CHARS


def test_rig_activity_is_explicit_for_control_and_aggregates_treatment() -> None:
    control = sibyl_memory.build_rig_activity(
        lane_activity={"activity_events": 2, "context_pack_requests": 1},
        lever_activity={},
        mode="fast",
    )
    treatment = sibyl_memory.build_rig_activity(
        lane_activity={"activity_events": 2, "context_pack_requests": 1},
        lever_activity={"plain_english_lanes": 4, "action_spine": 2},
        mode="fast",
    )

    assert control == {
        "activity_events": 2,
        "context_pack_requests": 1,
        "mode": "fast",
        "lever_activity": {},
    }
    assert treatment["activity_events"] == EXPECTED_TREATMENT_ACTIVITY_EVENTS
    assert treatment["lever_activity"] == {
        "plain_english_lanes": 4,
        "action_spine": 2,
    }


def test_distillation_activity_requires_rendered_treated_entities() -> None:
    receipt = _render_v1_distillation_receipt()
    activity, treatment = sibyl_memory.production_profile_treatment_activity(
        search_metadata={},
        operational_note_dedupe_mode="source",
        operational_note_lane_mode="reserved",
        distillation_profile="render_v1",
        distillation_receipts={"trajectory-1": receipt},
    )

    assert activity == {}
    assert set(treatment) == {"observed_absence", "digest_roles_budget"}
    evidence_set: list[dict[str, object]] = [
        {
            "id": "note-absence",
            "metadata": {
                "operational_source_id": "trajectory-1",
                "operational_note_distillation_profile": "render_v1",
                "note_kind": "observed_absence",
            },
        },
        {
            "id": "note-facts",
            "metadata": {
                "operational_source_id": "trajectory-1",
                "operational_note_distillation_profile": "render_v1",
                "note_kind": "facts",
            },
        },
    ]
    assert (
        sibyl_memory.rendered_distillation_treatment_activity(
            evidence_set=evidence_set,
            rendered_entity_ids=set(),
            distillation_receipts={"trajectory-1": receipt},
        )
        == {}
    )
    assert sibyl_memory.rendered_distillation_treatment_activity(
        evidence_set=evidence_set,
        rendered_entity_ids={"note-absence", "note-facts"},
        distillation_receipts={"trajectory-1": receipt},
    ) == {"observed_absence": 1, "digest_roles_budget": 2}


def test_adapter_renders_bundle_and_emits_explicit_per_lever_activity(  # noqa: PLR0915
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, object]] = []
    result = {
        "id": "session-1",
        "type": "session",
        "content": "source evidence " + "x" * 70_000,
        "score": 1.0,
        "metadata": {
            "longmemeval_v2_trajectory_id": "t1",
            "longmemeval_v2_chunk_index": 0,
            "longmemeval_v2_state_index": 0,
        },
        "_selection_origin": "search",
    }
    memory = object.__new__(sibyl_memory.SibylLiveApiMemory)
    sibyl_memory.Memory.__init__(memory, {})
    memory.api_url = "https://sibyl.invalid/api"
    memory.api_runtime = {"version": "test"}
    memory.ingest_api_runtime = {"version": "test"}
    memory.project_id = "project-render"
    memory.run_id = "run-render"
    memory.inserted_trajectories = 1
    memory.created_entities = 1
    memory.defer_embeddings = True
    memory.ingest_embedding_usage = {}
    memory.search_limit = 12
    memory.max_context_items = 8
    memory.max_context_chars_per_item = 100_000
    memory.max_context_total_chars = TREATMENT_CONTEXT_TOTAL_CHARS
    memory.render_char_total_treatment = True
    memory.render_group_lanes = True
    memory.render_action_spines = True
    memory.operational_note_dedupe_mode = "source_kind"
    memory.operational_note_lane_mode = "additive"
    memory.operational_note_distillation_profile = "baseline"
    memory.ingest_note_distillation_receipts = {}
    memory.evidence_char_budget = TREATMENT_CONTEXT_TOTAL_CHARS
    memory.retrieval_mode = "fast"
    memory.retrieval_max_planned_queries = 3
    memory._pending_embedding_job_ids = set()
    memory._pending_projection_job_ids = set()
    memory._ingest_finalized = True
    memory._chunk_catalog = {}
    memory._query_local = threading.local()
    sidecar = render_bundle.build_action_spine(
        {
            "id": "t1",
            "states": [{"action": "click('a1')", "accessibility_tree": "[a1] button 'Open'"}],
        }
    )
    assert sidecar is not None
    memory._action_spines = {"t1": sidecar}

    def request_json(
        _method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        assert path == "/context/pack"
        assert json is not None
        assert params is None
        requests.append(json)
        return {
            "sections": [],
            "evidence": {
                "results": [result],
                "filters": _production_composition_filters(),
            },
        }

    monkeypatch.setattr(memory, "_request_json", request_json)

    context = memory.query("Which control opens the panel?")
    metadata = memory.post_query_hook(
        query="Which control opens the panel?",
        query_image=None,
        memory_context=context,
    )

    evidence_request = requests[0]["evidence"]
    assert isinstance(evidence_request, dict)
    assert evidence_request["operational_note_dedupe_mode"] == "source_kind"
    assert evidence_request["operational_note_lane_mode"] == "additive"
    assert len(context) == EXPECTED_SPINE_COUNT
    assert "Evidence lane: Retrieved source evidence" in str(context[0]["value"])
    assert "Evidence lane: Action spines" in str(context[1]["value"])
    assert metadata is not None
    rig_activity = metadata["rig_activity"]
    assert isinstance(rig_activity, dict)
    lever_activity = rig_activity["lever_activity"]
    assert isinstance(lever_activity, dict)
    assert set(lever_activity) == {
        "reader_char_total",
        "note_kind_dedupe",
        "additive_note_lane",
        "plain_english_lanes",
        "action_spine",
    }
    assert all(value > 0 for value in lever_activity.values())


def test_adapter_delegates_note_distillation_to_production_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = object.__new__(sibyl_memory.SibylLiveApiMemory)
    memory.note_distillation = True
    memory._pending_note_distillation_job_ids = set()
    memory._pending_job_entity_ids = {}
    memory._pending_job_manifest_ids = {}
    memory.ingest_embedding_usage = {}
    memory.ingest_note_distillation_usage = {}
    memory.ingest_note_distillation_receipts = {}
    memory.operational_note_distillation_profile = "render_v1"
    memory.embedding_job_wait_timeout_seconds = 1.0
    memory.embedding_job_poll_seconds = 0.0
    memory._remember_note_distillation_jobs(
        {
            "written_entities": 1,
            "background_jobs": {
                "note_distillation": {
                    "status": "queued",
                    "job_ids": ["note-job-1"],
                }
            },
        }
    )

    def request_json(
        _method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        assert path == "/jobs/status"
        assert params is None
        assert json == {"job_ids": ["note-job-1"]}
        return {
            "jobs": {
                "note-job-1": {
                    "status": "complete",
                    "result": {
                        "status": "complete",
                        "provider": "openai",
                        "model": "gpt-test",
                        "source_id": "trajectory-1",
                        "input_tokens": EXPECTED_NOTE_INPUT_TOKENS,
                        "output_tokens": EXPECTED_NOTE_OUTPUT_TOKENS,
                        "duration_ms": 42,
                        "distillation_receipt": _render_v1_distillation_receipt(),
                    },
                }
            }
        }

    monkeypatch.setattr(memory, "_request_json", request_json)
    memory._drain_note_distillations()

    assert memory._pending_note_distillation_job_ids == set()
    assert memory.ingest_note_distillation_usage == {
        "provider": "openai",
        "model": "gpt-test",
        "requests": 1,
        "input_tokens": EXPECTED_NOTE_INPUT_TOKENS,
        "output_tokens": EXPECTED_NOTE_OUTPUT_TOKENS,
        "total_tokens": EXPECTED_NOTE_INPUT_TOKENS + EXPECTED_NOTE_OUTPUT_TOKENS,
        "cost_usd": 0.001,
        "cost_complete": True,
        "duration_ms": 42,
    }
    assert memory.ingest_note_distillation_receipts == {
        "trajectory-1": _render_v1_distillation_receipt()
    }


def test_production_note_distillation_enqueue_fails_closed() -> None:
    memory = object.__new__(sibyl_memory.SibylLiveApiMemory)
    memory.note_distillation = True
    memory._pending_note_distillation_job_ids = set()

    with pytest.raises(RuntimeError, match="enqueue degraded"):
        memory._remember_note_distillation_jobs(
            {
                "written_entities": 1,
                "background_jobs": {
                    "note_distillation": {
                        "status": "degraded",
                        "error": "queue unavailable",
                    }
                },
            }
        )


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
            "input_tokens": EXPECTED_NOTE_INPUT_TOKENS,
            "output_tokens": EXPECTED_NOTE_OUTPUT_TOKENS,
            "total_tokens": EXPECTED_NOTE_INPUT_TOKENS + EXPECTED_NOTE_OUTPUT_TOKENS,
            "cost_usd": 0.001,
            "cost_complete": True,
        },
    }


def _production_composition_filters() -> dict[str, object]:
    return {
        "retrieval_mode": "fast",
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
                    "membership_equal": True,
                    "order_equal": True,
                },
                "hard_budget": {
                    "mode": "characters",
                    "limit": TREATMENT_CONTEXT_TOTAL_CHARS,
                    "selected": 70_000,
                    "within": True,
                },
            },
        },
    }
