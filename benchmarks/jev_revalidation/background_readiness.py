"""Offline, theoretical no-wait receipt scenarios over a fully validated archive."""

from __future__ import annotations

import argparse
import asyncio
import math
import statistics
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from sibyl_core.ai.decisions import DecisionObservation, DecisionRequest
from sibyl_core.ai.openrouter_decisions import DECISIONS_ENDPOINT, OpenRouterDecisionRoute
from sibyl_core.tasks._evidence_json import canonical, read_json_value

from . import background_policy, quality_speed, support_inputs
from .runner import _percentile, _sha, _write, timestamp

VERSION = "jev-background-readiness-v1"
LEADS_MS = (0, 50, 100, 150, 200, 250, 500, 1000)
LIMITS = [
    "Theoretical conditional replay, not a measured background scheduler or production qualification.",
    "No pre-validation overlap was recorded. Every finite lead is an explicit scenario assumption.",
    "Readiness uses the measured Jev preparation/transport/interpretation prefix; durable storage, "
    "current-authority lookup, receipt lookup and scheduler delays were not measured.",
    "Foreground bypass time is a zero-overhead lower bound. Fallback reuses the paired baseline "
    "critic sample, with no change in provider contention or queueing modeled.",
    "Every scenario pays for all background Jev calls, including late and unused results. "
    "Background work is not canceled after a foreground decision.",
    "Scenarios assume current authorization and identical request/source state at the consumer. "
    "No live authorization, source-change frequency or protected receipt readiness was measured.",
    "All-ready is an ideal ceiling without an invented finite lead or candidate-origin time.",
    "Synthetic plain reflections and two dependent repeats do not establish general critic "
    "equivalence, actual memory readiness, or downstream task quality.",
]


def _money(value: Any) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError("all call costs must be known")
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("all call costs must be finite and nonnegative")
    return result


def _duration(value: Any) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("finite nonnegative timing required")
    return float(value)


def _prefix(
    archive: Path, entry: dict[str, Any], row: dict[str, Any], raw: dict[str, Any]
) -> float:
    start, end = timestamp(row["started_at"]), timestamp(raw["completed_at"])
    dispatch = timestamp(raw["dispatch_started_at"])
    finished = timestamp(row["completed_at"])
    if start is None or dispatch is None or end is None or finished is None:
        raise ValueError("complete timezone-aware path timestamps required")
    if not start <= dispatch <= end <= finished:
        raise ValueError("Jev timestamps must lie within its path")
    prefix = _duration(row["service_ms"])
    if row["route"] == "fallback":
        critic = quality_speed.raw_receipt(
            archive,
            entry["id"],
            "critic",
            str(quality_speed.critic_pair.CONTROLS["endpoint"]),
            entry["critic_request"],
        )
        boundary = timestamp(critic["dispatch_started_at"])
        if boundary is None or not end <= boundary <= finished:
            raise ValueError("critic must start after Jev completion")
        prefix = (boundary - start).total_seconds() * 1000
    if prefix + 1 < _duration(raw["elapsed_ms"]) or prefix > row["service_ms"] + 1:
        raise ValueError("Jev prefix must contain its transport and fit its path")
    return _duration(prefix)


def paired_samples(
    archive: Path,
    schedule: list[dict[str, Any]],
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Extract paired baseline samples only after the full archive replay succeeds."""
    indexed = {r["id"]: r for r in rows}
    baseline = {(e["case_id"], e["repeat"]): e for e in schedule if e["arm"] == "critic"}
    for row in rows:
        if row["unknown_cost_calls"]:
            raise ValueError("archive contains unknown call costs")
        _money(row["known_cost_usd"])
    pairs = []
    for entry in schedule:
        if entry["arm"] != "jev_then_critic":
            continue
        other = baseline[entry["case_id"], entry["repeat"]]
        if entry["critic_request"] != other["critic_request"]:
            raise ValueError("paired critic requests differ")
        row, original = indexed[entry["id"]], indexed[other["id"]]
        raw = quality_speed.raw_receipt(
            archive, entry["id"], "jev", DECISIONS_ENDPOINT, entry["jev_wire"]
        )
        request = DecisionRequest.model_validate_json(canonical(entry["jev_request"]))
        observation = DecisionObservation.model_validate_json(
            canonical(
                {
                    **row["jev_observation"],
                    "elapsed_ms": raw["elapsed_ms"],
                }
            )
        )
        observation.validate_for(
            request, expected_model_id=OpenRouterDecisionRoute().resolved_model_id
        )
        pairs.append(
            {
                "id": entry["id"],
                "case_id": entry["case_id"],
                "repeat": entry["repeat"],
                "request": request,
                "observation": observation,
                "prefix_ms": _prefix(archive, entry, row, raw),
                "baseline_ms": _duration(original["service_ms"]),
                "baseline_action": original["action"],
                "baseline_cost": _money(original["known_cost_usd"]),
                "jev_cost": _money(observation.observed_cost_usd),
                "eligible": row["route"] == "bypass",
                "route_reason": row["route_reason"],
            }
        )
    return pairs


def scenario_rows(pairs: list[dict[str, Any]], lead_ms: int | None) -> list[dict[str, Any]]:
    """A receipt arriving after dispatch cannot change that foreground decision."""
    result = []
    for pair in pairs:
        ready = background_policy.select_ready(
            pair["request"],
            pair["observation"],
            completed_ms=pair["prefix_ms"],
            now_ms=pair["prefix_ms"] if lead_ms is None else lead_ms,
            authorized=True,
            expected_model_id=OpenRouterDecisionRoute().resolved_model_id,
        )
        bypass = ready == "ready" and pair["eligible"]
        foreground = 0.0 if bypass else pair["baseline_ms"]
        origin = None if lead_ms is None else lead_ms + foreground
        result.append(
            {
                "case_id": pair["case_id"],
                "repeat": pair["repeat"],
                "archive_routed_id": pair["id"],
                "receipt_status": ready,
                "route": "bypass" if bypass else "direct_critic",
                "archived_route_reason": pair["route_reason"],
                "action": "accept" if bypass else pair["baseline_action"],
                "baseline_action": pair["baseline_action"],
                "baseline_ms": pair["baseline_ms"],
                "foreground_lower_bound_ms": foreground,
                "candidate_origin_lower_bound_ms": origin,
                "baseline_candidate_origin_lower_bound_ms": None
                if lead_ms is None
                else lead_ms + pair["baseline_ms"],
                "all_work_completion_lower_bound_ms": None
                if origin is None
                else max(pair["prefix_ms"], origin),
                "jev_prefix_proxy_ms": pair["prefix_ms"],
                "cost_usd": str(
                    pair["jev_cost"] + (Decimal(0) if bypass else pair["baseline_cost"])
                ),
                "background_cost_usd": str(pair["jev_cost"]),
                "calls": 1 if bypass else 2,
            }
        )
    return result


def metrics(rows: list[dict[str, Any]], cases: list[dict[str, Any]]) -> dict[str, Any]:
    by_case = {case["id"]: case for case in cases}
    result: dict[str, Any] = {
        "paths": len(rows),
        "receipt_statuses": dict(Counter(r["receipt_status"] for r in rows)),
        "ready_hits": sum(r["receipt_status"] == "ready" for r in rows),
        "bypasses": sum(r["route"] == "bypass" for r in rows),
        "misses": sum(r["receipt_status"] != "ready" for r in rows),
        "direct_critic_calls": sum(r["route"] == "direct_critic" for r in rows),
        "background_calls": len(rows),
        "total_calls": sum(r["calls"] for r in rows),
        "late_calls": sum(r["receipt_status"] == "pending" for r in rows),
        "unused_background_calls": sum(r["route"] == "direct_critic" for r in rows),
        "cost_usd": str(sum((Decimal(r["cost_usd"]) for r in rows), Decimal(0))),
        "background_cost_usd": str(
            sum((Decimal(r["background_cost_usd"]) for r in rows), Decimal(0))
        ),
        "actions": dict(Counter(r["action"] for r in rows)),
        "strict_correct": sum(
            r["action"] == by_case[r["case_id"]]["expected_action"] for r in rows
        ),
        "rubric_correct": sum(
            r["action"]
            in by_case[r["case_id"]].get(
                "acceptable_actions", [by_case[r["case_id"]]["expected_action"]]
            )
            for r in rows
        ),
        "false_accepts": sum(
            r["action"] == "accept" and by_case[r["case_id"]]["expected_action"] != "accept"
            for r in rows
        ),
        "paired_safe_losses": sum(
            r["baseline_action"] == "accept"
            and r["action"] != "accept"
            and by_case[r["case_id"]]["expected_action"] == "accept"
            for r in rows
        ),
        "useful_assertions_available": sum(
            len(by_case[r["case_id"]]["expected"])
            for r in rows
            if r["action"] == "accept" and by_case[r["case_id"]]["expected_action"] == "accept"
        ),
    }
    for field in (
        "foreground_lower_bound_ms",
        "candidate_origin_lower_bound_ms",
        "baseline_candidate_origin_lower_bound_ms",
        "all_work_completion_lower_bound_ms",
        "jev_prefix_proxy_ms",
    ):
        values = [r[field] for r in rows if r[field] is not None]
        result[field] = {
            "p50": statistics.median(values) if values else None,
            "p95": _percentile(values, 0.95),
        }
    return result


def analyze(pairs: list[dict[str, Any]], cases: list[dict[str, Any]]) -> dict[str, Any]:
    scenarios: list[dict[str, Any]] = []
    for lead in (*LEADS_MS, None):
        rows = scenario_rows(pairs, lead)
        scenarios.append(
            {
                "name": "all_ready_ideal_ceiling" if lead is None else f"assumed_lead_{lead}ms",
                "assumed_lead_ms": lead,
                "metrics": metrics(rows, cases),
                "rows": rows,
                "per_repeat": {
                    str(repeat): metrics([r for r in rows if r["repeat"] == repeat], cases)
                    for repeat in sorted({r["repeat"] for r in rows})
                },
            }
        )
    baseline_times = [p["baseline_ms"] for p in pairs]
    return {
        "version": VERSION,
        "theoretical": True,
        "production_qualified": False,
        "baseline": {
            "paths": len(pairs),
            "p50_ms": statistics.median(baseline_times),
            "p95_ms": _percentile(baseline_times, 0.95),
            "cost_usd": str(sum((p["baseline_cost"] for p in pairs), Decimal(0))),
        },
        "ideal_can_meet_original_median_target": scenarios[-1]["metrics"][
            "foreground_lower_bound_ms"
        ]["p50"]
        <= 0.8 * statistics.median(baseline_times),
        "scenarios": scenarios,
        "limits": LIMITS,
    }


def _inputs(cases_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    cases = support_inputs.load_cases(cases_path)
    return cases, quality_speed.manifest(cases_path), quality_speed.entries(cases)


def _finish(
    archive: Path,
    out: Path,
    cases_path: Path,
    cases: list[dict[str, Any]],
    current: dict[str, Any],
    schedule: list[dict[str, Any]],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    if out.resolve().is_relative_to(archive.resolve()):
        raise ValueError("analysis output must be outside the immutable archive")
    pairs = paired_samples(archive, schedule, rows)
    result = analyze(pairs, cases)
    files = sorted(p for p in archive.rglob("*") if p.is_file())
    result["archive"] = {
        "path": str(archive.resolve()),
        "validated_paths": len(rows),
        "manifest": current,
        "files_sha256": {str(p.relative_to(archive)): _sha(p) for p in files},
        "original_acquisition_cost_usd": str(
            sum((_money(r["known_cost_usd"]) for r in rows), Decimal(0))
        ),
        "original_acquisition_calls": sum(len(r["stages"]) for r in rows),
        "cases_path": str(cases_path.resolve()),
        "cases_sha256": _sha(cases_path),
    }
    result["analysis_program_sha256"] = {
        Path(__file__).name: _sha(Path(__file__)),
        "background_policy.py": _sha(Path(background_policy.__file__)),
    }
    out.mkdir(parents=True, exist_ok=False)
    _write(out / "analysis.json", result)
    _write(
        out / "completion.json",
        {
            "mode": "offline_counterfactual",
            "paths": len(pairs),
            "analysis_sha256": _sha(out / "analysis.json"),
        },
    )
    return result


async def run(cases_path: Path, archive: Path, out: Path) -> dict[str, Any]:
    cases, current, schedule = await asyncio.to_thread(_inputs, cases_path)
    rows = await quality_speed.replay_rows(archive, current, schedule, cases)
    completion = await asyncio.to_thread((archive / "completion.json").read_bytes)
    if read_json_value(completion) != {"mode": "live", "paths": len(schedule)}:
        raise ValueError("complete original live archive required")
    return await asyncio.to_thread(
        _finish, archive, out, cases_path, cases, current, schedule, rows
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.cases, args.archive, args.output_dir))


if __name__ == "__main__":
    main()
