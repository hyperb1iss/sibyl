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
        ), '{"max_input_chars":40000,"model":"offline"}'

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
    # A completed cohort's sources are not selected again, so nothing redispatches.
    before = await client.execute_query(
        "SELECT uuid, result_json FROM memory_validation_executions ORDER BY uuid;"
    )
    monkeypatch.setattr(
        Extractor, "extract_with_usage", AsyncMock(side_effect=AssertionError("redispatch"))
    )
    again = await reflection.run_reflection_dream_cycle({}, str(org.id))
    assert again["sources_scanned"] == 0, again
    after = await client.execute_query(
        "SELECT uuid, result_json FROM memory_validation_executions ORDER BY uuid;"
    )
    assert before == after


async def test_an_abstaining_cohort_covers_its_sources_until_one_changes(
    cohort_runtime, monkeypatch
):
    from dataclasses import replace

    from sibyl_core.services.content_raw_persistence import get_raw_memory, save_raw_memory

    org, _context, client, _runtime = cohort_runtime
    sources = await capture(cohort_runtime)
    abstention = {"procedure": None, "abstention_reason": "One configuration change is no habit."}

    async def factory():
        return Extractor(
            CriticOutput,
            agent=Agent(TestModel(custom_output_args=abstention), output_type=CriticOutput),
        ), '{"max_input_chars":40000,"model":"offline"}'

    monkeypatch.setattr(procedure_validation, "validation_extractor", factory)
    first = await reflection.run_reflection_dream_cycle({}, str(org.id), candidate_limit=0)
    assert first["failed"] == 0, first
    assert first["sources"][0]["source_ids"] == sorted(s["uuid"] for s in sources)
    assert first["sources"][0]["candidate_count"] == 0
    stages = await client.execute_query("SELECT * FROM memory_validation_executions;")
    assert [stage["state"] for stage in stages] == ["returned"]

    second = await reflection.run_reflection_dream_cycle({}, str(org.id), candidate_limit=0)
    assert second["sources_scanned"] == 0, second

    # A new observation of one source is new evidence; its unchanged sibling
    # stays covered, so the changed source is reflected on its own.
    memory = await get_raw_memory(organization_id=str(org.id), memory_id=sources[0]["uuid"])
    assert memory is not None
    await save_raw_memory(
        replace(memory, raw_content=memory.raw_content + " The loaded value was stale."),
        expected_revision=memory.revision,
        embedding_provider=None,
    )
    third = await reflection.run_reflection_dream_cycle({}, str(org.id), candidate_limit=0)
    assert [item.get("source_id") for item in third["sources"]] == [memory.id], third


async def test_a_covered_source_is_ruled_out_before_authorization(cohort_runtime, monkeypatch):
    from sibyl_core.services.dream_checkpoints import current_source_observations
    from sibyl_core.services.ordinary_cohort import ReflectedSources

    org, context, client, _runtime = cohort_runtime
    for index in range(40):
        await memory_raw.remember_raw(
            RawMemoryRememberRequest(raw_content=f"Observation {index}: the config reloaded."),
            http_request=SimpleNamespace(headers={}, client=None),
            org=org,
            ctx=context,
        )
    ids = sorted(
        row["uuid"] for row in await client.execute_query("SELECT uuid FROM raw_captures;")
    )
    observed = await current_source_observations(str(org.id), ids)
    assert set(observed) == set(ids)
    fresh = set(ids[::10])
    covered = frozenset(
        (context.user_id, identifier, *observed[identifier])
        for identifier in ids
        if identifier not in fresh
    )
    monkeypatch.setattr(
        reflection,
        "reflected_sources",
        AsyncMock(return_value=ReflectedSources(covered, frozenset())),
    )
    authorized = []
    load = reflection._load_dream_work

    async def counted(group_id, source):
        authorized.append(source.id)
        return await load(group_id, source)

    monkeypatch.setattr(reflection, "_load_dream_work", counted)
    pages = []

    async def partition_free(org_id, sources, *, dry_run):
        pages.append(sorted(source.id for source in sources))
        return [], {source.id for source in sources}

    monkeypatch.setattr(ordinary_cohorts, "reflect_cohorts", partition_free)
    # The walk wraps the whole corpus to find four fresh sources, and only
    # those four are ever authorized.
    await reflection._reflect_dream_sources(
        group_id=str(org.id), run_id="covered", dry_run=True, limit=20
    )
    assert pages == [sorted(fresh)]
    assert sorted(authorized) == sorted(fresh)


async def _departed_member_sources(cohort_runtime):
    """Two private captures by a member who then leaves the organization."""
    from sibyl.persistence.surreal.auth import (
        SurrealOrganizationMembershipRepository,
        SurrealUserRepository,
    )
    from sibyl.persistence.surreal.auth_runtime._common import _auth_client_scope

    org, _context, client, _runtime = cohort_runtime
    async with _auth_client_scope() as auth:
        member = await SurrealUserRepository.from_client(auth).create_local_user(
            email="departed@example.test", password="fixture-password-123", name="Departed"
        )
        memberships = SurrealOrganizationMembershipRepository.from_client(auth)
        await memberships.add_member(
            organization_id=org.id, user_id=member.id, role=OrganizationRole.MEMBER
        )
    member_context = await ordinary_cohorts.resolve_auth_context(
        claims={"sub": str(member.id), "org": str(org.id)}
    )
    for body in [
        "Restart the worker after rotating the queue credentials.",
        "The worker kept the old credentials until it restarted.",
    ]:
        await memory_raw.remember_raw(
            RawMemoryRememberRequest(raw_content=body),
            http_request=SimpleNamespace(headers={}, client=None),
            org=org,
            ctx=member_context,
        )
    async with _auth_client_scope() as auth:
        await SurrealOrganizationMembershipRepository.from_client(auth).remove_member(
            organization_id=org.id, user_id=member.id
        )
    rows = await client.execute_query(
        "SELECT uuid FROM raw_captures WHERE principal_id = $member;", member=str(member.id)
    )
    return sorted(row["uuid"] for row in rows)


async def test_a_departed_members_sources_neither_seed_nor_fill_a_page(cohort_runtime, monkeypatch):
    org, _context, _client, _runtime = cohort_runtime
    departed = await _departed_member_sources(cohort_runtime)
    assert len(departed) == 2
    # Still readable by their owner's own principal: the read gate alone lets
    # them through, but no cohort of theirs can ever reach a provider.
    from sibyl_core.services.content_raw_persistence import get_raw_memory

    for identifier in departed:
        memory = await get_raw_memory(organization_id=str(org.id), memory_id=identifier)
        assert memory is not None
        assert await reflection._load_dream_work(str(org.id), memory) is not None
    fresh = [source for source in await capture(cohort_runtime) if source["uuid"] not in departed]
    assert len(fresh) == 2
    pages = []

    async def partition_free(org_id, sources, *, dry_run):
        pages.append(sorted(source.id for source in sources))
        return [], {source.id for source in sources}

    monkeypatch.setattr(ordinary_cohorts, "reflect_cohorts", partition_free)
    for _ in range(3):
        await reflection._reflect_dream_sources(
            group_id=str(org.id), run_id="departed", dry_run=True, limit=20
        )
    assert pages == [sorted(source["uuid"] for source in fresh)] * 3, departed


async def test_ordinary_cohort_candidate_write_failure_retains_execution_and_replays(
    cohort_runtime, monkeypatch
):
    from sibyl_core.services import ordinary_cohort

    org, _context, client, _runtime = cohort_runtime
    sources = await capture(cohort_runtime)
    install_model(monkeypatch, sources[0])
    remember = ordinary_cohort.remember_reflection_candidate_review
    monkeypatch.setattr(
        ordinary_cohort,
        "remember_reflection_candidate_review",
        AsyncMock(side_effect=ValueError("candidate write failed")),
    )

    receipt = await reflection.run_reflection_dream_cycle({}, str(org.id), candidate_limit=0)
    stages = await client.execute_query("SELECT * FROM memory_validation_executions;")
    assert len(stages) == 1
    stage = stages[0]
    assert stage["state"] == "returned"
    assert json.loads(stage["usage_json"])["requests"] == 1
    assert receipt["failed"] == 1
    assert receipt["promoted"] == 0
    assert receipt["sources"][0]["outcome"] == "error"
    assert receipt["sources"][0]["reason"] == "candidate write failed"
    assert receipt["sources"][0]["execution_state"] == "returned"
    assert receipt["sources"][0]["operation_id"] == stage["uuid"]
    assert receipt["model_usage"]["execution_ids"] == [stage["uuid"]]
    assert not await client.execute_query(
        "SELECT * FROM raw_captures WHERE capture_surface='reflection_candidate';"
    )

    monkeypatch.setattr(ordinary_cohort, "remember_reflection_candidate_review", remember)
    extraction = AsyncMock(side_effect=AssertionError("proposal redispatch"))
    monkeypatch.setattr(Extractor, "extract_with_usage", extraction)
    replay = await reflection.run_reflection_dream_cycle({}, str(org.id), candidate_limit=0)
    assert replay["failed"] == 0, replay
    assert replay["sources"][0]["candidate_count"] == 1
    assert replay["sources"][0]["operation_id"] == stage["uuid"]
    assert replay["model_usage"]["execution_ids"] == [stage["uuid"]]
    replayed_stages = await client.execute_query("SELECT * FROM memory_validation_executions;")
    assert len(replayed_stages) == 1
    for field in ("uuid", "state", "request_json", "result_json", "usage_json"):
        assert replayed_stages[0][field] == stage[field]
    candidates = await client.execute_query(
        "SELECT * FROM raw_captures WHERE capture_surface='reflection_candidate';"
    )
    assert len(candidates) == 1
    extraction.assert_not_awaited()


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
    from sibyl_core.tasks.memory_progress import ProgressCriticOutput
    from sibyl_core.tasks.ordinary_proposals import QUALIFICATION
    from sibyl_core.tasks.procedure_review import ReviewSubmission, review_digest

    org, context, client, _runtime = cohort_runtime
    sources = await capture(cohort_runtime)
    install_model(monkeypatch, sources[0])
    proposal_factory = procedure_validation.validation_extractor
    count = 0
    submission = None
    corrected_text = None

    async def factory(output_type=CriticOutput):
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
            assert output_type is ProgressCriticOutput
            output = {
                "findings": [],
                "prior_assessments": [
                    {
                        "finding_id": submission.finding_ids()[0],
                        "disposition": "resolved",
                        "supported_reduction": "The claim now requires checking the recorded logs.",
                        "remaining_concern": None,
                        "evidence_refs": [{"evidence_id": "s0"}],
                    }
                ],
            }
        return Extractor(
            output_type,
            agent=Agent(TestModel(custom_output_args=output), output_type=output_type),
        ), '{"max_input_chars":40000,"model":"offline"}'

    monkeypatch.setattr(procedure_validation, "validation_extractor", factory)
    monkeypatch.setattr(procedure_validation, "_validation_extractor", factory)
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
    assert {r["review_state"] for r in rows} == {"promoted", "pending"}
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
    # A viewer's sources fail the send gate, so selection refuses them before
    # any preparation rather than paging them into a cohort that must fail.
    assert receipt["failed"] == 0
    assert receipt["sources_scanned"] == 0
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
    assert receipt["failed"] == (1 if raced else 0), receipt
    assert receipt["candidates"][0]["applied"] is not raced
    assert receipt["candidates"][0]["outcome"] == ("error" if raced else "auto_promote")
    if raced:
        assert receipt["candidates"][0]["reason"] == "retired"
        assert receipt["candidates"][0]["promoted_id"] is None
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
    from sibyl_core.services.content_raw_recall import recall_raw_memory
    from sibyl_core.services.graph_read_availability import available_graph_entities

    candidates = await execute(
        "SELECT * FROM raw_captures WHERE capture_surface='reflection_candidate';"
    )
    assert len(candidates) == 1
    entity_id = candidates[0]["metadata"]["promoted_entity_id"]
    visible = await available_graph_entities(str(org.id), [entity_id], runtime=_runtime)
    assert bool(visible) is not raced
    recalled = await recall_raw_memory(
        organization_id=str(org.id),
        principal_id=_context.user_id,
        query="Logs reveal configuration",
    )
    assert (candidates[0]["uuid"] in {memory.id for memory in recalled}) is not raced
    stages = await execute("SELECT usage_json FROM memory_validation_executions;")
    assert sum(json.loads(stage["usage_json"])["requests"] for stage in stages) == 2


class _RecordingBudget:
    def __init__(self):
        self.reservations = []
        self.settlements = []

    async def reserve(self, context, *, surface, estimated_tokens):
        self.reservations.append((context, surface, estimated_tokens))

    async def settle(self, context, *, surface, reserved_tokens, actual_tokens, period=None):
        self.settlements.append((context, surface, reserved_tokens, actual_tokens))


async def test_dream_cohorts_reserve_against_the_owner_and_organization_budgets(
    cohort_runtime, monkeypatch
):
    from sibyl_core.ai.llm.budget import set_budget_enforcer

    org, _context, _client, _runtime = cohort_runtime
    sources = await capture(cohort_runtime)
    install_model(monkeypatch, sources[0])
    budget = _RecordingBudget()
    set_budget_enforcer(budget)
    try:
        receipt = await reflection.run_reflection_dream_cycle({}, str(org.id))
    finally:
        set_budget_enforcer(None)

    assert receipt["sources"][0]["outcome"] == "reflected"
    assert budget.reservations, "the proposal never reserved against a budget"
    owners = {context.user_id for context, _surface, _tokens in budget.reservations}
    organizations = {context.organization_id for context, _surface, _tokens in budget.reservations}
    assert owners == {str(sources[0]["principal_id"])}
    assert organizations == {str(org.id)}
    assert all(tokens > 0 for _context, _surface, tokens in budget.reservations)
    for context, _surface, reserved, actual in budget.settlements:
        assert (context.user_id, context.organization_id) == (
            str(sources[0]["principal_id"]),
            str(org.id),
        )
        assert reserved > 0
        assert actual >= 0
    spend = receipt["spend"]
    assert spend["cap_tokens"] == reflection.settings.consolidation_run_max_tokens
    assert spend["reserved_tokens"] == sum(t for _c, _s, t in budget.reservations)
    assert 0 <= spend["committed_tokens"] <= spend["reserved_tokens"] + spend["charged_tokens"]
    assert spend["stopped_reason"] is None
    assert receipt["stopped_reason"] is None
    assert receipt["budget_refused"] == 0


async def test_dream_run_stops_admitting_work_at_its_token_ceiling(cohort_runtime, monkeypatch):
    from sibyl_core.ai.llm.budget import RUN_TOKEN_CEILING, set_budget_enforcer

    org, _context, _client, _runtime = cohort_runtime
    sources = await capture(cohort_runtime)
    install_model(monkeypatch, sources[0])
    monkeypatch.setattr(reflection.settings, "consolidation_run_max_tokens", 1)
    budget = _RecordingBudget()
    set_budget_enforcer(budget)
    try:
        receipt = await reflection.run_reflection_dream_cycle({}, str(org.id), candidate_limit=0)
    finally:
        set_budget_enforcer(None)

    cohort = receipt["sources"][0]
    assert cohort["outcome"] == "error"
    assert cohort["reason"] == RUN_TOKEN_CEILING
    assert cohort["budget"]["kind"] == RUN_TOKEN_CEILING
    assert cohort["budget"]["run_cap_tokens"] == 1
    assert cohort["budget"]["committed_tokens"] == 0
    assert cohort["budget"]["requested_tokens"] > 1
    # The ceiling refused the call before the monthly buckets saw it.
    assert budget.reservations == []
    assert receipt["stopped_reason"] == RUN_TOKEN_CEILING
    assert receipt["spend"]["refusals"] == 1
    assert receipt["spend"]["exhausted"] is True
    assert receipt["spend"]["committed_tokens"] == 0
    assert receipt["budget_refused"] == 1
    assert receipt["failed"] == 1


async def test_a_run_ceiling_leaves_later_cohorts_pending_with_a_receipt(
    cohort_runtime, monkeypatch
):
    """Once the ledger is exhausted, a cohort is skipped with a reason and stays pending."""
    from sibyl.jobs import ordinary_cohorts
    from sibyl_core.ai.errors import LLMRunBudgetExceededError
    from sibyl_core.ai.llm.budget import RUN_TOKEN_CEILING, llm_run_spend_ledger
    from sibyl_core.services.content_raw_persistence import get_raw_memory

    org, _context, _client, _runtime = cohort_runtime
    sources = await capture(cohort_runtime)
    calls = install_model(monkeypatch, sources[0])
    loaded = [
        await get_raw_memory(organization_id=str(org.id), memory_id=row["uuid"]) for row in sources
    ]

    with llm_run_spend_ledger(1) as ledger:
        with pytest.raises(LLMRunBudgetExceededError):
            ledger.admit(surface="memory", estimated_tokens=2)
        results, consumed = await ordinary_cohorts.reflect_cohorts(
            str(org.id), loaded, dry_run=False
        )

    assert results == [
        {
            "source_ids": sorted(row["uuid"] for row in sources),
            "outcome": "skip",
            "reason": RUN_TOKEN_CEILING,
            "stage_kind": "ordinary_cohort",
        }
    ]
    assert consumed == {row["uuid"] for row in sources}
    assert calls == [], "a skipped cohort must not build an extractor"


@pytest.mark.parametrize("kind", ["ceiling", "monthly"])
@pytest.mark.parametrize(("stage", "at"), [("proposal", 1), ("critic", 2)])
async def test_a_budget_refusal_leaves_the_stage_free_to_run_next_time(
    cohort_runtime, monkeypatch, stage, at, kind
):
    """A refusal before dispatch releases its claim; the next run completes the stage.

    Recording the refusal as a failed stage would block it for good, because a
    failed execution is never claimed again and its sources stay uncovered.
    """
    from tests.budget_refusal import refuse_reservation

    org, _context, client, _runtime = cohort_runtime
    sources = await capture(cohort_runtime)
    calls = install_model(monkeypatch, sources[0])
    refuse_reservation(monkeypatch, at=at, kind=kind)

    first = await reflection.run_reflection_dream_cycle({}, str(org.id))
    rows = await client.execute_query("SELECT state, error_type FROM memory_validation_executions;")
    assert all(row["state"] != "failed" for row in rows), rows
    assert first["budget_refused"] == 1, first
    refused = first["sources"][0] if stage == "proposal" else first["candidates"][0]
    assert refused["outcome"] == "error"
    assert refused["budget"] is not None
    if stage == "proposal":
        assert rows == []
        assert first["sources_reflected"] == 0
    else:
        assert [row["state"] for row in rows] == ["returned"]
        assert first["promoted"] == 0
    assert first["stopped_reason"] == ("run_token_ceiling" if kind == "ceiling" else None)
    if stage == "proposal":
        # install_model hands out its proposal output by call count, and the
        # refused run already used those calls; the next run starts afresh.
        calls.clear()

    second = await reflection.run_reflection_dream_cycle({}, str(org.id))
    assert second["failed"] == 0, second
    assert second["promoted"] == 1, second
    rows = await client.execute_query("SELECT state FROM memory_validation_executions;")
    assert sorted(row["state"] for row in rows) == ["returned", "returned"]


async def test_individual_passes_run_after_the_ceiling_is_reached(cohort_runtime, monkeypatch):
    """Individual passes use the heuristic writer and cost no tokens, so they are never skipped."""
    from sibyl.jobs import ordinary_cohorts
    from sibyl_core.ai.errors import LLMRunBudgetExceededError
    from sibyl_core.ai.llm.budget import get_llm_run_spend_ledger

    org, _context, _client, _runtime = cohort_runtime
    await capture(cohort_runtime)

    async def exhaust(org_id, sources, *, dry_run):
        ledger = get_llm_run_spend_ledger()
        with pytest.raises(LLMRunBudgetExceededError):
            ledger.admit(surface="memory", estimated_tokens=ledger.cap_tokens + 1)
        return [], set()

    passes = []

    async def individual(**kwargs):
        passes.append(kwargs["source"].id)
        return {"source_id": kwargs["source"].id, "outcome": "reflected"}

    monkeypatch.setattr(ordinary_cohorts, "reflect_cohorts", exhaust)
    monkeypatch.setattr(reflection, "_reflect_dream_source", individual)

    receipt = await reflection.run_reflection_dream_cycle({}, str(org.id), candidate_limit=0)

    assert len(passes) == 2
    assert receipt["stopped_reason"] == "run_token_ceiling"
    assert receipt["sources_reflected"] == 2


def _packet_pages(monkeypatch, pages=2):
    from types import SimpleNamespace

    from sibyl.jobs import ordinary_cohorts

    manifest = {"source_id": "source", "pages": list(range(pages))}
    packets = [
        SimpleNamespace(binding={"manifest": manifest, "index": index}, sha256=str(index))
        for index in range(pages)
    ]
    monkeypatch.setattr(
        ordinary_cohorts, "prepare_stored_source_packets", AsyncMock(return_value=packets)
    )
    monkeypatch.setattr(ordinary_cohorts, "writable_source_authority", AsyncMock())


async def test_a_packet_page_refused_by_the_ceiling_lands_in_the_receipt(monkeypatch):
    from sibyl.jobs import ordinary_cohorts
    from sibyl_core.ai.errors import LLMRunBudgetExceededError

    _packet_pages(monkeypatch)
    refusal = LLMRunBudgetExceededError(
        "LLM run token ceiling reached",
        surface="memory",
        details={"kind": "run_token_ceiling", "run_cap_tokens": 1},
    )
    monkeypatch.setattr(ordinary_cohorts, "propose_stored_cohort", AsyncMock(side_effect=refusal))

    result = await ordinary_cohorts._reflect_packet_source("org", "owner", "source")

    assert result["outcome"] == "error"
    assert result["source_pass_complete"] is False
    assert [page["outcome"] for page in result["pages"]] == ["failed", "failed"]
    assert all(page["reason"] == "run_token_ceiling" for page in result["pages"])
    assert all(page["budget"]["kind"] == "run_token_ceiling" for page in result["pages"])
    assert reflection._budget_refusals([result]) == 2


async def test_packet_pages_reserve_against_the_owner_and_organization(monkeypatch):
    from sibyl.jobs import ordinary_cohorts
    from sibyl_core.ai.llm.budget import reserve_llm_budget, set_budget_enforcer

    _packet_pages(monkeypatch)

    async def propose(*args, **kwargs):
        await reserve_llm_budget(surface="memory", prompt="page evidence")
        return None, "execution"

    monkeypatch.setattr(ordinary_cohorts, "propose_stored_cohort", propose)
    budget = _RecordingBudget()
    set_budget_enforcer(budget)
    try:
        result = await ordinary_cohorts._reflect_packet_source("org", "owner", "source")
    finally:
        set_budget_enforcer(None)

    assert result["source_pass_complete"] is True
    assert [(c.user_id, c.organization_id) for c, _s, _t in budget.reservations] == [
        ("owner", "org"),
        ("owner", "org"),
    ]


def test_budget_refusals_count_items_and_packet_pages():
    results = [
        {"outcome": "error", "budget": {"kind": "run_token_ceiling"}},
        {"outcome": "reflected"},
        {"outcome": "error", "pages": [{"budget": {}}, {"outcome": "returned"}, {"budget": {}}]},
    ]

    assert reflection._budget_refusals(results) == 3
