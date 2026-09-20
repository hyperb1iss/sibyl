"""Fresh critic measurements remain separate from prior Jev acquisition."""

from __future__ import annotations

import statistics
from collections import Counter
from decimal import Decimal
from typing import Any

from .runner import _percentile

ARMS = ("direct", "hinted")


def summarize(
    cases: list[dict[str, Any]], schedule: list[dict[str, Any]], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    expected = {e["id"]: e for e in schedule}
    if len(rows) != len(expected) or {r["id"] for r in rows} != expected.keys():
        raise ValueError("complete unique path coverage required")
    gold = {c["id"]: c for c in cases}
    for row in rows:
        if any(row[k] != expected[row["id"]][k] for k in ("case_id", "repeat", "arm")):
            raise ValueError("path identity mismatch")

    def metrics(selected: list[dict[str, Any]]) -> dict[str, Any]:
        costs = [r["critic"]["usage"]["observed_cost_usd"] for r in selected]
        known = sum((Decimal(str(c)) for c in costs if c is not None), Decimal(0))
        hinted = [expected[r["id"]] for r in selected if r["arm"] == "hinted"]
        jev = sum((Decimal(e["prior_jev_cost_usd"]) for e in hinted), Decimal(0))
        times = [r["service_ms"] for r in selected]
        safe = [r for r in selected if gold[r["case_id"]]["expected_action"] == "accept"]
        return {
            "paths": len(selected),
            "fresh_critic_calls": len(selected),
            "actions": dict(Counter(r["action"] for r in selected)),
            "strict_correct": sum(
                r["action"] == gold[r["case_id"]]["expected_action"] for r in selected
            ),
            "rubric_correct": sum(
                r["action"]
                in gold[r["case_id"]].get(
                    "acceptable_actions", [gold[r["case_id"]]["expected_action"]]
                )
                for r in selected
            ),
            "safe_candidates": len(safe),
            "safe_accepted": sum(r["action"] == "accept" for r in safe),
            "false_accepts": sum(
                r["action"] == "accept" and gold[r["case_id"]]["expected_action"] != "accept"
                for r in selected
            ),
            "execution_failures": sum(r["action"] == "error" for r in selected),
            "fresh_known_cost_usd": str(known),
            "unknown_cost_calls": costs.count(None),
            "attributed_prior_jev_calls": len(hinted),
            "attributed_prior_jev_cost_usd": str(jev),
            "fresh_plus_attributed_jev_cost_usd": str(known + jev) if None not in costs else None,
            "service_p50_ms": statistics.median(times) if times else None,
            "service_p95_ms": _percentile(times, 0.95),
            "queue_p95_ms": _percentile([r["queue_ms"] for r in selected], 0.95),
            "output_tokens": sum(r["critic"]["usage"]["output_tokens"] or 0 for r in selected),
            "unknown_output_token_calls": sum(
                r["critic"]["usage"]["output_tokens"] is None for r in selected
            ),
            "available_hint_paths": sum(e["hint_status"] == "available" for e in hinted),
        }

    indexed = {(r["case_id"], r["repeat"], r["arm"]): r for r in rows}
    pairs = []
    for key in sorted({(r["case_id"], r["repeat"]) for r in rows}):
        a, b = (indexed[*key, arm] for arm in ARMS)
        pairs.append(
            {
                "case_id": key[0],
                "repeat": key[1],
                "direct_action": a["action"],
                "hinted_action": b["action"],
                "service_delta_ms": b["service_ms"] - a["service_ms"],
            }
        )
    return {
        "arms": {arm: metrics([r for r in rows if r["arm"] == arm]) for arm in ARMS},
        "per_repeat": {
            str(repeat): {
                arm: metrics([r for r in rows if r["repeat"] == repeat and r["arm"] == arm])
                for arm in ARMS
            }
            for repeat in sorted({r["repeat"] for r in rows})
        },
        "pairs": pairs,
        "semantic_review_status": "pending",
        "production_qualified": False,
        "limits": [
            "Fresh paired critic-stage measurements with pre-existing hints; no live background scheduler or complete Jev acquisition latency measured.",
            "Prior Jev acquisition cost is attributed separately, not billed again by this experiment.",
            "Same 32 synthetic cases as prior work and two dependent repeats; no new held-out or downstream quality evidence.",
            "Action-level matches do not establish finding correctness. Frozen source rubric and arm-blind semantic review are required.",
            "Historical Opus is a dated reference, not a contemporaneous control.",
        ],
    }
