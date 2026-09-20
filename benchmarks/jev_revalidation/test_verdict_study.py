"""Verdict factorial execution preserves exact contracts, acquisition costs and replay."""
# Synthetic factorial counts and usage form the accounting oracle.
# ruff: noqa: PLR2004

import json

import httpx
import pytest

from sibyl_core.ai.openrouter_decisions import DECISIONS_ENDPOINT, OpenRouterDecisionRoute
from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.memory_validation import PreparedMemoryValidation

from . import assertion_critic, assertion_study, critic_pair, verdict_critic, verdict_study
from .test_fast_critic import body_for
from .test_heldout_fast import case as case  # noqa: PLC0414
from .test_heldout_fast import inputs
from .test_verdict_critic import supported_output


def test_schedule_preserves_baseline_and_independent_live_acquisition(case):
    schedule = verdict_study.entries([case], repeats=1)
    assert schedule == verdict_study.entries([case], repeats=1)
    assert {e["arm"] for e in schedule} == set(verdict_study.ARMS)
    assert len(schedule) == 6
    assert {e["contract"] for e in assertion_study.entries([case], repeats=1)} == {
        "baseline",
        "assertion",
    }
    live = [e for e in schedule if e["hint_mode"] == "live_jev"]
    assert len({e["decision_request_id"] for e in live}) == 2
    assert live[0]["jev_wire"] == live[1]["jev_wire"]
    assert live[0]["prepared_payload"] == live[1]["prepared_payload"]
    for entry in schedule:
        prepared = PreparedMemoryValidation(entry["prepared_payload"])
        if entry["contract"] == "baseline":
            assert entry["direct_request"] == assertion_critic.critic_request(
                prepared, [], contract="assertion"
            )
        assert "GOLD-ONLY-CANARY" not in canonical(entry["direct_request"])


def install_network(monkeypatch, schedule, seen, *, jev_failure=False):
    bodies = {}
    for entry in schedule:
        prepared = PreparedMemoryValidation(entry["prepared_payload"])
        hashes = json.loads(entry["prepared_payload"])["assertion_hashes"]
        hints = [
            {"claim_path": k, "claim_sha256": v, "value": "supported"} for k, v in hashes.items()
        ]
        live = verdict_critic.critic_request(prepared, hints, contract=entry["contract"])
        output = supported_output(entry) if entry["contract"] == "verdict" else None
        response = body_for(output)
        if output is not None:
            response["choices"][0]["message"]["tool_calls"][0]["function"]["name"] = "VerdictOutput"
        for wire in (live, entry["direct_request"], entry["stress_request"]):
            bodies[critic_pair.digest(canonical(wire))] = response
    real_client = httpx.AsyncClient

    async def respond(request):
        body = json.loads(await request.aread())
        seen.append(str(request.url))
        if str(request.url) == DECISIONS_ENDPOINT:
            return httpx.Response(
                200,
                json={
                    "model": OpenRouterDecisionRoute().resolved_model_id,
                    "provider": "wrong" if jev_failure else "TypeSafe",
                    "answers": {
                        key: {"type": "choice", "choice": "supported"} for key in body["questions"]
                    },
                    "usage": {"input_tokens": 100, "output_tokens": 2, "cost": 0.002},
                },
            )
        return httpx.Response(200, json=bodies[critic_pair.digest(canonical(body))])

    def client(**kwargs):
        if isinstance(kwargs.get("transport"), httpx.AsyncHTTPTransport):
            kwargs["transport"] = httpx.MockTransport(respond)
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setenv("SIBYL_JEV_CRITIC_OPENROUTER_API_KEY", "synthetic-only")


async def fixture_run(case, tmp_path, monkeypatch, *, jev_failure=False):
    cases, rubric = inputs(tmp_path, case)
    plan = tmp_path / "plan"
    await verdict_study.run(cases, rubric, plan, repeats=1)
    seen = []
    install_network(
        monkeypatch, verdict_study.entries([case], repeats=1), seen, jev_failure=jev_failure
    )
    out = tmp_path / "live"
    result = await verdict_study.run(
        cases, rubric, out, repeats=1, freeze=plan / "manifest.json", live=True
    )
    return cases, rubric, out, result, seen


@pytest.mark.asyncio
@pytest.mark.parametrize("jev_failure", [False, True])
async def test_factorial_live_and_exact_replay_with_failed_acquisition(
    case, tmp_path, monkeypatch, jev_failure
):
    cases, rubric, out, result, seen = await fixture_run(
        case, tmp_path, monkeypatch, jev_failure=jev_failure
    )
    assert len(seen) == 8
    assert seen.count(DECISIONS_ENDPOINT) == 2
    assert result["all_arms_acquisition"]["known_cost_usd"] == "0.022"
    assert set(result["comparisons"]) == set(verdict_study.COMPARISONS)
    assert not result["production_qualified"]
    rows = json.loads((out / "rows.json").read_text())
    for row in rows:
        if row["contract"] == "verdict":
            assert row["critic"]["verdict_output"]["verdicts"]
            assert row["critic"]["projected_output"] == {"findings": [], "abstention_reason": None}
        if row["hint_mode"] == "live_jev" and jev_failure:
            assert row["hints"] == []
            assert row["jev_observation"]["execution_status"] == "invalid_response"
            assert row["known_cost_usd"] == "0.005"
    replay = await verdict_study.run(cases, rubric, tmp_path / "replay", repeats=1, replay_path=out)
    assert replay == result
    assert len(seen) == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["projection", "verdict", "contract", "request", "cost"])
async def test_replay_rejects_changed_projection_and_exact_receipts(
    case, tmp_path, monkeypatch, damage
):
    cases, rubric, out, _, seen = await fixture_run(case, tmp_path, monkeypatch)
    rows = json.loads((out / "rows.json").read_text())
    row = next(r for r in rows if r["contract"] == "verdict")
    path = out / "rows.json"
    record = rows
    if damage == "projection":
        row["critic"]["projected_output"]["abstention_reason"] = "changed"
    elif damage == "verdict":
        row["critic"]["verdict_output"]["verdicts"][0]["rationale"] = "changed"
    elif damage == "cost":
        row["critic"]["usage"]["observed_cost_usd"] = 0
    else:
        path = out / "invocations" / f"{row['id']}.json"
        record = json.loads(path.read_text())
        if damage == "contract":
            record["contract"] = "baseline"
        else:
            record["request"]["tools"][0]["function"]["parameters"] = {}
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="mismatch"):
        await verdict_study.run(cases, rubric, tmp_path / "replay", repeats=1, replay_path=out)
    assert not (tmp_path / "replay").exists()
    assert len(seen) == 8
