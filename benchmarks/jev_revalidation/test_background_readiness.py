"""Counterfactual accounting never consumes a late answer or drops its cost."""

# Toy timelines and prices are deliberately explicit.
# ruff: noqa: PLR2004

import json
from decimal import Decimal

import pytest

from sibyl_core.ai.decisions import ChoiceAnswer, DecisionObservation
from sibyl_core.ai.openrouter_decisions import OpenRouterDecisionRoute

from . import background_readiness as study
from . import quality_speed
from .support_inputs import make_request
from .test_quality_speed import cases_file, install_network


@pytest.fixture
def case():
    return {
        "id": "receipt-valve",
        "category": "scope",
        "content": "The valve is closed.",
        "claims": [],
        "sources": [
            {
                "id": "valve-source",
                "text": "Inspection confirms the valve is closed.",
                "provenance": "signed",
            }
        ],
        "expected": {"/content": "supported"},
        "expected_action": "accept",
        "rationale": "GOLD-ONLY-CANARY",
        "evidence_band": "small",
    }


def pair(case, *, eligible=True, prefix=100):
    request = make_request(case, "background-toy")
    observation = DecisionObservation(
        semantic_input_sha256=request.semantic_input_sha256,
        request_digest=request.request_digest,
        execution_status="completed",
        resolved_model_id=OpenRouterDecisionRoute().resolved_model_id,
        answers=(
            ChoiceAnswer(question_id="/content", value="supported", provider_confidence=0.999),
        ),
        observed_cost_usd=0.002,
        usage_status="observed",
        attempt_count=1,
        elapsed_ms=prefix,
    )
    return {
        "id": "toy",
        "case_id": case["id"],
        "repeat": 0,
        "request": request,
        "observation": observation,
        "prefix_ms": prefix,
        "baseline_ms": 1000,
        "baseline_action": "accept",
        "baseline_cost": Decimal("0.03"),
        "jev_cost": Decimal("0.002"),
        "eligible": eligible,
        "route_reason": "qualified" if eligible else "numeric_text",
    }


def test_pending_uses_baseline_immediately_and_pays_for_background(case):
    row = study.scenario_rows([pair(case, prefix=1500)], 0)[0]
    assert row["receipt_status"] == "pending"
    assert row["route"] == "direct_critic"
    assert row["foreground_lower_bound_ms"] == 1000
    assert row["candidate_origin_lower_bound_ms"] == 1000
    assert row["baseline_candidate_origin_lower_bound_ms"] == 1000
    assert row["all_work_completion_lower_bound_ms"] == 1500
    assert row["cost_usd"] == "0.032"
    assert row["calls"] == 2
    assert study.metrics([row], [case])["late_calls"] == 1


def test_exact_ready_boundary_preserves_policy_and_accounts_lead(case):
    accepted, rejected = study.scenario_rows([pair(case), pair(case, eligible=False)], 100)
    assert accepted["route"] == "bypass"
    assert accepted["candidate_origin_lower_bound_ms"] == 100
    assert accepted["all_work_completion_lower_bound_ms"] == 100
    assert accepted["cost_usd"] == "0.002"
    assert rejected["receipt_status"] == "ready"
    assert rejected["route"] == "direct_critic"
    assert rejected["candidate_origin_lower_bound_ms"] == 1100
    assert rejected["baseline_candidate_origin_lower_bound_ms"] == 1100
    assert accepted["baseline_candidate_origin_lower_bound_ms"] == 1100
    summary = study.metrics([accepted, rejected], [case])
    assert summary["ready_hits"] == 2
    assert summary["bypasses"] == 1
    assert summary["cost_usd"] == "0.034"
    assert summary["unused_background_calls"] == 1
    assert summary["late_calls"] == 0


def test_all_ready_is_separate_ceiling_without_fabricated_origin(case):
    report = study.analyze([pair(case)], [case])
    assert [s["assumed_lead_ms"] for s in report["scenarios"]] == [*study.LEADS_MS, None]
    ideal = report["scenarios"][-1]
    assert ideal["name"] == "all_ready_ideal_ceiling"
    assert ideal["rows"][0]["candidate_origin_lower_bound_ms"] is None
    assert ideal["rows"][0]["all_work_completion_lower_bound_ms"] is None
    assert report["theoretical"]
    assert not report["production_qualified"]


def test_future_or_stale_observation_cannot_change_foreground(case):
    sample = pair(case)
    sample["observation"] = sample["observation"].model_copy(update={"request_digest": "0" * 64})
    assert study.scenario_rows([sample], 0)[0]["receipt_status"] == "pending"
    row = study.scenario_rows([sample], 100)[0]
    assert row["receipt_status"] == "stale"
    assert row["route"] == "direct_critic"
    assert row["cost_usd"] == "0.032"


@pytest.mark.parametrize("value", [None, True, "NaN", "Infinity", "-0.1"])
def test_unknown_or_invalid_costs_rejected(value):
    with pytest.raises(ValueError, match="costs must"):
        study._money(value)


def test_paired_request_mismatch_rejected_before_receipt_access(tmp_path):
    common = {"case_id": "a", "repeat": 0}
    schedule = [
        {**common, "id": "base", "arm": "critic", "critic_request": {"x": 1}},
        {**common, "id": "routed", "arm": "jev_then_critic", "critic_request": {"x": 2}},
    ]
    rows = [{"id": item["id"], "unknown_cost_calls": 0, "known_cost_usd": "0"} for item in schedule]
    with pytest.raises(ValueError, match="paired critic requests differ"):
        study.paired_samples(tmp_path, schedule, rows)


async def archive_fixture(case, tmp_path, monkeypatch):
    cases = cases_file(tmp_path, [case])
    freeze = tmp_path / "freeze.json"
    freeze.write_text(json.dumps(quality_speed.manifest(cases)))
    install_network(monkeypatch)
    monkeypatch.setenv("SIBYL_JEV_CRITIC_OPENROUTER_API_KEY", "synthetic-mock-only")
    archive = tmp_path / "archive"
    await quality_speed.run(cases, archive, freeze=freeze, live=True)

    async def forbidden(*args, **kwargs):
        pytest.fail("offline analysis attempted live transport")

    monkeypatch.setattr(quality_speed, "send", forbidden)
    return cases, archive


@pytest.mark.asyncio
async def test_full_replay_before_exclusive_analysis_output(case, tmp_path, monkeypatch):
    cases, archive = await archive_fixture(case, tmp_path, monkeypatch)
    output = tmp_path / "analysis"
    report = await study.run(cases, archive, output)
    assert report["archive"]["validated_paths"] == 4
    assert report["archive"]["original_acquisition_calls"] == 4
    assert report["archive"]["original_acquisition_cost_usd"] == "0.064"
    assert "raw/case-0-repeat-0-jev_then_critic.jev.json" in report["archive"]["files_sha256"]
    assert report["scenarios"][0]["metrics"]["total_calls"] == 4
    assert report["scenarios"][-1]["metrics"]["total_calls"] == 2
    assert (output / "completion.json").is_file()
    with pytest.raises(ValueError, match="outside the immutable archive"):
        await study.run(cases, archive, archive / "analysis")
    with pytest.raises(FileExistsError):
        await study.run(cases, archive, output)


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["request", "extra", "completion"])
async def test_corrupt_archive_rejected_without_output(case, tmp_path, monkeypatch, corruption):
    cases, archive = await archive_fixture(case, tmp_path, monkeypatch)
    if corruption == "request":
        path = archive / "raw/case-0-repeat-0-jev_then_critic.jev.json"
        raw = json.loads(path.read_text())
        raw["request"]["state"] = "changed"
        path.write_text(json.dumps(raw))
    elif corruption == "extra":
        (archive / "raw/unexpected.json").write_text("{}")
    else:
        (archive / "completion.json").write_text('{"mode":"live","paths":3}')
    output = tmp_path / "analysis"
    with pytest.raises(
        ValueError, match=r"binding mismatch|artifact set mismatch|complete original"
    ):
        await study.run(cases, archive, output)
    assert not output.exists()


@pytest.mark.parametrize("elapsed", [float("nan"), -1, 100])
def test_prefix_rejects_invalid_transport_timing(tmp_path, elapsed):
    row = {
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:00.010000+00:00",
        "service_ms": 10,
        "route": "bypass",
    }
    raw = {
        "dispatch_started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:00.010000+00:00",
        "elapsed_ms": elapsed,
    }
    with pytest.raises(ValueError, match=r"timing required|prefix must"):
        study._prefix(tmp_path, {}, row, raw)


def test_ideal_cannot_meet_target_when_policy_defers(case):
    report = study.analyze([pair(case, eligible=False)], [case])
    assert not report["ideal_can_meet_original_median_target"]
    for scenario in report["scenarios"][:-1]:
        row = scenario["rows"][0]
        assert (
            row["candidate_origin_lower_bound_ms"]
            == row["baseline_candidate_origin_lower_bound_ms"]
        )
