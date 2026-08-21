from __future__ import annotations

from pathlib import Path

import pytest
from benchmarks.longmemeval_v2_memory import render_bundle

EXPECTED_ACTION_COUNT = 2
EXPECTED_LANE_COUNT = 3
EXPECTED_SPINE_COUNT = 2


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
