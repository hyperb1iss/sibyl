"""Routing must not turn confidence, missing observations, or gold into authority."""

# ruff: noqa: PLR2004
from copy import deepcopy
from typing import Any

import pytest

from .support_fallback_policy import candidate_row, numeric_text, route, select_threshold


def _row(case_id="safe", repeat=0):
    return {
        "case_id": case_id,
        "repeat": repeat,
        "arm": "grouped",
        "gold_supported": True,
        "assertion_count": 1,
        "numeric_text": False,
        "complete": True,
        "all_supported": True,
        "confidence": 0.9,
    }


def _pair(case_id, *, safe, score, numeric=False):
    return [
        {
            **_row(case_id, repeat),
            "gold_supported": safe,
            "confidence": score,
            "numeric_text": numeric,
        }
        for repeat in (0, 1)
    ]


def test_selector_uses_both_repeats_and_highest_tie():
    rows = _pair("safe", safe=True, score=0.91) + _pair("unsafe", safe=False, score=0.66)
    rows.append({**_row("flaky", 0), "confidence": 1.0})
    rows.append({**_row("flaky", 1), "all_supported": False})
    selected = select_threshold(rows)
    assert selected["threshold"] == 0.91
    assert len(selected["table"]) == 101
    assert max(row["safe_candidates_bypassed_both"] for row in selected["table"]) == 1


def test_any_unsafe_repeat_blocks_selection_and_no_coverage_rejects_all():
    rows = _pair("safe", safe=True, score=0.8) + _pair("unsafe", safe=False, score=1.0)
    rows[-1]["all_supported"] = False
    assert select_threshold(rows)["threshold"] is None
    assert route(rows[0], None) == ("fallback", "reject_all")


def test_numeric_deferral_applies_during_selection_and_evaluation():
    rows = _pair("safe", safe=True, score=0.9) + _pair(
        "numeric", safe=False, score=1.0, numeric=True
    )
    threshold = select_threshold(rows)["threshold"]
    assert threshold == 0.9
    assert route(rows[-1], threshold) == ("fallback", "numeric_text")
    assert route(rows[-1], threshold, numeric_guard=False) == ("bypass", "qualified")


@pytest.mark.parametrize(
    "damage", ["missing_repeat", "duplicate", "singleton", "missing_arm", "changed_gold"]
)
def test_invalid_calibration_grid_rejected(damage):
    rows = _pair("safe", safe=True, score=0.9)
    if damage == "missing_repeat":
        rows.pop()
    elif damage == "duplicate":
        rows.append(rows[0])
    elif damage == "singleton":
        rows[0]["arm"] = "singleton"
    elif damage == "missing_arm":
        rows[0].pop("arm")
    else:
        rows[1]["gold_supported"] = False
    with pytest.raises(ValueError, match=r"calibration|grouped"):
        select_threshold(rows)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"complete": False}, "incomplete"),
        ({"all_supported": False}, "unsupported"),
        ({"confidence": None}, "missing_confidence"),
        ({"confidence": 0.1}, "low_confidence"),
    ],
)
def test_nonqualifying_candidates_always_fall_back(change, reason):
    assert route({**_row(), **change}, 0.8) == ("fallback", reason)


def test_routing_never_reads_gold():
    original = _row()
    assert route(original, 0.8) == route({**original, "gold_supported": False}, 0.8)


@pytest.mark.parametrize(
    "text", ["Exactly THREE remained.", "revision v2", "half the stock", "\uff11\uff12 items"]
)
def test_numeric_rule_reads_input_only(text):
    case = {"content": "The latch is open.", "claims": [], "sources": [{"text": text}]}
    assert numeric_text(case)
    case["sources"][0]["text"] = "The latch is open."
    case.update(category="123", rationale="three", id="case-14")
    assert not numeric_text(case)


def test_receipt_reduction_uses_all_assertions_and_missing_confidence():
    case = {
        "id": "case",
        "content": "The lamp is blue.",
        "claims": ["The lid is shut."],
        "sources": [{"text": "The lamp is blue and the lid is shut."}],
        "expected": {"/content": "supported", "/claim_records/0/content": "supported"},
    }
    call: dict[str, Any] = {
        "case_id": "case",
        "repeat": 0,
        "arm": "grouped",
        "execution_status": "completed",
        "question_ids": list(case["expected"]),
        "answers": [
            {"question_id": path, "value": "supported", "provider_confidence": confidence}
            for path, confidence in zip(case["expected"], (0.99, 0.7), strict=True)
        ],
    }
    row = candidate_row(case, 0, [call])
    assert row["arm"] == "grouped"
    assert row["confidence"] == 0.7
    assert route(row, 0.8) == ("fallback", "low_confidence")
    singles = [
        {**call, "arm": "singleton", "question_ids": [answer["question_id"]], "answers": [answer]}
        for answer in call["answers"]
    ]
    single_row = candidate_row(case, 0, singles)
    assert single_row["arm"] == "singleton"
    with pytest.raises(ValueError, match="grouped"):
        select_threshold([single_row, {**single_row, "repeat": 1}])
    unknown = deepcopy(call)
    unknown["answers"][1]["provider_confidence"] = None
    assert candidate_row(case, 0, [unknown])["confidence"] is None
    with pytest.raises(ValueError, match="cover every assertion"):
        candidate_row(case, 0, [call, call])
    call["execution_status"] = "unavailable"
    call["answers"] = []
    assert route(candidate_row(case, 0, [call]), 0.8) == ("fallback", "incomplete")
