"""Fresh hints must include acquisition cost and time without contaminating controls."""

# Explicit synthetic counts and costs form the accounting oracle.
# ruff: noqa: PLR2004

import asyncio
import json
import time
from copy import deepcopy

import httpx
import pytest

from sibyl_core.ai.openrouter_decisions import DECISIONS_ENDPOINT, OpenRouterDecisionRoute

from . import fast_critic
from . import heldout_fast as study
from .runner import _sha


@pytest.fixture
def case():
    return {
        "id": "toy-heldout",
        "pair_group": "toy-pair",
        "category": "scope",
        "content": "The valve is closed.",
        "claims": [],
        "sources": [{"id": "source", "text": "The valve is closed.", "provenance": "signed"}],
        "expected": {"/content": "supported"},
        "expected_action": "accept",
        "rationale": "GOLD-ONLY-CANARY",
        "stress_hints": {"/content": "contradicted"},
    }


def inputs(tmp_path, case):
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([case]))
    rubric = tmp_path / "rubric.json"
    rubric.write_text(json.dumps({"cases_sha256": _sha(cases), "cases": [{"case_id": case["id"]}]}))
    return cases, rubric


def install_network(monkeypatch, *, seen=None, jev="supported", critic="accept", delay=0):
    real_client = httpx.AsyncClient

    async def respond(request):
        body = json.loads(await request.aread())
        if seen is not None:
            seen.append((str(request.url), body))
        assert "GOLD-ONLY-CANARY" not in json.dumps(body)
        assert "stress_hints" not in json.dumps(body)
        if str(request.url) == DECISIONS_ENDPOINT:
            if delay:
                await asyncio.sleep(delay)
            if jev == "transport":
                raise httpx.ReadTimeout("SECRET-ERROR-CANARY")
            return httpx.Response(
                200,
                json={
                    "model": OpenRouterDecisionRoute().resolved_model_id,
                    "provider": "wrong-route" if jev == "wrong_route" else "TypeSafe",
                    "answers": {
                        key: {"type": "choice", "choice": "supported", "confidence": 0.999}
                        for key in body["questions"]
                    },
                    "usage": {"input_tokens": 100, "output_tokens": 2, "cost": 0.002},
                },
            )
        assert str(request.url) == fast_critic.CONTROLS["endpoint"]
        if critic == "transport":
            raise httpx.ReadTimeout("SECRET-ERROR-CANARY")
        output = {"findings": [], "abstention_reason": None}
        if critic == "invalid":
            output["findings"] = [{"basis": "not-a-real-basis"}]
        return httpx.Response(
            200,
            json={
                "model": fast_critic.CONTROLS["model"],
                "provider": "Anthropic",
                "usage": {
                    "cost": 0.003,
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                },
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": "CriticOutput",
                                        "arguments": json.dumps(output),
                                    },
                                }
                            ]
                        },
                    }
                ],
            },
        )

    def client(**kw):
        if isinstance(kw.get("transport"), httpx.AsyncHTTPTransport):
            kw["transport"] = httpx.MockTransport(respond)
        return real_client(**kw)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setenv("SIBYL_JEV_CRITIC_OPENROUTER_API_KEY", "synthetic-test-only")


async def live_fixture(tmp_path, monkeypatch, case, **network):
    cases, rubric = inputs(tmp_path, case)
    plan = tmp_path / "plan"
    assert await study.run(cases, rubric, plan) is None
    install_network(monkeypatch, **network)
    out = tmp_path / "live"
    result = await study.run(cases, rubric, out, freeze=plan / "manifest.json", live=True)
    assert result is not None
    return cases, rubric, plan, out, result


@pytest.mark.asyncio
async def test_heldout_live_replay_three_distinct_arms(case, tmp_path, monkeypatch):
    seen = []
    cases, rubric, _, out, result = await live_fixture(tmp_path, monkeypatch, case, seen=seen)
    rows = json.loads((out / "rows.json").read_text())
    assert len(rows) == 6
    assert len(seen) == 8  # Six critics plus two actual Jev acquisitions.
    for arm in ("direct", "live_jev", "misleading"):
        selected = [r for r in rows if r["arm"] == arm]
        assert len(selected) == 2
        assert all(r["action"] == "accept" for r in selected)
        assert all(
            r["stages"] == (["jev", "critic"] if arm == "live_jev" else ["critic"])
            for r in selected
        )
        for r in selected:
            assert (
                r["hints"] == []
                if arm == "direct"
                else r["hints"][0]["value"]
                == ("supported" if arm == "live_jev" else "contradicted")
            )
            assert r["known_cost_usd"] == ("0.005" if arm == "live_jev" else "0.003")
            assert r["unknown_cost_calls"] == 0
    assert await study.run(cases, rubric, tmp_path / "replay", replay_path=out) == result
    assert len(seen) == 8
    with pytest.raises(FileExistsError):
        await study.run(cases, rubric, out)


def test_heldout_schedule_does_not_leak_gold_or_stress_to_live_jev(case):
    entries = study.entries([case])
    assert len(entries) == 6
    assert entries == study.entries([case])
    other = deepcopy(case)
    other["rationale"] = "ANOTHER-UNSENT-GOLD"
    other["expected"] = {"/content": "insufficient"}
    other["expected_action"] = "flag"
    other["stress_hints"] = {"/content": "supported"}
    changed = study.entries([other])
    for a, b in zip(entries, changed, strict=True):
        assert a["prepared_payload"] == b["prepared_payload"]
        assert a["jev_wire"] == b["jev_wire"]
        assert a["direct_request"] == b["direct_request"]
        assert "GOLD-ONLY-CANARY" not in json.dumps(a["jev_wire"])
        assert "GOLD-ONLY-CANARY" not in json.dumps(a["direct_request"])


@pytest.mark.asyncio
@pytest.mark.parametrize("jev", ["transport", "wrong_route"])
async def test_heldout_failed_jev_falls_back_and_keeps_cost(case, tmp_path, monkeypatch, jev):
    _, _, _, out, _ = await live_fixture(tmp_path, monkeypatch, case, jev=jev)
    rows = json.loads((out / "rows.json").read_text())
    for r in rows:
        if r["arm"] == "live_jev":
            assert r["stages"] == ["jev", "critic"]
            assert r["hints"] == []
            assert r["action"] == "accept"
            assert r["known_cost_usd"] == ("0.003" if jev == "transport" else "0.005")
            assert r["unknown_cost_calls"] == (1 if jev == "transport" else 0)
    assert all("SECRET-ERROR-CANARY" not in p.read_text() for p in out.rglob("*.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("critic", ["transport", "invalid"])
async def test_heldout_critic_failure_is_not_abstention(case, tmp_path, monkeypatch, critic):
    _, _, _, out, _ = await live_fixture(tmp_path, monkeypatch, case, critic=critic)
    rows = json.loads((out / "rows.json").read_text())
    assert len(rows) == 6
    assert all(r["action"] == "error" for r in rows)
    for r in rows:
        assert r["unknown_cost_calls"] == (1 if critic == "transport" else 0)
        expected = (0.002 if r["arm"] == "live_jev" else 0) + (0.003 if critic == "invalid" else 0)
        assert float(r["known_cost_usd"]) == pytest.approx(expected)


@pytest.mark.asyncio
async def test_heldout_acquisition_time_is_inside_path(case, tmp_path, monkeypatch):
    _, _, _, out, _ = await live_fixture(tmp_path, monkeypatch, case, delay=0.04)
    rows = json.loads((out / "rows.json").read_text())
    for r in rows:
        if r["arm"] == "live_jev":
            assert r["service_ms"] >= 40
            raw = json.loads((out / "raw" / f"{r['id']}.jev.json").read_text())
            assert r["service_ms"] >= raw["elapsed_ms"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    ["raw", "invocation", "row", "extra", "completion", "rubric", "missing_rubric", "timing"],
)
async def test_heldout_replay_rejects_corrupt_archive(case, tmp_path, monkeypatch, corruption):
    cases, rubric, _, out, _ = await live_fixture(tmp_path, monkeypatch, case)
    if corruption == "extra":
        (out / "raw" / "unexpected.json").write_text("{}")
    elif corruption == "completion":
        (out / "completion.json").write_text('{"mode":"live","paths":1}')
    elif corruption == "rubric":
        (out / "rubric.json").write_text("{}")
    elif corruption == "missing_rubric":
        (out / "rubric.json").unlink()
    elif corruption == "raw":
        f = next((out / "raw").glob("*.jev.json"))
        v = json.loads(f.read_text())
        v["request"]["state"] = "changed"
        f.write_text(json.dumps(v))
    elif corruption == "invocation":
        f = next(
            p for p in (out / "invocations").glob("*.json") if json.loads(p.read_text())["hints"]
        )
        v = json.loads(f.read_text())
        v["hints"] = []
        f.write_text(json.dumps(v))
    else:
        f = out / "rows.json"
        v = json.loads(f.read_text())
        if corruption == "row":
            v[0]["action"] = "flag"
        else:
            v[0]["service_ms"] = -1
            (out / "paths" / f"{v[0]['id']}.json").write_text(json.dumps(v[0]))
        f.write_text(json.dumps(v))
    with pytest.raises((ValueError, FileNotFoundError)):
        await study.run(cases, rubric, tmp_path / "replay", replay_path=out)
    assert not (tmp_path / "replay").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["freeze", "rubric", "stress"])
async def test_heldout_changed_inputs_reject_before_egress(case, tmp_path, monkeypatch, corruption):
    cases, rubric = inputs(tmp_path, case)
    plan = tmp_path / "plan"
    await study.run(cases, rubric, plan)
    if corruption == "freeze":
        f = plan / "manifest.json"
        v = json.loads(f.read_text())
        v["seed"] += 1
    elif corruption == "rubric":
        f = rubric
        v = json.loads(f.read_text())
        v["cases_sha256"] = "0" * 64
    else:
        f = cases
        v = json.loads(f.read_text())
        v[0]["stress_hints"] = {"/content": "supported"}
    f.write_text(json.dumps(v))
    seen = []
    install_network(monkeypatch, seen=seen)
    with pytest.raises(ValueError, match=r"frozen|rubric"):
        await study.run(cases, rubric, tmp_path / "live", freeze=plan / "manifest.json", live=True)
    assert not (tmp_path / "live").exists()
    assert seen == []


@pytest.mark.asyncio
async def test_heldout_cancellation_keeps_queued_and_dispatched_paths(case, tmp_path, monkeypatch):
    other = deepcopy(case)
    other["id"] = "second-toy"
    cases, rubric = inputs(tmp_path, case)
    cases.write_text(json.dumps([case, other]))
    rubric.write_text(
        json.dumps(
            {"cases_sha256": _sha(cases), "cases": [{"case_id": c["id"]} for c in [case, other]]}
        )
    )
    plan = tmp_path / "plan"
    await study.run(cases, rubric, plan)
    called = asyncio.Event()
    count = 0

    async def block(request):
        nonlocal count
        count += 1
        if count == study.CONCURRENCY:
            called.set()
        await asyncio.Event().wait()

    real = httpx.AsyncClient

    def client(**kw):
        if isinstance(kw.get("transport"), httpx.AsyncHTTPTransport):
            kw["transport"] = httpx.MockTransport(block)
        return real(**kw)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setenv("SIBYL_JEV_CRITIC_OPENROUTER_API_KEY", "synthetic-test-only")
    out = tmp_path / "live"
    task = asyncio.create_task(
        study.run(cases, rubric, out, freeze=plan / "manifest.json", live=True)
    )
    await asyncio.wait_for(called.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (out / "completion.json").exists()
    partial = json.loads((out / "partial.json").read_text())
    assert partial["scheduled"] == 12
    assert len(partial["missing"]) == 12
    assert len(list((out / "raw").glob("*.dispatch.json"))) == 8
    assert len(list((out / "raw").glob("*.json"))) == 16


@pytest.mark.asyncio
@pytest.mark.parametrize("arm", ["direct", "live_jev"])
async def test_heldout_preparation_is_inside_measured_path(case, tmp_path, monkeypatch, arm):
    entry = next(e for e in study.entries([case]) if e["arm"] == arm)
    prepare = study.critic_pair.prepare_case
    calls = []

    def delayed_prepare(*args):
        time.sleep(0.02)
        calls.append(args)
        return prepare(*args)

    monkeypatch.setattr(study.critic_pair, "prepare_case", delayed_prepare)
    install_network(monkeypatch)
    for directory in ("raw", "invocations"):
        (tmp_path / directory).mkdir()
    async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(retries=0)) as client:
        row = await study.execute(entry, case, client=client, out=tmp_path)
    assert len(calls) == 1
    assert row["service_ms"] >= 20
