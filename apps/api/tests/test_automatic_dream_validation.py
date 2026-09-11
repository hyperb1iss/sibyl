"""Automatic review resolves the real heuristic dream writer's protected input."""

from unittest.mock import AsyncMock

import pytest

from sibyl.jobs import reflection
from sibyl_core.services.memory_source_validation import SourceReadAuthority
from sibyl_core.services.reflection_validation import prepare_stored_reflection
from sibyl_core.services.surreal_content import remember_raw_memory
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
        CriticOutput,
        agent=Agent(TestModel(custom_output_args={"findings": []}), output_type=CriticOutput),
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
    assert archived.review_state == "archived"
    assert archived.metadata["archive_reason"] == "replaced_by_validated_correction"
    await _assert_job_reuses_validation(monkeypatch, resolver, recheck, first.id)


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
