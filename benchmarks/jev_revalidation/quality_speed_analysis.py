"""Quality, service latency, and cost accounting for synthetic validation paths."""

from __future__ import annotations

import math
import statistics
from collections import Counter
from decimal import Decimal
from typing import Any

from .runner import _percentile

ARMS = ("critic", "jev_then_critic")


def summarize(
    cases: list[dict[str, Any]], schedule: list[dict[str, Any]], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    expected = {e["id"]: e for e in schedule}
    if len(rows) != len(expected) or {r["id"] for r in rows} != expected.keys():
        raise ValueError("complete unique path coverage required")
    by_case = {c["id"]: c for c in cases}
    indexed = {}
    for row in rows:
        e = expected[row["id"]]
        if any(row[k] != e[k] for k in ("case_id", "repeat", "arm")):
            raise ValueError("path identity mismatch")
        if row["action"] not in {"accept", "flag", "abstain", "error", "not_executed"}:
            raise ValueError("invalid action")
        for field in ("service_ms", "queue_ms", "total_ms"):
            value = row[field]
            if value is not None and (
                type(value) not in (int, float) or not math.isfinite(value) or value < 0
            ):
                raise ValueError("invalid timing")
        indexed[row["case_id"], row["repeat"], row["arm"]] = row

    def metrics(selected: list[dict[str, Any]]) -> dict[str, Any]:
        timing = [r["service_ms"] for r in selected if r["service_ms"] is not None]
        costs = sum((Decimal(r["known_cost_usd"]) for r in selected), Decimal(0))
        correct = sum(r["action"] == by_case[r["case_id"]]["expected_action"] for r in selected)
        gold_safe = [r for r in selected if by_case[r["case_id"]]["expected_action"] == "accept"]
        unsafe = [r for r in selected if by_case[r["case_id"]]["expected_action"] != "accept"]
        exposed = [(by_case[r["case_id"]], r) for r in selected if r["action"] == "accept"]
        return {
            "paths": len(selected),
            "action_correct": correct,
            "rubric_correct": sum(
                r["action"]
                in by_case[r["case_id"]].get(
                    "acceptable_actions", [by_case[r["case_id"]]["expected_action"]]
                )
                for r in selected
            ),
            "actions": dict(Counter(r["action"] for r in selected)),
            "safe_candidates": len(gold_safe),
            "safe_accepted": sum(r["action"] == "accept" for r in gold_safe),
            "unsafe_or_insufficient": len(unsafe),
            "false_accepts": sum(r["action"] == "accept" for r in unsafe),
            "useful_assertions_available": sum(
                len(c["expected"]) for c, _ in exposed if c["expected_action"] == "accept"
            ),
            "unsafe_assertions_exposed": sum(
                v != "supported" for c, _ in exposed for v in c["expected"].values()
            ),
            "known_cost_usd": str(costs),
            "unknown_cost_calls": sum(r["unknown_cost_calls"] for r in selected),
            "cost_per_strict_correct_action_usd": str(costs / correct)
            if correct and not any(r["unknown_cost_calls"] for r in selected)
            else None,
            "service_p50_ms": statistics.median(timing) if timing else None,
            "service_p95_ms": _percentile(timing, 0.95),
            "queue_p95_ms": _percentile(
                [r["queue_ms"] for r in selected if r["queue_ms"] is not None], 0.95
            ),
            "total_p95_ms": _percentile(
                [r["total_ms"] for r in selected if r["total_ms"] is not None], 0.95
            ),
            "routes": dict(Counter(r["route"] for r in selected)),
        }

    arms = {arm: metrics([r for r in rows if r["arm"] == arm]) for arm in ARMS}
    pairs = []
    for case in cases:
        for repeat in range(2):
            a, b = (indexed[case["id"], repeat, arm] for arm in ARMS)
            pairs.append(
                {
                    "case_id": case["id"],
                    "repeat": repeat,
                    "baseline": a["action"],
                    "routed": b["action"],
                    "service_delta_ms": b["service_ms"] - a["service_ms"]
                    if a["service_ms"] is not None and b["service_ms"] is not None
                    else None,
                }
            )
    a, b = (arms[arm] for arm in ARMS)
    gates = {
        "zero_false_accepts": b["false_accepts"] == 0,
        "safe_coverage_not_worse": b["safe_accepted"] >= a["safe_accepted"],
        "no_paired_safe_loss": not any(
            p["baseline"] == "accept"
            and p["routed"] != "accept"
            and by_case[p["case_id"]]["expected_action"] == "accept"
            for p in pairs
        ),
        "action_quality_not_worse": b["rubric_correct"] >= a["rubric_correct"],
        "no_execution_failures": all(r["action"] not in {"error", "not_executed"} for r in rows),
        "all_cost_known": all(r["unknown_cost_calls"] == 0 for r in rows),
        "cost_not_worse": Decimal(b["known_cost_usd"]) <= Decimal(a["known_cost_usd"]),
        "median_20_percent_faster": a["service_p50_ms"] is not None
        and b["service_p50_ms"] is not None
        and b["service_p50_ms"] <= 0.8 * a["service_p50_ms"],
        "p95_not_worse": a["service_p95_ms"] is not None
        and b["service_p95_ms"] is not None
        and b["service_p95_ms"] <= a["service_p95_ms"],
    }
    return {
        "arms": arms,
        "pairs": pairs,
        "gates": gates,
        "diagnostic_target_met": all(gates.values()),
        "production_qualified": False,
        "strata": {
            band: {
                arm: metrics(
                    [
                        r
                        for r in rows
                        if r["arm"] == arm
                        and by_case[r["case_id"]].get("evidence_size_band", "unspecified") == band
                    ]
                )
                for arm in ARMS
            }
            for band in sorted({c.get("evidence_size_band", "unspecified") for c in cases})
        },
        "limits": [
            "Synthetic plain reflections, no end-task QA, publication or recall execution.",
            "Evidence availability is a deterministic accept/withhold diagnostic, not an executed retriever.",
            "Two repeats per candidate are correlated; small-sample p95 is descriptive.",
            "Service timing excludes harness queue; total timing includes it. Neither measures interactive recall.",
            "No correction generation or deployment. Prior .99 threshold unchanged, not refit.",
        ],
    }
