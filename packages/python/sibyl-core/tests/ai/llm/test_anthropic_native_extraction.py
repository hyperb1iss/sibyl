"""Native Anthropic extraction uses the production factory and strict validation."""

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx2 as httpx
import pytest
from tests.ai.llm.test_evidence_proposal_validation import procedure

from sibyl_core.ai import clients, providers
from sibyl_core.ai.errors import LLMError, LLMValidationError
from sibyl_core.ai.llm import Extractor
from sibyl_core.ai.llm import extractor as extraction
from sibyl_core.ai.llm.config import EnvConfigSource, LLMSurface
from sibyl_core.ai.transport import RecordingAnthropicClient, transport_policy
from sibyl_core.tasks.procedure_evidence import EvidenceProposal

ABSTENTION = {"outcome": {"kind": "abstention", "reason": "Insufficient synthetic evidence"}}


def response(output=ABSTENTION, *, usage=True, model="claude-opus-5"):
    payload = {
        "id": "msg_fixture",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": json.dumps(output)}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
    }
    if usage:
        payload["usage"] = {
            "input_tokens": 10,
            "output_tokens": 2,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
    return payload


@asynccontextmanager
async def configured(monkeypatch, respond, *, model="claude-opus-5", retries=2, output_retries=1):
    source = EnvConfigSource(
        {
            "SIBYL_LLM_MEMORY_PROVIDER": "anthropic",
            "SIBYL_LLM_MEMORY_MODEL": model,
            "SIBYL_LLM_MEMORY_TRANSPORT_MAX_RETRIES": str(retries),
            "ANTHROPIC_API_KEY": "fixture-key",
        }
    )
    monkeypatch.setattr(clients, "resolve_llm_config", source.resolve)
    reserve = AsyncMock()
    monkeypatch.setattr(extraction, "reserve_llm_budget", reserve)
    clients.invalidate_agent_cache()
    async with RecordingAnthropicClient(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(providers, "RecordingAnthropicClient", lambda: http)
        try:
            yield (
                Extractor(
                    EvidenceProposal,
                    surface=LLMSurface.MEMORY,
                    output_mode="native_strict",
                    max_tokens=8192,
                    output_retries=output_retries,
                    system_prompt="Synthetic policy",
                ),
                reserve,
                source,
            )
        finally:
            clients.invalidate_agent_cache()


def keys(value):
    if isinstance(value, dict):
        return set(value).union(*(keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(keys(item) for item in value))
    return set()


@pytest.mark.parametrize("model, sampling", [("claude-opus-5", False), ("claude-haiku-4-5", True)])
async def test_anthropic_native_factory_emits_profile_schema_and_settings(
    monkeypatch, model, sampling
):
    wires = []

    def respond(request):
        assert request.url.scheme == "https" and request.url.host == "api.anthropic.com"
        assert request.url.path == "/v1/messages"
        wires.append(json.loads(request.content))
        return httpx.Response(200, headers={"request-id": "req_native"}, json=response(model=model))

    async with configured(monkeypatch, respond, model=model) as (extractor, reserve, source):
        declared = await extractor.output_schema()
        result = await extractor.extract_with_usage("Synthetic evidence")
        wire = wires[0]
        assert wire["output_config"]["format"] == {"type": "json_schema", "schema": declared}
        assert "minimum" in keys(EvidenceProposal.model_json_schema())
        assert "minimum" not in keys(declared)
        assert "anyOf" in keys(declared)
        assert "tools" not in wire
        assert ("temperature" in wire) is sampling
        assert wire["max_tokens"] == 8192
        assert result.output.outcome.kind == "abstention"
        assert result.usage.provider == "anthropic"
        assert result.usage.input_tokens == 10 and result.usage.output_tokens == 2
        assert result.usage.transport_usage_complete is True
        assert result.usage.transport_attempts[0].request_id == "req_native"
        assert reserve.await_args.kwargs["attempt_envelope"] == 6
        assert json.dumps(declared, sort_keys=True) in reserve.await_args.kwargs["prompt"]
        config = (await source.resolve(LLMSurface.MEMORY)).to_llm_config()
        assert transport_policy(config)["max_retries"] == 2
        assert transport_policy(config)["receipt_version"] == "sibyl-sdk-attempt-v2"


@pytest.mark.parametrize("recover", [False, True])
async def test_anthropic_native_original_constraints_retry_or_fail(monkeypatch, recover):
    wires = []

    def respond(request):
        wires.append(json.loads(request.content))
        output = {"outcome": {"kind": "procedure", "procedure": procedure()}}
        output["outcome"]["procedure"]["actions"][0]["order"] = 0
        if recover and len(wires) > 1:
            output = ABSTENTION
        return httpx.Response(200, json=response(output))

    async with configured(monkeypatch, respond) as (extractor, _, _):
        if recover:
            result = await extractor.extract_with_usage("Synthetic evidence")
            assert result.output.outcome.kind == "abstention"
            assert result.usage.input_tokens == 20
            assert len(result.usage.transport_attempts) == 2
        else:
            with pytest.raises(LLMValidationError) as caught:
                await extractor.extract_with_usage("Synthetic evidence")
            receipt = caught.value.details["extraction_usage"]
            assert len(receipt["transport_attempts"]) == 2
            assert receipt["cost_usd"] is None and not receipt["cost_complete"]
        assert len(wires) == 2
        assert "greater_than_equal" in json.dumps(wires[1]["messages"])


@pytest.mark.parametrize("failure", [None, "http", "timeout", "invalid_json", "missing_usage"])
async def test_anthropic_native_physical_attempts_preserve_unknown_cost(monkeypatch, failure):
    calls = []

    def respond(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private exception", request=request)
        if len(calls) < 3 or failure == "http":
            return httpx.Response(
                500,
                headers={"retry-after-ms": "1", "request-id": f"retry_{len(calls)}"},
                json={"type": "error", "error": {"type": "api_error", "message": "private body"}},
            )
        if failure == "invalid_json":
            return httpx.Response(200, content=b"not json")
        return httpx.Response(
            200,
            headers={"request-id": "req_final"},
            json=response(usage=failure != "missing_usage"),
        )

    async with configured(monkeypatch, respond, output_retries=0) as (extractor, _, _):
        if failure:
            with pytest.raises(LLMError) as caught:
                await extractor.extract_with_usage("private prompt")
            receipt = caught.value.details["extraction_usage"]
        else:
            receipt = (await extractor.extract_with_usage("private prompt")).usage.model_dump()
        assert len(calls) == 3
        assert len(receipt["transport_attempts"]) == 3
        assert not receipt["cost_complete"] and receipt["cost_usd"] is None
        assert not receipt["transport_attempts"][0]["usage_known"]
        if failure == "timeout":
            assert [a["exception_type"] for a in receipt["transport_attempts"]] == [
                "ReadTimeout"
            ] * 3
            assert all(a["status_code"] is None for a in receipt["transport_attempts"])
        else:
            assert [a["status_code"] for a in receipt["transport_attempts"]] == [
                500,
                500,
                500 if failure == "http" else 200,
            ]
            assert all(a["exception_type"] is None for a in receipt["transport_attempts"])
            assert [a["request_id"] for a in receipt["transport_attempts"][:2]] == [
                "retry_1",
                "retry_2",
            ]
        assert "private" not in json.dumps(receipt)


async def test_anthropic_unsupported_native_model_and_openrouter_route_fail_before_budget(
    monkeypatch,
):
    def respond(request):
        pytest.fail("unsupported native policy must not dispatch")

    async with configured(monkeypatch, respond, model="claude-3-haiku-20240307") as (
        extractor,
        reserve,
        _,
    ):
        with pytest.raises(LLMError):
            await extractor.extract("synthetic")
        reserve.assert_not_awaited()
    async with configured(monkeypatch, respond) as (extractor, reserve, _):
        extractor.openrouter_provider = "fixture-route"
        with pytest.raises(LLMError):
            await extractor.extract("synthetic")
        reserve.assert_not_awaited()


async def test_anthropic_concurrent_attempts_remain_request_local(monkeypatch):
    async def respond(request):
        wire = json.loads(request.content)
        identity = wire["messages"][0]["content"][0]["text"]
        await asyncio.sleep(0)
        return httpx.Response(200, headers={"request-id": identity}, json=response())

    async with configured(monkeypatch, respond) as (extractor, _, _):
        results = await asyncio.gather(
            *(extractor.extract_with_usage(f"req_{i}") for i in range(8))
        )
        assert [r.usage.transport_attempts[0].request_id for r in results] == [
            f"req_{i}" for i in range(8)
        ]
        assert all(len(r.usage.transport_attempts) == 1 for r in results)


async def test_anthropic_native_budget_uses_transformed_schema_and_sdk_envelope(monkeypatch):
    from sibyl_core.ai.llm.budget import llm_budget_context, reserve_llm_budget, set_budget_enforcer

    reservations = []

    class Budget:
        async def reserve(self, context, *, estimated_tokens, **kwargs):
            reservations.append(estimated_tokens)
            raise RuntimeError("synthetic budget denial")

    def respond(request):
        pytest.fail("budget denial must precede physical dispatch")

    async with configured(monkeypatch, respond, retries=3, output_retries=2) as (extractor, _, _):
        monkeypatch.setattr(extraction, "reserve_llm_budget", reserve_llm_budget)
        schema = await extractor.output_schema()
        set_budget_enforcer(Budget())
        try:
            with (
                llm_budget_context(user_id="owner", organization_id="org"),
                pytest.raises(LLMError) as caught,
            ):
                await extractor.extract("Synthetic evidence")
        finally:
            set_budget_enforcer(None)
        prompt = "Synthetic policy\nSynthetic evidence\n" + json.dumps(schema, sort_keys=True)
        assert reservations == [(len(prompt) // 4 + 8192) * 12]
        assert caught.value.details["extraction_usage"]["transport_attempts"] == []


async def test_anthropic_prepared_schema_keeps_same_model_after_config_change(monkeypatch):
    wires = []

    def respond(request):
        wires.append(json.loads(request.content))
        return httpx.Response(200, json=response())

    async with configured(monkeypatch, respond) as (extractor, _, _):
        schema = await extractor.output_schema()
        changed = AsyncMock(side_effect=AssertionError("prepared extraction must retain its model"))
        monkeypatch.setattr(clients, "resolve_llm_config", changed)
        await extractor.extract("Synthetic evidence")
        changed.assert_not_awaited()
        assert wires[0]["model"] == "claude-opus-5"
        assert wires[0]["output_config"]["format"]["schema"] == schema


def test_prepared_extractor_does_not_reuse_agent_across_event_loops(monkeypatch):
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    made = []

    async def agent(*args, **kwargs):
        made.append(Agent(TestModel()))
        return made[-1]

    monkeypatch.setattr(extraction, "get_agent", agent)
    extractor = Extractor(EvidenceProposal, output_mode="native_strict")
    asyncio.run(extractor.output_schema())
    asyncio.run(extractor.output_schema())
    assert len(made) == 2


async def test_anthropic_cancellation_retains_unknown_attempt(monkeypatch):
    async def respond(request):
        raise asyncio.CancelledError("private cancellation")

    async with configured(monkeypatch, respond) as (extractor, _, _):
        with pytest.raises(asyncio.CancelledError) as caught:
            await extractor.extract("private prompt")
        receipt = caught.value.extraction_usage
        assert receipt["transport_attempts"] == [
            {
                "status_code": None,
                "exception_type": "CancelledError",
                "request_id": None,
                "usage_known": False,
            }
        ]
        assert receipt["cost_usd"] is None and not receipt["cost_complete"]
        assert "private" not in json.dumps(receipt)


async def test_anthropic_request_id_receipt_rejects_unsafe_header(monkeypatch):
    def respond(request):
        return httpx.Response(
            200, headers={"request-id": "private payload?secret"}, json=response()
        )

    async with configured(monkeypatch, respond) as (extractor, _, _):
        result = await extractor.extract_with_usage("synthetic")
        assert result.usage.transport_attempts[0].request_id is None


def test_anthropic_registered_model_resolves_provider_and_snapshot_without_defaults():
    from pydantic import SecretStr

    from sibyl_core.ai.llm.config import LLMConfig
    from sibyl_core.ai.registry import model_registry

    entry = model_registry.require("claude-opus-5")
    assert entry.provider == "anthropic" and entry.default_temperature is None
    config = LLMConfig(provider="anthropic", model=entry.alias, api_key=SecretStr("fixture"))
    assert providers.resolve_provider_model_id(config) == "claude-opus-5"
    with pytest.raises(LLMError):
        providers.resolve_provider_model_id(config.model_copy(update={"provider": "openai"}))
    assert model_registry.recommended_for("default").alias == "claude-haiku-4-5"


async def test_anthropic_build_receipt_hashes_actual_transformed_schema(monkeypatch):
    import hashlib

    from tests.test_episode_evidence import _contrast_group

    from sibyl_core.tasks import consolidation as c

    wires = []

    def respond(request):
        wires.append(json.loads(request.content))
        return httpx.Response(200, json=response())

    async with configured(monkeypatch, respond):
        result = await c.propose_conditional_procedure(
            _contrast_group(),
            max_input_chars=800_000,
            max_tokens=8192,
            output_mode="native_strict",
            model_override="claude-opus-5",
        )
        wire_schema = wires[0]["output_config"]["format"]["schema"]
        assert (
            result.receipt["wire_schema_sha256"]
            == hashlib.sha256(c._canonical(wire_schema)).hexdigest()
        )
        assert result.receipt["status"] == "abstained"
        assert result.receipt["usage"]["provider"] == "anthropic"
        assert result.receipt["usage"]["transport_attempts"][0]["usage_known"] is True
        with pytest.raises(c.ConsolidationInputBudgetExceeded):
            await c.propose_conditional_procedure(
                _contrast_group(),
                max_input_chars=1,
                output_mode="native_strict",
                model_override="claude-opus-5",
            )
        assert len(wires) == 1


async def test_anthropic_revision_binds_native_schema_profile_and_transport(monkeypatch):
    from sibyl_core.services import eval_publication as publication

    source = EnvConfigSource(
        {"SIBYL_LLM_MEMORY_PROVIDER": "anthropic", "SIBYL_LLM_MEMORY_MODEL": "claude-opus-5"}
    )
    monkeypatch.setattr(publication, "resolve_llm_config", source.resolve)
    monkeypatch.setattr(publication.core_config, "consolidation_output_mode", "native_strict")
    captured = []
    digest = publication._digest

    def capture(value):
        if isinstance(value, dict) and "wire_schema_sha256" in value:
            captured.append(value)
        return digest(value)

    monkeypatch.setattr(publication, "_digest", capture)
    await publication._extractor_policy()
    config = (await source.resolve(LLMSurface.MEMORY)).to_llm_config()
    expected = extraction.extraction_schema(
        EvidenceProposal,
        "native_strict",
        profile=providers.resolved_model_profile(config),
    )
    assert captured[0]["provider"] == "anthropic"
    assert captured[0]["wire_schema_sha256"] == digest(expected)
    assert captured[0]["transport"]["max_retries"] == 2


@pytest.mark.parametrize("read_failure", [False, True])
async def test_anthropic_redirect_hops_survive_buffered_body_failure(monkeypatch, read_failure):
    calls = []

    class BrokenBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise httpx.ReadError("private body failure")
            yield b""

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                307,
                headers={
                    "location": "https://api.anthropic.com/v1/messages?hop=1",
                    "request-id": "req_hop",
                },
            )
        if read_failure:
            return httpx.Response(200, headers={"request-id": "req_final"}, stream=BrokenBody())
        return httpx.Response(200, headers={"request-id": "req_final"}, json=response())

    async with configured(monkeypatch, respond, retries=0) as (extractor, _, _):
        if read_failure:
            with pytest.raises(LLMError) as caught:
                await extractor.extract("Synthetic evidence")
            receipt = caught.value.details["extraction_usage"]
        else:
            receipt = (await extractor.extract_with_usage("Synthetic evidence")).usage.model_dump()
        assert len(calls) == 2
        assert [attempt["status_code"] for attempt in receipt["transport_attempts"]] == [307, 200]
        assert [attempt["request_id"] for attempt in receipt["transport_attempts"]] == [
            "req_hop",
            "req_final",
        ]
        assert receipt["cost_complete"] is False and receipt["cost_usd"] is None
        assert "private" not in json.dumps(receipt)


@pytest.mark.parametrize(
    "field,value",
    [
        ("input_tokens", -1),
        ("output_tokens", True),
        ("input_tokens", 1.5),
        ("output_tokens", "2"),
        ("cache_read_input_tokens", -1),
        ("cache_creation_input_tokens", False),
        ("cache_creation", {"ephemeral_5m_input_tokens": -1}),
    ],
)
async def test_anthropic_malformed_usage_rejects_before_normalization(monkeypatch, field, value):
    calls = []

    def respond(request):
        calls.append(request)
        payload = response()
        payload["usage"][field] = value
        return httpx.Response(200, headers={"request-id": "req_invalid_usage"}, json=payload)

    async with configured(monkeypatch, respond, retries=0) as (extractor, _, _):
        with pytest.raises(LLMError) as caught:
            await extractor.extract("Synthetic evidence")
        receipt = caught.value.details["extraction_usage"]
        assert len(calls) == 1
        assert len(receipt["transport_attempts"]) == 1
        assert receipt["transport_attempts"][0]["status_code"] == 200
        assert not receipt["cost_complete"] and receipt["cost_usd"] is None
        assert not receipt["usage_complete"]


async def test_openai_client_retains_each_redirect_hop():
    import httpx2

    from sibyl_core.ai.transport import RecordingOpenAIClient, collect_transport_attempts

    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx2.Response(
                307,
                headers={
                    "location": "https://api.openai.com/v1/responses?hop=1",
                    "x-request-id": "openai_hop",
                },
            )
        return httpx2.Response(200, headers={"x-request-id": "openai_final"}, json={})

    async with RecordingOpenAIClient(transport=httpx2.MockTransport(respond)) as client:
        with collect_transport_attempts() as attempts:
            await client.post("https://api.openai.com/v1/responses", json={})
        assert [attempt.status_code for attempt in attempts] == [307, 200]
        assert [attempt.request_id for attempt in attempts] == ["openai_hop", "openai_final"]


async def test_anthropic_recording_does_not_buffer_streaming_consumers():
    from sibyl_core.ai.transport import collect_transport_attempts

    reads = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            reads.append("read")
            yield b"data: synthetic\n\n"

    async with RecordingAnthropicClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Body()))
    ) as client:
        with collect_transport_attempts() as attempts:
            async with client.stream("POST", "https://api.anthropic.com/v1/messages") as streamed:
                assert reads == []
                assert len(attempts) == 1 and attempts[0].status_code == 200
                assert await streamed.aread() == b"data: synthetic\n\n"
                assert reads == ["read"]


@pytest.mark.parametrize("deny", [False, True])
async def test_anthropic_uncovered_redirect_reserves_before_dispatch(monkeypatch, deny):
    sent = []
    reservations = []

    def respond(request):
        sent.append(request)
        assert len(reservations) >= len(sent)
        if len(sent) == 1:
            return httpx.Response(
                307, headers={"location": "https://api.anthropic.com/v1/messages?hop=1"}
            )
        return httpx.Response(200, json=response())

    async def reserve(**kwargs):
        reservations.append(kwargs)
        if deny and len(reservations) > 1:
            raise RuntimeError("synthetic extra reservation denied")

    async with configured(monkeypatch, respond, retries=0, output_retries=0) as (
        extractor,
        budget,
        _,
    ):
        budget.side_effect = reserve
        if deny:
            with pytest.raises(LLMError) as caught:
                await extractor.extract("Synthetic evidence")
            assert len(sent) == 1
            assert [
                a["status_code"]
                for a in caught.value.details["extraction_usage"]["transport_attempts"]
            ] == [307]
        else:
            await extractor.extract("Synthetic evidence")
            assert len(sent) == 2
        assert len(reservations) == 2
        assert reservations[0] == reservations[1]
        assert reservations[1]["attempt_envelope"] == 1


async def test_anthropic_redirect_reservations_are_request_local(monkeypatch):
    calls = {}

    async def respond(request):
        identity = json.loads(request.content)["messages"][0]["content"][0]["text"]
        calls[identity] = calls.get(identity, 0) + 1
        await asyncio.sleep(0)
        if identity == "redirect" and calls[identity] == 1:
            return httpx.Response(
                307, headers={"location": "https://api.anthropic.com/v1/messages?hop=1"}
            )
        return httpx.Response(200, json=response())

    async with configured(monkeypatch, respond, retries=0, output_retries=0) as (
        extractor,
        budget,
        _,
    ):
        await asyncio.gather(extractor.extract("redirect"), extractor.extract("direct"))
        prompts = [c.kwargs["prompt"] for c in budget.await_args_list]
        assert len(prompts) == 3
        assert sum("\nredirect\n" in p for p in prompts) == 2
        assert sum("\ndirect\n" in p for p in prompts) == 1
        assert calls == {"redirect": 2, "direct": 1}


async def test_anthropic_cancelled_extra_reservation_keeps_prior_attempt(monkeypatch):
    sent = []
    waiting = asyncio.Event()
    claims = []

    def respond(request):
        sent.append(request)
        return httpx.Response(
            307, headers={"location": "https://api.anthropic.com/v1/messages?hop=1"}
        )

    async def reserve(**kwargs):
        claims.append(kwargs)
        if len(claims) == 2:
            waiting.set()
            await asyncio.Future()

    async with configured(monkeypatch, respond, retries=0, output_retries=0) as (
        extractor,
        budget,
        _,
    ):
        budget.side_effect = reserve
        task = asyncio.create_task(extractor.extract("Synthetic evidence"))
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        receipt = caught.value.extraction_usage
        assert len(sent) == 1 and len(claims) == 2
        assert [a["status_code"] for a in receipt["transport_attempts"]] == [307]
        assert not receipt["cost_complete"] and receipt["cost_usd"] is None


async def test_anthropic_sdk_wrapping_preserves_terminal_budget_denial(monkeypatch):
    from sibyl_core.ai.errors import LLMBudgetExceededError

    sent = []
    reservations = []
    denial = LLMBudgetExceededError("Synthetic budget exhausted")

    def respond(request):
        sent.append(request)
        return httpx.Response(
            307, headers={"location": f"https://api.anthropic.com/v1/messages?hop={len(sent)}"}
        )

    async def reserve(**kwargs):
        reservations.append(kwargs)
        if len(reservations) > 1:
            raise denial

    async with configured(monkeypatch, respond, retries=1, output_retries=0) as (
        extractor,
        budget,
        _,
    ):
        budget.side_effect = reserve
        with pytest.raises(LLMBudgetExceededError) as caught:
            await extractor.extract("Synthetic evidence")
        assert caught.value is denial
        assert len(sent) == 2
        assert [r["attempt_envelope"] for r in reservations] == [2, 1]
        assert [
            a["status_code"] for a in denial.details["extraction_usage"]["transport_attempts"]
        ] == [307, 307]
        assert denial.details["extraction_usage"]["cost_complete"] is False
