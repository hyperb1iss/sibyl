"""Immutable Choice decisions and offline replay, without publication authority.

The first consumer is source support. Score and Noul are deliberately unsupported
until a caller needs their distinct semantics. Transport adapters must validate
complete responses here before exposing any semantic answer.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from typing import Annotated, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from sibyl_core.memory_pipeline.observations import SourceObservation
from sibyl_core.tasks._evidence_json import canonical, read_json_value

_Text = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
_Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
_Count = Annotated[int, Field(ge=0)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ChoiceOption(_StrictModel):
    label: _Text
    description: _Text


class ChoiceQuestion(_StrictModel):
    question_id: _Text
    primitive: Literal["choice"] = "choice"
    instructions: _Text
    options: Annotated[tuple[ChoiceOption, ...], Field(min_length=2)]

    @model_validator(mode="after")
    def unique_options(self) -> Self:
        labels = [option.label for option in self.options]
        if len(set(labels)) != len(labels):
            raise ValueError("choice labels must be unique")
        return self


class DecisionSubject(_StrictModel):
    candidate_id: _Text
    candidate_sha256: _Digest
    claim_path: _Text
    claim_sha256: _Digest


class DecisionRequest(_StrictModel):
    schema_version: Literal["sibyl-choice-decision-v1"] = "sibyl-choice-decision-v1"
    application: _Text
    question_set_version: _Text
    operation_id: _Text
    request_id: _Text
    caller_policy_version: _Text
    policy_epoch: _Count
    org_id: _Text
    project_id: _Text | None
    authorized_view_fingerprint: _Digest
    requested_model_id: _Text
    provider_route_id: _Text
    route_policy_sha256: _Digest
    source_refs: Annotated[tuple[SourceObservation, ...], Field(min_length=1)]
    subject_refs: Annotated[tuple[DecisionSubject, ...], Field(min_length=1)]
    state: _Text
    questions: Annotated[tuple[ChoiceQuestion, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def consistent_identity(self) -> Self:
        sources = [source.source.key for source in self.source_refs]
        subjects = [(subject.candidate_id, subject.claim_path) for subject in self.subject_refs]
        questions = [question.question_id for question in self.questions]
        for values in (sources, subjects, questions):
            if len(set(values)) != len(values):
                raise ValueError("decision identities must be unique")
        if any(source.source.organization_id != self.org_id for source in self.source_refs):
            raise ValueError("decision sources must belong to the request organization")
        return self

    @property
    def semantic_input_sha256(self) -> str:
        """Bind all semantic inputs, with SourceObservation.same_evidence semantics."""
        payload = self.model_dump(mode="json", exclude={"request_id", "operation_id"})
        for item, source in zip(payload["source_refs"], self.source_refs, strict=True):
            item.pop("revision")
            item["incarnation"] = source.effective_incarnation
        return _digest(payload)

    @property
    def request_digest(self) -> str:
        """Retain exact execution identity, including bookkeeping and transport IDs."""
        return _digest(self.model_dump(mode="json"))


class ChoiceProbability(_StrictModel):
    label: _Text
    probability: _Probability


class ChoiceAnswer(_StrictModel):
    question_id: _Text
    primitive: Literal["choice"] = "choice"
    value: _Text
    probabilities: tuple[ChoiceProbability, ...] | None = None
    provider_confidence: _Probability | None = None

    @model_validator(mode="after")
    def valid_distribution(self) -> Self:
        if self.probabilities is not None:
            labels = [item.label for item in self.probabilities]
            if not labels or len(set(labels)) != len(labels):
                raise ValueError("probability labels must be nonempty and unique")
            # This is our normalized receipt tolerance, not a calibration rule.
            if not math.isclose(
                math.fsum(item.probability for item in self.probabilities),
                1.0,
                rel_tol=0,
                abs_tol=1e-6,
            ):
                raise ValueError("probabilities must sum to one")
        return self


class DecisionObservation(_StrictModel):
    schema_version: Literal["sibyl-choice-observation-v1"] = "sibyl-choice-observation-v1"
    semantic_input_sha256: _Digest
    request_digest: _Digest
    execution_status: Literal["completed", "unavailable", "invalid_response", "stale_input"]
    resolved_model_id: _Text | None = None
    provider_request_id: _Text | None = None
    observed_provider: _Text | None = None
    answers: tuple[ChoiceAnswer, ...] = ()
    input_tokens: _Count | None = None
    output_tokens: _Count | None = None
    observed_cost_usd: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    usage_status: Literal["observed", "unknown"] = "unknown"
    attempt_count: _Count
    elapsed_ms: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    error_category: _Text | None = None

    @model_validator(mode="after")
    def consistent_outcome(self) -> Self:
        if self.execution_status == "completed":
            if (
                not self.answers
                or self.resolved_model_id is None
                or self.error_category is not None
            ):
                raise ValueError(
                    "completed decisions require a model and answers, without an error"
                )
        elif self.answers or self.error_category is None:
            raise ValueError("execution failures require a reason and cannot contain answers")
        if self.usage_status == "observed":
            if all(
                value is None
                for value in (self.input_tokens, self.output_tokens, self.observed_cost_usd)
            ):
                raise ValueError("observed usage requires at least one measured field")
        elif any(
            value is not None
            for value in (self.input_tokens, self.output_tokens, self.observed_cost_usd)
        ):
            raise ValueError("unknown usage cannot invent token counts or cost")
        return self

    def validate_for(self, request: DecisionRequest, *, expected_model_id: str) -> None:
        """Validate the whole answer set before a caller can consume any answer."""
        if (
            self.request_digest != request.request_digest
            or self.semantic_input_sha256 != request.semantic_input_sha256
        ):
            raise ValueError("observation belongs to a different request")
        if self.execution_status != "completed":
            return
        if self.resolved_model_id != expected_model_id:
            raise ValueError("unexpected resolved decision model")
        questions = {question.question_id: question for question in request.questions}
        answers = {answer.question_id: answer for answer in self.answers}
        if len(answers) != len(self.answers) or answers.keys() != questions.keys():
            raise ValueError("decision answers must cover every question exactly once")
        for identifier, answer in answers.items():
            labels = {option.label for option in questions[identifier].options}
            if answer.value not in labels:
                raise ValueError("answer is outside the question choices")
            if (
                answer.probabilities is not None
                and {item.label for item in answer.probabilities} != labels
            ):
                raise ValueError("distribution must cover exactly the question choices")


class DecisionProvider(Protocol):
    async def decide(self, request: DecisionRequest) -> DecisionObservation:
        """Return a request-bound observation; callers still validate and authorize it."""
        ...


class ReplayDecisionProvider:
    """Replay immutable serialized receipts, keyed by exact execution identity.

    Missing receipts are errors, never fabricated semantic abstentions. This
    provider owns no transport, credentials, memory writes, or cache rebinding.
    """

    def __init__(self, receipts: Mapping[str, bytes], *, expected_model_id: str) -> None:
        self._receipts = dict(receipts)
        self._expected_model_id = expected_model_id

    async def decide(self, request: DecisionRequest) -> DecisionObservation:
        try:
            payload = self._receipts[request.request_digest]
        except KeyError:
            raise ValueError("missing decision replay receipt") from None
        read_json_value(payload)  # Reject duplicate keys and nonfinite JSON before model parsing.
        observation = DecisionObservation.model_validate_json(payload)
        observation.validate_for(request, expected_model_id=self._expected_model_id)
        return observation


def _digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()
