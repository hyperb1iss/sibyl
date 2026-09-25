"""Private decision receipts fence retirement and preserve only safe usage after purge."""

import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from sibyl_core.ai.decisions import ChoiceAnswer, DecisionObservation
from sibyl_core.backends.surreal.schema_decisions import DECISION_SOURCE_FENCE_BACKFILL
from sibyl_core.services import decision_receipts as d
from sibyl_core.services.surreal_content import remember_raw_memory
from tests.test_reflection_identity import content_store as content_store
from tests.test_source_support import case as case
from tests.test_source_support import request_for


@pytest.fixture
async def invocation(content_store, case):
    raw = await remember_raw_memory(
        raw_content="Evidence text",
        organization_id="org",
        principal_id="owner",
        source_id="raw",
        embedding_provider=None,
    )
    policy = await d.configure_policy(
        organization_id="org",
        principal_id="owner",
        project_id=None,
        expected_epoch=0,
        enabled=True,
        policy_version="shadow-v1",
        route_policy_sha256="e" * 64,
    )
    request = request_for(*case, policy_epoch=policy.epoch)
    request = request.model_copy(
        update={
            "source_refs": tuple(
                replace(obs, source=replace(obs.source, id=raw.id)) for obs in request.source_refs
            )
        }
    )
    receipt = d.DecisionReceipt(
        "org", "owner", authorize=AsyncMock(), dispatch_guard="LET $ok=true;"
    )
    await receipt.begin(request, parent_id=raw.id, source_ids=[raw.id], policy=policy)
    return receipt, request, policy


def observation(request):
    return DecisionObservation(
        semantic_input_sha256=request.semantic_input_sha256,
        request_digest=request.request_digest,
        execution_status="completed",
        resolved_model_id="fixture/model",
        answers=tuple(
            ChoiceAnswer(question_id=q.question_id, value="supported") for q in request.questions
        ),
        attempt_count=1,
        elapsed_ms=10.0,
        input_tokens=23,
        output_tokens=4,
        usage_status="observed",
    )


async def row(receipt):
    return (
        await d._query(
            "SELECT * FROM semantic_decision_receipts WHERE uuid=$uuid;", uuid=receipt.id
        )
    )[0]


async def test_dispatch_and_exact_authorized_read(invocation):
    receipt, request, _ = invocation
    await receipt.before_dispatch()
    assert await receipt.finish(observation(request))
    stored = await receipt.load(request)
    assert stored["state"] == "completed"
    assert stored["attempt_count"] == 1
    assert "state" not in json.loads(stored["observation_json"])
    assert "request_json" not in stored
    reader = d.DecisionReceipt(
        "org", "owner", authorize=AsyncMock(), dispatch_guard="LET $ok=true;", receipt_id=receipt.id
    )
    assert await reader.load(request) == stored
    assert await reader.load(request.model_copy(update={"state": "different"})) is None
    intruder = d.DecisionReceipt(
        "org", "other", authorize=AsyncMock(), dispatch_guard="LET $ok=true;", receipt_id=receipt.id
    )
    assert await intruder.load(request) is None
    with pytest.raises(Exception, match="dispatch unavailable"):
        await receipt.before_dispatch()


async def retire(policy):
    return await d.configure_policy(
        organization_id="org",
        principal_id="owner",
        project_id=None,
        expected_epoch=policy.epoch,
        enabled=False,
        policy_version=policy.policy_version,
        route_policy_sha256=policy.route_policy_sha256,
    )


async def test_retirement_fences_dispatch_and_late_completion(invocation):
    receipt, request, policy = invocation
    await receipt.before_dispatch()
    disabled = await retire(policy)
    assert disabled.epoch == 2
    assert not await receipt.finish(observation(request))
    stored = await row(receipt)
    assert stored["state"] == "stale"
    assert stored.get("observation_json") is None
    assert json.loads(stored["usage_json"])["input_tokens"] == 23
    with pytest.raises(Exception, match="policy unavailable"):
        await receipt.load(request)
    with pytest.raises(Exception, match="epoch changed"):
        await retire(policy)
    await d.configure_policy(
        organization_id="org",
        principal_id="owner",
        project_id=None,
        expected_epoch=disabled.epoch,
        enabled=True,
        policy_version="shadow-v1",
        route_policy_sha256="e" * 64,
    )
    with pytest.raises(Exception, match="policy unavailable"):
        await receipt.before_dispatch()


@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize("soft", [False, True])
async def test_purge_before_or_after_completion_cannot_resurrect(invocation, completed, soft):
    receipt, request, _ = invocation
    await receipt.before_dispatch()
    if completed:
        assert await receipt.finish(observation(request))
    await d._query(
        "UPDATE raw_captures SET deleted_at=time::now() WHERE uuid=$source;"
        if soft
        else "DELETE raw_captures WHERE uuid=$source;",
        source=request.source_refs[0].source.id,
    )
    assert not await receipt.finish(observation(request))
    stored = await row(receipt)
    assert stored["purged"] is True
    assert stored["state"] == "stale"
    assert stored.get("observation_json") is None
    assert json.loads(stored["usage_json"])["input_tokens"] == 23
    assert await receipt.load(request) is None


async def test_cancel_does_not_invent_usage_or_hide_dispatch_intent(invocation):
    receipt, _, _ = invocation
    await receipt.before_dispatch()
    await receipt.cancel()
    stored = await row(receipt)
    assert stored["state"] == "cancelled"
    assert stored["attempt_count"] == 1
    assert stored.get("usage_json") is None
    assert stored.get("observation_json") is None


async def test_source_owner_guard_fences_result_without_losing_usage(invocation):
    receipt, request, _ = invocation
    await receipt.before_dispatch()
    receipt.dispatch_guard = "THROW 'Source changed';"
    assert not await receipt.finish(observation(request))
    stored = await row(receipt)
    assert stored["state"] == "stale"
    assert stored.get("observation_json") is None
    assert json.loads(stored["usage_json"])["output_tokens"] == 4


async def test_deleted_source_cannot_create_a_new_invocation(invocation):
    _receipt, request, policy = invocation
    await d._query(
        "DELETE raw_captures WHERE uuid=$source;", source=request.source_refs[0].source.id
    )
    fresh = d.DecisionReceipt("org", "owner", authorize=AsyncMock(), dispatch_guard="LET $ok=true;")
    with pytest.raises(Exception, match="source unavailable"):
        await fresh.begin(
            request.model_copy(update={"request_id": "another-invocation"}),
            parent_id=request.source_refs[0].source.id,
            source_ids=[request.source_refs[0].source.id],
            policy=policy,
        )
    assert len(await d._query("SELECT * FROM semantic_decision_receipts;")) == 1


async def test_stale_policy_cannot_begin_and_disabled_policy_cannot_dispatch(invocation):
    receipt, request, policy = invocation
    await retire(policy)
    with pytest.raises(Exception, match="policy unavailable"):
        await receipt.before_dispatch()
    assert (await row(receipt))["attempt_count"] == 0
    fresh = d.DecisionReceipt("org", "owner", authorize=AsyncMock(), dispatch_guard="LET $ok=true;")
    with pytest.raises(Exception, match="policy unavailable"):
        await fresh.begin(
            request.model_copy(update={"request_id": "another-invocation"}),
            parent_id=request.source_refs[0].source.id,
            source_ids=[request.source_refs[0].source.id],
            policy=policy,
        )


async def test_private_tables_have_no_client_permissions(invocation):
    for table in (
        "semantic_decision_policies",
        "semantic_decision_receipts",
        "semantic_decision_source_fences",
    ):
        info = await d._query(f"INFO FOR TABLE {table};")
        assert info
    database = (await d._query("INFO FOR DB;"))[0]
    for table in (
        "semantic_decision_policies",
        "semantic_decision_receipts",
        "semantic_decision_source_fences",
    ):
        assert "PERMISSIONS NONE" in database["tables"][table]


async def source_fences():
    return await d._query(
        "SELECT *, type::string(id) AS fence_id FROM semantic_decision_source_fences;"
    )


async def test_fence_exists_before_first_invocation_and_survives_recreation(content_store):
    raw = await remember_raw_memory(
        raw_content="An unreviewed source",
        organization_id="org",
        principal_id="owner",
        source_id="capture",
        embedding_provider=None,
    )
    original = (await source_fences())[0]
    assert original["organization_id"] == "org"
    assert original["source_id"] == raw.id
    assert await d._query("SELECT * FROM semantic_decision_receipts;") == []
    stored = (await d._query("SELECT * FROM raw_captures WHERE uuid=$source;", source=raw.id))[0]
    stored.pop("id", None)
    await d._query("DELETE raw_captures WHERE uuid=$source;", source=raw.id)
    deleted = (await source_fences())[0]
    assert deleted["fence_id"] == original["fence_id"]
    assert deleted["witness"] != original["witness"]
    await d._query("CREATE raw_captures CONTENT $row;", row=stored)
    recreated = (await source_fences())[0]
    assert recreated["fence_id"] == original["fence_id"]
    assert recreated["witness"] != deleted["witness"]
    assert len(await source_fences()) == 1


async def test_existing_raw_sources_are_backfilled_before_receipts(invocation):
    receipt, request, policy = invocation
    await d._query("DELETE semantic_decision_source_fences;")
    fresh = d.DecisionReceipt("org", "owner", authorize=AsyncMock(), dispatch_guard="LET $ok=true;")
    next_request = request.model_copy(update={"request_id": "backfilled-invocation"})
    with pytest.raises(Exception, match="source fence unavailable"):
        await fresh.begin(
            next_request,
            parent_id=request.source_refs[0].source.id,
            source_ids=[request.source_refs[0].source.id],
            policy=policy,
        )
    await d._query(DECISION_SOURCE_FENCE_BACKFILL)
    await fresh.begin(
        next_request,
        parent_id=request.source_refs[0].source.id,
        source_ids=[request.source_refs[0].source.id],
        policy=policy,
    )
    assert len(await source_fences()) == 1
    assert len(await d._query("SELECT * FROM semantic_decision_receipts;")) == 2
    assert (await row(receipt))["attempt_count"] == 0


async def test_dispatch_completion_and_read_do_not_write_shared_fences(invocation):
    receipt, request, _ = invocation
    fences = await source_fences()
    policies = await d._query("SELECT * FROM semantic_decision_policies;")
    source_states = await d._query("SELECT * FROM source_states;")
    await receipt.before_dispatch()
    assert await receipt.finish(observation(request))
    assert (await receipt.load(request))["state"] == "completed"
    assert await source_fences() == fences
    assert await d._query("SELECT * FROM semantic_decision_policies;") == policies
    assert await d._query("SELECT * FROM source_states;") == source_states
