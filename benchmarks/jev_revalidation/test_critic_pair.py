"""Check frozen transport, product validation, accounting, and replay integrity."""

import asyncio
import base64
import json
from copy import deepcopy

import httpx
import pytest

from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.memory_validation import VALIDATION_INSTRUCTIONS

from . import critic_pair as pair
from . import critic_pair_analysis as analysis
from .runner import _write
from .support_inputs import make_request
from .test_support_inputs import support_case as support_case  # noqa: PLC0414

OBSERVED_COST = 0.02


def entry_for(case):
    prepared = pair.prepare_case(case, "frozen-run")
    request = pair.critic_request(prepared)
    return {
        "id": "case-0-repeat-0-grouped-0",
        "request": request,
        "request_sha256": pair.digest(canonical(request)),
        "prepared_payload": prepared.payload_json,
    }


def raw_for(entry, output=None):
    body = {
        "model": "anthropic/claude-opus-5",
        "provider": "Anthropic",
        "usage": {"cost": 0.02, "prompt_tokens": 90, "completion_tokens": 10, "total_tokens": 100},
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {
                                "name": "CriticOutput",
                                "arguments": json.dumps(output or {"findings": []}),
                            },
                        }
                    ]
                },
            }
        ],
    }
    return encode_body(entry, body)


def encode_body(entry, body):
    text = json.dumps(body)
    return {
        "id": entry["id"],
        "request": entry["request"],
        "request_sha256": entry["request_sha256"],
        "http_status": 200,
        "response_body": text,
        "response_body_base64": base64.b64encode(text.encode()).decode(),
        "dispatch_started_at": "2026-09-19T00:00:00+00:00",
        "completed_at": "2026-09-19T00:00:01+00:00",
        "elapsed_ms": 1000.0,
        "error_code": None,
    }


def plan_for(case):
    entry = entry_for(case)
    prepared = pair.prepare_case(case, "frozen-run")
    return {
        "version": pair.VERSION,
        "controls": pair.CONTROLS,
        "plan_sha256": "a" * 64,
        "calls": [
            {
                "id": entry["id"],
                "case": case,
                "preparation_run_id": "frozen-run",
                "expected_jev_state_sha256": pair.digest(make_request(case, "frozen-run").state),
                "prepared_payload_sha256": pair.digest(prepared.payload_json),
                "prompt_sha256": pair.digest(prepared.prompt),
                "request_sha256": entry["request_sha256"],
            }
        ],
    }


def test_exact_product_prompt_and_no_gold(support_case):
    prepared = pair.prepare_case(support_case, "frozen-run")
    request = pair.critic_request(prepared)
    assert request["messages"] == [
        {"role": "user", "content": VALIDATION_INSTRUCTIONS + "\n\n" + prepared.payload_json}
    ]
    assert request["provider"] == {
        "only": ["anthropic"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }
    assert "temperature" not in request
    assert request["tool_choice"]["function"]["name"] == "CriticOutput"
    assert "PRIVATE-GOLD" not in json.dumps(request)
    changed = deepcopy(support_case)
    changed["expected"] = {}
    changed["rationale"] = "secret"
    assert pair.critic_request(pair.prepare_case(changed, "frozen-run")) == request


@pytest.mark.parametrize(
    "field",
    ["expected_jev_state_sha256", "prepared_payload_sha256", "prompt_sha256", "request_sha256"],
)
def test_tampered_plan_rejected(support_case, field):
    plan = plan_for(support_case)
    assert pair.prepare_entries(plan) == [entry_for(support_case)]
    plan["calls"][0][field] = "f" * 64
    with pytest.raises(ValueError, match="binding mismatch"):
        pair.prepare_entries(plan)


@pytest.mark.asyncio
async def test_success_uses_product_validator(support_case):
    entry = entry_for(support_case)
    call = await pair.interpret(entry, raw_for(entry))
    assert call["execution_status"] == "completed"
    assert call["result"]["status"] == "no_findings"
    assert call["result"]["input_sha256"] == pair.digest(entry["request"]["messages"][0]["content"])
    assert call["usage"]["observed_cost_usd"] == OBSERVED_COST


@pytest.mark.asyncio
async def test_invalid_hash_abstains_with_original_cost(support_case):
    entry = entry_for(support_case)
    output = {
        "findings": [
            {
                "claim_path": "/content",
                "claim_sha256": "f" * 64,
                "evidence_refs": [{"evidence_id": "s0"}],
                "basis": "factual_contradiction",
                "disposition": "reconsider",
                "critique": "The source differs.",
            }
        ]
    }
    call = await pair.interpret(entry, raw_for(entry, output))
    assert call["result"]["status"] == "abstain"
    assert call["result"]["reason"] == "critic_output_failed_mechanical_validation"
    assert call["usage"]["observed_cost_usd"] == OBSERVED_COST


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage", ["model", "provider", "http", "tokens", "multiple_tools", "truncated"]
)
async def test_failures_keep_accounting(support_case, damage):
    entry = entry_for(support_case)
    raw = raw_for(entry)
    body = json.loads(raw["response_body"])
    if damage in {"model", "provider"}:
        body[damage] = "wrong"
    elif damage == "tokens":
        del body["usage"]["prompt_tokens"]
    elif damage == "multiple_tools":
        body["choices"][0]["message"]["tool_calls"] *= 2
    elif damage == "truncated":
        body["choices"][0]["finish_reason"] = "length"
    raw = encode_body(entry, body)
    if damage == "http":
        raw["http_status"] = 500
    call = await pair.interpret(entry, raw)
    assert call["execution_status"] == "failed"
    assert call["result"] is None
    assert call["usage"]["observed_cost_usd"] == OBSERVED_COST
    assert call["usage"]["attempt_count"] == 1


@pytest.mark.asyncio
async def test_transport_error_unknown_billing_and_immediate_raw(support_case, tmp_path):
    entry = entry_for(support_case)
    (tmp_path / "receipts").mkdir()
    (tmp_path / "dispatch").mkdir()

    async def broken(request):
        raise httpx.ReadTimeout("secret-token-canary")

    async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as client:
        call = await pair._dispatch(client, entry, tmp_path)
    assert call["usage"]["observed_cost_usd"] is None
    assert call["usage"]["attempt_count"] is None
    assert not call["usage"]["attempt_count_known"]
    assert "secret-token-canary" not in json.dumps(call)
    assert (tmp_path / "receipts" / f"{entry['id']}.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", [None, "missing", "corrupt", "accounting", "request"])
async def test_replay_rejects_inconsistent_archive(support_case, tmp_path, damage):
    entry = entry_for(support_case)
    raw = raw_for(entry)
    call = await pair.interpret(entry, raw)
    (tmp_path / "receipts").mkdir()
    (tmp_path / "dispatch").mkdir()
    _write(
        tmp_path / "dispatch" / f"{entry['id']}.json",
        {
            **raw,
            "http_status": None,
            "response_body": None,
            "response_body_base64": None,
            "completed_at": None,
            "elapsed_ms": None,
            "error_code": None,
        },
    )
    _write(tmp_path / "calls.json", [call])
    receipt = tmp_path / "receipts" / f"{entry['id']}.json"
    if damage == "accounting":
        body = json.loads(raw["response_body"])
        body["usage"]["cost"] = 2
        raw = encode_body(entry, body)
    elif damage == "request":
        raw["request"] = {}
    if damage == "corrupt":
        receipt.write_text("invalid")
    elif damage != "missing":
        _write(receipt, raw)
    if damage is None:
        assert await pair.replay_calls([entry], tmp_path) == [call]
    else:
        with pytest.raises((ValueError, FileNotFoundError)):
            await pair.replay_calls([entry], tmp_path)


@pytest.mark.asyncio
async def test_prepare_live_replay_and_dispatch_preflight(support_case, tmp_path, monkeypatch):
    plan = plan_for(support_case)
    monkeypatch.setattr(analysis, "validate_plan", lambda _: None)
    monkeypatch.setattr(analysis, "summarize", lambda _, calls: {"count": len(calls)})
    monkeypatch.delenv("SIBYL_JEV_CRITIC_OPENROUTER_API_KEY", raising=False)
    prepared = tmp_path / "prepared"
    assert await pair.run(plan, prepared) == []
    assert json.loads((prepared / "completion.json").read_text())["scheduled"] == 1
    with pytest.raises(FileExistsError):
        await pair.run(plan, prepared)
    monkeypatch.setenv("SIBYL_JEV_CRITIC_OPENROUTER_API_KEY", "synthetic-test-key")
    entry = entry_for(support_case)
    body = json.loads(raw_for(entry)["response_body"])
    client_type = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs["follow_redirects"] is False
        return client_type(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)))

    monkeypatch.setattr(pair.httpx, "AsyncClient", client)
    live = tmp_path / "live"
    calls = await pair.run(plan, live, live=True)
    monkeypatch.delenv("SIBYL_JEV_CRITIC_OPENROUTER_API_KEY")
    assert await pair.run(plan, tmp_path / "replayed", replay=live) == calls
    for file in ("calls.json", "summary.json", "requests.json"):
        assert (live / file).read_bytes() == (tmp_path / "replayed" / file).read_bytes()
    dispatch_path = live / "dispatch" / f"{entry['id']}.json"
    original = dispatch_path.read_bytes()
    for damage in ("missing_dispatch", "modified_dispatch", "extra_receipt"):
        if damage == "missing_dispatch":
            dispatch_path.unlink()
        elif damage == "modified_dispatch":
            changed = json.loads(original)
            changed["dispatch_started_at"] = "changed"
            dispatch_path.write_text(json.dumps(changed))
        else:
            (live / "receipts" / "extra.json").write_text("{}")
        out = tmp_path / damage
        with pytest.raises((ValueError, FileNotFoundError)):
            await pair.run(plan, out, replay=live)
        assert not out.exists()
        dispatch_path.write_bytes(original)
        (live / "receipts" / "extra.json").unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_cancel_preserves_active_and_queued_denominators(support_case, tmp_path, monkeypatch):
    entries = [{**entry_for(support_case), "id": f"call-{index}"} for index in range(10)]
    (tmp_path / "receipts").mkdir()
    (tmp_path / "dispatch").mkdir()
    ready = asyncio.Event()
    hanging = asyncio.Event()
    active = 0

    async def blocked(request):
        nonlocal active
        active += 1
        if active == pair.CONTROLS["concurrency"]:
            ready.set()
        await hanging.wait()
        return httpx.Response(500)

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        pair.httpx, "AsyncClient", lambda **_: client_type(transport=httpx.MockTransport(blocked))
    )
    task = asyncio.create_task(pair._live(entries, tmp_path, "synthetic-test-key"))
    await asyncio.wait_for(ready.wait(), timeout=5)
    task.cancel()
    calls = await asyncio.wait_for(task, timeout=5)
    assert len(calls) == len(entries)
    assert all(call["execution_status"] == "failed" for call in calls)
    assert all(call["usage"]["observed_cost_usd"] is None for call in calls)
    assert all(call["usage"]["attempt_count"] is None for call in calls[:8])
    assert all(call["usage"]["attempt_count"] == 0 for call in calls[8:])
    _write(tmp_path / "calls.json", calls)
    assert await pair.replay_calls(entries, tmp_path) == calls
