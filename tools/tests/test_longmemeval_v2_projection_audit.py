from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from sibyl_core.projection import experience as projection
from sibyl_core.projection import slicing

EXPECTED_MIN_EVIDENCE_PARTS = 2
EXPECTED_MAX_EVIDENCE_PART_CHARS = 220
EXPECTED_MAX_WRITE_AMPLIFICATION = 2.0
EXPECTED_MAX_PASSAGE_BYTE_AMPLIFICATION = 2.5


def _load_module() -> ModuleType:
    path = Path(__file__).parents[2] / "benchmarks" / "longmemeval_v2_projection_audit.py"
    spec = importlib.util.spec_from_file_location("longmemeval_v2_projection_audit", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _report(tmp_path: Path, trajectory: dict[str, object], **kwargs: Any) -> dict[str, Any]:
    module = _load_module()
    source = tmp_path / "trajectories.jsonl"
    source.write_text(json.dumps(trajectory) + "\n", encoding="utf-8")
    return module.audit_trajectories(source, **kwargs)


def test_projection_audit_proves_source_support_and_replay(tmp_path: Path) -> None:
    report = _report(tmp_path, _trajectory(), content_max_chars=220)

    assert report["passed"] is True, report["issues"]
    assert report["counts"]["trajectories"] == 1
    assert report["counts"]["evidence_parts"] > EXPECTED_MIN_EVIDENCE_PARTS
    assert report["counts"]["actions"] == 1
    assert report["relationship_types"]["DERIVED_FROM"] > 0
    assert report["bounds"]["max_evidence_part_chars"] <= EXPECTED_MAX_EVIDENCE_PART_CHARS
    assert report["bounds"]["max_write_amplification"] <= EXPECTED_MAX_WRITE_AMPLIFICATION
    assert report["bounds"]["aggregate_write_amplification"] <= EXPECTED_MAX_WRITE_AMPLIFICATION


def test_passage_rows_are_accounted_by_bytes_not_against_the_inference_bound(
    tmp_path: Path,
) -> None:
    report = _report(tmp_path, _trajectory(), content_max_chars=220)

    passages = report["counts"]["passage_entities"]
    assert passages > 0
    assert report["entity_types"]["passage"] == passages
    # Passages outnumber the inferred rows several times over, which is exactly
    # why they cannot share the inference bound.
    assert passages > report["counts"]["derived_entities"]
    assert report["counts"]["derived_entities"] == report["counts"]["entities"] - (
        report["counts"]["raw_entities"] + passages + report["counts"]["manifest_entities"]
    )
    assert (
        report["bounds"]["max_passage_byte_amplification"]
        <= EXPECTED_MAX_PASSAGE_BYTE_AMPLIFICATION
    )
    assert report["counts"]["trajectories_above_passage_limit"] == 0


def test_a_fragmenting_segmenter_trips_the_write_amplification_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A projector that mints one inferred row per action is the runaway."""

    def fragmented_segments(
        experience: Any, observations: list[Any]
    ) -> list[tuple[str, list[Any]]]:
        segments = [
            (f"Goal: {experience.goal}\n{item.ordinal}. {item.action}", [item])
            for item in observations
            if item.action
        ]
        return segments or [(f"Goal: {experience.goal}", list(observations))]

    monkeypatch.setattr(
        "sibyl_core.projection.experience._procedure_segments",
        fragmented_segments,
    )

    report = _report(tmp_path, _wide_trajectory(states=20), content_max_chars=18_000)

    assert report["passed"] is False
    codes = {issue["code"] for issue in report["issues"]}
    assert "aggregate_write_amplification_above_limit" in codes
    assert report["bounds"]["aggregate_write_amplification"] > EXPECTED_MAX_WRITE_AMPLIFICATION
    assert report["counts"]["trajectories_above_write_limit"] == 1


def test_a_runaway_slicer_trips_the_passage_byte_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slicer that emits a passage per line pays the per-passage floor per line."""

    def one_line_slices(body: str) -> tuple[list[Any], Any]:
        entries, stats = slicing.slice_body(body)
        lines = body.split("\n")
        return [
            slicing.Slice([index], lines[index], entry.cut_depth, entry.breadcrumb, "runaway")
            for entry in entries
            for index in entry.line_indices
        ], stats

    monkeypatch.setattr("sibyl_core.projection.experience.slice_body", one_line_slices)

    report = _report(tmp_path, _wide_trajectory(states=4), content_max_chars=18_000)

    assert report["passed"] is False
    codes = {issue["code"] for issue in report["issues"]}
    assert "aggregate_passage_byte_amplification_above_limit" in codes
    assert (
        report["bounds"]["aggregate_passage_byte_amplification"]
        > EXPECTED_MAX_PASSAGE_BYTE_AMPLIFICATION
    )
    assert report["counts"]["trajectories_above_passage_limit"] == 1
    # The inference bound is indifferent to the slicer, which is the split.
    assert report["bounds"]["aggregate_write_amplification"] <= EXPECTED_MAX_WRITE_AMPLIFICATION


def test_passages_that_discard_the_body_they_name_are_caught_under_the_byte_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hollowed rows stay cheap in bytes, so the span check has to catch them."""

    def hollow_slices(body: str) -> tuple[list[Any], Any]:
        entries, stats = slicing.slice_body(body)
        for entry in entries:
            entry.content = entry.content[:1]
        return entries, stats

    monkeypatch.setattr("sibyl_core.projection.experience.slice_body", hollow_slices)

    report = _report(tmp_path, _wide_trajectory(states=2), content_max_chars=18_000)

    assert report["passed"] is False
    assert "passage_not_byte_exact" in {issue["code"] for issue in report["issues"]}
    # The byte bound alone would have called this thrift, which is the point.
    assert (
        report["bounds"]["aggregate_passage_byte_amplification"]
        <= EXPECTED_MAX_PASSAGE_BYTE_AMPLIFICATION
    )


def test_passages_claiming_the_same_parent_lines_twice_are_caught(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicating slicer re-carves nothing; it copies."""

    def doubled_slices(body: str) -> tuple[list[Any], Any]:
        entries, stats = slicing.slice_body(body)
        return [entry for entry in entries for _ in range(2)], stats

    monkeypatch.setattr("sibyl_core.projection.experience.slice_body", doubled_slices)

    report = _report(tmp_path, _wide_trajectory(states=2), content_max_chars=18_000)

    assert report["passed"] is False
    assert "passage_span_overlaps_sibling" in {issue["code"] for issue in report["issues"]}


def test_a_passage_cut_loose_from_its_parent_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passages are excluded from the inference bound, not from provenance."""
    original = projection._passage_projection

    def without_support(*args: Any, **kwargs: Any) -> tuple[list[Any], list[Any]]:
        entities, _ = original(*args, **kwargs)
        return entities, []

    monkeypatch.setattr(projection, "_passage_projection", without_support)

    report = _report(tmp_path, _wide_trajectory(states=2), content_max_chars=18_000)

    assert report["passed"] is False
    assert "passage_without_raw_parent" in {issue["code"] for issue in report["issues"]}


def test_a_passage_re_parented_onto_a_real_but_wrong_row_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existence of a parent is not identity of one.

    The span-partition check validates every span against the DECLARED parent,
    and that check is what earns passages their exclusion from the invention
    bound. Pointing the DERIVED_FROM edge at a different real raw observation
    proves the partition against one row while the graph links to another, and
    used to pass the audit clean.
    """
    original = projection._passage_projection
    seen_targets: list[str] = []

    def re_parented(*args: Any, **kwargs: Any) -> tuple[list[Any], list[Any]]:
        entities, relationships = original(*args, **kwargs)
        for relationship in relationships:
            seen_targets.append(relationship.target_id)
        return entities, relationships

    monkeypatch.setattr(projection, "_passage_projection", re_parented)
    baseline = _report(tmp_path, _wide_trajectory(states=2), content_max_chars=18_000)
    assert baseline["passed"] is True, "the unmodified projection should audit clean"

    # Now swap each passage's declared parent for a different real raw row.
    def mislabelled(*args: Any, **kwargs: Any) -> tuple[list[Any], list[Any]]:
        entities, relationships = original(*args, **kwargs)
        others = [target for target in seen_targets if target]
        for entity in entities:
            declared = str(entity.metadata.get("parent_entity_id"))
            wrong = next((target for target in others if target != declared), None)
            if wrong is not None:
                entity.metadata["parent_entity_id"] = wrong
        return entities, relationships

    monkeypatch.setattr(projection, "_passage_projection", mislabelled)
    report = _report(tmp_path, _wide_trajectory(states=2), content_max_chars=18_000)

    assert report["passed"] is False
    assert "passage_parent_edge_mismatch" in {issue["code"] for issue in report["issues"]}


def _trajectory() -> dict[str, object]:
    return {
        "id": "trajectory-1",
        "domain": "web",
        "environment": "test",
        "goal": "Update the deployment",
        "outcome": "success",
        "start_url": "https://example.test/start",
        "states": [
            {
                "state_index": 0,
                "step": 0,
                "url": "https://example.test/start",
                "action": None,
                "thought": "Open the deployment.",
                "accessibility_tree": "Root\n" + "Initial deployment state\n" * 20,
                "screenshot": None,
            },
            {
                "state_index": 1,
                "step": 1,
                "url": "https://example.test/done",
                "action": "click('Deploy')",
                "thought": "The deployment completed.",
                "accessibility_tree": "Root\n" + "Deployment complete\n" * 20,
                "screenshot": None,
            },
        ],
    }


def _wide_trajectory(*, states: int) -> dict[str, object]:
    """Many acting states, each small enough to stay one evidence part."""
    return {
        "id": "trajectory-wide",
        "domain": "web",
        "environment": "test",
        "goal": "Update the deployment",
        "outcome": "success",
        "start_url": "https://example.test/step-0",
        "states": [
            {
                "state_index": index,
                "step": index,
                "url": f"https://example.test/step-{index}",
                "action": None if index == 0 else f"click('Step {index}')",
                "thought": f"Work through step {index}.",
                "accessibility_tree": "\n".join(
                    [f"section 'Step {index}'"]
                    + [
                        f"\tStaticText 'Step {index} row {row} detail detail detail'"
                        for row in range(60)
                    ]
                ),
                "screenshot": None,
            }
            for index in range(states)
        ],
    }
