"""Real HTTP serialization and fail-closed accounting, without external calls."""

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from sibyl_core.ai.decisions import ChoiceOption, ChoiceQuestion, DecisionRequest, DecisionSubject
from sibyl_core.ai.openrouter_decisions import (
    DECISIONS_ENDPOINT,
    OpenRouterDecisionProvider,
    OpenRouterDecisionRoute,
)
from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind, SourceObservation


@pytest.fixture
def route():
    return OpenRouterDecisionRoute()


@pytest.fixture
def request_for_route(route):
    return DecisionRequest(
        application="source_support",
        question_set_version="support-v1",
        operation_id="operation-secret",
        request_id="request-secret",
        caller_policy_version="shadow-v1",
        policy_epoch=1,
        org_id="org-secret",
        project_id="project-secret",
        authorized_view_fingerprint="a" * 64,
        requested_model_id=route.requested_model_id,
        provider_route_id=route.route_id,
        route_policy_sha256=route.policy_sha256,
        source_refs=(
            SourceObservation(
                SourceIdentity("org-secret", SourceKind.RAW_CAPTURE, "source"),
                1,
                "b" * 64,
                1,
                True,
            ),
        ),
        subject_refs=(
            DecisionSubject(
                candidate_id="candidate",
                candidate_sha256="c" * 64,
                claim_path="/content",
                claim_sha256="d" * 64,
            ),
        ),
        state="The test failed. Claim: the test passed.",
        questions=(
            ChoiceQuestion(
                question_id="/content",
                instructions="Is the claim supported?",
                options=(
                    ChoiceOption(label="yes", description="The evidence supports it."),
                    ChoiceOption(label="no", description="The evidence does not support it."),
                ),
            ),
        ),
    )


def response_payload():
    return {
        "id": "gen-fixture-123",
        "model": "typesafe/jev-1.13-20260917",
        "provider": "TypeSafe",
        "answers": {
            "/content": {
                "type": "choice",
                "choice": "no",
                "confidence": 0.9,
                "probabilities": {"yes": 0, "no": 1},
            }
        },
        "usage": {"input_tokens": 80, "output_tokens": 5, "cost": 0.00002},
    }


async def decide(request, payload, *, status=200):
    transport = httpx.MockTransport(lambda _: httpx.Response(status, json=payload))
    async with OpenRouterDecisionProvider("test-secret", transport=transport) as provider:
        return await provider.decide(request)


async def test_exact_wire_request_and_observed_usage(request_for_route, route):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "POST"
        assert str(request.url) == DECISIONS_ENDPOINT
        assert request.headers["authorization"] == "Bearer test-secret"
        payload = json.loads(request.content)
        assert payload == {
            "model": route.requested_model_id,
            "state": request_for_route.state,
            "questions": {
                "/content": {
                    "type": "choice",
                    "instructions": "Is the claim supported?",
                    "criteria": {
                        "yes": "The evidence supports it.",
                        "no": "The evidence does not support it.",
                    },
                }
            },
            "provider": {
                "only": ["typesafe"],
                "allow_fallbacks": False,
                "data_collection": "deny",
                "zdr": True,
                "require_parameters": True,
            },
        }
        assert "org-secret" not in request.content.decode()
        assert "request-secret" not in request.content.decode()
        return httpx.Response(200, json=response_payload())

    async with OpenRouterDecisionProvider(
        "test-secret", transport=httpx.MockTransport(handler)
    ) as provider:
        observation = await provider.decide(request_for_route)
    assert len(calls) == 1
    assert observation.execution_status == "completed"
    assert observation.answers[0].value == "no"
    assert observation.answers[0].provider_confidence == 0.9
    assert observation.input_tokens == 80
    assert observation.output_tokens == 5
    assert observation.observed_cost_usd == 0.00002
    assert observation.usage_status == "observed"
    assert observation.attempt_count == 1
    observation.validate_for(request_for_route, expected_model_id=route.resolved_model_id)


@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": 0}, {"cost": 0.0}])
async def test_optional_values_remain_unknown(request_for_route, usage):
    payload = response_payload()
    payload["answers"]["/content"] = {"type": "choice", "choice": "no"}
    payload.pop("usage")
    payload.pop("id")
    if usage is not None:
        payload["usage"] = usage
    observation = await decide(request_for_route, payload)
    assert observation.execution_status == "completed"
    assert observation.provider_request_id is None
    assert observation.answers[0].probabilities is None
    assert observation.answers[0].provider_confidence is None
    assert observation.usage_status == ("observed" if usage else "unknown")
    assert observation.output_tokens is None
    assert observation.input_tokens == (usage or {}).get("input_tokens")
    assert observation.observed_cost_usd == (usage or {}).get("cost")


@pytest.mark.parametrize(
    "field,value",
    [
        ("requested_model_id", "different/model"),
        ("provider_route_id", "different/route"),
        ("route_policy_sha256", "f" * 64),
    ],
)
async def test_route_mismatch_never_leaves_process(request_for_route, field, value):
    def handler(_):
        pytest.fail("a mismatched route must not send evidence")

    async with OpenRouterDecisionProvider(
        "test-secret", transport=httpx.MockTransport(handler)
    ) as provider:
        observation = await provider.decide(request_for_route.model_copy(update={field: value}))
    assert observation.execution_status == "unavailable"
    assert observation.error_category == "route_binding_mismatch"
    assert observation.attempt_count == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"type": "score", "score": 0.5},
        {"choice": "unknown"},
        {"choice": True},
        {"probabilities": {"yes": 0.3, "no": 0.3}},
        {"probabilities": {"yes": 1.0}},
        {"probabilities": {"yes": False, "no": True}},
        {"probabilities": {"yes": "0", "no": "1"}},
        {"confidence": 1.1},
        {"confidence": True},
        {"confidence": "0.9"},
        {"reason": "untrusted free text"},
    ],
)
async def test_invalid_answer_preserves_accounting(request_for_route, changes):
    payload = response_payload()
    payload["answers"]["/content"].update(changes)
    observation = await decide(request_for_route, payload)
    assert observation.execution_status == "invalid_response"
    assert observation.error_category == "response_schema_mismatch"
    assert observation.answers == ()
    assert observation.provider_request_id == "gen-fixture-123"
    assert observation.observed_cost_usd == 0.00002
    assert observation.usage_status == "observed"


@pytest.mark.parametrize("answers", [{}, {"unexpected": {"type": "choice", "choice": "no"}}])
async def test_answer_set_must_match_exactly(request_for_route, answers):
    payload = response_payload()
    payload["answers"] = answers
    observation = await decide(request_for_route, payload)
    assert observation.execution_status == "invalid_response"
    assert observation.answers == ()


@pytest.mark.parametrize(
    "field,value,category",
    [
        ("model", "typesafe/jev-new", "model_mismatch"),
        ("provider", "Alternate", "provider_mismatch"),
        ("extra", "unexpected", "response_schema_mismatch"),
        ("usage", {"input_tokens": True, "cost": 0.001}, "response_schema_mismatch"),
    ],
)
async def test_invalid_envelope(request_for_route, field, value, category):
    payload = response_payload()
    payload[field] = value
    observation = await decide(request_for_route, payload)
    assert observation.execution_status == "invalid_response"
    assert observation.error_category == category
    if field == "usage":
        assert observation.input_tokens is None
        assert observation.observed_cost_usd == 0.001


@pytest.mark.parametrize("status", [301, 400, 401, 402, 403, 413, 429, 500, 503, 529])
async def test_http_failure_never_leaks_body_or_retries(request_for_route, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status,
            json={"error": {"message": "source-secret test-secret"}, "usage": {"cost": 0.001}},
            headers={"location": "https://attacker.invalid/collect"},
        )

    async with OpenRouterDecisionProvider(
        "test-secret", transport=httpx.MockTransport(handler)
    ) as provider:
        observation = await provider.decide(request_for_route)
    assert len(calls) == 1
    assert observation.execution_status == "unavailable"
    assert observation.error_category == f"http_{status}"
    assert observation.observed_cost_usd == 0.001
    assert "secret" not in observation.model_dump_json()


@pytest.mark.parametrize(
    "body",
    [b'{"answers":{},"answers":{}}', b'{"usage":{"cost":NaN}}', b"\xff", b"[]", b"not-json"],
)
async def test_ambiguous_or_malformed_json(request_for_route, body):
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=body))
    async with OpenRouterDecisionProvider("test-secret", transport=transport) as provider:
        observation = await provider.decide(request_for_route)
    assert observation.execution_status == "invalid_response"
    assert observation.answers == ()
    assert observation.usage_status == "unknown"


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout])
async def test_transport_errors_are_sanitized(request_for_route, error):
    def handler(_):
        raise error("source-secret test-secret")

    async with OpenRouterDecisionProvider(
        "test-secret", transport=httpx.MockTransport(handler)
    ) as provider:
        observation = await provider.decide(request_for_route)
    assert observation.execution_status == "unavailable"
    assert observation.attempt_count == 1
    assert observation.usage_status == "unknown"
    assert "secret" not in observation.model_dump_json()


async def test_total_deadline_cancels_slow_transport(request_for_route):
    route = OpenRouterDecisionRoute(deadline_seconds=0.01)
    request = request_for_route.model_copy(update={"route_policy_sha256": route.policy_sha256})
    interrupted = asyncio.Event()

    async def handler(_):
        try:
            await asyncio.Event().wait()
        finally:
            interrupted.set()
        raise AssertionError("unreachable")

    async with OpenRouterDecisionProvider(
        "test-secret", route=route, transport=httpx.MockTransport(handler)
    ) as provider:
        observation = await provider.decide(request)
    assert interrupted.is_set()
    assert observation.error_category == "deadline_exceeded"
    assert observation.attempt_count == 1
    assert observation.usage_status == "unknown"


async def test_cancellation_propagates(request_for_route):
    entered = asyncio.Event()

    async def handler(_):
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async with OpenRouterDecisionProvider(
        "test-secret", transport=httpx.MockTransport(handler)
    ) as provider:
        task = asyncio.create_task(provider.decide(request_for_route))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_closed_provider_is_zero_attempt_failure(request_for_route):
    provider = OpenRouterDecisionProvider("test-secret")
    await provider.aclose()
    observation = await provider.decide(request_for_route)
    assert observation.error_category == "transport_closed"
    assert observation.attempt_count == 0


def test_route_is_immutable_and_deadline_does_not_change_semantic_identity(
    route, request_for_route
):
    with pytest.raises(ValidationError):
        route.deadline_seconds = 1.0
    changed_route = OpenRouterDecisionRoute(deadline_seconds=1.0)
    assert route.policy_sha256 == changed_route.policy_sha256
    changed_request = request_for_route.model_copy(
        update={"route_policy_sha256": changed_route.policy_sha256}
    )
    assert request_for_route.semantic_input_sha256 == changed_request.semantic_input_sha256
    preferences = route.provider_preferences
    preferences["allow_fallbacks"] = True
    assert route.provider_preferences["allow_fallbacks"] is False


@pytest.mark.parametrize(
    "credential", ["", " ", "has whitespace", "has\nnewline", "has\x00null", "sëcret"]
)
def test_reject_invalid_credentials_without_echoing(credential):
    with pytest.raises(ValueError, match="a nonempty OpenRouter credential is required"):
        OpenRouterDecisionProvider(credential)


async def test_multi_question_requests_require_complete_answers(request_for_route):
    question = request_for_route.questions[0].model_copy(update={"question_id": "/other"})
    request = request_for_route.model_copy(
        update={"questions": (*request_for_route.questions, question)}
    )
    payload = response_payload()
    missing = await decide(request, payload)
    assert missing.execution_status == "invalid_response"
    payload["answers"]["/other"] = {"type": "choice", "choice": "yes"}
    complete = await decide(request, payload)
    assert complete.execution_status == "completed"
    assert len(complete.answers) == 2


async def test_shared_provider_calls_run_concurrently(request_for_route):
    entered = 0
    both_entered = asyncio.Event()

    async def handler(_):
        nonlocal entered
        entered += 1
        if entered == 2:
            both_entered.set()
        await both_entered.wait()
        return httpx.Response(200, json=response_payload())

    async with OpenRouterDecisionProvider(
        "test-secret", transport=httpx.MockTransport(handler)
    ) as provider:
        async with asyncio.timeout(1):
            observations = await asyncio.gather(
                provider.decide(request_for_route), provider.decide(request_for_route)
            )
    assert all(item.execution_status == "completed" for item in observations)
    assert entered == 2
