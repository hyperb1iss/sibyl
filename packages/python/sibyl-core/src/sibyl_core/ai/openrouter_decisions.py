"""Bounded Choice transport for the qualified OpenRouter Decisions route.

This adapter has no authorization or publication authority. Callers must authorize
external evidence transfer before calling it and protect the resulting receipt.
The published 32,000-token context ceiling is provider-enforced because the
catalog exposes no tokenizer and Decisions accepts no token-budget parameters.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Annotated, Literal, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, ValidationError

from sibyl_core.ai.decisions import (
    ChoiceAnswer,
    ChoiceProbability,
    DecisionObservation,
    DecisionRequest,
)
from sibyl_core.tasks._evidence_json import canonical, read_json_value

DECISIONS_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
_Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:/-]{1,256}$")]
_Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
_Count = Annotated[int, Field(ge=0)]
_Cost = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class OpenRouterDecisionRoute(_WireModel):
    """Immutable route qualification and privacy policy, with an operational deadline."""

    route_id: Literal["openrouter/typesafe-jev-1.13"] = "openrouter/typesafe-jev-1.13"
    requested_model_id: Literal["typesafe/jev-1.13"] = "typesafe/jev-1.13"
    resolved_model_id: Literal["typesafe/jev-1.13-20260917"] = "typesafe/jev-1.13-20260917"
    expected_provider: Literal["TypeSafe"] = "TypeSafe"
    deadline_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 30.0

    @property
    def provider_preferences(self) -> dict[str, object]:
        return {
            "only": ["typesafe"],
            "allow_fallbacks": False,
            "data_collection": "deny",
            "zdr": True,
            "require_parameters": True,
        }

    @property
    def policy_sha256(self) -> str:
        payload = {
            "version": "sibyl-openrouter-choice-route-v1",
            "endpoint": DECISIONS_ENDPOINT,
            "route": self.model_dump(mode="json", exclude={"deadline_seconds"}),
            "provider": self.provider_preferences,
        }
        return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


class _ChoiceWire(_WireModel):
    type: Literal["choice"]
    choice: str
    probabilities: dict[str, _Probability] | None = None
    confidence: _Probability | None = None


class _UsageWire(_WireModel):
    input_tokens: _Count | None = None
    output_tokens: _Count | None = None
    cost: _Cost | None = None


class _ResponseWire(_WireModel):
    id: _Identifier | None = None
    model: _Identifier
    provider: _Identifier
    answers: dict[str, _ChoiceWire]
    usage: _UsageWire | None = None


class _MetadataWire(BaseModel):
    """Independently valid accounting survives malformed semantic answers."""

    model_config = ConfigDict(extra="ignore", strict=True)
    id: _Identifier | None = None
    model: _Identifier | None = None
    provider: _Identifier | None = None
    usage: _UsageWire | None = None


def _metadata(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        return {}
    result: dict[str, object] = {}
    for wire_name, receipt_name in (
        ("id", "provider_request_id"),
        ("model", "resolved_model_id"),
        ("provider", "observed_provider"),
    ):
        try:
            metadata = _MetadataWire.model_validate({wire_name: payload.get(wire_name)})
        except ValidationError:
            continue
        value = getattr(metadata, wire_name)
        if value is not None:
            result[receipt_name] = value
    usage = payload.get("usage")
    if isinstance(usage, dict):
        for wire_name, receipt_name in (
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
            ("cost", "observed_cost_usd"),
        ):
            try:
                validated = _UsageWire.model_validate({wire_name: usage.get(wire_name)})
            except ValidationError:
                continue
            value = getattr(validated, wire_name)
            if value is not None:
                result[receipt_name] = value
                result["usage_status"] = "observed"
    return result


class OpenRouterDecisionProvider:
    """One request per decision, with no alternate routes or retry amplification.

    The owned client supports concurrent calls and must be closed by its owner.
    Cancellation propagates; the caller records its interrupted execution rather
    than pretending that a possibly billable request never reached the provider.
    """

    def __init__(
        self,
        api_key: SecretStr | str,
        *,
        route: OpenRouterDecisionRoute | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        secret = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        if not secret or any(not 33 <= ord(character) <= 126 for character in secret):
            raise ValueError("a nonempty OpenRouter credential is required")
        self.route = route or OpenRouterDecisionRoute()
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {secret}"},
            timeout=self.route.deadline_seconds,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def decide(self, request: DecisionRequest) -> DecisionObservation:
        started = time.monotonic()
        identity = {
            "semantic_input_sha256": request.semantic_input_sha256,
            "request_digest": request.request_digest,
        }

        def failure(
            category: str,
            *,
            attempted: bool = True,
            invalid: bool = False,
            metadata: dict[str, object] | None = None,
        ) -> DecisionObservation:
            return DecisionObservation.model_validate(
                {
                    **identity,
                    **(metadata or {}),
                    "execution_status": "invalid_response" if invalid else "unavailable",
                    "attempt_count": int(attempted),
                    "elapsed_ms": (time.monotonic() - started) * 1000,
                    "error_category": category,
                }
            )

        if (
            request.requested_model_id != self.route.requested_model_id
            or request.provider_route_id != self.route.route_id
            or request.route_policy_sha256 != self.route.policy_sha256
        ):
            return failure("route_binding_mismatch", attempted=False)
        if self._client.is_closed:
            return failure("transport_closed", attempted=False)
        body = {
            "model": self.route.requested_model_id,
            "state": request.state,
            "questions": {
                question.question_id: {
                    "type": "choice",
                    "instructions": question.instructions,
                    "criteria": {option.label: option.description for option in question.options},
                }
                for question in request.questions
            },
            "provider": self.route.provider_preferences,
        }
        try:
            async with asyncio.timeout(self.route.deadline_seconds):
                response = await self._client.post(DECISIONS_ENDPOINT, json=body)
        except (TimeoutError, httpx.TimeoutException):
            return failure("deadline_exceeded")
        except httpx.HTTPError:
            return failure("transport_error")
        # Parse even denied responses for independently valid billing metadata.
        try:
            payload = read_json_value(response.content)
        except (ValueError, UnicodeError):
            return failure(
                "malformed_json" if response.status_code == 200 else f"http_{response.status_code}",
                invalid=response.status_code == 200,
            )
        metadata = _metadata(payload)
        if response.status_code != 200:
            return failure(f"http_{response.status_code}", metadata=metadata)
        try:
            wire = _ResponseWire.model_validate(payload)
            if wire.provider != self.route.expected_provider:
                return failure("provider_mismatch", invalid=True, metadata=metadata)
            if wire.model != self.route.resolved_model_id:
                return failure("model_mismatch", invalid=True, metadata=metadata)
            answers = tuple(
                ChoiceAnswer(
                    question_id=identifier,
                    value=answer.choice,
                    probabilities=(
                        tuple(
                            ChoiceProbability(label=label, probability=probability)
                            for label, probability in answer.probabilities.items()
                        )
                        if answer.probabilities is not None
                        else None
                    ),
                    provider_confidence=answer.confidence,
                )
                for identifier, answer in wire.answers.items()
            )
            observation = DecisionObservation.model_validate(
                {
                    **identity,
                    **metadata,
                    "execution_status": "completed",
                    "answers": answers,
                    "attempt_count": 1,
                    "elapsed_ms": (time.monotonic() - started) * 1000,
                }
            )
            observation.validate_for(request, expected_model_id=self.route.resolved_model_id)
        except ValueError:
            return failure("response_schema_mismatch", invalid=True, metadata=metadata)
        return observation
