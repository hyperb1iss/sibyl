"""Offline source-support routing; no publication or complete-critic authority."""

from __future__ import annotations

import math
import re
from typing import Any

POLICY_VERSION = "jev-support-fallback-v1"
_NUMBER = re.compile(
    r"\d|\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|"
    r"forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion|"
    r"trillion|dozen|half|quarter|double|triple|pair|couple|once|twice)\b",
    re.IGNORECASE,
)


def numeric_text(case: dict[str, Any]) -> bool:
    """Conservative explicit-token deferral, not a general arithmetic detector."""
    texts = [case["content"], *case["claims"], *(source["text"] for source in case["sources"])]
    return any(_NUMBER.search(text) is not None for text in texts)


def candidate_row(case: dict[str, Any], repeat: int, calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Reduce validated call receipts; gold is retained only for offline scoring."""
    paths = {"/content", *(f"/claim_records/{i}/content" for i in range(len(case["claims"])))}
    questions = [question for call in calls for question in call["question_ids"]]
    if (
        len(questions) != len(set(questions))
        or set(questions) != paths
        or any(call["case_id"] != case["id"] or call["repeat"] != repeat for call in calls)
        or len({call["arm"] for call in calls}) != 1
    ):
        raise ValueError("candidate calls must cover every assertion exactly in one arm/repeat")
    complete = all(call["execution_status"] == "completed" for call in calls)
    answers = [answer for call in calls for answer in call["answers"]]
    answer_paths = [answer["question_id"] for answer in answers]
    if complete and (len(answer_paths) != len(paths) or set(answer_paths) != paths):
        raise ValueError("completed candidate must have every assertion answer exactly once")
    confidence_values = [answer.get("provider_confidence") for answer in answers]
    known_confidence = complete and all(
        type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1
        for value in confidence_values
    )
    return {
        "case_id": case["id"],
        "repeat": repeat,
        "arm": calls[0]["arm"],
        "gold_supported": all(label == "supported" for label in case["expected"].values()),
        "assertion_count": len(paths),
        "numeric_text": numeric_text(case),
        "complete": complete,
        "all_supported": complete and all(answer["value"] == "supported" for answer in answers),
        "confidence": min(confidence_values) if known_confidence else None,
    }


def route(
    row: dict[str, Any], threshold: float | None, *, numeric_guard: bool = True
) -> tuple[str, str]:
    """A bypass covers source support only; every other outcome keeps the fallback."""
    if threshold is None:
        return "fallback", "reject_all"
    if (
        type(threshold) not in (int, float)
        or not math.isfinite(threshold)
        or not 0 <= threshold <= 1
    ):
        raise ValueError("threshold must be finite and within zero and one")
    if not row["complete"]:
        return "fallback", "incomplete"
    if not row["all_supported"]:
        return "fallback", "unsupported"
    confidence = row["confidence"]
    if confidence is None:
        return "fallback", "missing_confidence"
    if (
        type(confidence) not in (int, float)
        or not math.isfinite(confidence)
        or not 0 <= confidence <= 1
    ):
        raise ValueError("candidate confidence must be finite and within zero and one")
    if numeric_guard and row["numeric_text"]:
        return "fallback", "numeric_text"
    return ("fallback", "low_confidence") if confidence < threshold else ("bypass", "qualified")


def select_threshold(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Fit grouped calibration only, maximizing distinct safe candidates cleared twice."""
    indexed: dict[str, dict[int, dict[str, Any]]] = {}
    for row in rows:
        if row.get("arm") != "grouped":
            raise ValueError("threshold selection requires grouped calibration")
        repeats = indexed.setdefault(row["case_id"], {})
        if row["repeat"] in repeats or type(row["repeat"]) is not int:
            raise ValueError("calibration requires exactly two distinct repeats per candidate")
        repeats[row["repeat"]] = row
    if not indexed or any(set(repeats) != {0, 1} for repeats in indexed.values()):
        raise ValueError("calibration requires exactly two distinct repeats per candidate")
    for repeats in indexed.values():
        for field in ("gold_supported", "numeric_text", "assertion_count"):
            if repeats[0][field] != repeats[1][field]:
                raise ValueError("calibration candidate properties changed between repeats")
    table = []
    for step in range(101):
        threshold = step / 100
        decisions = {
            case_id: [route(row, threshold)[0] == "bypass" for row in repeats.values()]
            for case_id, repeats in indexed.items()
        }
        safe_both = sum(
            indexed[case_id][0]["gold_supported"] and all(bypasses)
            for case_id, bypasses in decisions.items()
        )
        false_any = sum(
            not indexed[case_id][0]["gold_supported"] and any(bypasses)
            for case_id, bypasses in decisions.items()
        )
        table.append(
            {
                "threshold": threshold,
                "safe_candidates_bypassed_both": safe_both,
                "unsafe_candidates_bypassed_any": false_any,
            }
        )
    eligible = [
        row
        for row in table
        if row["unsafe_candidates_bypassed_any"] == 0 and row["safe_candidates_bypassed_both"] > 0
    ]
    best = (
        max(eligible, key=lambda row: (row["safe_candidates_bypassed_both"], row["threshold"]))
        if eligible
        else None
    )
    return {"threshold": best["threshold"] if best else None, "table": table}
