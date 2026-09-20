"""Readiness never waits, rebinds evidence, or substitutes for current authority."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from sibyl_core.ai.decisions import ChoiceAnswer, DecisionObservation
from sibyl_core.ai.openrouter_decisions import OpenRouterDecisionRoute

from . import background_policy as policy
from .support_inputs import make_request

MODEL = OpenRouterDecisionRoute().resolved_model_id


@pytest.fixture
def request_and_observation():
    request = make_request(
        {
            "id": "valve",
            "content": "The valve is closed.",
            "claims": [],
            "sources": [{"id": "source", "text": "The valve is closed.", "provenance": "signed"}],
        },
        "readiness-test",
    )
    observation = DecisionObservation(
        semantic_input_sha256=request.semantic_input_sha256,
        request_digest=request.request_digest,
        execution_status="completed",
        resolved_model_id=MODEL,
        answers=(
            ChoiceAnswer(question_id="/content", value="supported", provider_confidence=0.999),
        ),
        attempt_count=1,
        elapsed_ms=100,
    )
    return request, observation


def select(request, observation, **kwargs):
    return policy.select_ready(
        request,
        observation,
        **{"completed_ms": 100, "now_ms": 100, "authorized": True, **kwargs},
        expected_model_id=MODEL,
    )


@pytest.mark.parametrize(("now", "status"), [(0, "pending"), (99, "pending"), (100, "ready")])
def test_completed_receipt_must_precede_the_consumer(request_and_observation, now, status):
    request, observation = request_and_observation
    assert select(request, observation, now_ms=now) == status


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request_id", "another-execution"),
        ("policy_epoch", 1),
        ("caller_policy_version", "changed-policy"),
        ("authorized_view_fingerprint", "a" * 64),
        ("route_policy_sha256", "b" * 64),
        ("project_id", "another-project"),
        ("state", "Changed source text"),
    ],
)
def test_changed_request_never_borrows_a_completed_answer(request_and_observation, field, value):
    request, observation = request_and_observation
    assert select(request.model_copy(update={field: value}), observation) == "stale"


@pytest.mark.parametrize(
    ("field", "value"), [("generation", 2), ("revision", 2), ("incarnation", "new")]
)
def test_lifecycle_change_misses_even_if_semantic_identity_is_unchanged(
    request_and_observation, field, value
):
    request, observation = request_and_observation
    changed = request.model_copy(
        update={"source_refs": (replace(request.source_refs[0], **{field: value}),)}
    )
    assert select(changed, observation) == "stale"


@pytest.mark.parametrize(
    ("field", "value"), [("resolved_model_id", "other-model"), ("answers", ())]
)
def test_ready_but_invalid_receipt_defers(request_and_observation, field, value):
    request, observation = request_and_observation
    assert select(request, observation.model_copy(update={field: value})) == "invalid"


def test_missing_and_revoked_receipts_cannot_authorize(request_and_observation):
    request, observation = request_and_observation
    assert select(request, None) == "missing"
    assert select(request, observation, authorized=False) == "unauthorized"
    assert select(request, observation, completed_ms=None) == "pending"


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True])
@pytest.mark.parametrize("field", ["now_ms", "completed_ms"])
def test_invalid_time_cannot_create_a_hit(request_and_observation, field, value):
    request, observation = request_and_observation
    with pytest.raises(ValueError, match="finite and nonnegative"):
        select(request, observation, **{field: value})


async def test_pending_background_work_does_not_delay_or_cancel_the_critic(request_and_observation):
    request, observation = request_and_observation
    started, release = asyncio.Event(), asyncio.Event()

    async def background():
        started.set()
        await release.wait()
        return observation

    task = asyncio.create_task(background())
    try:
        await started.wait()
        selected = policy.probe_task(request, task, authorized=True, expected_model_id=MODEL)
        assert selected == "pending"
        critic = AsyncMock(return_value="critic-result")
        assert await critic() == "critic-result"
        critic.assert_awaited_once()
        assert not task.done()
        release.set()
        assert await task == observation
        assert selected == "pending"  # Later completion cannot rewrite the selected route.
        assert policy.probe_task(request, task, authorized=True, expected_model_id=MODEL) == "ready"
        assert (
            policy.probe_task(request, task, authorized=False, expected_model_id=MODEL)
            == "unauthorized"
        )
    finally:
        release.set()
        await task


@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_background_task_is_not_a_semantic_result(request_and_observation, cancel):
    request, _ = request_and_observation

    async def fail():
        raise RuntimeError("provider failure")

    task = asyncio.create_task(fail())
    if cancel:
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert policy.probe_task(request, task, authorized=True, expected_model_id=MODEL) == "invalid"
