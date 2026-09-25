"""Shadow observations leave the critic unchanged and stay behind source authority."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from sibyl_core.ai.decisions import ChoiceAnswer, DecisionObservation
from sibyl_core.ai.llm.extractor import Extractor
from sibyl_core.ai.openrouter_decisions import OpenRouterDecisionRoute
from sibyl_core.config import settings
from sibyl_core.services import ordinary_cohort, procedure_validation, reflection_validation
from sibyl_core.services import semantic_decisions as shadow
from sibyl_core.services.decision_receipts import configure_policy
from sibyl_core.services.memory_source_validation import SourceReadAuthority
from sibyl_core.tasks.memory_validation import CriticOutput
from tests.test_ordinary_cohort import cohort_sources as cohort_sources
from tests.test_ordinary_cohort import content_store as content_store
from tests.test_ordinary_cohort import install_proposal
from tests.test_ordinary_cohort import runtime as runtime


class Provider:
    instances: ClassVar[list] = []
    behavior = None

    def __init__(self, *_args, **_kwargs):
        self.closed = False
        self.requests = []
        self.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True

    async def decide(self, request):
        self.requests.append(request)
        if self.behavior is not None:
            await self.behavior(request)
        return DecisionObservation(
            semantic_input_sha256=request.semantic_input_sha256,
            request_digest=request.request_digest,
            execution_status="completed",
            resolved_model_id=OpenRouterDecisionRoute().resolved_model_id,
            observed_provider="TypeSafe",
            answers=tuple(
                ChoiceAnswer(question_id=q.question_id, value="contradicted")
                for q in request.questions
            ),
            input_tokens=17,
            output_tokens=3,
            usage_status="observed",
            attempt_count=1,
            elapsed_ms=2.0,
        )


@pytest.fixture(autouse=True)
def settings_and_provider(monkeypatch):
    monkeypatch.setattr(settings, "source_support_shadow_enabled", False)
    monkeypatch.setattr(settings, "decision_openrouter_api_key", SecretStr("test-only-key"))
    monkeypatch.setattr(shadow, "OpenRouterDecisionProvider", Provider)
    monkeypatch.setattr(Provider, "instances", [])
    monkeypatch.setattr(Provider, "behavior", None)


@pytest.fixture
async def enrolled(cohort_sources, monkeypatch, content_store, runtime):
    install_proposal(monkeypatch, cohort_sources)
    resolver = AsyncMock(return_value=SourceReadAuthority("owner"))
    memory, _ = await ordinary_cohort.propose_stored_cohort(
        "org", "owner", [s.id for s in cohort_sources], resolver, authorize=AsyncMock()
    )
    assert memory is not None
    original = await reflection_validation.prepare_stored_reflection(
        "org", "owner", memory.id, resolver
    )
    critic = Extractor(
        CriticOutput,
        agent=Agent(TestModel(custom_output_args={"findings": []}), output_type=CriticOutput),
    )
    monkeypatch.setattr(
        procedure_validation,
        "validation_extractor",
        AsyncMock(return_value=(critic, '{"model":"offline"}')),
    )
    policy = await configure_policy(
        organization_id="org",
        principal_id="owner",
        project_id=None,
        expected_epoch=0,
        enabled=True,
        policy_version="source-support-shadow-v1",
        route_policy_sha256=OpenRouterDecisionRoute().policy_sha256,
    )
    return original, resolver, policy


async def receipt_rows(content_store):
    return await content_store.execute_query("SELECT * FROM semantic_decision_receipts;")


async def test_disabled_shadow_has_no_storage_or_provider_work(monkeypatch):
    load = AsyncMock(side_effect=AssertionError("disabled shadow queried storage"))
    monkeypatch.setattr(shadow, "load_policy", load)
    await shadow.observe_reflection_source_support(SimpleNamespace(), AsyncMock())
    assert not Provider.instances
    load.assert_not_awaited()


@pytest.mark.parametrize("skip", ["unenrolled", "credential", "route"])
async def test_shadow_requires_explicit_egress_controls(monkeypatch, skip):
    monkeypatch.setattr(settings, "source_support_shadow_enabled", True)
    route = OpenRouterDecisionRoute()
    policy = SimpleNamespace(enabled=True, route_policy_sha256=route.policy_sha256)
    if skip == "unenrolled":
        policy = None
    elif skip == "credential":
        monkeypatch.setattr(settings, "decision_openrouter_api_key", SecretStr(""))
    else:
        policy.route_policy_sha256 = "f" * 64
    monkeypatch.setattr(shadow, "load_policy", AsyncMock(return_value=policy))
    original = SimpleNamespace(
        memory=SimpleNamespace(organization_id="org", principal_id="owner", project_id=None)
    )
    await shadow.observe_reflection_source_support(original, AsyncMock())
    assert not Provider.instances


async def test_shadow_preserves_real_critic_and_source_bound_read(
    enrolled, monkeypatch, content_store
):
    original, resolver, _ = enrolled
    baseline = await reflection_validation.validate_reflection_stage(original, resolver)
    monkeypatch.setattr(settings, "source_support_shadow_enabled", True)
    actual = await reflection_validation.validate_reflection_stage(original, resolver)
    assert actual == baseline
    assert actual["status"] == "no_findings"
    assert len(Provider.instances) == 1 and Provider.instances[0].closed
    receipts = await receipt_rows(content_store)
    assert len(receipts) == 1
    receipt = receipts[0]
    observation = await shadow.load_reflection_source_support(original, resolver, receipt["uuid"])
    assert observation is not None
    assert {answer.value for answer in observation.answers} == {"contradicted"}
    assert len(observation.answers) == len(Provider.instances[0].requests[0].questions)
    assert original.memory.raw_content not in json.dumps(receipt, default=str)
    # A fresh source view cannot be used to read an old receipt under a new claim.
    changed = replace(original, authority=SourceReadAuthority("owner", projects=frozenset({"x"})))
    with pytest.raises(ValueError):
        await shadow.load_reflection_source_support(changed, resolver, receipt["uuid"])


@pytest.mark.parametrize("failure", ["provider", "retired", "revoked", "deleted"])
async def test_failed_shadow_keeps_baseline_and_sanitizes_receipts(
    enrolled, monkeypatch, content_store, caplog, failure
):
    original, resolver, policy = enrolled
    baseline = await reflection_validation.validate_reflection_stage(original, resolver)

    async def interfere(request):
        if failure == "provider":
            raise RuntimeError("private-body-and-secret-must-not-be-logged")
        if failure == "retired":
            await configure_policy(
                organization_id="org",
                principal_id="owner",
                project_id=None,
                expected_epoch=policy.epoch,
                enabled=False,
                policy_version=policy.policy_version,
                route_policy_sha256=policy.route_policy_sha256,
            )
        elif failure == "revoked":
            resolver.return_value = None
        else:
            await content_store.execute_query(
                "DELETE raw_captures WHERE uuid=$source AND organization_id='org';",
                source=original.sources[0].id,
            )

    monkeypatch.setattr(Provider, "behavior", staticmethod(interfere))
    # Finish the ordinary critic before mutating authority in the independent shadow.
    monkeypatch.setattr(
        reflection_validation, "_validate_prepared_reflection", AsyncMock(return_value=baseline)
    )
    monkeypatch.setattr(settings, "source_support_shadow_enabled", True)
    actual = await reflection_validation.validate_reflection_stage(original, resolver)
    assert actual == baseline
    assert Provider.instances[0].closed
    row = (await receipt_rows(content_store))[0]
    assert row.get("observation_json") is None
    assert "private-body-and-secret" not in caplog.text
    assert "private-body-and-secret" not in json.dumps(row, default=str)
    if failure in {"retired", "deleted", "revoked"}:
        assert json.loads(row["usage_json"])["input_tokens"] == 17


async def test_authority_change_before_dispatch_prevents_egress(
    enrolled, monkeypatch, content_store
):
    original, resolver, _ = enrolled
    resolver.return_value = None
    monkeypatch.setattr(settings, "source_support_shadow_enabled", True)
    await shadow.observe_reflection_source_support(original, resolver)
    assert not Provider.instances
    assert not await receipt_rows(content_store)


async def test_cancelled_shadow_retains_dispatch_intent_and_closes(
    enrolled, monkeypatch, content_store
):
    original, resolver, _ = enrolled
    dispatched = asyncio.Event()

    async def wait(request):
        dispatched.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(Provider, "behavior", staticmethod(wait))
    monkeypatch.setattr(settings, "source_support_shadow_enabled", True)
    task = asyncio.create_task(shadow.observe_reflection_source_support(original, resolver))
    await asyncio.wait_for(dispatched.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = (await receipt_rows(content_store))[0]
    assert row["state"] == "cancelled"
    assert row["attempt_count"] == 1
    assert row.get("observation_json") is None
    assert Provider.instances[0].closed


async def test_published_reflection_keeps_authorized_shadow_observation(
    enrolled, monkeypatch, content_store
):
    from sibyl_core.services.memory_reflection import promote_reflection_candidate_review
    from sibyl_core.services.ordinary_publication import ordinary_promotion_binding

    original, resolver, _ = enrolled
    monkeypatch.setattr(settings, "source_support_shadow_enabled", True)
    critic = await reflection_validation.validate_reflection_stage(original, resolver)
    receipt = (await receipt_rows(content_store))[0]
    binding = await ordinary_promotion_binding(
        "org", "owner", original.memory.id, critic["execution_id"], resolver, AsyncMock()
    )
    promoted = await promote_reflection_candidate_review(
        organization_id="org",
        principal_id="owner",
        candidate_id=original.memory.id,
        promote_to_scope="private",
        promote_to_scope_key="owner",
        validation_promotion=binding,
    )
    assert promoted.success
    current = await reflection_validation.prepare_stored_reflection(
        "org", "owner", original.memory.id, resolver, publication=True
    )
    observation = await shadow.load_reflection_source_support(current, resolver, receipt["uuid"])
    assert observation is not None
    assert observation.execution_status == "completed"


async def test_critic_failure_cancels_and_joins_shadow(monkeypatch):
    started = asyncio.Event()
    ended = asyncio.Event()

    async def running(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            ended.set()

    async def failing(*args, **kwargs):
        await started.wait()
        raise ValueError("critic failure")

    monkeypatch.setattr(shadow, "observe_reflection_source_support", running)
    monkeypatch.setattr(reflection_validation, "_validate_prepared_reflection", failing)
    monkeypatch.setattr(
        procedure_validation, "validation_extractor", AsyncMock(return_value=(object(), "{}"))
    )
    monkeypatch.setattr(settings, "source_support_shadow_enabled", True)
    with pytest.raises(ValueError, match="critic failure"):
        await reflection_validation.validate_reflection_stage(SimpleNamespace(), AsyncMock())
    assert ended.is_set()


async def test_shadow_does_not_write_critic_source_fences(enrolled, monkeypatch, content_store):
    original, resolver, _ = enrolled
    query = "SELECT source_id, validation_write_witness FROM source_states ORDER BY source_id;"
    before = await content_store.execute_query(query)
    monkeypatch.setattr(settings, "source_support_shadow_enabled", True)
    await shadow.observe_reflection_source_support(original, resolver)
    after = await content_store.execute_query(query)
    assert before == after
    assert len(Provider.instances) == 1 and Provider.instances[0].requests
    assert (await receipt_rows(content_store))[0]["state"] == "completed"


async def test_shadow_uses_real_transport_and_private_receipt(enrolled, monkeypatch, content_store):
    import httpx

    from sibyl_core.ai.openrouter_decisions import OpenRouterDecisionProvider

    original, resolver, _ = enrolled
    sent = []

    async def respond(request):
        sent.append(request)
        body = json.loads(request.content)
        assert str(request.url) == "https://openrouter.ai/api/alpha/decisions"
        assert body["provider"]["allow_fallbacks"] is False
        assert body["provider"]["zdr"] is True
        return httpx.Response(
            200,
            json={
                "id": "gen-dec-integration",
                "model": OpenRouterDecisionRoute().resolved_model_id,
                "provider": "TypeSafe",
                "answers": {
                    key: {"type": "choice", "choice": "supported"} for key in body["questions"]
                },
                "usage": {"input_tokens": 12, "output_tokens": 3, "cost": 0.001},
            },
        )

    monkeypatch.setattr(
        shadow,
        "OpenRouterDecisionProvider",
        lambda key, *, route: OpenRouterDecisionProvider(
            key, route=route, transport=httpx.MockTransport(respond)
        ),
    )
    monkeypatch.setattr(settings, "source_support_shadow_enabled", True)
    await shadow.observe_reflection_source_support(original, resolver)
    assert len(sent) == 1
    row = (await receipt_rows(content_store))[0]
    observed = await shadow.load_reflection_source_support(original, resolver, row["uuid"])
    assert observed is not None
    assert observed.provider_request_id == "gen-dec-integration"
    assert observed.observed_cost_usd == 0.001
