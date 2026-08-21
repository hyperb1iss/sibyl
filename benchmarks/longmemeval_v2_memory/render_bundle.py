"""Question-blind render treatments for the LongMemEval-V2 adapter."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ACTION_SPINE_SCHEMA_VERSION = "sibyl-longmemeval-v2-action-spine-v1"
ACTION_SPINE_FILENAME = "action_spines.jsonl.gz"
CHECKPOINT_ACTION_SPINE_FILENAME = "checkpoint_action_spines.jsonl"
DISTILLATION_RECEIPT_FILENAME = "distillation_receipts.jsonl.gz"
CHECKPOINT_DISTILLATION_RECEIPT_FILENAME = "checkpoint_distillation_receipts.jsonl"

LEVER_CONTEXT_TOTAL_CHARS = "reader_char_total"
LEVER_NOTE_KIND_DEDUPE = "note_kind_dedupe"
LEVER_ADDITIVE_NOTE_LANE = "additive_note_lane"
LEVER_ENGLISH_LANE_GROUPING = "plain_english_lanes"
LEVER_ACTION_SPINES = "action_spine"
LEVER_OBSERVED_ABSENCE = "observed_absence"
LEVER_DIGEST_ROLES_BUDGET = "digest_roles_budget"
RENDER_BUNDLE_LEVERS = (
    LEVER_CONTEXT_TOTAL_CHARS,
    LEVER_NOTE_KIND_DEDUPE,
    LEVER_ADDITIVE_NOTE_LANE,
    LEVER_ENGLISH_LANE_GROUPING,
    LEVER_ACTION_SPINES,
    LEVER_OBSERVED_ABSENCE,
    LEVER_DIGEST_ROLES_BUDGET,
)

LANE_DISTILLED = "Distilled operational notes"
LANE_SOURCE = "Retrieved source evidence"
LANE_SUPPORT = "Supporting neighboring states"
LANE_ACTION_SPINE = "Action spines"
LANE_ORDER = (LANE_DISTILLED, LANE_SOURCE, LANE_SUPPORT, LANE_ACTION_SPINE)

_ACTION_TARGET_RE = re.compile(
    r"\b(?:click|fill|select_option|press|hover|check|uncheck)"
    r"\(\s*(?P<quote>['\"])(?P<target>[^'\"]+)(?P=quote)"
)
_ACCESSIBILITY_ID_RE = re.compile(r"^\s*\[(?P<target>[^\]]+)\]\s+(?P<node>.+?)\s*$")


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def build_action_spine(trajectory: Mapping[str, object]) -> dict[str, object] | None:
    """Build one deterministic, question-blind action summary for a trajectory."""
    trajectory_id = _clean(trajectory.get("id"))
    if not trajectory_id:
        raise ValueError("action spine requires a trajectory id")
    states = trajectory.get("states")
    if not isinstance(states, list):
        raise ValueError("action spine requires trajectory states")

    lines = [f"Trajectory action spine: {trajectory_id}"]
    goal = _clean(trajectory.get("goal"))
    if goal:
        lines.append(f"Goal: {goal}")
    action_count = 0
    annotated_action_count = 0
    unresolved_target_count = 0
    for ordinal, state in enumerate(states, start=1):
        if not isinstance(state, Mapping):
            continue
        action = _clean(state.get("action"))
        if not action:
            continue
        action_count += 1
        target_match = _ACTION_TARGET_RE.search(action)
        annotation = None
        if target_match is not None:
            target = target_match.group("target")
            annotation = _accessibility_nodes(state).get(target)
            if annotation is None:
                unresolved_target_count += 1
        if annotation is not None:
            annotated_action_count += 1
            lines.append(f"{ordinal}. {action}  # [{target}] {annotation}")
        else:
            lines.append(f"{ordinal}. {action}")
    if action_count == 0:
        return None

    content = "\n".join(lines)
    unsigned: dict[str, object] = {
        "schema_version": ACTION_SPINE_SCHEMA_VERSION,
        "trajectory_id": trajectory_id,
        "content": content,
        "action_count": action_count,
        "annotated_action_count": annotated_action_count,
        "unresolved_target_count": unresolved_target_count,
    }
    return {**unsigned, "action_spine_sha256": canonical_sha256(unsigned)}


def group_results_by_lane(
    results: Sequence[dict[str, object]],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Group selected members by reader-facing lane without reordering a lane."""
    grouped: dict[str, list[dict[str, object]]] = {lane: [] for lane in LANE_ORDER}
    original_ids = [_result_id(result) for result in results]
    for result in results:
        lane = result_lane(result)
        candidate = dict(result)
        candidate["_render_lane_title"] = lane
        grouped[lane].append(candidate)
    ordered = [candidate for lane in LANE_ORDER for candidate in grouped[lane]]
    grouped_ids = [_result_id(result) for result in ordered]
    return ordered, {
        "enabled": True,
        "lane_order": list(LANE_ORDER),
        "lane_item_counts": {lane: len(grouped[lane]) for lane in LANE_ORDER},
        "nonempty_lane_count": sum(bool(grouped[lane]) for lane in LANE_ORDER),
        "membership_preserved": Counter(original_ids) == Counter(grouped_ids),
        "within_lane_order_preserved": all(
            [_result_id(item) for item in grouped[lane]]
            == [_result_id(item) for item in results if result_lane(item) == lane]
            for lane in LANE_ORDER
        ),
    }


def append_action_spines(
    results: Sequence[dict[str, object]],
    *,
    sidecars: Mapping[str, Mapping[str, object]],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Append at most one sidecar for each selected trajectory, in first-seen order."""
    trajectory_ids: list[str] = []
    seen: set[str] = set()
    for result in results:
        metadata = result.get("metadata")
        trajectory_id = (
            _clean(metadata.get("longmemeval_v2_trajectory_id"))
            if isinstance(metadata, Mapping)
            else ""
        )
        if trajectory_id and trajectory_id not in seen:
            seen.add(trajectory_id)
            trajectory_ids.append(trajectory_id)

    appended: list[dict[str, object]] = []
    missing: list[str] = []
    for trajectory_id in trajectory_ids:
        sidecar = sidecars.get(trajectory_id)
        if sidecar is None:
            missing.append(trajectory_id)
            continue
        _validate_action_spine(sidecar)
        appended.append(
            {
                "id": f"action-spine:{trajectory_id}",
                "type": "action_spine",
                "content": str(sidecar["content"]),
                "metadata": {
                    "longmemeval_v2_trajectory_id": trajectory_id,
                    "action_spine_sha256": sidecar["action_spine_sha256"],
                    "action_count": sidecar["action_count"],
                    "annotated_action_count": sidecar["annotated_action_count"],
                },
                "_selection_origin": "action_spine",
            }
        )
    return [*results, *appended], {
        "enabled": True,
        "selected_trajectory_count": len(trajectory_ids),
        "available_spine_count": len(trajectory_ids) - len(missing),
        "appended_spine_count": len(appended),
        "missing_trajectory_ids": missing,
        "annotated_action_count": sum(
            int(sidecars[trajectory_id]["annotated_action_count"])
            for trajectory_id in trajectory_ids
            if trajectory_id in sidecars
        ),
    }


def result_lane(result: Mapping[str, object]) -> str:
    origin = _clean(result.get("_selection_origin"))
    result_type = _clean(result.get("type"))
    metadata = result.get("metadata")
    projection_kind = (
        _clean(metadata.get("projection_kind")) if isinstance(metadata, Mapping) else ""
    )
    if origin == "action_spine" or result_type == "action_spine":
        return LANE_ACTION_SPINE
    if origin.startswith("context_pack:") or projection_kind == "distilled_note":
        return LANE_DISTILLED
    if origin in {"neighbor", "state_part", "state_part_refinement", "traversal"}:
        return LANE_SUPPORT
    return LANE_SOURCE


def screen_context_composition_receipt(
    composition: Mapping[str, object] | None,
) -> dict[str, dict[str, object]]:
    """Screen production note-selection levers without reading benchmark scores."""
    blocked = {
        "status": "blocked_missing_treatment_artifact",
        "survives": False,
        "activity_events": 0,
    }
    screens = {
        LEVER_NOTE_KIND_DEDUPE: dict(blocked),
        LEVER_ADDITIVE_NOTE_LANE: dict(blocked),
    }
    if not isinstance(composition, Mapping):
        return screens
    activity = composition.get("activity_receipt")
    if not isinstance(activity, Mapping):
        return screens

    note_dedupe = activity.get("note_dedupe")
    if (
        composition.get("operational_note_dedupe_mode") == "source_kind"
        and isinstance(note_dedupe, Mapping)
        and note_dedupe.get("mode") == "source_kind"
    ):
        source_count = _exact_nonnegative_int(note_dedupe.get("duplicate_source_count"))
        source_kind_count = _exact_nonnegative_int(note_dedupe.get("duplicate_source_kind_count"))
        if source_count is not None and source_kind_count is not None:
            rescued = max(0, source_count - source_kind_count)
            screens[LEVER_NOTE_KIND_DEDUPE] = {
                "status": "survived" if rescued else "blocked_no_treatment_activity",
                "survives": rescued > 0,
                "activity_events": rescued,
                "duplicate_source_count": source_count,
                "duplicate_source_kind_count": source_kind_count,
            }

    additive = activity.get("additive_note_lane")
    parity = activity.get("raw_parity")
    hard_budget = activity.get("hard_budget")
    if (
        composition.get("operational_note_lane_mode") == "additive"
        and isinstance(additive, Mapping)
        and isinstance(parity, Mapping)
        and isinstance(hard_budget, Mapping)
    ):
        admitted_ids = additive.get("admitted_note_ids")
        parity_ok = parity.get("membership_equal") is True and parity.get("order_equal") is True
        hard_budget_ok = (
            hard_budget.get("mode") == "characters"
            and _exact_positive_int(hard_budget.get("limit")) is not None
            and _exact_nonnegative_int(hard_budget.get("selected")) is not None
            and hard_budget.get("within") is True
        )
        if isinstance(admitted_ids, list) and all(
            isinstance(value, str) and value for value in admitted_ids
        ):
            screens[LEVER_ADDITIVE_NOTE_LANE] = {
                "status": (
                    "survived"
                    if admitted_ids and parity_ok and hard_budget_ok
                    else (
                        "blocked_raw_parity"
                        if not parity_ok
                        else (
                            "blocked_hard_budget"
                            if not hard_budget_ok
                            else "blocked_no_treatment_activity"
                        )
                    )
                ),
                "survives": bool(admitted_ids) and parity_ok and hard_budget_ok,
                "activity_events": len(admitted_ids) if parity_ok and hard_budget_ok else 0,
                "admitted_note_ids": list(admitted_ids),
                "raw_membership_equal": parity.get("membership_equal"),
                "raw_order_equal": parity.get("order_equal"),
                "hard_budget": dict(hard_budget),
            }
    return screens


def screen_distillation_receipts(
    receipts: Mapping[str, Mapping[str, object]],
) -> dict[str, dict[str, object]]:
    """Screen raw render-v1 distillation receipts without model or score access."""
    blocked = {
        "status": "blocked_missing_treatment_artifact",
        "survives": False,
        "activity_events": 0,
    }
    screens = {
        LEVER_OBSERVED_ABSENCE: dict(blocked),
        LEVER_DIGEST_ROLES_BUDGET: dict(blocked),
    }
    if not receipts:
        return screens

    absence_activity = 0
    digest_activity = 0
    invalid_absence: list[str] = []
    invalid_digest: list[str] = []
    for source_id, receipt in sorted(receipts.items()):
        if receipt.get("profile") != "render_v1":
            invalid_absence.append(source_id)
            invalid_digest.append(source_id)
            continue
        absence = receipt.get("observed_absence")
        if not _valid_observed_absence_receipt(absence):
            invalid_absence.append(source_id)
        elif isinstance(absence, Mapping):
            absence_activity += int(absence["admitted_count"])
        digest = receipt.get("digest")
        render = receipt.get("render")
        if not _valid_digest_render_receipt(digest, render):
            invalid_digest.append(source_id)
        elif isinstance(digest, Mapping):
            digest_activity += int(digest["admitted_line_count"])

    if invalid_absence:
        screens[LEVER_OBSERVED_ABSENCE] = {
            "status": "blocked_invalid_treatment_artifact",
            "survives": False,
            "activity_events": 0,
            "invalid_source_ids": invalid_absence,
        }
    else:
        screens[LEVER_OBSERVED_ABSENCE] = {
            "status": "survived" if absence_activity else "blocked_no_treatment_activity",
            "survives": absence_activity > 0,
            "activity_events": absence_activity,
            "receipt_count": len(receipts),
        }
    if invalid_digest:
        screens[LEVER_DIGEST_ROLES_BUDGET] = {
            "status": "blocked_invalid_treatment_artifact",
            "survives": False,
            "activity_events": 0,
            "invalid_source_ids": invalid_digest,
        }
    else:
        screens[LEVER_DIGEST_ROLES_BUDGET] = {
            "status": "survived" if digest_activity else "blocked_no_treatment_activity",
            "survives": digest_activity > 0,
            "activity_events": digest_activity,
            "receipt_count": len(receipts),
        }
    return screens


def _valid_observed_absence_receipt(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    proposed = _exact_nonnegative_int(value.get("proposed_count"))
    admitted = _exact_nonnegative_int(value.get("admitted_count"))
    rejected = _exact_nonnegative_int(value.get("rejected_count"))
    proposals = value.get("proposals")
    if (
        proposed is None
        or admitted is None
        or rejected is None
        or not isinstance(proposals, list)
        or proposed != admitted + rejected
        or proposed != len(proposals)
    ):
        return False
    for proposal in proposals:
        if not isinstance(proposal, Mapping):
            return False
        status = proposal.get("status")
        if status not in {"admitted", "rejected"}:
            return False
        inventory_complete = proposal.get("inventory_complete")
        rejection_reasons = proposal.get("inventory_rejection_reasons")
        if not isinstance(inventory_complete, bool) or not isinstance(rejection_reasons, list):
            return False
        if status == "admitted" and (
            inventory_complete is not True or proposal.get("reason") != "complete_inventory"
        ):
            return False
    return True


def _valid_digest_render_receipt(digest: object, render: object) -> bool:
    if not isinstance(digest, Mapping) or not isinstance(render, Mapping):
        return False
    roles = digest.get("roles")
    budget = digest.get("configured_budget")
    candidate_lines = _exact_nonnegative_int(digest.get("candidate_line_count"))
    admitted_lines = _exact_nonnegative_int(digest.get("admitted_line_count"))
    digest_chars = _exact_nonnegative_int(digest.get("digest_chars"))
    render_chars = _exact_nonnegative_int(render.get("chars"))
    render_lines = _exact_nonnegative_int(render.get("lines"))
    max_note_chars = _exact_positive_int(render.get("max_note_chars"))
    if (
        not isinstance(roles, list)
        or not roles
        or not all(isinstance(role, str) and role for role in roles)
        or not isinstance(budget, Mapping)
        or not all(
            _exact_positive_int(budget.get(key)) is not None
            for key in ("digest_chars", "lines_per_observation", "lines_total", "line_chars")
        )
        or candidate_lines is None
        or admitted_lines is None
        or admitted_lines > candidate_lines
        or digest_chars is None
        or digest_chars > int(budget["digest_chars"])
        or render_chars is None
        or render_lines is None
        or max_note_chars is None
        or digest.get("within_digest_char_budget") is not True
        or digest.get("within_line_budget") is not True
        or render.get("within_note_char_budget") is not True
    ):
        return False
    notes = render.get("notes")
    return isinstance(notes, list) and all(isinstance(note, Mapping) for note in notes)


def _exact_nonnegative_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _exact_positive_int(value: object) -> int | None:
    normalized = _exact_nonnegative_int(value)
    return normalized if normalized is not None and normalized > 0 else None


def write_action_spines(
    path: Path,
    sidecars: Mapping[str, Mapping[str, object]],
    *,
    compressed: bool,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    if compressed:
        with temporary.open("wb") as raw_handle:
            with gzip.GzipFile(
                filename="",
                fileobj=raw_handle,
                mode="wb",
                mtime=0,
            ) as gzip_handle:
                with io.TextIOWrapper(gzip_handle, encoding="utf-8") as handle:
                    _write_sidecar_lines(handle, sidecars)
            raw_handle.flush()
            os.fsync(raw_handle.fileno())
    else:
        with temporary.open("w", encoding="utf-8") as handle:
            _write_sidecar_lines(handle, sidecars)
            handle.flush()
            os.fsync(handle.fileno())
    temporary.replace(path)


def read_action_spines(path: Path, *, compressed: bool) -> dict[str, dict[str, object]]:
    sidecars: dict[str, dict[str, object]] = {}
    if compressed:
        handle_context = gzip.open(path, mode="rt", encoding="utf-8")
    else:
        handle_context = path.open(mode="r", encoding="utf-8")
    with handle_context as handle:
        for line in handle:
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("action-spine row must be an object")
            _validate_action_spine(value)
            trajectory_id = str(value["trajectory_id"])
            if trajectory_id in sidecars:
                raise ValueError(f"duplicate action spine for {trajectory_id}")
            sidecars[trajectory_id] = value
    return sidecars


def write_distillation_receipts(
    path: Path,
    receipts: Mapping[str, Mapping[str, object]],
    *,
    compressed: bool,
) -> None:
    """Persist raw production receipts as a deterministic, tamper-evident sidecar."""
    rows: dict[str, dict[str, object]] = {}
    for source_id, receipt in receipts.items():
        clean_source_id = _clean(source_id)
        if not clean_source_id:
            raise ValueError("distillation receipt requires a source id")
        unsigned = {"source_id": clean_source_id, "distillation_receipt": dict(receipt)}
        rows[clean_source_id] = {
            **unsigned,
            "distillation_receipt_sha256": canonical_sha256(unsigned),
        }
    _write_json_rows(path, rows, compressed=compressed)


def read_distillation_receipts(
    path: Path,
    *,
    compressed: bool,
) -> dict[str, dict[str, object]]:
    rows = _read_json_rows(path, compressed=compressed)
    receipts: dict[str, dict[str, object]] = {}
    for source_id, row in rows.items():
        digest = row.get("distillation_receipt_sha256")
        unsigned = {
            key: value for key, value in row.items() if key != "distillation_receipt_sha256"
        }
        if digest != canonical_sha256(unsigned):
            raise ValueError("distillation receipt digest does not bind its content")
        receipt = row.get("distillation_receipt")
        if not isinstance(receipt, dict):
            raise ValueError("distillation receipt row is missing its production receipt")
        receipts[source_id] = receipt
    return receipts


def _write_sidecar_lines(handle: Any, sidecars: Mapping[str, Mapping[str, object]]) -> None:
    for trajectory_id in sorted(sidecars):
        sidecar = sidecars[trajectory_id]
        _validate_action_spine(sidecar)
        handle.write(json.dumps(dict(sidecar), sort_keys=True, separators=(",", ":")) + "\n")


def _write_json_rows(
    path: Path,
    rows: Mapping[str, Mapping[str, object]],
    *,
    compressed: bool,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    if compressed:
        with temporary.open("wb") as raw_handle:
            with gzip.GzipFile(
                filename="",
                fileobj=raw_handle,
                mode="wb",
                mtime=0,
            ) as gzip_handle:
                with io.TextIOWrapper(gzip_handle, encoding="utf-8") as handle:
                    _write_json_row_lines(handle, rows)
            raw_handle.flush()
            os.fsync(raw_handle.fileno())
    else:
        with temporary.open("w", encoding="utf-8") as handle:
            _write_json_row_lines(handle, rows)
            handle.flush()
            os.fsync(handle.fileno())
    temporary.replace(path)


def _write_json_row_lines(handle: Any, rows: Mapping[str, Mapping[str, object]]) -> None:
    for key in sorted(rows):
        handle.write(json.dumps(dict(rows[key]), sort_keys=True, separators=(",", ":")) + "\n")


def _read_json_rows(path: Path, *, compressed: bool) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    handle_context = (
        gzip.open(path, mode="rt", encoding="utf-8")
        if compressed
        else path.open(mode="r", encoding="utf-8")
    )
    with handle_context as handle:
        for line in handle:
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("receipt row must be an object")
            source_id = _clean(value.get("source_id"))
            if not source_id:
                raise ValueError("receipt row requires a source id")
            if source_id in rows:
                raise ValueError(f"duplicate receipt for {source_id}")
            rows[source_id] = value
    return rows


def _validate_action_spine(sidecar: Mapping[str, object]) -> None:
    if sidecar.get("schema_version") != ACTION_SPINE_SCHEMA_VERSION:
        raise ValueError("action-spine schema is invalid")
    digest = sidecar.get("action_spine_sha256")
    unsigned = {key: value for key, value in sidecar.items() if key != "action_spine_sha256"}
    if digest != canonical_sha256(unsigned):
        raise ValueError("action-spine digest does not bind its content")


def _accessibility_nodes(state: Mapping[str, object]) -> dict[str, str]:
    tree = _clean_multiline(state.get("accessibility_tree"))
    nodes: dict[str, str] = {}
    for line in tree.splitlines():
        match = _ACCESSIBILITY_ID_RE.match(line)
        if match is not None:
            nodes.setdefault(match.group("target"), match.group("node").strip())
    return nodes


def _result_id(result: Mapping[str, object]) -> str:
    return _clean(result.get("id"))


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _clean_multiline(value: object) -> str:
    return str(value or "").strip()


__all__ = [
    "ACTION_SPINE_FILENAME",
    "CHECKPOINT_ACTION_SPINE_FILENAME",
    "CHECKPOINT_DISTILLATION_RECEIPT_FILENAME",
    "DISTILLATION_RECEIPT_FILENAME",
    "LANE_ORDER",
    "LEVER_ADDITIVE_NOTE_LANE",
    "LEVER_ACTION_SPINES",
    "LEVER_CONTEXT_TOTAL_CHARS",
    "LEVER_DIGEST_ROLES_BUDGET",
    "LEVER_ENGLISH_LANE_GROUPING",
    "LEVER_NOTE_KIND_DEDUPE",
    "LEVER_OBSERVED_ABSENCE",
    "RENDER_BUNDLE_LEVERS",
    "append_action_spines",
    "build_action_spine",
    "file_sha256",
    "group_results_by_lane",
    "read_action_spines",
    "read_distillation_receipts",
    "result_lane",
    "screen_context_composition_receipt",
    "screen_distillation_receipts",
    "write_action_spines",
    "write_distillation_receipts",
]
