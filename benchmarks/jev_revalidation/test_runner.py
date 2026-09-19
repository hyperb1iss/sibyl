"""Toy cases exercise measurement integrity without seeing the frozen corpus."""

# Numeric expectations are the explicit toy experiment outcomes.
# ruff: noqa: PLR2004

import argparse
import json

import pytest

from sibyl_core.ai.decisions import ChoiceAnswer, DecisionObservation
from sibyl_core.ai.openrouter_decisions import OpenRouterDecisionRoute

from . import prompts
from .runner import (
    baseline_prediction,
    policy_action,
    replay_observation,
    run,
    summarize,
    timestamp,
)


@pytest.fixture
def toy():
    return {
        "id": "toy",
        "category": "toy-category",
        "memory": "The office uses port 1234.",
        "event": "The office now uses port 5678.",
        "memory_effective_at": "2026-01-01T00:00:00Z",
        "event_effective_at": "2026-02-01T00:00:00Z",
        "source_authority": "authoritative",
        "expected_relation": "superseded",
        "expected_action": "retire",
        "rationale": "GOLD-ONLY-RATIONALE-NEVER-SEND",
    }


@pytest.mark.parametrize("arm", ["direct", "decomposed"])
def test_requests_exclude_gold_and_policy_metadata(toy, arm):
    request = prompts.make_request([toy], arm, "toy-run")
    changed = dict(
        toy,
        expected_relation="unrelated",
        expected_action="retain",
        rationale="changed gold",
        category="secret-category",
        id="secret-id",
        source_authority="unknown",
        memory_effective_at=None,
        event_effective_at=None,
    )
    assert request == prompts.make_request([changed], arm, "toy-run")
    assert "GOLD-ONLY-RATIONALE" not in request.model_dump_json()
    assert "2026-01-01" not in request.state


@pytest.mark.parametrize("relation", ["supported", "compatible", "unrelated", "non_evidence"])
def test_non_destructive_relations_retain(toy, relation):
    assert policy_action(relation, toy) == "retain"


@pytest.mark.parametrize("relation", ["contradicted", "superseded"])
@pytest.mark.parametrize(
    ("authority", "memory_date", "event_date", "expected"),
    [
        ("authoritative", "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", "retire"),
        ("reported", "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", "review"),
        ("authoritative", "2026-02-01T00:00:00Z", "2026-01-01T00:00:00Z", "retain"),
        ("reported", "2026-02-01T00:00:00Z", "2026-01-01T00:00:00Z", "retain"),
        ("authoritative", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "review"),
        ("authoritative", None, "2026-02-01T00:00:00Z", "review"),
        ("authoritative", "2026-01-01T00:00:00Z", None, "review"),
        ("authoritative", "2026-01-01", "2026-02-01", "review"),
        ("authoritative", "2026-01-01T00:00:00", "2026-02-01T00:00:00", "review"),
        ("authoritative", "2026-01-01T00:00:00Z", "2026-01-01T01:00:00+01:00", "review"),
    ],
)
def test_retirement_requires_authority_and_strict_event_order(
    toy, relation, authority, memory_date, event_date, expected
):
    case = dict(
        toy,
        source_authority=authority,
        memory_effective_at=memory_date,
        event_effective_at=event_date,
    )
    assert policy_action(relation, case) == expected


@pytest.mark.parametrize(
    "value", [True, 123, "2026-99-99T00:00:00Z", "2026-01-01T00:00:00+99:00", "20260101"]
)
def test_invalid_timestamps_never_establish_order(value):
    assert timestamp(value) is None


def test_uncertain_never_retires(toy):
    assert policy_action("uncertain", toy) == "review"


def test_failures_remain_in_denominator_and_unknown_usage_is_not_free():
    rows = [
        {
            "arm": "direct",
            "status": "completed",
            "relation": "superseded",
            "action": "retire",
            "expected_relation": "superseded",
            "expected_action": "retire",
        },
        {
            "arm": "direct",
            "status": "unavailable",
            "relation": None,
            "action": None,
            "expected_relation": "superseded",
            "expected_action": "retire",
        },
    ]
    calls = [{"execution_status": "unavailable", "attempt_count": 1, "elapsed_ms": 25.0}]
    summary = summarize(rows, calls)
    assert summary["cases"] == 2
    assert summary["relation_accuracy"] == 0.5
    assert summary["action_accuracy"] == 0.5
    assert summary["gold_retire_recall"] == 0.5
    assert summary["transport_failures"] == 1
    assert summary["observed_cost_usd"] is None
    assert summary["unknown_observed_cost_usd_calls"] == 1


def test_existing_polarity_contradiction_is_review_only(toy):
    case = dict(toy, memory="Database backups are enabled.", event="Database backups are disabled.")
    relation, action, proposal, kinds = baseline_prediction(case)
    assert relation == "contradicted"
    assert proposal == "review_only"
    assert action == "review"
    assert "contradiction" in kinds


def test_duplicate_proposal_never_retires_existing_memory(toy):
    case = dict(toy, event=toy["memory"])
    relation, action, proposal, _ = baseline_prediction(case)
    assert (relation, action, proposal) == ("supported", "retain", "duplicate_new_candidate")


async def test_replay_validates_exact_request_and_rejects_corruption(toy, tmp_path):
    request = prompts.make_request([toy], "direct", "toy-run")
    route = OpenRouterDecisionRoute()
    observation = DecisionObservation(
        semantic_input_sha256=request.semantic_input_sha256,
        request_digest=request.request_digest,
        execution_status="completed",
        resolved_model_id=route.resolved_model_id,
        observed_provider="TypeSafe",
        answers=(ChoiceAnswer(question_id="c0.relation", value="superseded"),),
        attempt_count=1,
        elapsed_ms=1.0,
    )
    path = tmp_path / "receipt.json"
    payload = {
        "request": request.model_dump(mode="json"),
        "observation": observation.model_dump(mode="json"),
    }
    path.write_text(json.dumps(payload))
    assert await replay_observation(path, request, route.resolved_model_id) == observation
    payload["observation"]["request_digest"] = "f" * 64
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="different request"):
        await replay_observation(path, request, route.resolved_model_id)
    path.write_text('{"request":{},"request":{},"observation":{}}')
    with pytest.raises(ValueError, match="keys must be unique"):
        await replay_observation(path, request, route.resolved_model_id)


async def test_prepare_records_all_cases_and_refuses_output_reuse(toy, tmp_path):
    cases = tmp_path / "toy.json"
    cases.write_text(json.dumps([toy]))
    args = argparse.Namespace(
        cases=cases,
        out=tmp_path / "run",
        arms="direct,decomposed",
        batch_size=1,
        repeats=2,
        concurrency=2,
        live=False,
        replay=None,
    )
    summary = await run(args)
    assert summary["arms"]["direct"]["pooled"]["cases"] == 2
    assert summary["arms"]["direct"]["pooled"]["failed_cases"] == 2
    assert summary["arms"]["baseline"]["pooled"]["relation_accuracy"] is None
    assert len(list((args.out / "receipts").glob("*.json"))) == 4
    with pytest.raises(FileExistsError):
        await run(args)
