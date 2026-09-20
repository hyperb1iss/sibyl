"""Freeze a source-support fallback policy on calibration, then score held-out receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

from sibyl_core.tasks._evidence_json import canonical, read_json_value

from . import support_fallback_policy as policy
from . import support_inputs, support_study
from .runner import _sha, _write

HISTORICAL_RUNNER_SHA = "8020c00ba15f487695961ffa5ad8761d015a52b331ef6e3ad78c66c44a37f96a"
_RUNNER_KEY = "benchmark/support_study.py"
_REPEATS = 2


def _digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _read(path: Path) -> Any:
    return read_json_value(path.read_bytes())


def _semantic_fingerprint(case: dict[str, Any]) -> str:
    """Ignore fixture identities and gold; detect reordered, renamed duplicate evidence."""

    def text(value: str) -> str:
        return " ".join(unicodedata.normalize("NFC", value).split())

    return _digest(
        {
            "content": text(case["content"]),
            "claims": sorted(text(claim) for claim in case["claims"]),
            "sources": sorted(
                (text(source["text"]), source["provenance"]) for source in case["sources"]
            ),
        }
    )


def _validate_manifest(
    cases_path: Path,
    cases: list[dict[str, Any]],
    manifest: dict[str, Any],
    calls: list[support_study.ScheduledCall],
    historical_runner_sha: str | None,
) -> None:
    if manifest.get("repeats") != _REPEATS or type(manifest.get("repeats")) is not int:
        raise ValueError("fallback qualification requires exactly two repeats")
    current = support_study._manifest(
        cases_path,
        cases,
        calls,
        run_id=manifest["run_id"],
        repeats=_REPEATS,
        seed=manifest["seed"],
        concurrency=manifest["concurrency"],
        mode=manifest["mode"],
    )
    if historical_runner_sha is not None:
        if historical_runner_sha != HISTORICAL_RUNNER_SHA:
            raise ValueError("unrecognized historical runner exception")
        if manifest.get("code_hashes", {}).get(_RUNNER_KEY) != historical_runner_sha:
            raise ValueError("historical runner exception does not match archived runner")
        current["code_hashes"][_RUNNER_KEY] = historical_runner_sha
    support_study._validate_replay(current, manifest)


def _load_run(
    cases_path: Path,
    run_path: Path,
    *,
    historical_runner_sha: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    cases = support_inputs.load_cases(cases_path)
    manifest = _read(run_path / "manifest.json")
    calls = support_study.schedule(
        cases, manifest["run_id"], repeats=_REPEATS, seed=manifest["seed"]
    )
    _validate_manifest(cases_path, cases, manifest, calls, historical_runner_sha)
    if _read(run_path / "schedule.json") != [call.identity() for call in calls]:
        raise ValueError("archived schedule differs from frozen geometry")
    archived = support_study._replay_calls(run_path, calls)
    rows = []
    for arm in support_study.ARMS:
        for case in cases:
            for repeat in range(_REPEATS):
                selected = [
                    row
                    for row in archived.values()
                    if (row["arm"], row["case_id"], row["repeat"]) == (arm, case["id"], repeat)
                ]
                rows.append({**policy.candidate_row(case, repeat, selected), "arm": arm})
    bindings = {
        "cases_sha256": _sha(cases_path),
        "run_id": manifest["run_id"],
        "route_policy_sha256": manifest["route_policy_sha256"],
        "program_hashes": manifest["program_hashes"],
        "current_code_hashes": support_study._code_hashes(),
        "historical_runner_exception": historical_runner_sha,
        "artifact_hashes": {
            name: _sha(run_path / name) for name in ("manifest.json", "calls.json", "schedule.json")
        },
        "receipt_hashes": {
            call.key: _sha(run_path / "receipts" / f"{call.key}.json") for call in calls
        },
        "case_fingerprints": {case["id"]: _semantic_fingerprint(case) for case in cases},
        "accounting": {
            arm: support_study._accounting([row for row in archived.values() if row["arm"] == arm])
            for arm in support_study.ARMS
        },
    }
    return cases, rows, bindings


def _rate(count: int, denominator: int) -> float | None:
    return count / denominator if denominator else None


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    unsafe = [row for row in rows if not row["gold_supported"]]
    safe = [row for row in rows if row["gold_supported"]]
    bypass = [row for row in rows if row["action"] == "bypass"]
    false = [row for row in bypass if not row["gold_supported"]]
    return {
        "candidate_evaluations": len(rows),
        "complete": sum(row["complete"] for row in rows),
        "bypassed": len(bypass),
        "fallback": len(rows) - len(bypass),
        "safe_candidate_evaluations": len(safe),
        "unsafe_candidate_evaluations": len(unsafe),
        "false_bypass": len(false),
        "false_bypass_rate": _rate(len(false), len(unsafe)),
        "bypass_precision": _rate(len(bypass) - len(false), len(bypass)),
        "safe_bypass_coverage": _rate(len(bypass) - len(false), len(safe)),
        "fallback_reasons": dict(
            sorted(Counter(row["reason"] for row in rows if row["action"] != "bypass").items())
        ),
    }


def _distinct(rows: list[dict[str, Any]]) -> dict[str, int]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["case_id"], []).append(row)
    if any(
        {row["repeat"] for row in group} != {0, 1} or len(group) != _REPEATS
        for group in grouped.values()
    ):
        raise ValueError("distinct metrics require exact two-repeat candidate coverage")
    return {
        "cases": len(grouped),
        "unsafe_cases": sum(not group[0]["gold_supported"] for group in grouped.values()),
        "safe_cases": sum(group[0]["gold_supported"] for group in grouped.values()),
        "false_bypass_any_repeat": sum(
            not group[0]["gold_supported"] and any(row["action"] == "bypass" for row in group)
            for group in grouped.values()
        ),
        "false_bypass_both_repeats": sum(
            not group[0]["gold_supported"] and all(row["action"] == "bypass" for row in group)
            for group in grouped.values()
        ),
        "safe_bypass_both_repeats": sum(
            group[0]["gold_supported"] and all(row["action"] == "bypass" for row in group)
            for group in grouped.values()
        ),
        "safe_bypass_any_repeat": sum(
            group[0]["gold_supported"] and any(row["action"] == "bypass" for row in group)
            for group in grouped.values()
        ),
    }


def score(
    rows: list[dict[str, Any]],
    accounting: dict[str, Any],
    threshold: float | None,
) -> dict[str, Any]:
    methods = {}
    for arm in support_study.ARMS:
        for guarded in (True, False):
            routed: list[dict[str, Any]] = []
            for row in rows:
                if row["arm"] == arm:
                    action, reason = policy.route(row, threshold, numeric_guard=guarded)
                    routed.append({**row, "action": action, "reason": reason})
            pooled = _metrics(routed)
            cost = accounting[arm]["observed_cost_usd"]
            known = accounting[arm]["unknown_observed_cost_usd_calls"] == 0
            methods[f"{arm}_{'guarded' if guarded else 'confidence_only'}"] = {
                "economic_status": "conditional_non_activation",
                "role": "primary"
                if arm == "grouped" and guarded
                else "ablation"
                if arm == "grouped"
                else "diagnostic",
                "pooled": pooled,
                "distinct": _distinct(routed),
                "repeats": {
                    str(repeat): _metrics([row for row in routed if row["repeat"] == repeat])
                    for repeat in range(_REPEATS)
                },
                "multi_assertion": _metrics([row for row in routed if row["assertion_count"] > 1]),
                "multi_assertion_distinct": _distinct(
                    [row for row in routed if row["assertion_count"] > 1]
                ),
                "single_assertion": _metrics(
                    [row for row in routed if row["assertion_count"] == 1]
                ),
                "accounting": accounting[arm],
                "conditional_break_even_support_check_cost_usd": cost / pooled["bypassed"]
                if known and cost is not None and pooled["bypassed"] and not pooled["false_bypass"]
                else None,
                "candidate_outcomes": routed,
            }
    return {
        "threshold": threshold,
        "methods": methods,
        "limits": [
            "Synthetic diagnostics with correlated assertions and two repeated calls, not independent samples.",
            "All routing calls are charged, including fallbacks and failures with observed cost.",
            "Break-even divides total observed Jev cost by bypassed candidate evaluations; support-check cost and actual savings are unmeasured. Not estimated when observed false bypass exists.",
            "The numeric guard detects explicit tokens, not mathematical correctness; confidence is not calibrated probability.",
            "Bypass is a simulated routing decision, never publication authority or evidence of whole-critic equivalence.",
        ],
    }


def _code_binding() -> dict[str, str]:
    return {
        "policy_version": policy.POLICY_VERSION,
        "policy_sha256": _sha(Path(policy.__file__)),
        "analysis_sha256": _sha(Path(__file__)),
    }


def calibrate(
    cases_path: Path,
    run_path: Path,
    out: Path,
    *,
    holdout_cases_sha: str,
    historical_runner_sha: str | None = None,
) -> dict[str, Any]:
    if re.fullmatch(r"[a-f0-9]{64}", holdout_cases_sha) is None:
        raise ValueError("holdout fixture hash must be frozen before calibration")
    _, rows, bindings = _load_run(cases_path, run_path, historical_runner_sha=historical_runner_sha)
    selected = policy.select_threshold([row for row in rows if row["arm"] == "grouped"])
    selection = {
        **_code_binding(),
        "holdout_cases_sha256": holdout_cases_sha,
        "calibration_cases_path": str(cases_path.resolve()),
        "calibration_run_path": str(run_path.resolve()),
        "calibration": bindings,
        "candidate_rows": rows,
        "threshold": selected["threshold"],
        "threshold_table": selected["table"],
    }
    selection["selection_sha256"] = _digest(selection)
    out.mkdir(parents=True, exist_ok=False)
    _write(out / "selection.json", selection)
    _write(out / "calibration.json", score(rows, bindings["accounting"], selected["threshold"]))
    return selection


def _validate_selection(selection: dict[str, Any]) -> None:
    payload = {key: value for key, value in selection.items() if key != "selection_sha256"}
    if selection.get("selection_sha256") != _digest(payload):
        raise ValueError("selection content hash mismatch")
    if any(selection.get(key) != value for key, value in _code_binding().items()):
        raise ValueError("selection policy or analysis code changed")
    _, rows, bindings = _load_run(
        Path(selection["calibration_cases_path"]),
        Path(selection["calibration_run_path"]),
        historical_runner_sha=selection["calibration"]["historical_runner_exception"],
    )
    if bindings != selection["calibration"] or rows != selection["candidate_rows"]:
        raise ValueError("calibration inputs changed after selection")
    selected = policy.select_threshold([row for row in rows if row["arm"] == "grouped"])
    if selected != {"threshold": selection["threshold"], "table": selection["threshold_table"]}:
        raise ValueError("selected threshold differs from frozen calibration rule")


def evaluate(cases_path: Path, run_path: Path, selection_path: Path, out: Path) -> dict[str, Any]:
    selection = _read(selection_path)
    _validate_selection(selection)
    if _sha(cases_path) != selection["holdout_cases_sha256"]:
        raise ValueError("held-out fixture differs from frozen hash")
    _, rows, bindings = _load_run(cases_path, run_path)
    calibration = selection["calibration"]
    for key in ("program_hashes", "route_policy_sha256", "current_code_hashes"):
        if bindings[key] != calibration[key]:
            raise ValueError(f"held-out control mismatch: {key}")
    left, right = calibration["case_fingerprints"], bindings["case_fingerprints"]
    if set(left) & set(right) or set(left.values()) & set(right.values()):
        raise ValueError("calibration and held-out cases overlap")
    result = {
        **_code_binding(),
        "selection_file_sha256": _sha(selection_path),
        "selection_sha256": selection["selection_sha256"],
        "holdout": bindings,
        "candidate_rows": rows,
        **score(rows, bindings["accounting"], selection["threshold"]),
    }
    out.mkdir(parents=True, exist_ok=False)
    _write(out / "evaluation.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("calibrate", "evaluate"):
        child = subparsers.add_parser(name)
        child.add_argument("--cases", type=Path, required=True)
        child.add_argument("--run", type=Path, required=True)
        child.add_argument("--out", type=Path, required=True)
        if name == "calibrate":
            child.add_argument("--holdout-cases-sha", required=True)
            child.add_argument("--historical-runner-sha")
        else:
            child.add_argument("--selection", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "calibrate":
        result = calibrate(
            args.cases,
            args.run,
            args.out,
            holdout_cases_sha=args.holdout_cases_sha,
            historical_runner_sha=args.historical_runner_sha,
        )
    else:
        result = evaluate(args.cases, args.run, args.selection, args.out)
    sys.stdout.write(json.dumps(result, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
