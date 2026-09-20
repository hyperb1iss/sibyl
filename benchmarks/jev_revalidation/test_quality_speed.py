"""Complete synthetic validation paths preserve quality, timing and raw evidence."""

# Explicit toy timings and costs make path accounting independently checkable.
# ruff: noqa: PLR2004

import asyncio
import base64
import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from sibyl_core.ai.openrouter_decisions import DECISIONS_ENDPOINT, OpenRouterDecisionRoute

from . import quality_speed as study


@pytest.fixture
def case():
    return {
        "id": "valve-case",
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


def entry(case, arm="jev_then_critic"):
    return next(
        item for item in study.entries([case]) if item["arm"] == arm and item["repeat"] == 0
    )


def critic_body(request, outcome="accept"):
    payload = json.loads(request["messages"][0]["content"].split("\n\n", 1)[1])
    output = {"findings": [], "abstention_reason": None}
    if outcome == "abstain":
        output["abstention_reason"] = "The source does not identify the inspected valve."
    elif outcome in {"flag", "invalid"}:
        output["findings"] = [
            {
                "claim_path": "/content",
                "claim_sha256": payload["assertion_hashes"]["/content"],
                "evidence_refs": [{"evidence_id": "s0" if outcome == "flag" else "valve-source"}],
                "basis": "factual_contradiction",
                "disposition": "reconsider",
                "critique": "The inspected valve was open.",
            }
        ]
    return {
        "model": "anthropic/claude-opus-5",
        "provider": "Anthropic",
        "usage": {"cost": 0.03, "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {"name": "CriticOutput", "arguments": json.dumps(output)},
                        }
                    ]
                },
            }
        ],
    }


def network(
    *,
    choice="supported",
    confidence=0.999,
    outcome="accept",
    clock=None,
    seen=None,
    fail_jev=False,
    fail_critic=False,
    stall_jev=False,
):
    async def respond(request):
        body = json.loads(await request.aread())
        if seen is not None:
            seen.append((str(request.url), body))
        if "GOLD-ONLY-CANARY" in json.dumps(body):
            pytest.fail("offline gold reached the wire")
        if str(request.url) == DECISIONS_ENDPOINT:
            if clock is not None:
                clock[0] += 0.01
            if stall_jev:
                await asyncio.Event().wait()
            if fail_jev:
                raise httpx.ReadTimeout("SECRET-EXCEPTION-CANARY")
            return httpx.Response(
                200,
                json={
                    "model": OpenRouterDecisionRoute().resolved_model_id,
                    "provider": "TypeSafe",
                    "answers": {
                        key: {"type": "choice", "choice": choice, "confidence": confidence}
                        for key in body["questions"]
                    },
                    "usage": {"input_tokens": 100, "output_tokens": 2, "cost": 0.002},
                },
            )
        if clock is not None:
            clock[0] += 0.025
        if fail_critic:
            raise httpx.ReadTimeout("SECRET-EXCEPTION-CANARY")
        return httpx.Response(200, json=critic_body(body, outcome))

    return httpx.MockTransport(respond)


def install_network(monkeypatch, **options):
    original = httpx.AsyncClient
    transport = network(**options)

    def client(**kwargs):
        # Keep the decision adapter's recording bridge intact; replace external I/O only.
        if isinstance(kwargs.get("transport"), httpx.AsyncHTTPTransport):
            kwargs["transport"] = transport
        return original(**kwargs)

    monkeypatch.setattr(study.httpx, "AsyncClient", client)


def cases_file(tmp_path, cases):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(cases))
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("choice", "stages", "action", "cost", "elapsed"),
    [
        ("supported", ["jev"], "accept", "0.002", 10),
        ("insufficient", ["jev", "critic"], "flag", "0.032", 35),
    ],
)
async def test_complete_path_measures_routing_plus_fallback(
    case, tmp_path, monkeypatch, choice, stages, action, cost, elapsed
):
    clock = [0.0]
    monkeypatch.setattr(study, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    seen = []
    (tmp_path / "raw").mkdir()
    async with httpx.AsyncClient(
        transport=network(choice=choice, outcome="flag", clock=clock, seen=seen)
    ) as client:
        row = await study.execute(entry(case), case, client=client, out=tmp_path)
    assert row["stages"] == stages
    assert row["action"] == action
    assert row["service_ms"] == pytest.approx(elapsed)
    assert row["known_cost_usd"] == cost
    assert row["unknown_cost_calls"] == 0
    assert len(seen) == len(stages)
    assert {path.name for path in (tmp_path / "raw").iterdir()} == {
        f"{row['id']}.{stage}{suffix}.json" for stage in stages for suffix in ("", ".dispatch")
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "action"), [("abstain", "abstain"), ("invalid", "error"), ("flag", "flag")]
)
async def test_mechanical_failure_differs_from_real_abstention(case, tmp_path, outcome, action):
    (tmp_path / "raw").mkdir()
    async with httpx.AsyncClient(transport=network(outcome=outcome)) as client:
        row = await study.execute(entry(case, "critic"), case, client=client, out=tmp_path)
    assert row["action"] == action
    assert row["known_cost_usd"] == "0.03"
    assert row["unknown_cost_calls"] == 0
    if outcome == "invalid":
        assert row["critic"]["result"]["reason"] == "critic_output_failed_mechanical_validation"


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_critic", [False, True])
async def test_failed_routing_keeps_fallback_and_unknown_accounting(case, tmp_path, fail_critic):
    (tmp_path / "raw").mkdir()
    async with httpx.AsyncClient(
        transport=network(fail_jev=True, fail_critic=fail_critic)
    ) as client:
        row = await study.execute(entry(case), case, client=client, out=tmp_path)
    assert row["stages"] == ["jev", "critic"]
    assert row["action"] == ("error" if fail_critic else "accept")
    assert row["unknown_cost_calls"] == (2 if fail_critic else 1)
    assert row["known_cost_usd"] == ("0" if fail_critic else "0.03")
    assert "SECRET-EXCEPTION-CANARY" not in json.dumps(row)
    assert all(
        "SECRET-EXCEPTION-CANARY" not in path.read_text() for path in (tmp_path / "raw").iterdir()
    )


def test_schedule_pairs_identical_inputs_and_ignores_gold_on_wire(case):
    schedule = study.entries([case])
    assert len(schedule) == 4
    assert schedule == study.entries([case])
    for repeat in range(2):
        a, b = [row for row in schedule if row["repeat"] == repeat]
        assert a["critic_request"] == b["critic_request"]
        assert a["jev_wire"] == b["jev_wire"]
        assert a["prepared_payload"] == b["prepared_payload"]
    changed = deepcopy(case)
    changed.update(
        expected_action="flag", expected={"/content": "contradicted"}, rationale="OTHER-GOLD"
    )
    assert study.entries([changed]) == schedule


@pytest.mark.asyncio
async def test_freeze_and_key_guards_happen_before_output(case, tmp_path, monkeypatch):
    path = cases_file(tmp_path, [case])
    monkeypatch.delenv("SIBYL_JEV_CRITIC_OPENROUTER_API_KEY", raising=False)
    prepare = tmp_path / "prepared"
    assert await study.run(path, prepare) is None
    freeze = prepare / "manifest.json"
    for label, options in [("missing-freeze", {}), ("missing-key", {"freeze": freeze})]:
        out = tmp_path / label
        with pytest.raises(ValueError, match=r"requires pre-call freeze|credential required"):
            await study.run(path, out, live=True, **options)
        assert not out.exists()
    monkeypatch.setenv("SIBYL_JEV_CRITIC_OPENROUTER_API_KEY", "synthetic-test-key")
    changed = json.loads(freeze.read_text())
    changed["threshold"] = 0.1
    freeze.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="frozen"):
        await study.run(path, tmp_path / "changed", live=True, freeze=freeze)
    assert not (tmp_path / "changed").exists()
    with pytest.raises(FileExistsError):
        await study.run(path, prepare)


async def live_archive(case, tmp_path, monkeypatch, **network_options):
    path = cases_file(tmp_path, [case])
    prepare = tmp_path / "prepared"
    await study.run(path, prepare)
    monkeypatch.setenv("SIBYL_JEV_CRITIC_OPENROUTER_API_KEY", "synthetic-test-key")
    install_network(monkeypatch, **{"choice": "insufficient", **network_options})
    live = tmp_path / "live"
    summary = await study.run(path, live, freeze=prepare / "manifest.json", live=True)
    monkeypatch.delenv("SIBYL_JEV_CRITIC_OPENROUTER_API_KEY")
    return path, live, summary


@pytest.mark.asyncio
async def test_replay_is_exact_without_key_and_keeps_all_paths(case, tmp_path, monkeypatch):
    path, live, expected = await live_archive(case, tmp_path, monkeypatch)
    replay = tmp_path / "replay"
    assert await study.run(path, replay, replay=live) == expected
    assert (replay / "rows.json").read_bytes() == (live / "rows.json").read_bytes()
    assert expected["arms"]["critic"]["paths"] == expected["arms"]["jev_then_critic"]["paths"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing", "extra", "request", "cost", "dispatch", "rows"])
async def test_replay_rejects_artifact_damage_before_output(case, tmp_path, monkeypatch, damage):
    path, live, _ = await live_archive(case, tmp_path, monkeypatch)
    raw_path = next(p for p in (live / "raw").iterdir() if p.name.endswith(".critic.json"))
    if damage == "missing":
        raw_path.unlink()
    elif damage == "extra":
        (live / "raw" / "extra.json").write_text("{}")
    elif damage == "rows":
        rows = json.loads((live / "rows.json").read_text())
        rows.pop()
        (live / "rows.json").write_text(json.dumps(rows))
    else:
        target = raw_path.with_suffix(".dispatch.json") if damage == "dispatch" else raw_path
        raw = json.loads(target.read_text())
        if damage == "request":
            raw["request"]["model"] = "other-model"
        elif damage == "dispatch":
            raw["dispatch_started_at"] = "changed"
        else:
            body = json.loads(raw["response_body"])
            body["usage"]["cost"] = 100
            raw["response_body"] = json.dumps(body)
            raw["response_body_base64"] = base64.b64encode(raw["response_body"].encode()).decode()
        target.write_text(json.dumps(raw))
    out = tmp_path / "damaged-replay"
    with pytest.raises((ValueError, FileNotFoundError)):
        await study.run(path, out, replay=live)
    assert not out.exists()


def scored_rows(cases):
    schedule = study.entries(cases)
    rows = [
        {
            "id": item["id"],
            "case_id": item["case_id"],
            "repeat": item["repeat"],
            "arm": item["arm"],
            "action": cases[item["case_index"]]["expected_action"],
            "service_ms": 100 if item["arm"] == "critic" else 50,
            "queue_ms": 0,
            "total_ms": 100 if item["arm"] == "critic" else 50,
            "known_cost_usd": "0.03" if item["arm"] == "critic" else "0.002",
            "unknown_cost_calls": 0,
            "route": "fallback" if item["arm"] == "critic" else "bypass",
        }
        for item in schedule
    ]
    return schedule, rows


@pytest.mark.parametrize(
    ("damage", "gate"),
    [
        ("unsafe_accept", "zero_false_accepts"),
        ("coverage", "safe_coverage_not_worse"),
        ("wrong_flag", "action_quality_not_worse"),
        ("slower", "median_20_percent_faster"),
        ("tail", "p95_not_worse"),
        ("failure", "no_execution_failures"),
        ("unknown_cost", "all_cost_known"),
    ],
)
def test_cheapness_cannot_override_quality_coverage_or_speed(case, damage, gate):
    unsafe = deepcopy(case)
    unsafe.update(id="unsafe", expected_action="flag", expected={"/content": "contradicted"})
    cases = [case, unsafe]
    schedule, rows = scored_rows(cases)
    good = study.summarize(cases, schedule, rows)
    assert good["diagnostic_target_met"]
    assert not good["production_qualified"]
    safe = [r for r in rows if r["arm"] == "jev_then_critic" and r["case_id"] == case["id"]]
    bad = [r for r in rows if r["arm"] == "jev_then_critic" and r["case_id"] == unsafe["id"]]
    if damage == "unsafe_accept":
        bad[0]["action"] = "accept"
    elif damage == "coverage":
        safe[0]["action"] = "abstain"
    elif damage == "wrong_flag":
        bad[0]["action"] = "abstain"
    elif damage == "slower":
        for row in safe + bad:
            row["service_ms"] = row["total_ms"] = 90
    elif damage == "tail":
        bad[0]["service_ms"] = bad[0]["total_ms"] = 200
    elif damage == "failure":
        bad[0]["action"] = "error"
    else:
        bad[0]["unknown_cost_calls"] = 1
    summary = study.summarize(cases, schedule, rows)
    assert not summary["gates"][gate]
    assert not summary["diagnostic_target_met"]
    assert summary["arms"]["jev_then_critic"]["paths"] == 4
    if damage == "failure":
        assert summary["arms"]["jev_then_critic"]["action_correct"] == 3


def test_summary_rejects_missing_or_duplicate_path_denominators(case):
    schedule, rows = scored_rows([case])
    for broken in (rows[:-1], [*rows[:-1], rows[0]]):
        with pytest.raises(ValueError, match="coverage"):
            study.summarize([case], schedule, broken)


@pytest.mark.asyncio
async def test_real_jev_deadline_retains_stage_cost_and_replays(case, tmp_path, monkeypatch):
    provider_type = study.OpenRouterDecisionProvider

    def short_deadline(*args, **kwargs):
        return provider_type(*args, route=OpenRouterDecisionRoute(deadline_seconds=0.01), **kwargs)

    monkeypatch.setattr(study, "OpenRouterDecisionProvider", short_deadline)
    path, live, summary = await live_archive(case, tmp_path, monkeypatch, stall_jev=True)
    rows = json.loads((live / "rows.json").read_text())
    routed = [row for row in rows if row["arm"] == "jev_then_critic"]
    assert len(routed) == 2
    for row in routed:
        assert row["stages"] == ["jev", "critic"]
        assert row["jev_observation"]["error_category"] == "deadline_exceeded"
        assert row["unknown_cost_calls"] == 1
        assert row["known_cost_usd"] == "0.03"
        assert row["action"] == "accept"
    assert not summary["gates"]["all_cost_known"]
    assert not summary["diagnostic_target_met"]
    assert await study.run(path, tmp_path / "replay", replay=live) == summary


@pytest.mark.asyncio
async def test_external_cancel_preserves_scheduled_denominator_without_pass(
    case, tmp_path, monkeypatch
):
    path = cases_file(tmp_path, [case])
    await study.run(path, tmp_path / "prepared")
    monkeypatch.setenv("SIBYL_JEV_CRITIC_OPENROUTER_API_KEY", "synthetic-test-key")
    entered = asyncio.Event()

    async def blocked(request):
        entered.set()
        await asyncio.Event().wait()

    original_client = httpx.AsyncClient

    def client(**kwargs):
        if isinstance(kwargs.get("transport"), httpx.AsyncHTTPTransport):
            kwargs["transport"] = httpx.MockTransport(blocked)
        return original_client(**kwargs)

    monkeypatch.setattr(study.httpx, "AsyncClient", client)
    out = tmp_path / "cancelled"
    task = asyncio.create_task(
        study.run(path, out, live=True, freeze=tmp_path / "prepared" / "manifest.json")
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    partial = json.loads((out / "partial.json").read_text())
    assert partial["scheduled"] == 4
    assert len(partial["recorded"]) + len(partial["missing"]) == partial["scheduled"]
    assert not (out / "summary.json").exists()
    assert not (out / "completion.json").exists()
    raw = [
        json.loads(p.read_text())
        for p in (out / "raw").iterdir()
        if not p.name.endswith(".dispatch.json")
    ]
    assert raw
    assert all(r["error_code"] == "cancelled" for r in raw)
