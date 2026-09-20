"""Toy-only freeze, replay integrity, leakage, and routing accounting checks."""

# Thresholds, costs, and counts below are explicit toy expectations.
# ruff: noqa: PLR2004

import argparse
import json
from copy import deepcopy

import pytest

from sibyl_core.ai.decisions import ChoiceAnswer, DecisionObservation, DecisionRequest
from sibyl_core.ai.openrouter_decisions import OpenRouterDecisionRoute

from . import support_fallback_study as study
from . import support_study


def _case(identifier, content, *, safe=True, claims=None):
    claims = claims or []
    return {
        "id": identifier,
        "category": "toy",
        "content": content,
        "claims": claims,
        "sources": [
            {
                "id": "raw",
                "text": content if safe else "The evidence describes a different outcome.",
                "provenance": "reported",
            }
        ],
        "expected": {
            "/content": "supported" if safe else "insufficient",
            **{f"/claim_records/{i}/content": "supported" for i in range(len(claims))},
        },
        "rationale": "Synthetic toy expectation.",
    }


async def _archive(tmp_path, name, cases, *, missing_cost=False):
    cases_path = tmp_path / f"{name}-cases.json"
    cases_path.write_text(json.dumps(cases))
    args = argparse.Namespace(
        cases=cases_path,
        output_dir=tmp_path / name,
        repeats=2,
        seed=19,
        concurrency=2,
        live=False,
        replay=None,
    )
    await support_study.run(args)
    calls_path = args.output_dir / "calls.json"
    calls = json.loads(calls_path.read_text())
    by_id = {case["id"]: case for case in cases}
    for call in calls:
        path = args.output_dir / "receipts" / f"{call['key']}.json"
        envelope = json.loads(path.read_text())
        request = DecisionRequest.model_validate_json(json.dumps(envelope["request"]))
        confidence = (
            1.0
            if all(label == "supported" for label in by_id[call["case_id"]]["expected"].values())
            else 0.75
        )
        observation = DecisionObservation(
            semantic_input_sha256=request.semantic_input_sha256,
            request_digest=request.request_digest,
            execution_status="completed",
            resolved_model_id=OpenRouterDecisionRoute().resolved_model_id,
            answers=tuple(
                ChoiceAnswer(
                    question_id=q.question_id, value="supported", provider_confidence=confidence
                )
                for q in request.questions
            ),
            input_tokens=100,
            output_tokens=10,
            observed_cost_usd=None if missing_cost else 0.001,
            usage_status="observed",
            attempt_count=1,
            elapsed_ms=10.0,
        )
        envelope["observation"] = observation.model_dump(mode="json")
        path.write_text(json.dumps(envelope))
        call.update(
            envelope["observation"],
            dispatch_started=True,
            dispatch_started_at="2026-01-01T00:00:00+00:00",
        )
    calls_path.write_text(json.dumps(calls))
    return cases_path, args.output_dir


async def _calibrated(tmp_path):
    calibration = [
        _case("cal-safe", "The beacon is green."),
        _case("cal-unsafe", "The lamp is red.", safe=False),
    ]
    heldout = [
        _case("test-safe", "The door is open.", claims=["The door is open."]),
        _case("test-unsafe", "The gate is closed.", safe=False),
    ]
    cases_path, run = await _archive(tmp_path, "calibration", calibration)
    holdout_path, holdout_run = await _archive(tmp_path, "holdout", heldout)
    out = tmp_path / "selected"
    selected = study.calibrate(cases_path, run, out, holdout_cases_sha=study._sha(holdout_path))
    return selected, out / "selection.json", holdout_path, holdout_run


@pytest.mark.asyncio
async def test_frozen_threshold_generalizes_without_refitting_and_counts_all_calls(tmp_path):
    selected, selection_path, cases_path, run = await _calibrated(tmp_path)
    assert selected["threshold"] == 1.0
    assert len(selected["candidate_rows"]) == 8
    result = study.evaluate(cases_path, run, selection_path, tmp_path / "evaluation")
    assert result["threshold"] == selected["threshold"]
    primary = result["methods"]["grouped_guarded"]
    assert primary["pooled"]["candidate_evaluations"] == 4
    assert primary["pooled"]["bypassed"] == 2
    assert primary["distinct"]["safe_bypass_both_repeats"] == 1
    assert primary["distinct"]["false_bypass_any_repeat"] == 0
    assert primary["multi_assertion"]["safe_bypass_coverage"] == 1.0
    assert primary["accounting"]["scheduled_calls"] == 4
    assert primary["conditional_break_even_support_check_cost_usd"] == 0.002
    singleton = result["methods"]["singleton_guarded"]
    assert singleton["accounting"]["scheduled_calls"] == 6
    assert singleton["conditional_break_even_support_check_cost_usd"] == 0.003


@pytest.mark.asyncio
async def test_false_bypass_never_yields_economic_qualification(tmp_path):
    path, run = await _archive(
        tmp_path, "unsafe", [_case("unsafe", "The lamp is green.", safe=False)]
    )
    _, rows, bindings = study._load_run(path, run)
    result = study.score(rows, bindings["accounting"], 0.5)
    primary = result["methods"]["grouped_guarded"]
    assert primary["pooled"]["bypassed"] == primary["pooled"]["false_bypass"] == 2
    assert primary["conditional_break_even_support_check_cost_usd"] is None
    assert primary["economic_status"] == "conditional_non_activation"
    rejected = study.score(rows, bindings["accounting"], None)
    assert rejected["methods"]["grouped_guarded"]["pooled"]["bypassed"] == 0
    assert (
        rejected["methods"]["grouped_guarded"]["conditional_break_even_support_check_cost_usd"]
        is None
    )


@pytest.mark.asyncio
async def test_unknown_cost_does_not_invent_break_even(tmp_path):
    cases = [_case("safe", "The switch is open.")]
    path, run = await _archive(tmp_path, "unknown-cost", cases, missing_cost=True)
    _, rows, bindings = study._load_run(path, run)
    result = study.score(rows, bindings["accounting"], 1.0)
    assert (
        result["methods"]["grouped_guarded"]["conditional_break_even_support_check_cost_usd"]
        is None
    )
    assert (
        result["methods"]["grouped_guarded"]["accounting"]["unknown_observed_cost_usd_calls"] == 2
    )


@pytest.mark.asyncio
async def test_numeric_guard_and_ablation_share_threshold(tmp_path):
    path, run = await _archive(tmp_path, "numeric", [_case("safe", "Two crates arrived.")])
    _, rows, bindings = study._load_run(path, run)
    result = study.score(rows, bindings["accounting"], 1.0)
    assert result["methods"]["grouped_guarded"]["pooled"]["bypassed"] == 0
    assert result["methods"]["grouped_guarded"]["pooled"]["fallback_reasons"] == {"numeric_text": 2}
    assert result["methods"]["grouped_confidence_only"]["pooled"]["bypassed"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage", ["fixture", "route", "program", "product", "repeats", "schedule", "receipt"]
)
async def test_calibration_rejects_changed_inputs_before_output(tmp_path, damage):
    path, run = await _archive(tmp_path, "calibration", [_case("safe", "The lamp is green.")])
    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if damage == "fixture":
        path.write_text(path.read_text() + "\n")
    elif damage == "schedule":
        (run / "schedule.json").write_text("[]")
    elif damage == "receipt":
        next((run / "receipts").glob("*.json")).unlink()
    else:
        if damage == "route":
            manifest["route_policy_sha256"] = "0" * 64
        elif damage == "program":
            manifest["program_hashes"]["support_inputs.py"] = "0" * 64
        elif damage == "product":
            manifest["code_hashes"]["sibyl_core/ai/decisions.py"] = "0" * 64
        else:
            manifest["repeats"] = 1
        manifest_path.write_text(json.dumps(manifest))
    out = tmp_path / "selection"
    with pytest.raises(ValueError, match=r"mismatch|differs|requires|receipt"):
        study.calibrate(path, run, out, holdout_cases_sha="a" * 64)
    assert not out.exists()


@pytest.mark.asyncio
async def test_historical_runner_exception_is_exact_explicit_and_recorded(tmp_path):
    path, run = await _archive(tmp_path, "historical", [_case("safe", "The lamp is green.")])
    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["code_hashes"][study._RUNNER_KEY] = study.HISTORICAL_RUNNER_SHA
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="code_hashes"):
        study.calibrate(path, run, tmp_path / "implicit", holdout_cases_sha="a" * 64)
    with pytest.raises(ValueError, match="historical runner"):
        study.calibrate(
            path,
            run,
            tmp_path / "wrong",
            holdout_cases_sha="a" * 64,
            historical_runner_sha="b" * 64,
        )
    selection = study.calibrate(
        path,
        run,
        tmp_path / "explicit",
        holdout_cases_sha="a" * 64,
        historical_runner_sha=study.HISTORICAL_RUNNER_SHA,
    )
    assert selection["calibration"]["historical_runner_exception"] == study.HISTORICAL_RUNNER_SHA


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["threshold", "threshold_table", "policy_sha256", "analysis_sha256", "candidate_rows"]
)
async def test_evaluation_rejects_selection_tamper_even_with_recomputed_digest(tmp_path, field):
    selection, selection_path, cases, run = await _calibrated(tmp_path)
    selection[field] = (
        0.5
        if field == "threshold"
        else []
        if field in {"threshold_table", "candidate_rows"}
        else "0" * 64
    )
    selection["selection_sha256"] = study._digest(
        {key: value for key, value in selection.items() if key != "selection_sha256"}
    )
    selection_path.write_text(json.dumps(selection))
    out = tmp_path / "evaluation"
    with pytest.raises(ValueError, match=r"changed|differs"):
        study.evaluate(cases, run, selection_path, out)
    assert not out.exists()


@pytest.mark.asyncio
async def test_evaluation_rejects_unfrozen_holdout_bytes(tmp_path):
    _, selection_path, cases, run = await _calibrated(tmp_path)
    cases.write_text(cases.read_text() + "\n")
    with pytest.raises(ValueError, match="frozen hash"):
        study.evaluate(cases, run, selection_path, tmp_path / "evaluation")


@pytest.mark.asyncio
@pytest.mark.parametrize("reuse", ["identity", "semantic_content"])
async def test_evaluation_rejects_calibration_leakage(tmp_path, reuse):
    calibration = [_case("cal-safe", "The beacon is green.")]
    heldout = deepcopy(calibration)
    if reuse == "identity":
        heldout[0]["content"] = "An unrelated door is open."
    else:
        heldout[0]["id"] = "new-id"
        heldout[0]["sources"][0]["id"] = "renamed-source"
        heldout[0]["category"] = "new-category"
        heldout[0]["rationale"] = "different annotation"
    path, run = await _archive(tmp_path, "calibration", calibration)
    holdout_path, holdout_run = await _archive(tmp_path, "heldout", heldout)
    study.calibrate(path, run, tmp_path / "selected", holdout_cases_sha=study._sha(holdout_path))
    with pytest.raises(ValueError, match="overlap"):
        study.evaluate(
            holdout_path, holdout_run, tmp_path / "selected/selection.json", tmp_path / "evaluation"
        )


def test_semantic_fingerprints_ignore_gold_order_and_whitespace():
    case = _case("left", "Door is open.", claims=["Handle is up.", "Latch is free."])
    changed = deepcopy(case)
    changed.update(id="right", expected={}, content="Door  is open.")
    changed["claims"].reverse()
    changed["sources"][0]["id"] = "other"
    assert study._semantic_fingerprint(changed) == study._semantic_fingerprint(case)
