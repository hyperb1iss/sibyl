"""Toy-only geometry, failure, accounting, and replay checks for source support."""

# Counts and costs below are explicit toy experiment expectations.
# ruff: noqa: PLR2004

import argparse
import asyncio
import json
from copy import deepcopy

import pytest

from sibyl_core.ai.decisions import ChoiceAnswer, DecisionObservation
from sibyl_core.ai.openrouter_decisions import OpenRouterDecisionRoute

from . import support_study as study
from .test_support_inputs import support_case as support_case  # noqa: PLC0414


def _args(tmp_path, cases, name="run", **overrides):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(cases))
    return argparse.Namespace(
        cases=path,
        output_dir=tmp_path / name,
        repeats=2,
        seed=41,
        concurrency=3,
        live=False,
        replay=None,
        **overrides,
    )


def _observation(request, value="supported"):
    return DecisionObservation(
        semantic_input_sha256=request.semantic_input_sha256,
        request_digest=request.request_digest,
        execution_status="completed",
        resolved_model_id=OpenRouterDecisionRoute().resolved_model_id,
        answers=tuple(
            ChoiceAnswer(question_id=q.question_id, value=value) for q in request.questions
        ),
        input_tokens=100,
        output_tokens=3,
        observed_cost_usd=0.002,
        usage_status="observed",
        attempt_count=1,
        elapsed_ms=12.0,
    )


def test_geometry_keeps_full_state_and_matching_subjects(support_case):
    calls = study.schedule([support_case], "run", repeats=2, seed=41)
    assert len(calls) == 6
    assert calls == study.schedule([support_case], "run", repeats=2, seed=41)
    assert len({call.request.request_id for call in calls}) == 6
    for repeat in range(2):
        grouped = next(
            call.request for call in calls if call.repeat == repeat and call.arm == "grouped"
        )
        singles = [
            call.request for call in calls if call.repeat == repeat and call.arm == "singleton"
        ]
        assert len(singles) == 2
        for single in singles:
            assert single.state == grouped.state
            assert single.source_refs == grouped.source_refs
            assert single.operation_id == grouped.operation_id
            assert single.questions[0] in grouped.questions
            assert single.subject_refs[0] in grouped.subject_refs
            assert single.subject_refs[0].claim_path == single.questions[0].question_id
            assert single.model_dump(
                exclude={"request_id", "questions", "subject_refs"}
            ) == grouped.model_dump(exclude={"request_id", "questions", "subject_refs"})


def test_seed_controls_order_without_changing_requests(support_case):
    cases = [{**support_case, "id": f"case-{i}"} for i in range(4)]
    first = study.schedule(cases, "run", repeats=2, seed=41)
    other = study.schedule(cases, "run", repeats=2, seed=42)
    assert [call.key for call in first] != [call.key for call in other]
    assert {call.key: call.request for call in first} == {call.key: call.request for call in other}


def test_group_failure_counts_every_assertion_and_never_clears(support_case):
    rows = study._initial_rows([support_case], 1)
    calls = study.schedule([support_case], "run", repeats=1, seed=41)
    for call in calls:
        observation = (
            study._failed(call.request, "toy_failure")
            if call.arm == "grouped"
            else _observation(call.request)
        )
        study._score_rows(rows, call, observation)
    summary = study.report(rows, [])
    group = summary["arms"]["grouped"]["pooled"]
    assert group["assertions"] == group["failed_assertions"] == 2
    assert group["accuracy"] == group["false_clear_rate"] == 0.0
    assert group["cleared_candidates"] == 0
    single = summary["arms"]["singleton"]["pooled"]
    assert single["false_support"] == single["false_clears"] == 1
    assert single["false_clear_rate"] == 1.0
    assert summary["paired"]["both_complete"] == 0
    assert summary["paired"]["incomplete_pairs"] == 2
    assert summary["paired"]["semantic_difference_count"] == 0


def test_precision_and_coverage_use_candidate_denominators(support_case):
    safe = deepcopy(support_case)
    safe["id"] = "safe"
    safe["expected"] = dict.fromkeys(safe["expected"], "supported")
    rows = study._initial_rows([safe, support_case], 1)
    for call in study.schedule([safe, support_case], "run", repeats=1, seed=41):
        study._score_rows(rows, call, _observation(call.request))
    scores = study.report(rows, [])["arms"]["grouped"]["pooled"]
    assert scores["accuracy"] == 0.75
    assert scores["clear_precision"] == 0.5
    assert scores["supported_candidate_coverage"] == 1.0
    assert scores["false_clear_rate"] == 1.0


def test_semantic_differences_require_two_completed_answers(support_case):
    rows = study._initial_rows([support_case], 1)
    for call in study.schedule([support_case], "run", repeats=1, seed=41):
        study._score_rows(
            rows,
            call,
            _observation(call.request, "supported" if call.arm == "grouped" else "insufficient"),
        )
    paired = study.report(rows, [])["paired"]
    assert paired["both_complete"] == paired["semantic_difference_count"] == 2
    assert paired["semantic_disagreement_rate"] == 1.0


@pytest.mark.asyncio
async def test_prepare_preserves_grid_and_writes_no_observed_cost(support_case, tmp_path):
    args = _args(tmp_path, [support_case])
    result = await study.run(args)
    for arm in study.ARMS:
        assert result["arms"][arm]["pooled"]["assertions"] == 4
        assert result["arms"][arm]["pooled"]["failed_assertions"] == 4
        assert result["arms"][arm]["accounting"]["observed_cost_usd"] is None
    manifest = json.loads((args.output_dir / "manifest.json").read_text())
    assert manifest["scheduled_calls"] == 6
    assert "benchmark/runner.py" in manifest["code_hashes"]
    assert "sibyl_core/tasks/procedure_review.py" in manifest["code_hashes"]


async def _completed_archive(tmp_path, support_case):
    args = _args(tmp_path, [support_case])
    await study.run(args)
    path = args.output_dir / "calls.json"
    calls = json.loads(path.read_text())
    by_key = {call["key"]: call for call in calls}
    for receipt in (args.output_dir / "receipts").glob("*.json"):
        stored = json.loads(receipt.read_text())
        request = study.DecisionRequest.model_validate_json(json.dumps(stored["request"]))
        stored["observation"] = _observation(request).model_dump(mode="json")
        receipt.write_text(json.dumps(stored))
        by_key[receipt.stem].update(stored["observation"])
        by_key[receipt.stem].update(
            dispatch_started=True, dispatch_started_at="2026-01-01T00:00:00+00:00"
        )
    path.write_text(json.dumps(calls))
    return args


@pytest.mark.asyncio
async def test_replay_scoring_preserves_usage(support_case, tmp_path):
    args = await _completed_archive(tmp_path, support_case)
    replay = _args(tmp_path, [support_case], "replay")
    replay.replay = args.output_dir
    result = await study.run(replay)
    assert result["arms"]["grouped"]["accounting"]["observed_cost_usd"] == 0.004
    assert result["arms"]["singleton"]["accounting"]["observed_cost_usd"] == 0.008
    assert result["paired"]["both_complete"] == 4
    assert json.loads((replay.output_dir / "calls.json").read_text()) == json.loads(
        (args.output_dir / "calls.json").read_text()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage", ["missing", "corrupt", "cost", "attempts", "latency", "request", "answers"]
)
async def test_replay_rejects_damaged_receipt_before_output(support_case, tmp_path, damage):
    args = await _completed_archive(tmp_path, support_case)
    receipt = next((args.output_dir / "receipts").glob("*.json"))
    envelope = json.loads(receipt.read_text())
    if damage == "missing":
        receipt.unlink()
    elif damage == "corrupt":
        receipt.write_text("broken json")
    else:
        if damage == "request":
            envelope["request"]["state"] += "changed"
        elif damage == "answers":
            envelope["observation"]["answers"][0]["value"] = "contradicted"
        else:
            field = {
                "cost": "observed_cost_usd",
                "attempts": "attempt_count",
                "latency": "elapsed_ms",
            }[damage]
            envelope["observation"][field] += 1
        receipt.write_text(json.dumps(envelope))
    replay = _args(tmp_path, [support_case], "replay")
    replay.replay = args.output_dir
    with pytest.raises(ValueError, match="replay receipt"):
        await study.run(replay)
    assert not replay.output_dir.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing_field", "boolean_attempts"])
async def test_replay_rejects_malformed_call_observation(support_case, tmp_path, damage):
    args = await _completed_archive(tmp_path, support_case)
    path = args.output_dir / "calls.json"
    calls = json.loads(path.read_text())
    if damage == "missing_field":
        calls[0].pop("error_category")
    else:
        calls[0]["attempt_count"] = True
    path.write_text(json.dumps(calls))
    replay = _args(tmp_path, [support_case], "replay")
    replay.replay = args.output_dir
    with pytest.raises(ValueError, match="replay receipt"):
        await study.run(replay)
    assert not replay.output_dir.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["cases_sha256", "code_hashes", "schedule_sha256", "route_policy_sha256"]
)
async def test_replay_rejects_changed_freeze(support_case, tmp_path, field):
    args = _args(tmp_path, [support_case])
    await study.run(args)
    path = args.output_dir / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest[field] = "changed"
    path.write_text(json.dumps(manifest))
    replay = _args(tmp_path, [support_case], "replay")
    replay.replay = args.output_dir
    with pytest.raises(ValueError, match=field):
        await study.run(replay)
    assert not replay.output_dir.exists()


@pytest.mark.asyncio
async def test_provider_exception_keeps_unknown_accounting(support_case, tmp_path, monkeypatch):
    class Broken:
        def __init__(self, key):
            pass

        async def decide(self, request):
            raise RuntimeError("secret text must not be persisted")

        async def aclose(self):
            pass

    monkeypatch.setattr(study, "OpenRouterDecisionProvider", Broken)
    monkeypatch.setenv("SIBYL_DECISION_OPENROUTER_API_KEY", "toy-not-a-secret")
    args = _args(tmp_path, [support_case])
    args.live = True
    result = await study.run(args)
    accounting = result["arms"]["grouped"]["accounting"]
    assert accounting["unknown_attempt_count_calls"] == 2
    assert accounting["observed_cost_usd"] is None
    assert "secret text" not in (args.output_dir / "calls.json").read_text()
    replay = _args(tmp_path, [support_case], "exception-replay")
    replay.replay = args.output_dir
    replay_result = await study.run(replay)
    assert replay_result == result
    original_calls = json.loads((args.output_dir / "calls.json").read_text())
    replay_calls = json.loads((replay.output_dir / "calls.json").read_text())
    assert replay_calls == original_calls
    assert all(call["dispatch_started_at"] is not None for call in original_calls)


@pytest.mark.asyncio
async def test_cancellation_keeps_all_scheduled_rows(support_case, tmp_path, monkeypatch):
    class Cancelled:
        def __init__(self, key):
            pass

        async def decide(self, request):
            raise asyncio.CancelledError

        async def aclose(self):
            pass

    monkeypatch.setattr(study, "OpenRouterDecisionProvider", Cancelled)
    monkeypatch.setenv("SIBYL_DECISION_OPENROUTER_API_KEY", "toy-not-a-secret")
    args = _args(tmp_path, [support_case])
    args.live = True
    with pytest.raises(asyncio.CancelledError):
        await study.run(args)
    predictions = json.loads((args.output_dir / "predictions.json").read_text())
    calls = json.loads((args.output_dir / "calls.json").read_text())
    assert len(predictions) == 8
    assert len(calls) == 6
    assert all(row["prediction"] is None for row in predictions)
