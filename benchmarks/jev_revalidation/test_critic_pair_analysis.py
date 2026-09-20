"""Frozen selection and whole-critic counterfactual accounting controls."""

# ruff: noqa: PLR2004

from copy import deepcopy

import pytest

from sibyl_core.tasks.memory_validation import PreparedMemoryValidation

from . import critic_pair
from . import critic_pair_analysis as study
from .test_support_fallback_study import _calibrated


@pytest.mark.asyncio
async def test_plan_covers_every_primary_bypass_and_charges_all_routing(tmp_path):
    _, selection, cases, run = await _calibrated(tmp_path)
    plan = study.build_plan(cases, run, selection)
    assert sorted((r["case_id"], r["repeat"]) for r in plan["calls"]) == [
        ("test-safe", 0),
        ("test-safe", 1),
    ]
    assert plan["routing_calls"] == 4
    assert plan["routing_cost_usd"] == 0.004
    study.validate_plan(plan)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["selection", "drop_call", "route", "cost", "prompt"])
async def test_plan_cannot_be_rebound_by_recomputing_its_digest(tmp_path, change):
    _, selection, cases, run = await _calibrated(tmp_path)
    plan = study.build_plan(cases, run, selection)
    if change == "selection":
        plan["selection_sha256"] = "0" * 64
    elif change == "drop_call":
        plan["calls"].pop()
    elif change == "route":
        plan["controls"]["injected"] = True
    elif change == "cost":
        plan["routing_cost_usd"] = 0
    else:
        plan["calls"][0]["prompt_sha256"] = "0" * 64
    plan["plan_sha256"] = study.digest({k: v for k, v in plan.items() if k != "plan_sha256"})
    with pytest.raises(ValueError, match="paired plan"):
        study.validate_plan(plan)


@pytest.mark.asyncio
async def test_critic_preparation_must_match_retained_jev_evidence(tmp_path, monkeypatch):
    _, selection, cases, run = await _calibrated(tmp_path)
    original = critic_pair.prepare_case

    def changed(case, run_id):
        prepared = original(case, run_id)
        return PreparedMemoryValidation(
            prepared.payload_json.replace("door is open", "door is shut")
        )

    monkeypatch.setattr(critic_pair, "prepare_case", changed)
    with pytest.raises(ValueError, match="semantic evidence"):
        study.build_plan(cases, run, selection)


def toy_receipts():
    plan = {
        "plan_sha256": "p",
        "routing_calls": 10,
        "routing_cost_usd": 0.01,
        "calls": [
            {"id": "a", "case_id": "a", "repeat": 0, "request_sha256": "a"},
            {"id": "b", "case_id": "a", "repeat": 1, "request_sha256": "b"},
        ],
    }
    receipts = [
        {
            "id": c["id"],
            "request_sha256": c["request_sha256"],
            "execution_status": "completed",
            "result": {"status": "no_findings", "submission": None},
            "usage": {"observed_cost_usd": 0.1},
            "elapsed_ms": 3,
        }
        for c in plan["calls"]
    ]
    return plan, receipts


def test_whole_critic_counterfactual_charges_all_routing_not_only_bypasses():
    plan, receipts = toy_receipts()
    result = study.summarize(plan, receipts)
    assert result["conditional_avoided_cost_usd"] == pytest.approx(0.19)
    assert result["selected_distinct_candidates"] == 1
    assert result["measured_experiment_cost_usd"] == 0.2
    assert result["all_grouped_routing_calls"] == 10


@pytest.mark.parametrize("status", ["reconsider", "abstain", "invalid_response", "unavailable"])
def test_any_disagreement_or_failure_disqualifies_but_preserves_its_cost(status):
    plan, receipts = toy_receipts()
    if status in {"reconsider", "abstain"}:
        receipts[0]["result"]["status"] = status
    else:
        receipts[0]["execution_status"] = status
        receipts[0]["result"] = None
    result = study.summarize(plan, receipts)
    assert result["qualification"] == "not_qualified"
    assert result["conditional_avoided_cost_usd"] is None
    assert result["known_observed_critic_cost_usd"] == 0.2
    assert result["unqualified_cost_difference_usd"] == pytest.approx(0.19)


def test_unknown_cost_stays_unknown_even_when_every_critic_agrees():
    plan, receipts = toy_receipts()
    receipts[0]["usage"]["observed_cost_usd"] = None
    result = study.summarize(plan, receipts)
    assert result["qualification"] == "not_qualified"
    assert result["conditional_avoided_cost_usd"] is None
    assert result["unqualified_cost_difference_usd"] is None
    assert result["measured_experiment_cost_usd"] is None
    assert result["known_observed_critic_cost_usd"] == 0.1


@pytest.mark.parametrize("damage", ["missing", "duplicate", "request", "invalid_cost"])
def test_summary_rejects_incomplete_or_corrupted_accounting(damage):
    plan, receipts = toy_receipts()
    if damage == "missing":
        receipts.pop()
    elif damage == "duplicate":
        receipts.append(deepcopy(receipts[0]))
    elif damage == "request":
        receipts[0]["request_sha256"] = "other"
    else:
        receipts[0]["usage"]["observed_cost_usd"] = True
    with pytest.raises(ValueError, match=r"cover|mismatch|cost"):
        study.summarize(plan, receipts)
