"""Prepare and score synthetic source-validity experiments without provider calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from sibyl_core.tasks._evidence_json import read_json_value

from .runner import RELATIONS, policy_action, prompt_dependencies, select_prompts, timestamp

_CONFLICTS = frozenset({"contradicted", "superseded"})
_DISPOSITIONS = frozenset({"retain", "review", "retire", "overlay"})


def typed_disposition(relation: str, case: dict[str, Any]) -> str:
    """Apply explicit temporal and claim-kind metadata after legacy eligibility."""
    legacy = policy_action(relation, case)
    if legacy != "retire":
        return legacy
    kind = case.get("claim_kind")
    if kind == "historical":
        return "review"
    as_of = timestamp(case.get("as_of"))
    event_time = timestamp(case.get("event_effective_at"))
    if kind not in {"current_state", "standing_rule"} or as_of is None or event_time is None:
        return "review"
    if event_time > as_of:
        return "retain"
    return _extent_disposition(case, kind, as_of, event_time)


def _extent_disposition(
    case: dict[str, Any], kind: str, as_of: datetime, event_time: datetime
) -> str:
    extent = case.get("event_extent")
    if extent == "permanent":
        return "retire"
    if extent != "temporary":
        return "review"
    valid_until = timestamp(case.get("event_valid_until"))
    if valid_until is None or valid_until <= event_time:
        return "review"
    if as_of >= valid_until or kind == "standing_rule":
        return "retain"
    return "overlay"


def _bytes(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode()


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _load(path: Path) -> Any:
    return read_json_value(path.read_bytes())


def _write(path: Path, value: object) -> None:
    with path.open("xb") as stream:
        stream.write(_bytes(value))


def load_cases(path: Path) -> list[dict[str, Any]]:
    payload = _load(path)
    cases = payload.get("cases") if isinstance(payload, dict) else payload
    if not isinstance(cases, list) or not cases:
        raise ValueError("source cases must be a nonempty list")
    seen = set()
    for case in cases:
        _validate_case(case)
        if case["id"] in seen:
            raise ValueError("source case IDs must be unique")
        seen.add(case["id"])
    return cases


def _validate_case(case: object) -> None:
    if not isinstance(case, dict):
        raise TypeError("source case must be an object")
    text = ("id", "category", "memory", "event", "source_text", "source_authority")
    if any(not isinstance(case.get(key), str) or not case[key] for key in text):
        raise ValueError("source case lacks text identity or evidence")
    if (
        case.get("expected_relation") not in RELATIONS
        or case.get("expected_source_relation") not in RELATIONS
    ):
        raise ValueError("source case has an invalid relation label")
    if case.get("expected_action") not in {"retain", "review", "retire"}:
        raise ValueError("source case has an invalid legacy action")
    if case.get("expected_disposition") not in _DISPOSITIONS:
        raise ValueError("source case has an invalid disposition")
    if typed_disposition(case["expected_source_relation"], case) != case["expected_disposition"]:
        raise ValueError("source case disposition disagrees with typed policy")


def projections(cases: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    summary = [dict(case) for case in cases]
    source = [
        {
            **case,
            "event": case["source_text"],
            "expected_relation": case["expected_source_relation"],
            "expected_action": policy_action(case["expected_source_relation"], case),
        }
        for case in cases
    ]
    return summary, source


def prepare(cases_path: Path, out: Path) -> dict[str, Any]:
    cases = load_cases(cases_path)
    summary, source = projections(cases)
    manifest = {
        "master_sha256": _digest(cases_path.read_bytes()),
        "summary_sha256": _digest(_bytes(summary)),
        "source_sha256": _digest(_bytes(source)),
        "case_count": len(cases),
        "source_study_sha256": _digest(Path(__file__).read_bytes()),
    }
    out.mkdir(parents=True, exist_ok=False)
    _write(out / "summary.json", summary)
    _write(out / "source.json", source)
    _write(out / "manifest.json", manifest)
    return manifest


def _validate_prompt_program(manifest: dict[str, Any], version: str) -> None:
    program = select_prompts(version)
    expected = _digest(Path(str(program.__file__)).read_bytes())
    dependencies = prompt_dependencies(version)
    if manifest.get("prompts_sha256") != expected:
        raise ValueError("run prompt hash does not match the pinned program")
    if manifest.get("prompt_dependencies_sha256") != dependencies:
        raise ValueError("run prompt dependency hashes do not match the pinned program")


def _read_run(
    path: Path, cases: list[dict[str, Any]], *, prompt_version: str, arm: str
) -> tuple[dict[str, Any], dict[tuple[str, int], dict[str, Any]], dict[str, dict[str, Any]]]:
    manifest = _load(path / "manifest.json")
    if not isinstance(manifest, dict):
        raise TypeError("run manifest must be an object")
    if manifest.get("cases_sha256") != _digest(_bytes(cases)):
        raise ValueError("run corpus hash does not match prepared projection")
    if manifest.get("prompt_version") != prompt_version or manifest.get("arms") != [arm]:
        raise ValueError("run prompt version or arm does not match study design")
    _validate_prompt_program(manifest, prompt_version)
    repeats = manifest.get("repeats")
    if type(repeats) is not int or repeats < 1 or manifest.get("case_count") != len(cases):
        raise ValueError("invalid run repeat count or case count")
    predictions = _load(path / "predictions.json")
    if not isinstance(predictions, list):
        raise TypeError("run predictions must be a list")
    expected = {(case["id"], repeat) for case in cases for repeat in range(repeats)}
    by_id = {case["id"]: case for case in cases}
    grid: dict[tuple[str, int], dict[str, Any]] = {}
    baseline: dict[str, dict[str, Any]] = {}
    for row in predictions:
        _validate_prediction(row, by_id)
        case_id = row["case_id"]
        if row["arm"] == "baseline":
            if row.get("repeat") != 0 or case_id in baseline:
                raise ValueError("duplicate or invalid baseline row")
            baseline[case_id] = row
            continue
        key = (case_id, row["repeat"])
        if row["arm"] != arm or key not in expected or key in grid:
            raise ValueError("unexpected or duplicate comparison row")
        grid[key] = row
    if set(grid) != expected or set(baseline) != set(by_id):
        raise ValueError("missing comparison or baseline rows")
    return manifest, grid, baseline


def _validate_prediction(row: object, cases: dict[str, dict[str, Any]]) -> None:
    if not isinstance(row, dict) or row.get("case_id") not in cases:
        raise ValueError("unknown prediction case")
    if (
        type(row.get("repeat")) is not int
        or not isinstance(row.get("status"), str)
        or not isinstance(row.get("arm"), str)
    ):
        raise ValueError("invalid prediction repeat or status")
    case = cases[row["case_id"]]
    for key in ("category", "expected_relation", "expected_action"):
        if row.get(key) != case[key]:
            raise ValueError("prediction labels do not match projection")
    if row["status"] == "completed" and (
        row.get("relation") not in RELATIONS
        or row.get("action") not in {"retain", "review", "retire"}
    ):
        raise ValueError("completed prediction lacks a valid relation or action")
    if row["status"] != "completed" and (
        row.get("relation") is not None or row.get("action") is not None
    ):
        raise ValueError("failed prediction contains an action or relation")


def _method_row(case: dict[str, Any], repeat: int, action: str | None) -> dict[str, Any]:
    return {
        "case_id": case["id"],
        "category": case["category"],
        "repeat": repeat,
        "expected_disposition": case["expected_disposition"],
        "action": action,
        "status": "completed" if action is not None else "measurement_failure",
    }


def _score_pair(
    case: dict[str, Any], repeat: int, summary: dict[str, Any], source: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    summary_relation = summary["relation"] if summary["status"] == "completed" else None
    source_relation = source["relation"] if source["status"] == "completed" else None
    legacy = policy_action(summary_relation, case) if summary_relation is not None else None
    summary_typed = (
        typed_disposition(summary_relation, case) if summary_relation is not None else None
    )
    source_typed = typed_disposition(source_relation, case) if source_relation is not None else None
    gated = summary_typed
    if summary_typed in {"retire", "overlay"}:
        if source_relation is None:
            gated = None
        elif source_relation not in _CONFLICTS:
            gated = "review"
    return {
        name: _method_row(case, repeat, action)
        for name, action in (
            ("summary_legacy", legacy),
            ("summary_typed", summary_typed),
            ("source_typed", source_typed),
            ("gated_typed", gated),
        )
    }


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(rows)
    correct = sum(row["action"] == row["expected_disposition"] for row in rows)
    retired = sum(row["expected_disposition"] == "retire" for row in rows)
    overlays = sum(row["expected_disposition"] == "overlay" for row in rows)
    retire_correct = sum(row["action"] == "retire" == row["expected_disposition"] for row in rows)
    overlay_correct = sum(row["action"] == "overlay" == row["expected_disposition"] for row in rows)
    reviewed = sum(row["action"] == "review" for row in rows)
    return {
        "cases": count,
        "correct": correct,
        "accuracy": correct / count if count else None,
        "measurement_failures": sum(row["status"] != "completed" for row in rows),
        "dangerous_retirement_count": sum(
            row["action"] == "retire" and row["expected_disposition"] != "retire" for row in rows
        ),
        "overlay_errors": sum(
            row["action"] == "overlay" and row["expected_disposition"] != "overlay" for row in rows
        ),
        "gold_retire_cases": retired,
        "retire_captured": retire_correct,
        "retire_recall": retire_correct / retired if retired else None,
        "gold_overlay_cases": overlays,
        "overlay_captured": overlay_correct,
        "overlay_recall": overlay_correct / overlays if overlays else None,
        "review_count": reviewed,
        "review_rate": reviewed / count if count else None,
    }


def _accounting(path: Path, arm: str, manifest: dict[str, Any]) -> dict[str, Any]:
    calls = _load(path / "calls.json")
    batch_size = manifest.get("batch_size")
    if type(batch_size) is not int or batch_size < 1:
        raise ValueError("invalid accounting batch size")
    if not isinstance(calls, list):
        raise TypeError("invalid original call accounting")
    expected = {
        f"{arm}-{repeat}-{offset}"
        for repeat in range(manifest["repeats"])
        for offset in range(0, manifest["case_count"], batch_size)
    }
    observed = set()
    for call in calls:
        _validate_call(call, arm)
        if call["batch"] not in expected or call["batch"] in observed:
            raise ValueError("unexpected or duplicate original call")
        observed.add(call["batch"])
    if observed != expected:
        raise ValueError("missing original call accounting")
    result: dict[str, Any] = {
        "calls": len(calls),
        "physical_attempts": sum(call["attempt_count"] for call in calls),
        "mode": manifest.get("mode", "unknown"),
    }
    for key in ("input_tokens", "output_tokens", "observed_cost_usd"):
        known = [call[key] for call in calls if call.get(key) is not None]
        result[key] = sum(known) if known else None
        result[f"unknown_{key}_calls"] = len(calls) - len(known)
    return result


def _validate_call(call: object, arm: str) -> None:
    if (
        not isinstance(call, dict)
        or call.get("arm") != arm
        or not isinstance(call.get("batch"), str)
    ):
        raise ValueError("invalid original call accounting")
    for key in ("attempt_count", "input_tokens", "output_tokens"):
        value = call.get(key)
        if value is None and key != "attempt_count":
            continue
        if type(value) is not int or value < 0:
            raise ValueError("invalid original token or attempt accounting")
    cost = call.get("observed_cost_usd")
    if cost is not None and (type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0):
        raise ValueError("invalid original cost accounting")


def analyze(cases_path: Path, summary_run: Path, source_run: Path, out: Path) -> dict[str, Any]:
    cases = load_cases(cases_path)
    summary_cases, source_cases = projections(cases)
    summary_manifest, summaries, baseline = _read_run(
        summary_run, summary_cases, prompt_version="v2", arm="decomposed"
    )
    source_manifest, sources, _ = _read_run(
        source_run, source_cases, prompt_version="v3", arm="direct"
    )
    route_policy = summary_manifest.get("route_policy_sha256")
    if (
        not isinstance(route_policy, str)
        or re.fullmatch(r"[0-9a-f]{64}", route_policy) is None
        or route_policy != source_manifest.get("route_policy_sha256")
    ):
        raise ValueError("summary and source runs require the same valid route policy")
    if summary_manifest["repeats"] != source_manifest["repeats"]:
        raise ValueError("summary and source runs require matched repeats")
    methods: dict[str, list[dict[str, Any]]] = {
        name: [] for name in ("summary_legacy", "summary_typed", "source_typed", "gated_typed")
    }
    disagreements = []
    baseline_gaps = []
    for case in cases:
        for repeat in range(summary_manifest["repeats"]):
            key = (case["id"], repeat)
            scored = _score_pair(case, repeat, summaries[key], sources[key])
            for name, row in scored.items():
                methods[name].append(row)
            if summaries[key].get("relation") != sources[key].get("relation"):
                disagreements.append(
                    {
                        "case_id": case["id"],
                        "repeat": repeat,
                        "summary_relation": summaries[key].get("relation"),
                        "source_relation": sources[key].get("relation"),
                        "expected_disposition": case["expected_disposition"],
                        "actions": {name: row["action"] for name, row in scored.items()},
                    }
                )
            base = baseline[case["id"]]
            if base.get("action") != case["expected_disposition"]:
                baseline_gaps.append(
                    {
                        "case_id": case["id"],
                        "repeat": repeat,
                        "proposal": base.get("proposal"),
                        "baseline_action": base.get("action"),
                        "expected_disposition": case["expected_disposition"],
                        "source_typed_action": scored["source_typed"]["action"],
                    }
                )
    result = {
        "master_sha256": _digest(cases_path.read_bytes()),
        "source_study_sha256": _digest(Path(__file__).read_bytes()),
        "input_artifact_sha256": {
            name: {
                filename: _digest((path / filename).read_bytes())
                for filename in ("manifest.json", "predictions.json", "calls.json")
            }
            for name, path in (("summary", summary_run), ("source", source_run))
        },
        "summary_run": str(summary_run.resolve()),
        "source_run": str(source_run.resolve()),
        "summary_projection_sha256": summary_manifest["cases_sha256"],
        "source_projection_sha256": source_manifest["cases_sha256"],
        "methods": {
            name: {
                "pooled": metrics(rows),
                "repeats": {
                    str(repeat): metrics([row for row in rows if row["repeat"] == repeat])
                    for repeat in range(summary_manifest["repeats"])
                },
            }
            for name, rows in methods.items()
        },
        "disagreements": disagreements,
        "baseline_gaps": baseline_gaps,
        "accounting": {
            "summary": _accounting(summary_run, "decomposed", summary_manifest),
            "source": _accounting(source_run, "direct", source_manifest),
        },
        "notes": [
            "Metadata is an oracle fixture, not a model prediction.",
            "Failed measurements remain incorrect in all denominators.",
            "Baseline findings are proposals, not production retirement.",
            "Accounting reflects original requests and is not multiplied by scoring methods.",
        ],
    }
    out.mkdir(parents=True, exist_ok=False)
    _write(out / "analysis.json", result)
    _write(out / "predictions.json", methods)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--cases", type=Path, required=True)
    prepare_parser.add_argument("--out", type=Path, required=True)
    analyze_parser = commands.add_parser("analyze")
    analyze_parser.add_argument("--cases", type=Path, required=True)
    analyze_parser.add_argument("--summary-run", type=Path, required=True)
    analyze_parser.add_argument("--source-run", type=Path, required=True)
    analyze_parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = (
        prepare(args.cases, args.out)
        if args.command == "prepare"
        else analyze(args.cases, args.summary_run, args.source_run, args.out)
    )
    sys.stdout.write(json.dumps(result, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
