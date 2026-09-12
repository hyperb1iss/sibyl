"""Automatic review resolves the real heuristic dream writer's protected input."""

from unittest.mock import AsyncMock

import pytest

from sibyl.jobs import reflection
from sibyl_core.services.memory_source_validation import SourceReadAuthority
from sibyl_core.services.reflection_validation import prepare_stored_reflection
from sibyl_core.services.surreal_content import remember_raw_memory
from sibyl_core.tasks.memory_progress import ProgressCriticOutput
from tests.test_dream_source_checkpoints import dream_store as dream_store  # noqa: PLC0414


async def test_automatic_validation_loads_real_dream_candidate(dream_store, monkeypatch):
    source = await remember_raw_memory(
        organization_id="dream-org",
        principal_id="owner",
        source_id="session",
        raw_content="Decision: validate inputs before parsing.",
        embedding_provider=None,
    )
    receipt = await reflection.run_reflection_dream_cycle({}, "dream-org", candidate_limit=0)
    assert receipt["failed"] == 0
    rows = await dream_store.execute_query(
        "SELECT * FROM raw_captures WHERE capture_surface='reflection_candidate';"
    )
    assert rows
    prepared = await prepare_stored_reflection(
        "dream-org", "owner", rows[0]["uuid"], AsyncMock(return_value=SourceReadAuthority("owner"))
    )
    assert prepared.sources[0].id == source.id
    assert source.raw_content in prepared.prepared.prompt
    assert prepared.candidate.content == rows[0]["raw_content"]

    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    from sibyl_core.ai.llm.extractor import Extractor
    from sibyl_core.services import procedure_validation
    from sibyl_core.services.reflection_validation import validate_reflection_stage
    from sibyl_core.tasks.memory_validation import CriticOutput

    extractor = Extractor(
        CriticOutput,
        agent=Agent(TestModel(custom_output_args={"findings": []}), output_type=CriticOutput),
    )
    monkeypatch.setattr(
        procedure_validation,
        "validation_extractor",
        AsyncMock(return_value=(extractor, '{"model":"offline"}')),
    )
    resolver = AsyncMock(return_value=SourceReadAuthority("owner"))
    first = await validate_reflection_stage(prepared, resolver)
    second = await validate_reflection_stage(prepared, resolver)
    assert first == second
    assert first["status"] == "no_findings"
    assert first["usage"]["requests"] == 1


@pytest.mark.parametrize("mutation", [None, "source", "candidate", "caller_payload"])
async def test_corrected_candidate_real_writer_replay(dream_store, monkeypatch, mutation):
    import json

    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    from sibyl_core.ai.llm.extractor import Extractor
    from sibyl_core.services import procedure_validation
    from sibyl_core.services.automatic_reflection import _persist_corrected
    from sibyl_core.services.reflection_validation import validate_reflection_stage
    from sibyl_core.tasks.memory_validation import CriticOutput
    from sibyl_core.tasks.procedure_review import ReviewSubmission, review_digest

    await remember_raw_memory(
        organization_id="dream-org",
        principal_id="owner",
        source_id="session",
        raw_content="Decision: validate inputs before parsing.",
        embedding_provider=None,
    )
    await reflection.run_reflection_dream_cycle({}, "dream-org", candidate_limit=0)
    rows = await dream_store.execute_query(
        "SELECT * FROM raw_captures WHERE capture_surface='reflection_candidate';"
    )
    resolver = AsyncMock(return_value=SourceReadAuthority("owner"))
    parent = await prepare_stored_reflection("dream-org", "owner", rows[0]["uuid"], resolver)
    assertion = json.loads(parent.prepared.payload_json)["assertions"]["/content"]
    review = ReviewSubmission(
        parent_operation_id=json.loads(parent.prepared.payload_json)["parent_operation_id"],
        parent_candidate_sha256=json.loads(parent.prepared.payload_json)["parent_candidate_sha256"],
        findings=[
            {
                "claim_path": "/content",
                "claim_sha256": review_digest(assertion),
                "evidence_refs": [{"evidence_id": "s0"}],
                "basis": "factual_contradiction",
                "disposition": "reconsider",
                "critique": "Preserve the observed ordering.",
            }
        ],
    )
    output = {
        "content": "Validate inputs before parsing.",
        "abstention_reason": None,
        "assessments": [
            {
                "finding_id": review.finding_ids()[0],
                "disposition": "accepted",
                "explanation": "The original decision specifies ordering.",
                "evidence_refs": [{"evidence_id": "s0"}],
            }
        ],
    }
    extractor = Extractor(
        CriticOutput, agent=Agent(TestModel(custom_output_args=output), output_type=CriticOutput)
    )
    monkeypatch.setattr(
        procedure_validation,
        "validation_extractor",
        AsyncMock(return_value=(extractor, '{"model":"offline"}')),
    )
    outcome = await validate_reflection_stage(parent, resolver, review)
    assert outcome["status"] == "corrected"
    from dataclasses import replace

    from sibyl_core.services.source_observations import SourceUnavailableError
    from sibyl_core.services.surreal_content import save_raw_memory

    if mutation == "source":
        source = parent.sources[0]
        await save_raw_memory(
            replace(source, raw_content="Corrected source advice."),
            expected_revision=source.revision,
            embedding_provider=None,
        )
        with pytest.raises(SourceUnavailableError):
            await _persist_corrected(parent, resolver, outcome)
        return
    if mutation == "caller_payload":
        outcome["content"] = "Forged caller-side replacement."
    first = await _persist_corrected(parent, resolver, outcome)
    if mutation == "candidate":
        await save_raw_memory(
            replace(first, raw_content="Unreviewed replacement."),
            expected_revision=first.revision,
            embedding_provider=None,
        )
        with pytest.raises(Exception, match="Corrected candidate changed"):
            await _persist_corrected(parent, resolver, outcome)
        return
    replay = await _persist_corrected(parent, resolver, outcome)
    assert first.id == replay.id
    assert first.revision == replay.revision
    child = await prepare_stored_reflection("dream-org", "owner", first.id, resolver)
    assert child.candidate.content == output["content"]
    from sibyl_core.services.automatic_reflection import automatically_review_reflection
    from sibyl_core.services.surreal_content import get_raw_memory

    critic = Extractor(
        CriticOutput,
        agent=Agent(
            TestModel(
                custom_output_args={"findings": [review.findings[0].model_dump(mode="json")]}
            ),
            output_type=CriticOutput,
        ),
    )
    recheck = Extractor(
        ProgressCriticOutput,
        agent=Agent(
            TestModel(
                custom_output_args={
                    "findings": [],
                    "prior_assessments": [
                        {
                            "finding_id": review.finding_ids()[0],
                            "disposition": "resolved",
                            "supported_reduction": "The corrected ordering agrees with the original decision.",
                            "remaining_concern": None,
                            "evidence_refs": [{"evidence_id": "s0"}],
                        }
                    ],
                }
            ),
            output_type=ProgressCriticOutput,
        ),
    )
    monkeypatch.setattr(
        procedure_validation,
        "_validation_extractor",
        AsyncMock(return_value=(recheck, '{"model":"offline"}')),
    )
    monkeypatch.setattr(
        procedure_validation,
        "validation_extractor",
        AsyncMock(
            side_effect=[
                (critic, '{"model":"offline"}'),
                (extractor, '{"model":"offline"}'),
                (recheck, '{"model":"offline"}'),
            ]
        ),
    )
    resumed = await automatically_review_reflection(
        "dream-org", "owner", parent.memory.id, resolver
    )
    assert resumed.status == "corrected"
    assert resumed.candidate is not None
    assert resumed.candidate.id == first.id
    assert len(resumed.executions) == 3
    archived = await get_raw_memory(organization_id="dream-org", memory_id=parent.memory.id)
    assert archived is not None
    assert archived.review_state == "pending"
    assert "archive_reason" not in archived.metadata
    await _assert_job_reuses_validation(monkeypatch, resolver, critic, first.id)


async def _assert_job_reuses_validation(monkeypatch, resolver, recheck, candidate_id):
    from sibyl_core.services import procedure_validation
    from tests.test_jobs_reflection import _preview, _promotion

    monkeypatch.setattr(
        procedure_validation,
        "validation_extractor",
        AsyncMock(return_value=(recheck, '{"model":"offline"}')),
    )
    monkeypatch.setattr("sibyl.jobs.ordinary_cohorts.writable_source_authority", resolver)
    monkeypatch.setattr(
        reflection,
        "preview_reflection_candidate_promotion",
        AsyncMock(return_value=_preview(candidate_id=candidate_id)),
    )
    promote = AsyncMock(return_value=_promotion(candidate_id=candidate_id))
    monkeypatch.setattr(reflection, "promote_reflection_candidate_review", promote)
    monkeypatch.setattr(reflection, "log_memory_audit_event", AsyncMock())
    receipt = await reflection.run_reflection_dream_cycle(
        {}, "dream-org", source_limit=0, candidate_limit=5
    )
    assert receipt["failed"] == 0
    assert receipt["promoted"] == 1
    promote.assert_awaited_once()
    assert promote.await_args.kwargs["candidate_id"] == candidate_id
    assert receipt["candidates"][0]["validation_executions"]


async def test_automatic_promotion_rejects_changed_reviewed_revision(monkeypatch):
    from types import SimpleNamespace

    from sibyl_core.services import memory_reflection
    from tests.test_jobs_reflection import _raw_memory

    plan = SimpleNamespace(
        candidate_memory=_raw_memory(revision=2),
        target_scope="private",
        target_scope_key=None,
        raw_source_ids=["source"],
    )
    monkeypatch.setattr(
        memory_reflection, "_resolve_reflection_promotion_plan", AsyncMock(return_value=plan)
    )
    write = AsyncMock()
    monkeypatch.setattr(memory_reflection, "_apply_promotion_plan", write)
    result = await memory_reflection.promote_reflection_candidate_review(
        candidate_id=plan.candidate_memory.id,
        organization_id=plan.candidate_memory.organization_id,
        principal_id=plan.candidate_memory.principal_id,
        promote_to_scope="private",
        expected_candidate_revision=1,
    )
    assert not result.success
    assert result.reason == "validated_candidate_changed"
    write.assert_not_awaited()


@pytest.mark.parametrize("interrupt", [False, True])
async def test_ordinary_two_repairs_reenter_original_and_child(
    dream_store, monkeypatch, ordinary_graph, interrupt
):
    import json

    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    from sibyl_core.ai.llm.extractor import Extractor
    from sibyl_core.services import procedure_validation
    from sibyl_core.services.automatic_reflection import automatically_review_reflection
    from sibyl_core.tasks.memory_validation import CriticOutput
    from sibyl_core.tasks.procedure_review import ReviewSubmission
    from sibyl_core.tasks.reflection_correction import ReflectionCorrection

    await remember_raw_memory(
        organization_id="dream-org",
        principal_id="owner",
        source_id="session",
        raw_content="Decision: validate inputs before parsing and retain the original bytes.",
        embedding_provider=None,
    )
    await reflection.run_reflection_dream_cycle({}, "dream-org", candidate_limit=0)
    candidates = await dream_store.execute_query(
        "SELECT * FROM raw_captures WHERE capture_surface='reflection_candidate';"
    )
    parent_id = candidates[0]["uuid"]
    resolver = AsyncMock(return_value=SourceReadAuthority("owner"))
    original_extract = Extractor.extract_with_usage
    partial = "Validate inputs before parsing, then always discard original bytes."
    final = "Validate inputs before parsing and retain the original bytes."
    calls = []

    async def factory(output_type=CriticOutput):
        return Extractor(output_type), '{"model":"offline"}'

    async def extract(reader, prompt):
        payload = json.loads(prompt.splitlines()[-1])
        calls.append(reader.output_type.__name__)
        if reader.output_type in (CriticOutput, ProgressCriticOutput):
            resolved = payload["assertions"]["/content"]["statement"] == final
            output = {
                "findings": []
                if resolved
                else [
                    {
                        "claim_path": "/content",
                        "claim_sha256": payload["assertion_hashes"]["/content"],
                        "evidence_refs": [{"evidence_id": "s0"}],
                        "basis": "factual_contradiction",
                        "disposition": "reconsider",
                        "critique": "Preserve both validation ordering and original bytes.",
                    }
                ]
            }
            if reader.output_type is ProgressCriticOutput:
                review = ReviewSubmission.model_validate(payload["prior_progress"]["review"])
                output["prior_assessments"] = [
                    {
                        "finding_id": identity,
                        "disposition": "resolved" if resolved else "partially_resolved",
                        "supported_reduction": "The ordering now agrees with the decision."
                        if not resolved
                        else "Ordering and byte retention agree with the decision.",
                        "remaining_concern": None
                        if resolved
                        else "Discarding the original bytes contradicts the decision.",
                        "evidence_refs": [{"evidence_id": "s0"}],
                    }
                    for identity in review.finding_ids()
                ]
        else:
            assert reader.output_type is ReflectionCorrection
            content = payload["original"]["assertions"]["/content"]["statement"]
            output = {
                "content": final if content == partial else partial,
                "abstention_reason": None,
                "assessments": [
                    {
                        "finding_id": identity,
                        "disposition": "accepted",
                        "explanation": "Apply the observed source decision.",
                        "evidence_refs": [{"evidence_id": "s0"}],
                    }
                    for identity in payload["finding_ids"]
                ],
            }
        reader._agent = Agent(TestModel(custom_output_args=output), output_type=reader.output_type)
        return await original_extract(reader, prompt)

    monkeypatch.setattr(procedure_validation, "validation_extractor", factory)
    monkeypatch.setattr(procedure_validation, "_validation_extractor", factory)
    monkeypatch.setattr(Extractor, "extract_with_usage", extract)
    if interrupt:
        import asyncio

        _install_child_interruption(monkeypatch)
        with pytest.raises(asyncio.CancelledError):
            await automatically_review_reflection("dream-org", "owner", parent_id, resolver)
        assert (
            len(await dream_store.execute_query("SELECT * FROM memory_validation_executions;")) == 2
        )
    result = await automatically_review_reflection("dream-org", "owner", parent_id, resolver)
    assert result.status == "corrected"
    assert result.candidate.raw_content == final
    assert len(result.executions) == 5
    assert calls == [
        "CriticOutput",
        "ReflectionCorrection",
        "ProgressCriticOutput",
        "ReflectionCorrection",
        "ProgressCriticOutput",
    ]
    monkeypatch.setattr(
        Extractor,
        "extract_with_usage",
        AsyncMock(side_effect=AssertionError("completed frontier redispatched")),
    )
    original_replay = await automatically_review_reflection(
        "dream-org", "owner", parent_id, resolver
    )
    child_replay = await automatically_review_reflection(
        "dream-org", "owner", result.candidate.id, resolver
    )
    assert original_replay == child_replay == result
    stages = await dream_store.execute_query("SELECT * FROM memory_validation_executions;")
    assert len(stages) == 5
    assert all(json.loads(row["usage_json"])["requests"] == 1 for row in stages)
    assert max(len(row["dependency_ids"]) for row in stages) == 4
    await _assert_ordinary_published_frontier(result, parent_id, resolver, dream_store)


async def _assert_ordinary_published_frontier(result, parent_id, resolver, dream_store):
    import json

    from sibyl_core.services.automatic_reflection import automatically_review_reflection
    from sibyl_core.services.content_raw_recall import recall_raw_memory
    from sibyl_core.services.memory_reflection import promote_reflection_candidate_review
    from sibyl_core.services.ordinary_publication import ordinary_promotion_binding

    binding = await ordinary_promotion_binding(
        "dream-org", "owner", result.candidate.id, result.executions[-1], resolver, AsyncMock()
    )
    promoted = await promote_reflection_candidate_review(
        organization_id="dream-org",
        principal_id="owner",
        candidate_id=result.candidate.id,
        expected_candidate_revision=result.candidate.revision,
        promote_to_scope="private",
        validation_promotion=binding,
    )
    assert promoted.success, promoted
    assert result.candidate.id in {
        memory.id
        for memory in await recall_raw_memory(
            organization_id="dream-org", principal_id="owner", query="validate inputs"
        )
    }
    for identity in (parent_id, result.candidate.id):
        replay = await automatically_review_reflection("dream-org", "owner", identity, resolver)
        assert replay.candidate.id == result.candidate.id
        assert replay.executions == result.executions
    assert len(await dream_store.execute_query("SELECT * FROM memory_validation_executions;")) == 5

    import os

    if os.environ.get("SIBYL_OPERATIONAL_TEST_URL"):
        import asyncio
        import subprocess
        import sys
        from pathlib import Path

        import sibyl_core

        payload = {
            "kind": "reflection",
            "org": "dream-org",
            "root": parent_id,
            "candidate": result.candidate.id,
            "executions": list(result.executions),
            "url": os.environ["SIBYL_OPERATIONAL_TEST_URL"],
            "namespace": dream_store._namespace,
        }
        worker = Path(sibyl_core.__file__).parents[2] / "tests" / "frontier_replay.py"
        completed = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, str(worker)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
        )
        assert completed.returncode == 0, completed.stdout[-3000:] + completed.stderr[-3000:]
        assert "FRESH_PROCESS_ROOT_CHILD_REPLAY_PASS" in completed.stdout

    await dream_store.execute_query(
        "UPDATE memory_validation_executions SET purged=true, result_json=NONE, recovery_key=NONE WHERE uuid=$uuid;",
        uuid=result.executions[0],
    )
    assert result.candidate.id not in {
        memory.id
        for memory in await recall_raw_memory(
            organization_id="dream-org", principal_id="owner", query="validate inputs"
        )
    }
    with pytest.raises(ValueError):
        await automatically_review_reflection("dream-org", "owner", parent_id, resolver)
    retired = await dream_store.execute_query("SELECT * FROM memory_validation_executions;")
    assert len(retired) == 5
    assert all(row["purged"] for row in retired)
    assert all(json.loads(row["usage_json"])["requests"] == 1 for row in retired)


@pytest.fixture
async def ordinary_graph(monkeypatch):
    from sibyl_core.services import memory_reflection
    from sibyl_core.services.graph_client import (
        SurrealGraphClient,
        mark_graph_schema_dirty,
        prepare_graph_schema,
    )
    from sibyl_core.services.graph_entities import EntityManager
    from sibyl_core.services.graph_relationships import RelationshipManager
    from sibyl_core.services.graph_runtime import GraphRuntime

    client = SurrealGraphClient(group_id="dream-org", url="memory://")
    mark_graph_schema_dirty("dream-org")
    await prepare_graph_schema(client)
    runtime = GraphRuntime(
        client=client,
        entity_manager=EntityManager(client, group_id="dream-org"),
        relationship_manager=RelationshipManager(client, group_id="dream-org"),
    )
    monkeypatch.setattr(
        memory_reflection, "get_surreal_graph_runtime", AsyncMock(return_value=runtime)
    )
    monkeypatch.setattr(
        "sibyl_core.services.graph_runtime.get_surreal_graph_runtime",
        AsyncMock(return_value=runtime),
    )
    monkeypatch.setattr(
        "sibyl_core.services.graph_derivations.get_source_authority_resolver",
        lambda: AsyncMock(return_value=SourceReadAuthority("owner")),
    )
    try:
        yield runtime
    finally:
        await client.close()


def _install_child_interruption(monkeypatch):
    import asyncio

    from sibyl_core.services import automatic_reflection

    persist = automatic_reflection._persist_corrected
    interrupted = False

    async def after_child(*args, **kwargs):
        nonlocal interrupted
        child = await persist(*args, **kwargs)
        if not interrupted:
            interrupted = True
            raise asyncio.CancelledError
        return child

    monkeypatch.setattr(automatic_reflection, "_persist_corrected", after_child)


async def test_default_job_promotes_repaired_frontier_and_reaches_later_candidate(
    dream_store, monkeypatch, ordinary_graph
):
    import sys

    from sibyl_core.ai.llm.extractor import Extractor
    from sibyl_core.services.content_raw_recall import recall_raw_memory

    actual_extract = Extractor.extract_with_usage

    async def actual_job(result, parent_id, resolver, store):
        monkeypatch.setattr("sibyl.jobs.ordinary_cohorts.writable_source_authority", resolver)
        monkeypatch.setattr(reflection, "log_memory_audit_event", AsyncMock())
        first = await reflection.run_reflection_dream_cycle(
            {}, "dream-org", source_limit=0, candidate_limit=1
        )
        assert first["failed"] == 0, first
        assert first["promoted"] == 1, first
        assert result.candidate.id in {
            memory.id
            for memory in await recall_raw_memory(
                organization_id="dream-org", principal_id="owner", query="validate inputs"
            )
        }
        second = await reflection.run_reflection_dream_cycle(
            {}, "dream-org", source_limit=0, candidate_limit=1
        )
        assert second["failed"] == 0, second
        assert second["promoted"] == 0, second
        assert len(await store.execute_query("SELECT * FROM memory_validation_executions;")) == 5
        await _assert_later_candidate_is_reached(monkeypatch, store, actual_extract)

    monkeypatch.setattr(sys.modules[__name__], "_assert_ordinary_published_frontier", actual_job)
    await test_ordinary_two_repairs_reenter_original_and_child(
        dream_store, monkeypatch, ordinary_graph, False
    )


async def _assert_later_candidate_is_reached(monkeypatch, store, actual_extract):
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    from sibyl_core.ai.llm.extractor import Extractor

    source = await remember_raw_memory(
        organization_id="dream-org",
        principal_id="owner",
        source_id="later-session",
        raw_content="Decision: preserve transaction receipts before returning success.",
        embedding_provider=None,
    )
    prepared = await reflection._reflect_dream_source(
        source=source,
        work=await reflection._load_dream_work("dream-org", source),
        group_id="dream-org",
        run_id="later-candidate-fixture",
        dry_run=False,
    )
    assert prepared["persisted_count"] == 1
    calls = []

    async def extract(reader, prompt):
        calls.append(prompt)
        reader._agent = Agent(
            TestModel(custom_output_args={"findings": []}), output_type=reader.output_type
        )
        return await actual_extract(reader, prompt)

    monkeypatch.setattr(Extractor, "extract_with_usage", extract)
    later = await reflection.run_reflection_dream_cycle(
        {}, "dream-org", source_limit=0, candidate_limit=1
    )
    assert later["failed"] == 0, later
    assert later["promoted"] == 1, repr(later)
    assert len(calls) == 1
    assert len(await store.execute_query("SELECT * FROM memory_validation_executions;")) == 6
    monkeypatch.setattr(
        Extractor,
        "extract_with_usage",
        AsyncMock(side_effect=AssertionError("published heuristic redispatched")),
    )
    from sibyl_core.services.automatic_reflection import automatically_review_reflection

    accepted = next(item for item in later["candidates"] if item["outcome"] == "auto_promote")
    replay = await automatically_review_reflection(
        "dream-org",
        "owner",
        accepted["candidate_id"],
        AsyncMock(return_value=SourceReadAuthority("owner")),
    )
    assert replay.candidate.id == accepted["candidate_id"]
    again = await reflection.run_reflection_dream_cycle(
        {}, "dream-org", source_limit=0, candidate_limit=1
    )
    assert again["failed"] == again["promoted"] == 0, again
    await _assert_published_checkpoint_tamper_denied(store, accepted["candidate_id"])


async def _assert_published_checkpoint_tamper_denied(store, candidate_id):
    import copy

    from sibyl_core.services.source_observations import SourceUnavailableError

    rows = await store.execute_query(
        "SELECT metadata FROM raw_captures WHERE uuid=$uuid;", uuid=candidate_id
    )
    metadata = rows[0]["metadata"]
    for mutation in ("event", "source", "entity", "original"):
        changed = copy.deepcopy(metadata)
        event = changed["reflection_findings"][-1]
        if mutation == "event":
            event["reason"] = "unsupported publication reason"
        elif mutation == "source":
            event["source_ids"] = ["foreign-source"]
        elif mutation == "entity":
            changed["promoted_entity_id"] = "foreign-entity"
        else:
            changed["reflection_findings"].insert(0, {**event, "kind": "correction"})
        try:
            await store.execute_query(
                "UPDATE raw_captures SET metadata=$metadata WHERE uuid=$uuid;",
                metadata=changed,
                uuid=candidate_id,
            )
            with pytest.raises(SourceUnavailableError):
                await prepare_stored_reflection(
                    "dream-org",
                    "owner",
                    candidate_id,
                    AsyncMock(return_value=SourceReadAuthority("owner")),
                    publication=True,
                )
        finally:
            await store.execute_query(
                "UPDATE raw_captures SET metadata=$metadata WHERE uuid=$uuid;",
                metadata=metadata,
                uuid=candidate_id,
            )
        await prepare_stored_reflection(
            "dream-org",
            "owner",
            candidate_id,
            AsyncMock(return_value=SourceReadAuthority("owner")),
            publication=True,
        )
