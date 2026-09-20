"""Paired hints bind their old receipt and fresh critic before any quality claim."""

# Explicit synthetic counts and prices are the accounting oracle.
# ruff: noqa: PLR2004

import asyncio
import json

import httpx
import pytest

from . import fast_critic, quality_speed
from . import fast_critic_study as study
from .runner import _sha
from .test_quality_speed import cases_file, install_network


@pytest.fixture
def case():
    return {
        "id": "toy",
        "category": "scope",
        "content": "The valve is closed.",
        "claims": [],
        "sources": [{"id": "source", "text": "The valve is closed.", "provenance": "signed"}],
        "expected": {"/content": "supported"},
        "expected_action": "accept",
        "rationale": "GOLD-ONLY-CANARY",
    }


async def source_fixture(tmp_path, monkeypatch, case):
    cases = cases_file(tmp_path, [case])
    rubric = tmp_path / "rubric.json"
    rubric.write_text(json.dumps({"cases_sha256": _sha(cases), "cases": [{"case_id": case["id"]}]}))
    freeze = tmp_path / "source-freeze.json"
    freeze.write_text(json.dumps(quality_speed.manifest(cases)))
    source = tmp_path / "source"
    with monkeypatch.context() as patch:
        install_network(patch)
        patch.setenv("SIBYL_JEV_CRITIC_OPENROUTER_API_KEY", "synthetic-test-only")
        await quality_speed.run(cases, source, freeze=freeze, live=True)
    return cases, source, rubric


def fast_network(monkeypatch, *, fail=False, seen=None):
    real_client = httpx.AsyncClient

    async def respond(request):
        body = json.loads(await request.aread())
        if seen is not None:
            seen.append(body)
        assert "GOLD-ONLY-CANARY" not in json.dumps(body)
        assert str(request.url) == fast_critic.CONTROLS["endpoint"]
        assert body["model"] == fast_critic.CONTROLS["model"]
        if fail:
            raise httpx.ReadTimeout("SECRET-ERROR-CANARY")
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
                                        "arguments": json.dumps(
                                            {"findings": [], "abstention_reason": None}
                                        ),
                                    },
                                }
                            ]
                        },
                    }
                ],
            },
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(
            **(
                {**kw, "transport": httpx.MockTransport(respond)}
                if isinstance(kw.get("transport"), httpx.AsyncHTTPTransport)
                else kw
            )
        ),
    )
    monkeypatch.setenv("SIBYL_JEV_CRITIC_OPENROUTER_API_KEY", "synthetic-test-only")


@pytest.mark.asyncio
async def test_prepare_live_replay_and_attributed_cost(case, tmp_path, monkeypatch):
    cases, source, rubric = await source_fixture(tmp_path, monkeypatch, case)
    plan = tmp_path / "plan"
    assert await study.run(cases, source, rubric, plan) is None
    schedule = json.loads((plan / "schedule.json").read_text())
    assert len(schedule) == 4
    for e in schedule:
        assert bool(e["hints"]) == (e["arm"] == "hinted")
        assert len(e["prior_jev_request_digest"]) == 64
    seen = []
    fast_network(monkeypatch, seen=seen)
    live = tmp_path / "live"
    result = await study.run(cases, source, rubric, live, freeze=plan / "manifest.json", live=True)
    assert result is not None
    assert len(seen) == 4
    assert result["arms"]["direct"]["fresh_known_cost_usd"] == "0.006"
    assert result["arms"]["hinted"]["fresh_plus_attributed_jev_cost_usd"] == "0.010"
    assert result["arms"]["hinted"]["attributed_prior_jev_calls"] == 2
    assert result["semantic_review_status"] == "pending"
    assert not result["production_qualified"]
    assert await study.run(cases, source, rubric, tmp_path / "replay", replay_path=live) == result
    assert len(seen) == 4  # No extra transport during either source or fresh replay.
    with pytest.raises(FileExistsError):
        await study.run(cases, source, rubric, live)


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["freeze", "source", "rubric"])
async def test_invalid_inputs_fail_before_egress(case, tmp_path, monkeypatch, corruption):
    cases, source, rubric = await source_fixture(tmp_path, monkeypatch, case)
    plan = tmp_path / "plan"
    await study.run(cases, source, rubric, plan)
    if corruption == "freeze":
        p = plan / "manifest.json"
        value = json.loads(p.read_text())
        value["seed"] += 1
        p.write_text(json.dumps(value))
    elif corruption == "source":
        p = next((source / "raw").glob("*.jev.json"))
        value = json.loads(p.read_text())
        value["request"]["state"] = "changed"
        p.write_text(json.dumps(value))
    else:
        value = json.loads(rubric.read_text())
        value["cases_sha256"] = "0" * 64
        rubric.write_text(json.dumps(value))
    seen = []
    fast_network(monkeypatch, seen=seen)
    with pytest.raises(ValueError, match=r"mismatch|changed|frozen|complete fresh"):
        await study.run(
            cases, source, rubric, tmp_path / "out", freeze=plan / "manifest.json", live=True
        )
    assert seen == []
    assert not (tmp_path / "out").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption", ["raw", "row", "extra", "completion", "rubric", "missing_rubric"]
)
async def test_corrupt_fresh_archive_rejected(case, tmp_path, monkeypatch, corruption):
    cases, source, rubric = await source_fixture(tmp_path, monkeypatch, case)
    plan = tmp_path / "plan"
    await study.run(cases, source, rubric, plan)
    fast_network(monkeypatch)
    live = tmp_path / "live"
    await study.run(cases, source, rubric, live, freeze=plan / "manifest.json", live=True)
    if corruption == "raw":
        p = next((live / "raw").glob("*.critic.json"))
        value = json.loads(p.read_text())
        value["request"]["max_tokens"] = 7
        p.write_text(json.dumps(value))
    elif corruption == "row":
        p = live / "rows.json"
        value = json.loads(p.read_text())
        value[0]["action"] = "flag"
        p.write_text(json.dumps(value))
    elif corruption == "missing_rubric":
        (live / "rubric.json").unlink()
    elif corruption == "rubric":
        (live / "rubric.json").write_text("{}")
    elif corruption == "extra":
        (live / "raw" / "extra.json").write_text("{}")
    else:
        (live / "completion.json").write_text('{"mode":"live","paths":3}')
    error = FileNotFoundError if corruption == "missing_rubric" else ValueError
    with pytest.raises(error, match=r"mismatch|changed|frozen|complete fresh|rubric.json"):
        await study.run(cases, source, rubric, tmp_path / "replay", replay_path=live)
    assert not (tmp_path / "replay").exists()


@pytest.mark.asyncio
async def test_transport_failures_keep_all_scheduled_denominators(case, tmp_path, monkeypatch):
    cases, source, rubric = await source_fixture(tmp_path, monkeypatch, case)
    plan = tmp_path / "plan"
    await study.run(cases, source, rubric, plan)
    fast_network(monkeypatch, fail=True)
    result = await study.run(
        cases, source, rubric, tmp_path / "live", freeze=plan / "manifest.json", live=True
    )
    assert result is not None
    for arm in ("direct", "hinted"):
        assert result["arms"][arm]["paths"] == 2
        assert result["arms"][arm]["execution_failures"] == 2
        assert result["arms"][arm]["unknown_cost_calls"] == 2
        assert result["arms"][arm]["fresh_plus_attributed_jev_cost_usd"] is None


@pytest.mark.asyncio
async def test_cancellation_retains_dispatch_and_missing_paths(case, tmp_path, monkeypatch):
    cases, source, rubric = await source_fixture(tmp_path, monkeypatch, case)
    plan = tmp_path / "plan"
    await study.run(cases, source, rubric, plan)
    called = asyncio.Event()

    async def block(request):
        called.set()
        await asyncio.Event().wait()

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real(
            **(
                {**kw, "transport": httpx.MockTransport(block)}
                if isinstance(kw.get("transport"), httpx.AsyncHTTPTransport)
                else kw
            )
        ),
    )
    monkeypatch.setenv("SIBYL_JEV_CRITIC_OPENROUTER_API_KEY", "synthetic-test-only")
    out = tmp_path / "live"
    task = asyncio.create_task(
        study.run(cases, source, rubric, out, freeze=plan / "manifest.json", live=True)
    )
    await called.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (out / "completion.json").exists()
    partial = json.loads((out / "partial.json").read_text())
    assert partial["scheduled"] == 4
    assert len(partial["missing"]) == 4
    assert len(list((out / "raw").glob("*.dispatch.json"))) == 4


def test_stale_hint_digest_rejected(case):
    entry = next(e for e in quality_speed.entries([case]) if e["arm"] == "jev_then_critic")
    row = {
        "jev_observation": {
            "request_digest": "0" * 64,
            "semantic_input_sha256": "0" * 64,
            "execution_status": "unavailable",
            "error_category": "failed",
            "attempt_count": 0,
        }
    }
    with pytest.raises(ValueError, match="different request"):
        study.hint_labels(entry, row)
