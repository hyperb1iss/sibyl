"""Default free-form captures flow through the scheduled proposal and publisher."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from sibyl.api.routes import memory_raw
from sibyl.api.schemas import RawMemoryRememberRequest
from sibyl.jobs import ordinary_cohorts, reflection
from sibyl_core.ai.llm.extractor import Extractor
from sibyl_core.auth import OrganizationRole
from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.services import procedure_validation
from sibyl_core.services.graph_client import SurrealGraphClient, prepare_graph_schema
from sibyl_core.services.graph_entities import EntityManager
from sibyl_core.services.graph_relationships import RelationshipManager
from sibyl_core.services.graph_runtime import GraphRuntime
from sibyl_core.tasks.memory_validation import CriticOutput


@pytest.fixture
async def cohort_runtime(monkeypatch):
    from sibyl.jobs.lifecycle_repair import resolve_source_authority
    from sibyl.persistence.surreal.auth import (
        SurrealOrganizationMembershipRepository,
        SurrealOrganizationRepository,
        SurrealUserRepository,
    )
    from sibyl_core.backends.surreal import SurrealAuthClient, bootstrap_auth_schema

    auth = SurrealAuthClient(url="memory://")
    await bootstrap_auth_schema(auth, reset=True)
    user = await SurrealUserRepository.from_client(auth).create_local_user(
        email="cohort@example.test", password="fixture-password-123", name="Fixture"
    )
    org = await SurrealOrganizationRepository.from_client(auth).create(name="Cohort fixture")
    await SurrealOrganizationMembershipRepository.from_client(auth).add_member(
        organization_id=org.id, user_id=user.id, role=OrganizationRole.OWNER
    )

    @asynccontextmanager
    async def auth_session():
        yield auth

    monkeypatch.setattr(
        "sibyl.persistence.surreal.auth_runtime._common.surreal_auth_client_scope", auth_session
    )
    context = await ordinary_cohorts.resolve_auth_context(
        claims={"sub": str(user.id), "org": str(org.id)}
    )
    client = SurrealContentClient(url="memory://")
    graph = SurrealGraphClient(group_id=str(org.id), url="memory://")
    await bootstrap_content_schema(client, reset=True)
    await prepare_graph_schema(graph)
    runtime = GraphRuntime(
        client=graph,
        entity_manager=EntityManager(graph, group_id=str(org.id)),
        relationship_manager=RelationshipManager(graph, group_id=str(org.id)),
    )

    @asynccontextmanager
    async def session():
        yield client

    monkeypatch.setattr("sibyl_core.services.content_client.surreal_content_client", session)
    monkeypatch.setattr(
        "sibyl_core.services.content_models.configured_raw_memory_embedding_provider", lambda: None
    )
    monkeypatch.setattr(
        "sibyl_core.services.memory_reflection.get_surreal_graph_runtime",
        AsyncMock(return_value=runtime),
    )
    monkeypatch.setattr(
        "sibyl_core.services.graph_runtime.get_surreal_graph_runtime",
        AsyncMock(return_value=runtime),
    )
    monkeypatch.setattr(
        "sibyl_core.services.graph_derivations.get_source_authority_resolver",
        lambda: resolve_source_authority,
    )
    monkeypatch.setattr("sibyl.api.routes.memory_auth.log_memory_audit_event", AsyncMock())
    monkeypatch.setattr(reflection, "log_memory_audit_event", AsyncMock())
    monkeypatch.setattr(memory_raw, "publish_raw_capture_changed", AsyncMock())
    try:
        yield org, context, client, runtime
    finally:
        await graph.close()
        await client.close()
        await auth.close()


async def capture(cohort_runtime):
    org, context, client, _runtime = cohort_runtime
    for body in [
        "Inspect logs before changing configuration.",
        "The logs show which configuration value was loaded.",
    ]:
        await memory_raw.remember_raw(
            RawMemoryRememberRequest(raw_content=body),
            http_request=SimpleNamespace(headers={}, client=None),
            org=org,
            ctx=context,
        )
    return await client.execute_query("SELECT * FROM raw_captures ORDER BY uuid;")


def install_model(monkeypatch, source):
    output = {
        "procedure": {
            "kind": "pattern",
            "goal": {
                "statement": "Logs reveal configuration choices",
                "label": "inferred",
                "support": [
                    {
                        "episode_id": source["uuid"],
                        "start_byte": 0,
                        "end_byte": len(source["raw_content"].encode()),
                    }
                ],
            },
        },
        "abstention_reason": None,
    }
    calls = []

    async def factory():
        calls.append(True)
        value = output if len(calls) <= 2 else {"findings": []}
        return Extractor(
            CriticOutput, agent=Agent(TestModel(custom_output_args=value), output_type=CriticOutput)
        ), '{"model":"offline"}'

    monkeypatch.setattr(procedure_validation, "validation_extractor", factory)
    return calls


async def test_ordinary_cohort_public_capture_scheduled_publish_recall(cohort_runtime, monkeypatch):
    org, context, client, _runtime = cohort_runtime
    sources = await capture(cohort_runtime)
    calls = install_model(monkeypatch, sources[0])
    receipt = await reflection.run_reflection_dream_cycle({}, str(org.id))
    assert receipt["failed"] == 0, receipt
    assert receipt["promoted"] == 1, receipt
    assert len(calls) == 3
    stages = await client.execute_query("SELECT * FROM memory_validation_executions;")
    assert len(stages) == 2
    assert all(s["state"] == "returned" for s in stages)
    assert sum(json.loads(s["usage_json"])["requests"] for s in stages) == 2
    assert not await client.execute_query("SELECT * FROM dream_source_checkpoints;")
    from sibyl_core.services.content_raw_recall import recall_raw_memory

    recalled = await recall_raw_memory(
        organization_id=str(org.id), principal_id=context.user_id, query="Logs reveal configuration"
    )
    candidate = next(m for m in recalled if m.capture_surface == "reflection_candidate")
    assert candidate.metadata["confidence"] == 0
    assert candidate.review_state == "promoted"
    from sibyl_core.services.validation_promotion import validated_graph_current

    assert await validated_graph_current(str(org.id), candidate.metadata["promoted_entity_id"])
    # Identical cohort work may be selected again, but its durable proposal is reused.
    before = await client.execute_query(
        "SELECT uuid, result_json FROM memory_validation_executions ORDER BY uuid;"
    )
    monkeypatch.setattr(
        Extractor, "extract_with_usage", AsyncMock(side_effect=AssertionError("redispatch"))
    )
    await reflection.run_reflection_dream_cycle({}, str(org.id))
    after = await client.execute_query(
        "SELECT uuid, result_json FROM memory_validation_executions ORDER BY uuid;"
    )
    assert before == after


@pytest.mark.parametrize("phase", ["proposal", "critic", "publication"])
async def test_ordinary_cohort_current_role_fences_each_stage(cohort_runtime, monkeypatch, phase):

    from sibyl_core.services import memory_reflection

    org, context, client, _runtime = cohort_runtime
    sources = await capture(cohort_runtime)
    install_model(monkeypatch, sources[0])
    calls = 0
    extract = Extractor.extract_with_usage

    async def revoke():
        from sibyl.persistence.surreal.auth_runtime._common import _auth_client_scope

        async with _auth_client_scope() as auth:
            rows = await auth.execute_query(
                "UPDATE organization_members SET role='viewer' WHERE organization_id=$org AND user_id=$user RETURN AFTER;",
                org=str(org.id),
                user=context.user_id,
            )
        assert len(rows) == 1

    async def revoke_after_extract(self, prompt):
        nonlocal calls
        value = await extract(self, prompt)
        calls += 1
        if (phase == "proposal" and calls == 1) or (phase == "critic" and calls == 2):
            await revoke()
        return value

    monkeypatch.setattr(Extractor, "extract_with_usage", revoke_after_extract)
    if phase == "publication":
        reserve = memory_reflection._reserve_promotion

        async def revoke_before_reserve(*args, **kwargs):
            await revoke()
            return await reserve(*args, **kwargs)

        monkeypatch.setattr(memory_reflection, "_reserve_promotion", revoke_before_reserve)
    receipt = await reflection.run_reflection_dream_cycle({}, str(org.id))
    assert receipt["promoted"] == 0
    assert receipt["failed"] == 1, receipt
    stages = await client.execute_query("SELECT * FROM memory_validation_executions;")
    assert sum(json.loads(s["usage_json"])["requests"] for s in stages) == calls
    assert calls == (1 if phase == "proposal" else 2)
    assert not await client.execute_query(
        "SELECT * FROM raw_captures WHERE review_state='promoted';"
    )


async def test_ordinary_cohort_does_not_hide_source_sensitivity(cohort_runtime, monkeypatch):
    org, _context, client, _runtime = cohort_runtime
    sources = await capture(cohort_runtime)
    await client.execute_query(
        "UPDATE raw_captures SET metadata.contains_sensitive=true WHERE uuid=$id;",
        id=sources[0]["uuid"],
    )
    install_model(monkeypatch, sources[0])
    receipt = await reflection.run_reflection_dream_cycle({}, str(org.id))
    assert receipt["failed"] == 0, receipt
    assert receipt["promoted"] == 0
    assert receipt["archived"] == 1
    assert "sensitive_candidate" in receipt["candidates"][0]["exception_reasons"]


async def test_ordinary_cohort_correction_recheck_and_publication(cohort_runtime, monkeypatch):
    from sibyl_core.services.reflection_validation import prepare_stored_reflection
    from sibyl_core.tasks.ordinary_proposals import QUALIFICATION
    from sibyl_core.tasks.procedure_review import ReviewSubmission, review_digest

    org, context, client, _runtime = cohort_runtime
    sources = await capture(cohort_runtime)
    install_model(monkeypatch, sources[0])
    proposal_factory = procedure_validation.validation_extractor
    count = 0
    submission = None
    corrected_text = None

    async def factory():
        nonlocal count, submission, corrected_text
        count += 1
        if count <= 2:
            return await proposal_factory()
        if count == 3:
            rows = await client.execute_query(
                "SELECT uuid FROM raw_captures WHERE capture_surface='reflection_candidate';"
            )
            prepared = await prepare_stored_reflection(
                str(org.id),
                context.user_id,
                rows[0]["uuid"],
                ordinary_cohorts.writable_source_authority,
            )
            payload = json.loads(prepared.prepared.payload_json)
            claim = payload["assertions"]["/content"]
            submission = ReviewSubmission(
                parent_operation_id=payload["parent_operation_id"],
                parent_candidate_sha256=payload["parent_candidate_sha256"],
                findings=[
                    {
                        "claim_path": "/content",
                        "claim_sha256": review_digest(claim),
                        "evidence_refs": [{"evidence_id": "s0"}],
                        "basis": "factual_contradiction",
                        "disposition": "reconsider",
                        "critique": "Keep the observation specific to the recorded configuration.",
                    }
                ],
            )
            corrected_text = (
                QUALIFICATION
                + "\nThe recorded logs may identify the loaded configuration; check the actual log contents."
            )
            output = {"findings": [f.model_dump(mode="json") for f in submission.findings]}
        elif count == 4:
            output = {
                "content": corrected_text,
                "abstention_reason": None,
                "assessments": [
                    {
                        "finding_id": submission.finding_ids()[0],
                        "disposition": "accepted",
                        "explanation": "Keep the statement conditional and source-specific.",
                        "evidence_refs": [{"evidence_id": "s0"}],
                    }
                ],
            }
        else:
            assert count == 5
            output = {"findings": []}
        return Extractor(
            CriticOutput,
            agent=Agent(TestModel(custom_output_args=output), output_type=CriticOutput),
        ), '{"model":"offline"}'

    monkeypatch.setattr(procedure_validation, "validation_extractor", factory)
    receipt = await reflection.run_reflection_dream_cycle({}, str(org.id))
    assert receipt["failed"] == 0, receipt
    assert receipt["promoted"] == 1, receipt
    assert count == 5
    stages = await client.execute_query("SELECT * FROM memory_validation_executions;")
    assert len(stages) == 4
    assert sum(json.loads(s["usage_json"])["requests"] for s in stages) == 4
    rows = await client.execute_query(
        "SELECT * FROM raw_captures WHERE capture_surface='reflection_candidate';"
    )
    assert {r["review_state"] for r in rows} == {"promoted", "archived"}
    child = next(r for r in rows if r["review_state"] == "promoted")
    assert child["raw_content"] == corrected_text
    assert QUALIFICATION in child["raw_content"]
    from sibyl_core.services.validation_promotion import validated_graph_current

    assert await validated_graph_current(str(org.id), child["metadata"]["promoted_entity_id"])


async def test_ordinary_cohort_preparation_denial_never_falls_back(cohort_runtime, monkeypatch):
    org, context, client, _runtime = cohort_runtime
    await capture(cohort_runtime)
    from sibyl.persistence.surreal.auth_runtime._common import _auth_client_scope

    async with _auth_client_scope() as auth:
        await auth.execute_query(
            "UPDATE organization_members SET role='viewer' WHERE organization_id=$org AND user_id=$user;",
            org=str(org.id),
            user=context.user_id,
        )
    factory = AsyncMock(side_effect=AssertionError("provider preparation after denied authority"))
    monkeypatch.setattr(procedure_validation, "validation_extractor", factory)
    receipt = await reflection.run_reflection_dream_cycle({}, str(org.id))
    assert receipt["failed"] == 1
    assert receipt["sources_reflected"] == 0
    factory.assert_not_awaited()
    assert not await client.execute_query("SELECT * FROM dream_source_checkpoints;")
    assert not await client.execute_query("SELECT * FROM memory_validation_executions;")
    assert not await client.execute_query(
        "SELECT * FROM raw_captures WHERE capture_surface='reflection_candidate';"
    )


@pytest.mark.parametrize(
    "change",
    [
        "source_sensitive",
        "source_sensitive_string",
        "source_sensitive_tag",
        "candidate_conflict",
        "candidate_scope",
    ],
)
async def test_ordinary_cohort_late_publication_policy_interpretation(
    cohort_runtime, monkeypatch, change
):
    from sibyl_core.services import memory_reflection

    org, _context, client, _runtime = cohort_runtime
    sources = await capture(cohort_runtime)
    install_model(monkeypatch, sources[0])
    reserve = memory_reflection._reserve_promotion

    async def changed(plan, *args, **kwargs):
        if change in {"source_sensitive", "source_sensitive_string"}:
            await client.execute_query(
                "UPDATE raw_captures SET metadata.contains_sensitive=$value WHERE uuid=$id;",
                id=sources[0]["uuid"],
                value="true" if change == "source_sensitive_string" else True,
            )
        elif change == "source_sensitive_tag":
            await client.execute_query(
                "UPDATE raw_captures SET tags=['secret'] WHERE uuid=$id;", id=sources[0]["uuid"]
            )
        elif change == "candidate_conflict":
            await client.execute_query(
                "UPDATE raw_captures SET metadata.conflicts_with_source_ids=['conflict'] WHERE uuid=$id;",
                id=plan.candidate_memory.id,
            )
        else:
            await client.execute_query(
                "UPDATE raw_captures SET metadata.suggested_memory_scope='organization' WHERE uuid=$id;",
                id=plan.candidate_memory.id,
            )
        return await reserve(plan, *args, **kwargs)

    monkeypatch.setattr(memory_reflection, "_reserve_promotion", changed)
    receipt = await reflection.run_reflection_dream_cycle({}, str(org.id))
    # The existing interpreter accepts literal booleans, not string flags.
    denied = change != "source_sensitive_string"
    assert receipt["promoted"] == (0 if denied else 1), receipt
    assert receipt["failed"] == (1 if denied else 0), receipt
    promoted = await client.execute_query(
        "SELECT * FROM raw_captures WHERE review_state='promoted';"
    )
    assert len(promoted) == (0 if denied else 1)
    stages = await client.execute_query("SELECT usage_json FROM memory_validation_executions;")
    assert sum(json.loads(stage["usage_json"])["requests"] for stage in stages) == 2


@pytest.mark.parametrize("raced", [False, True])
async def test_ordinary_cohort_native_final_policy_race(cohort_runtime, monkeypatch, raced):
    import asyncio
    import os

    if not os.environ.get("SIBYL_COHORT_NATIVE_URL"):
        pytest.skip("native multi-connection publication policy race")
    org, _context, client, _runtime = cohort_runtime
    sources = await capture(cohort_runtime)
    install_model(monkeypatch, sources[0])
    execute = client.execute_query
    execute_raw = client.execute_query_raw
    before = await execute(
        "SELECT generation, revision, incarnation, deleted FROM source_states WHERE source_id=$id;",
        id=sources[0]["uuid"],
    )
    assert len(before) == 1
    delayed_once = False

    async def delayed(query, **kwargs):
        nonlocal delayed_once
        if (
            delayed_once
            or not kwargs.get("validation_binding")
            or kwargs.get("record", {}).get("review_state") != "promoted"
        ):
            return await execute_raw(query, **kwargs)
        assert "IF $snapshot_digest!=$ordinary_snapshot" in query
        delayed_once = True
        pending = asyncio.create_task(
            execute_raw(
                query.replace(
                    "IF $snapshot_digest!=$ordinary_snapshot",
                    "SLEEP 1s; IF $snapshot_digest!=$ordinary_snapshot",
                    1,
                ),
                **kwargs,
            )
        )
        try:
            if raced:
                await asyncio.sleep(0.25)
                changed = await execute(
                    "UPDATE raw_captures SET metadata.contains_sensitive=true WHERE uuid=$id RETURN AFTER;",
                    id=sources[0]["uuid"],
                )
                assert len(changed) == 1
            return await pending
        finally:
            if not pending.done():
                await pending

    monkeypatch.setattr(client, "execute_query_raw", delayed)
    receipt = await reflection.run_reflection_dream_cycle({}, str(org.id))
    assert delayed_once
    assert receipt["promoted"] == (0 if raced else 1), receipt
    if raced:
        assert not await execute("SELECT * FROM raw_captures WHERE review_state='promoted';")
        row = await execute(
            "SELECT metadata FROM raw_captures WHERE uuid=$id;", id=sources[0]["uuid"]
        )
        assert row[0]["metadata"]["contains_sensitive"] is True

    after = await execute(
        "SELECT generation, revision, incarnation, deleted FROM source_states WHERE source_id=$id;",
        id=sources[0]["uuid"],
    )
    assert after == before
    stages = await execute("SELECT usage_json FROM memory_validation_executions;")
    assert sum(json.loads(stage["usage_json"])["requests"] for stage in stages) == 2
