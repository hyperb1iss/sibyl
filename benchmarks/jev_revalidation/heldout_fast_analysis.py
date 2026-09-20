"""Complete-path cost and timing; finding correctness still requires blind review."""

from __future__ import annotations

import math
import statistics
from collections import Counter
from decimal import Decimal
from typing import Any

from .runner import _percentile

ARMS = ("direct", "live_jev", "misleading")
PROTOCOL = {
    "primary": "live_jev versus direct; same full Haiku prompt and schema, actual Jev acquisition",
    "quality": "blind semantic-pass gain strictly positive, with no new paired false accept, unsupported finding, missed concern, or lost safe accept",
    "speed": {"service_p50_ratio_max": 0.8, "service_p95_ratio_max": 1.0},
    "cost": {"total_cost_ratio_max": 1.0, "unknown_cost_calls": 0},
    "stress": "misleading hints are a separate robustness veto; never pool their cost or latency with live_jev",
    "unit": "minimal-pair clusters; repeated paths and cases within a pair are dependent",
    "timing": "candidate preparation through Jev when applicable, full critic and mechanical validation; queue reported separately",
    "authority": "synthetic experiment only; no publication authority or inferred downstream quality",
}


def _metrics(rows: list[dict[str, Any]], gold: dict[str, dict[str, Any]]) -> dict[str, Any]:
    usages = [r["critic"]["usage"] for r in rows]
    decisions = [r["jev_observation"] for r in rows if r["jev_observation"] is not None]
    costs = [u["observed_cost_usd"] for u in usages + decisions]
    known = sum((Decimal(str(c)) for c in costs if c is not None), Decimal(0))
    safe = [r for r in rows if gold[r["case_id"]]["expected_action"] == "accept"]
    result: dict[str, Any] = {
        "paths": len(rows),
        "critic_calls": len(usages),
        "jev_calls": len(decisions),
        "actions": dict(Counter(r["action"] for r in rows)),
        "strict_correct": sum(r["action"] == gold[r["case_id"]]["expected_action"] for r in rows),
        "rubric_correct": sum(
            r["action"]
            in gold[r["case_id"]].get("acceptable_actions", [gold[r["case_id"]]["expected_action"]])
            for r in rows
        ),
        "safe_candidates": len(safe),
        "safe_accepted": sum(r["action"] == "accept" for r in safe),
        "false_accepts": sum(
            r["action"] == "accept" and gold[r["case_id"]]["expected_action"] != "accept"
            for r in rows
        ),
        "execution_failures": sum(r["action"] == "error" for r in rows),
        "jev_failures": sum(d["execution_status"] != "completed" for d in decisions),
        "hinted_paths": sum(bool(r["hints"]) for r in rows),
        "known_cost_usd": str(known),
        "unknown_cost_calls": costs.count(None),
        "total_cost_usd": str(known) if None not in costs else None,
        "jev_known_cost_usd": str(
            sum(
                (
                    Decimal(str(d["observed_cost_usd"]))
                    for d in decisions
                    if d["observed_cost_usd"] is not None
                ),
                Decimal(0),
            )
        ),
        "known_attempts": sum(u["attempt_count"] or 0 for u in usages + decisions),
        "unknown_attempt_calls": sum(u["attempt_count"] is None for u in usages + decisions),
        "useful_assertions_available": sum(
            len(gold[r["case_id"]]["expected"]) for r in safe if r["action"] == "accept"
        ),
    }
    for field in ("input_tokens", "output_tokens"):
        result[field] = sum(u[field] or 0 for u in usages + decisions)
        result[f"unknown_{field}_calls"] = sum(u[field] is None for u in usages + decisions)
    for field in ("service_ms", "queue_ms", "total_ms"):
        values = [r[field] for r in rows]
        result[f"{field}_p50"] = statistics.median(values) if values else None
        result[f"{field}_p95"] = _percentile(values, 0.95)
    return result


def summarize(
    cases: list[dict[str, Any]], schedule: list[dict[str, Any]], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    expected = {e["id"]: e for e in schedule}
    if (
        len(rows) != len(expected)
        or len(expected) != len(schedule)
        or {r["id"] for r in rows} != expected.keys()
    ):
        raise ValueError("complete unique path coverage required")
    gold = {c["id"]: c for c in cases}
    indexed = {}
    for row in rows:
        if any(row[k] != expected[row["id"]][k] for k in ("case_id", "repeat", "arm")):
            raise ValueError("path identity mismatch")
        for field in ("service_ms", "queue_ms", "total_ms"):
            value = row[field]
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("finite nonnegative path timing required")
        indexed[row["case_id"], row["repeat"], row["arm"]] = row
    pairs = []
    for case_id, repeat in sorted({(r["case_id"], r["repeat"]) for r in rows}):
        direct = indexed[case_id, repeat, "direct"]
        for arm in ARMS[1:]:
            other = indexed[case_id, repeat, arm]
            safe = gold[case_id]["expected_action"] == "accept"
            pairs.append(
                {
                    "case_id": case_id,
                    "pair_group": gold[case_id].get("pair_group", case_id),
                    "repeat": repeat,
                    "comparison": arm,
                    "direct_action": direct["action"],
                    "other_action": other["action"],
                    "service_delta_ms": other["service_ms"] - direct["service_ms"],
                    "new_false_accept": not safe
                    and direct["action"] != "accept"
                    and other["action"] == "accept",
                    "lost_safe_accept": safe
                    and direct["action"] == "accept"
                    and other["action"] != "accept",
                }
            )
    arms = {arm: _metrics([r for r in rows if r["arm"] == arm], gold) for arm in ARMS}
    direct, live = arms["direct"], arms["live_jev"]
    ratios = {
        field: live[field] / direct[field] if direct[field] else None
        for field in ("service_ms_p50", "service_ms_p95")
    }
    costs_known = direct["total_cost_usd"] is not None and live["total_cost_usd"] is not None
    ratios["cost"] = (
        float(Decimal(live["total_cost_usd"]) / Decimal(direct["total_cost_usd"]))
        if costs_known and Decimal(direct["total_cost_usd"]) > 0
        else None
    )
    clusters = sorted({c.get("pair_group", c["id"]) for c in cases})
    repeats = sorted({r["repeat"] for r in rows})
    joint = [
        {
            "pair_group": group,
            "repeat": repeat,
            "arm": arm,
            "all_rubric_correct": all(
                indexed[c["id"], repeat, arm]["action"]
                in c.get("acceptable_actions", [c["expected_action"]])
                for c in cases
                if c.get("pair_group", c["id"]) == group
            ),
        }
        for group in clusters
        for repeat in repeats
        for arm in ARMS
    ]
    return {
        "arms": arms,
        "pairs": pairs,
        "independent_clusters": len(clusters),
        "cluster_joint_actions": joint,
        "per_repeat": {
            str(repeat): {
                arm: _metrics([r for r in rows if r["repeat"] == repeat and r["arm"] == arm], gold)
                for arm in ARMS
            }
            for repeat in repeats
        },
        "primary_ratios": ratios,
        "mechanical_gates": {
            "median_20_percent_faster": ratios["service_ms_p50"] is not None
            and ratios["service_ms_p50"] <= PROTOCOL["speed"]["service_p50_ratio_max"],
            "p95_not_worse": ratios["service_ms_p95"] is not None and ratios["service_ms_p95"] <= 1,
            "all_primary_cost_known": costs_known,
            "cost_not_worse": ratios["cost"] is not None and ratios["cost"] <= 1,
            "no_primary_new_false_accept": not any(
                p["new_false_accept"] for p in pairs if p["comparison"] == "live_jev"
            ),
            "no_primary_lost_safe_accept": not any(
                p["lost_safe_accept"] for p in pairs if p["comparison"] == "live_jev"
            ),
        },
        "stress_action_harm": any(
            p["new_false_accept"] or p["lost_safe_accept"]
            for p in pairs
            if p["comparison"] == "misleading"
        ),
        "all_arms_acquisition": _metrics(rows, gold),
        "semantic_review_status": "pending",
        "production_qualified": False,
        "limits": [
            "Finding correctness, unsupported findings and missed concerns require arm-blind review; action matches alone do not satisfy quality gates.",
            "Fresh synthetic minimal pairs are not downstream task or production evidence; repeats are dependent.",
            "The misleading arm measures resistance to wrong advisory labels, not actual Jev acquisition latency.",
            "Complete service includes preparation and model work but excludes source retrieval, authorization, durable publication and interactive recall.",
        ],
    }
