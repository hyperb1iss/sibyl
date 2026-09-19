"""Decision identity and replay fail closed before any semantic consumption."""

from dataclasses import replace

import pytest
from pydantic import ValidationError

from sibyl_core.ai.decisions import (
    ChoiceAnswer,
    ChoiceOption,
    ChoiceProbability,
    ChoiceQuestion,
    DecisionObservation,
    DecisionRequest,
    DecisionSubject,
    ReplayDecisionProvider,
)
from sibyl_core.memory_pipeline.observations import (
    SourceIdentity,
    SourceKind,
    SourceObservation,
)


@pytest.fixture
def decision_request():
    return DecisionRequest(
        application="source_support",
        question_set_version="support-v1",
        operation_id="operation",
        request_id="decision_request",
        caller_policy_version="shadow-v1",
        policy_epoch=1,
        org_id="org",
        project_id="project",
        authorized_view_fingerprint="a" * 64,
        requested_model_id="fixture/model",
        provider_route_id="offline",
        route_policy_sha256="b" * 64,
        source_refs=(
            SourceObservation(
                SourceIdentity("org", SourceKind.RAW_CAPTURE, "source"), 1, "c" * 64, 1, True
            ),
        ),
        subject_refs=(
            DecisionSubject(
                candidate_id="candidate",
                candidate_sha256="d" * 64,
                claim_path="/content",
                claim_sha256="e" * 64,
            ),
        ),
        state='{"claim":"The test passed.","evidence":"The test failed."}',
        questions=(
            ChoiceQuestion(
                question_id="/content",
                instructions="Does the evidence support this claim?",
                options=(
                    ChoiceOption(label="supported", description="The evidence establishes it."),
                    ChoiceOption(label="contradicted", description="The evidence contradicts it."),
                    ChoiceOption(label="insufficient", description="Evidence is missing."),
                    ChoiceOption(label="ambiguous", description="Evidence is conflicting."),
                ),
            ),
        ),
    )


def observation(decision_request, **changes):
    values = dict(
        semantic_input_sha256=decision_request.semantic_input_sha256,
        request_digest=decision_request.request_digest,
        execution_status="completed",
        resolved_model_id="fixture/model-v1",
        answers=(ChoiceAnswer(question_id="/content", value="contradicted"),),
        attempt_count=1,
        elapsed_ms=10.0,
    )
    values.update(changes)
    return DecisionObservation(**values)


def update(decision_request, **changes):
    values = {name: getattr(decision_request, name) for name in type(decision_request).model_fields}
    values.update(changes)
    return DecisionRequest(**values)


@pytest.mark.parametrize("change", ["revision", "incarnation", "transport"])
def test_semantic_identity_reuses_only_equivalent_evidence(decision_request, change):
    source = decision_request.source_refs[0]
    if change == "revision":
        changed = update(decision_request, source_refs=(replace(source, revision=2),))
    elif change == "incarnation":
        changed = update(
            decision_request,
            source_refs=(replace(source, incarnation=source.effective_incarnation),),
        )
    else:
        changed = update(
            decision_request, operation_id="new operation", request_id="new decision_request"
        )
    assert changed.semantic_input_sha256 == decision_request.semantic_input_sha256
    assert changed.request_digest != decision_request.request_digest


@pytest.mark.parametrize(
    "field,value",
    [
        ("application", "different"),
        ("question_set_version", "v2"),
        ("caller_policy_version", "policy-v2"),
        ("policy_epoch", 2),
        ("project_id", "different"),
        ("authorized_view_fingerprint", "f" * 64),
        ("requested_model_id", "different"),
        ("provider_route_id", "different"),
        ("route_policy_sha256", "f" * 64),
        ("state", "new query, environment or selected evidence"),
    ],
)
def test_semantic_identity_binds_context_and_policy(decision_request, field, value):
    assert (
        update(decision_request, **{field: value}).semantic_input_sha256
        != decision_request.semantic_input_sha256
    )


@pytest.mark.parametrize("change", ["kind", "generation", "hash", "durable", "rebuilt"])
def test_semantic_identity_binds_complete_source_observation(decision_request, change):
    source = decision_request.source_refs[0]
    changes = {
        "kind": {"source": replace(source.source, kind=SourceKind.GRAPH_ENTITY)},
        "generation": {"generation": 2},
        "hash": {"content_sha256": "f" * 64},
        "durable": {"durable": False},
        "rebuilt": {"incarnation": "new-ledger"},
    }
    changed = update(decision_request, source_refs=(replace(source, **changes[change]),))
    assert changed.semantic_input_sha256 != decision_request.semantic_input_sha256


def test_semantic_identity_binds_claim_and_exact_question(decision_request):
    subject = decision_request.subject_refs[0].model_copy(update={"claim_sha256": "f" * 64})
    question = decision_request.questions[0].model_copy(
        update={"instructions": "Different question"}
    )
    assert (
        update(decision_request, subject_refs=(subject,)).semantic_input_sha256
        != decision_request.semantic_input_sha256
    )
    assert (
        update(decision_request, questions=(question,)).semantic_input_sha256
        != decision_request.semantic_input_sha256
    )


def test_request_rejects_cross_org_and_repeated_identity(decision_request):
    with pytest.raises(ValidationError, match="organization"):
        update(decision_request, org_id="other")
    for field in ("source_refs", "subject_refs", "questions"):
        with pytest.raises(ValidationError, match="unique"):
            update(decision_request, **{field: getattr(decision_request, field) * 2})


def test_request_roundtrip_and_immutable_collections(decision_request):
    restored = DecisionRequest.model_validate_json(decision_request.model_dump_json())
    assert restored == decision_request
    assert restored.semantic_input_sha256 == decision_request.semantic_input_sha256
    with pytest.raises(ValidationError):
        decision_request.state = "changed"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, 1.1, True, "0.5"])
def test_probability_rejects_invalid_values(value):
    with pytest.raises(ValidationError):
        ChoiceProbability(label="supported", probability=value)
    with pytest.raises(ValidationError):
        ChoiceAnswer(question_id="q", value="supported", provider_confidence=value)


@pytest.mark.parametrize(
    "probabilities",
    [
        (),
        (ChoiceProbability(label="supported", probability=0.1),),
        (
            ChoiceProbability(label="supported", probability=0.5),
            ChoiceProbability(label="supported", probability=0.5),
        ),
    ],
)
def test_distribution_rejects_empty_incomplete_mass_and_duplicates(probabilities):
    with pytest.raises(ValidationError):
        ChoiceAnswer(question_id="q", value="supported", probabilities=probabilities)


@pytest.mark.parametrize(
    "answers",
    [
        (),
        (ChoiceAnswer(question_id="other", value="supported"),),
        (
            ChoiceAnswer(question_id="/content", value="supported"),
            ChoiceAnswer(question_id="/content", value="supported"),
        ),
        (ChoiceAnswer(question_id="/content", value="unknown-label"),),
        (
            ChoiceAnswer(
                question_id="/content",
                value="supported",
                probabilities=(ChoiceProbability(label="supported", probability=1.0),),
            ),
        ),
    ],
)
def test_observation_rejects_incomplete_or_invalid_answer_sets(decision_request, answers):
    with pytest.raises(ValueError):
        observation(decision_request, answers=answers).validate_for(
            decision_request, expected_model_id="fixture/model-v1"
        )


def test_optional_provider_fields_remain_absent(decision_request):
    result = observation(decision_request)
    result.validate_for(decision_request, expected_model_id="fixture/model-v1")
    assert result.answers[0].probabilities is None
    assert result.answers[0].provider_confidence is None
    assert result.observed_cost_usd is None
    assert result.input_tokens is None
    assert result.usage_status == "unknown"


def test_insufficient_is_a_semantic_result_not_transport_failure(decision_request):
    result = observation(
        decision_request, answers=(ChoiceAnswer(question_id="/content", value="insufficient"),)
    )
    result.validate_for(decision_request, expected_model_id="fixture/model-v1")
    assert result.execution_status == "completed"
    failure = observation(
        decision_request, execution_status="unavailable", answers=(), error_category="timeout"
    )
    failure.validate_for(decision_request, expected_model_id="fixture/model-v1")
    assert not failure.answers
    with pytest.raises(ValidationError):
        observation(decision_request, execution_status="unavailable", error_category="timeout")


@pytest.mark.parametrize(
    "changes",
    [
        {"input_tokens": 0},
        {"observed_cost_usd": 0.0},
        {"usage_status": "observed"},
        {"attempt_count": True},
        {"elapsed_ms": float("nan")},
        {"execution_status": "unknown"},
        {"applied": True},
    ],
)
def test_observation_rejects_fabricated_accounting_and_authority(decision_request, changes):
    with pytest.raises(ValidationError):
        observation(decision_request, **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"request_digest": "f" * 64},
        {"semantic_input_sha256": "f" * 64},
        {"resolved_model_id": "unexpected-model"},
    ],
)
def test_observation_rejects_mismatched_identity(decision_request, changes):
    with pytest.raises(ValueError):
        observation(decision_request, **changes).validate_for(
            decision_request, expected_model_id="fixture/model-v1"
        )


@pytest.mark.asyncio
async def test_replay_is_deterministic_and_preserves_original_accounting(decision_request):
    result = observation(
        decision_request,
        input_tokens=11,
        output_tokens=7,
        observed_cost_usd=0.002,
        usage_status="observed",
    )
    provider = ReplayDecisionProvider(
        {decision_request.request_digest: result.model_dump_json().encode()},
        expected_model_id="fixture/model-v1",
    )
    assert await provider.decide(decision_request) == result
    assert await provider.decide(decision_request) == result
    with pytest.raises(ValueError, match="missing"):
        await provider.decide(update(decision_request, request_id="different execution"))


@pytest.mark.asyncio
async def test_replay_rejects_duplicate_json_keys(decision_request):
    payload = observation(decision_request).model_dump_json()
    payload = payload.replace('"attempt_count":1', '"attempt_count":1,"attempt_count":2')
    provider = ReplayDecisionProvider(
        {decision_request.request_digest: payload.encode()}, expected_model_id="fixture/model-v1"
    )
    with pytest.raises(ValueError, match="unique"):
        await provider.decide(decision_request)


@pytest.mark.parametrize("primitive", ["score", "noul", "unknown"])
def test_unsupported_primitives_are_not_coerced_to_choice(primitive):
    with pytest.raises(ValidationError):
        ChoiceAnswer(question_id="q", primitive=primitive, value="supported")


def test_partial_usage_retains_known_fields_without_inventing_the_rest(decision_request):
    result = observation(decision_request, usage_status="observed", input_tokens=10)
    assert result.input_tokens == 10
    assert result.output_tokens is None
    assert result.observed_cost_usd is None
