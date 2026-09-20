"""Six-arm orchestration preserves physical calls, contracts, accounting and replay."""

# Explicit toy budgets and factorial counts form the accounting oracle.
# ruff: noqa: PLR2004

import asyncio
import json
from copy import deepcopy

import httpx
import pytest

from sibyl_core.ai.decisions import DecisionRequest
from sibyl_core.ai.openrouter_decisions import DECISIONS_ENDPOINT
from sibyl_core.tasks.memory_validation import PreparedMemoryValidation

from . import assertion_study as study
from . import fast_critic, heldout_fast
from .test_heldout_fast import case as case  # noqa: PLC0414
from .test_heldout_fast import inputs, install_network


def test_six_arm_schedule_keeps_evidence_and_unique_physical_jev_identity(case):
    entries = study.entries([case], repeats=1)
    assert len(entries) == 6
    assert {e["arm"] for e in entries} == set(study.ARMS)
    assert entries == study.entries([case], repeats=1)
    assert len({e["prepared_payload"] for e in entries}) == 1
    live = [e for e in entries if e["hint_mode"] == "live_jev"]
    assert len(live) == 2
    requests = [DecisionRequest.model_validate_json(json.dumps(e["jev_request"])) for e in live]
    assert requests[0].request_id != requests[1].request_id
    assert requests[0].request_digest != requests[1].request_digest
    assert requests[0].semantic_input_sha256 == requests[1].semantic_input_sha256
    assert live[0]["jev_wire"] == live[1]["jev_wire"]
    for e in entries:
        prepared = PreparedMemoryValidation(e["prepared_payload"])
        baseline = fast_critic.critic_request(prepared, [])
        if e["contract"] == "baseline":
            assert e["direct_request"] == baseline
        else:
            assert e["direct_request"]["tools"] == baseline["tools"]
            assert prepared.prompt in e["direct_request"]["messages"][0]["content"]
        assert "GOLD-ONLY-CANARY" not in json.dumps(e["direct_request"])
        assert "GOLD-ONLY-CANARY" not in json.dumps(e["jev_wire"])


@pytest.mark.parametrize("repeats", [0, -1, True])
def test_invalid_repeats_rejected(case, repeats):
    with pytest.raises(ValueError, match="positive integer"):
        study.entries([case], repeats=repeats)


async def fixture_run(case, tmp_path, monkeypatch, **network):
    cases, rubric = inputs(tmp_path, case)
    plan = tmp_path / "plan"
    await study.run(cases, rubric, plan, repeats=1)
    seen = []
    install_network(monkeypatch, seen=seen, **network)
    out = tmp_path / "live"
    result = await study.run(
        cases, rubric, out, repeats=1, freeze=plan / "manifest.json", live=True
    )
    return cases, rubric, plan, out, result, seen


@pytest.mark.asyncio
async def test_live_factorial_replays_without_new_calls(case, tmp_path, monkeypatch):
    cases, rubric, plan, out, result, seen = await fixture_run(case, tmp_path, monkeypatch)
    current = json.loads((plan / "manifest.json").read_text())
    assert current["critic_calls"] == 6
    assert current["jev_calls"] == 2
    assert current["contracts"] == ["baseline", "assertion"]
    assert len(seen) == 8
    assert sum(url == DECISIONS_ENDPOINT for url, _ in seen) == 2
    rows = json.loads((out / "rows.json").read_text())
    for row in rows:
        assert row["arm"] == f"{row['contract']}_{row['hint_mode']}"
        expected_cost = "0.005" if row["hint_mode"] == "live_jev" else "0.003"
        assert row["known_cost_usd"] == expected_cost
        invocation = json.loads((out / "invocations" / f"{row['id']}.json").read_text())
        assert invocation["contract"] == row["contract"]
        assert row["action"] == "accept"
    assert set(result["comparisons"]) == set(study.COMPARISONS)
    assert result["all_arms_acquisition"]["known_cost_usd"] == "0.022"
    assert result["all_arms_acquisition"]["jev_calls"] == 2
    assert result["semantic_review_status"] == "pending"
    assert not result["production_qualified"]
    replay = await study.run(cases, rubric, tmp_path / "replay", repeats=1, replay_path=out)
    assert replay == result
    assert len(seen) == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["wrong_route", "transport"])
async def test_each_live_arm_keeps_independent_failed_jev_accounting(
    case, tmp_path, monkeypatch, failure
):
    _, _, _, out, result, seen = await fixture_run(case, tmp_path, monkeypatch, jev=failure)
    assert sum(url == DECISIONS_ENDPOINT for url, _ in seen) == 2
    rows = json.loads((out / "rows.json").read_text())
    for row in rows:
        if row["hint_mode"] == "live_jev":
            assert row["hints"] == []
            assert row["action"] == "accept"
            assert row["unknown_cost_calls"] == (1 if failure == "transport" else 0)
    for arm in ("baseline_live_jev", "assertion_live_jev"):
        assert result["arms"][arm]["jev_failures"] == 1
        assert result["arms"][arm]["known_cost_usd"] == (
            "0.003" if failure == "transport" else "0.005"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["contract", "decision_request_id", "hint_mode"])
async def test_contract_corruption_rejected_before_egress(case, damage):
    entry = deepcopy(study.entries([case], repeats=1)[0])
    entry[damage] = "changed"
    with pytest.raises(ValueError, match=r"binding changed|identity changed"):
        await study.execute(entry, case, client=None, out=None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage", ["invocation_contract", "row_contract", "raw_prompt", "observation_cost"]
)
async def test_replay_rederives_contract_and_cost(case, tmp_path, monkeypatch, damage):
    cases, rubric, _, out, _, seen = await fixture_run(case, tmp_path, monkeypatch)
    if damage == "invocation_contract":
        path = next((out / "invocations").glob("*.json"))
        record = json.loads(path.read_text())
        record["contract"] = "assertion" if record["contract"] == "baseline" else "baseline"
    elif damage == "raw_prompt":
        path = next((out / "raw").glob("*.critic.json"))
        record = json.loads(path.read_text())
        record["request"]["messages"][0]["content"] += "extra instruction"
    else:
        path = out / "rows.json"
        record = json.loads(path.read_text())
        row = next(r for r in record if r["hint_mode"] == "live_jev")
        if damage == "row_contract":
            row["contract"] = "unknown"
        else:
            row["jev_observation"]["observed_cost_usd"] = 0
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="mismatch"):
        await study.run(cases, rubric, tmp_path / "replay", repeats=1, replay_path=out)
    assert not (tmp_path / "replay").exists()
    assert len(seen) == 8


@pytest.mark.asyncio
async def test_changed_repeat_count_fails_freeze_before_calls(case, tmp_path, monkeypatch):
    cases, rubric = inputs(tmp_path, case)
    plan = tmp_path / "plan"
    await study.run(cases, rubric, plan, repeats=1)
    seen = []
    install_network(monkeypatch, seen=seen)
    with pytest.raises(ValueError, match="frozen manifest"):
        await study.run(
            cases, rubric, tmp_path / "live", repeats=2, freeze=plan / "manifest.json", live=True
        )
    assert not seen


@pytest.mark.asyncio
async def test_injected_executor_keeps_cancellation_receipts(case, tmp_path, monkeypatch):
    cases, rubric = inputs(tmp_path, case)
    plan = tmp_path / "plan"
    await study.run(cases, rubric, plan, repeats=2)
    dispatched, count = asyncio.Event(), 0

    async def blocked(request):
        nonlocal count
        count += 1
        if count == heldout_fast.CONCURRENCY:
            dispatched.set()
        await asyncio.Event().wait()

    original = httpx.AsyncClient

    def client(**kwargs):
        if isinstance(kwargs.get("transport"), httpx.AsyncHTTPTransport):
            kwargs["transport"] = httpx.MockTransport(blocked)
        return original(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setenv("SIBYL_JEV_CRITIC_OPENROUTER_API_KEY", "synthetic-mock-only")
    out = tmp_path / "live"
    task = asyncio.create_task(
        study.run(cases, rubric, out, repeats=2, freeze=plan / "manifest.json", live=True)
    )
    await asyncio.wait_for(dispatched.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (out / "completion.json").exists()
    partial = json.loads((out / "partial.json").read_text())
    assert partial["scheduled"] == len(partial["missing"]) == 12
    assert len(list((out / "raw").glob("*.json"))) == 16
