"""Question-blind render treatments for the LongMemEval-V2 adapter."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ACTION_SPINE_SCHEMA_VERSION = "sibyl-longmemeval-v2-action-spine-v1"
ACTION_SPINE_FILENAME = "action_spines.jsonl.gz"
CHECKPOINT_ACTION_SPINE_FILENAME = "checkpoint_action_spines.jsonl"

LEVER_CONTEXT_TOTAL_CHARS = "context_total_chars"
LEVER_ENGLISH_LANE_GROUPING = "english_lane_grouping"
LEVER_ACTION_SPINES = "action_spines"

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


def write_action_spines(
    path: Path,
    sidecars: Mapping[str, Mapping[str, object]],
    *,
    compressed: bool,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if compressed:
        with path.open("wb") as raw_handle:
            with gzip.GzipFile(
                filename="",
                fileobj=raw_handle,
                mode="wb",
                mtime=0,
            ) as gzip_handle:
                with io.TextIOWrapper(gzip_handle, encoding="utf-8") as handle:
                    _write_sidecar_lines(handle, sidecars)
        return
    with path.open("w", encoding="utf-8") as handle:
        _write_sidecar_lines(handle, sidecars)


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


def _write_sidecar_lines(handle: Any, sidecars: Mapping[str, Mapping[str, object]]) -> None:
    for trajectory_id in sorted(sidecars):
        sidecar = sidecars[trajectory_id]
        _validate_action_spine(sidecar)
        handle.write(json.dumps(dict(sidecar), sort_keys=True, separators=(",", ":")) + "\n")


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
    "LANE_ORDER",
    "LEVER_ACTION_SPINES",
    "LEVER_CONTEXT_TOTAL_CHARS",
    "LEVER_ENGLISH_LANE_GROUPING",
    "append_action_spines",
    "build_action_spine",
    "file_sha256",
    "group_results_by_lane",
    "read_action_spines",
    "result_lane",
    "write_action_spines",
]
