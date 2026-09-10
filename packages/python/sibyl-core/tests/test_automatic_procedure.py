"""Stored signed critique drives the existing extraction and persistence owners."""

import json
from unittest.mock import AsyncMock

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from sibyl_core.ai.llm.extractor import Extractor
from sibyl_core.services import automatic_procedure as automatic
from sibyl_core.services import eval_publication as publication
from sibyl_core.services import procedure_validation as validation
from sibyl_core.tasks.memory_validation import CriticOutput
from sibyl_core.tasks.procedure_review import ReviewSubmission, review_digest
from tests.test_eval_publication import admitted_pair as admitted_pair
from tests.test_eval_publication import content_store as content_store
from tests.test_eval_publication import evidence as evidence
from tests.test_eval_publication import proposal as proposal
from tests.test_eval_publication import rows
from tests.test_validation_execution import candidate as candidate


@pytest.fixture
async def correction_model(candidate, proposal, monkeypatch):
    original = await validation.prepare_stored_procedure_validation("org", "owner", candidate.id)
    payload = json.loads(original.prepared.payload_json)
    finding = {
        "claim_path": "/goal",
        "claim_sha256": review_digest(payload["assertions"]["/goal"]),
        "evidence_refs": [{"evidence_id": "episode:0"}],
        "basis": "unsupported_generalization",
        "disposition": "qualify",
        "critique": "Keep the goal within the original evidence.",
    }
    review = ReviewSubmission(
        parent_operation_id=payload["parent_operation_id"],
        parent_candidate_sha256=payload["parent_candidate_sha256"],
        findings=[finding],
    )
    calls = []
    controls = {"recheck_abstain": False, "correction_abstain": False}

    async def critic():
        calls.append("critic")
        output = {"findings": [finding] if len(calls) == 1 else []}
        if len(calls) > 1 and controls["recheck_abstain"]:
            output["abstention_reason"] = "Original evidence remains insufficient."
        return Extractor(
            CriticOutput,
            agent=Agent(TestModel(custom_output_args=output), output_type=CriticOutput),
        ), '{"model":"offline"}'

    async def agent(extractor):
        calls.append("correction")
        output = proposal[1].proposal.model_dump(mode="json")
        if controls["correction_abstain"]:
            output.update(
                procedure=None, abstention_reason="Original evidence cannot support a correction."
            )
        output["assessments"] = [
            {
                "finding_id": review.finding_ids()[0],
                "disposition": "accepted",
                "explanation": "The goal remains limited to the observed condition.",
                "evidence_refs": [{"evidence_id": "episode:0"}],
            }
        ]
        return Agent(TestModel(custom_output_args=output), output_type=extractor.output_type)

    monkeypatch.setattr(validation, "validation_extractor", critic)
    monkeypatch.setattr(Extractor, "_get_agent", agent)
    monkeypatch.setattr(
        automatic,
        "_extractor_policy",
        AsyncMock(
            return_value=publication._ExtractorPolicy(
                "test",
                "r",
                100000,
                2048,
                "tool",
                None,
            )
        ),
    )

    # Critic agents are explicit. Preserve them while substituting the correction factory.
    async def get_agent(extractor):
        if extractor.output_type is CriticOutput:
            return extractor._agent
        return await agent(extractor)

    monkeypatch.setattr(Extractor, "_get_agent", get_agent)
    return controls


async def test_signed_automatic_no_findings_preserves_pending(candidate):
    result = await automatic.automatically_reconsider_procedure(
        organization_id="org",
        principal_id="owner",
        parent_id=candidate.id,
        authorize=AsyncMock(),
    )
    assert result.status == "validated" and result.candidate_id == candidate.id
    assert len(await rows("eval_consolidations")) == 1
    assert (await rows("raw_captures"))[-1]["review_state"] == "pending"


async def test_signed_automatic_correction_rechecks_and_replays(candidate, correction_model):
    from pydantic import TypeAdapter

    from sibyl_core.tasks.procedure_correction_result import ProcedureCorrectionResult

    original_rows = await rows("eval_consolidations")
    original = await validation.prepare_stored_procedure_validation("org", "owner", candidate.id)
    args = dict(
        organization_id="org", principal_id="owner", parent_id=candidate.id, authorize=AsyncMock()
    )
    first = await automatic.automatically_reconsider_procedure(**args)
    assert first.status == "corrected"
    assert first.candidate_id != candidate.id and len(first.executions) == 3
    assert first.stored.memory.review_state == "pending"
    assert (
        first.stored.build_receipt["reconsideration"]["submission"]["parent_operation_id"]
        == candidate.metadata["eval_consolidation"]
    )
    assert len(await rows("eval_consolidations")) == 2
    second = await automatic.automatically_reconsider_procedure(**args)
    assert second == first
    stages = await rows("memory_validation_executions")
    assert len(stages) == 3
    stage = next(row for row in stages if row["uuid"] == first.executions[1])
    retained = TypeAdapter(ProcedureCorrectionResult).validate_json(stage["result_json"])
    assert retained.result.group == original.artifact.group
    assert (
        retained.parent_candidate_sha256
        == json.loads(original.prepared.payload_json)["parent_candidate_sha256"]
    )
    assert (
        next(
            row
            for row in await rows("eval_consolidations")
            if row["uuid"] == original_rows[0]["uuid"]
        )
        == original_rows[0]
    )


async def test_signed_correction_recorded_cancellation_resumes_without_extraction(
    candidate, correction_model, monkeypatch
):
    import asyncio

    from sibyl_core.services.validation_execution import ValidationExecution
    from sibyl_core.tasks.procedure_correction_result import ProcedureCorrectionResult

    record = ValidationExecution.record_result
    interrupted = False

    async def cancel_after_record(execution, result):
        nonlocal interrupted
        await record(execution, result)
        if isinstance(result, ProcedureCorrectionResult) and not interrupted:
            interrupted = True
            raise asyncio.CancelledError()

    monkeypatch.setattr(ValidationExecution, "record_result", cancel_after_record)
    args = dict(
        organization_id="org", principal_id="owner", parent_id=candidate.id, authorize=AsyncMock()
    )
    with pytest.raises(asyncio.CancelledError):
        await automatic.automatically_reconsider_procedure(**args)
    stored = await rows("memory_validation_executions")
    correction = next(row for row in stored if row["state"] == "recorded")
    assert json.loads(correction["usage_json"])["requests"] == 1
    monkeypatch.setattr(
        automatic,
        "propose_conditional_procedure",
        AsyncMock(side_effect=AssertionError("duplicate paid extraction")),
    )
    result = await automatic.automatically_reconsider_procedure(**args)
    assert result.status == "corrected" and len(await rows("eval_consolidations")) == 2


async def test_signed_correction_identity_alone_cannot_authorize_write(proposal):
    from dataclasses import replace

    operation, result = proposal
    corrected = replace(
        operation,
        correction_execution_id="a" * 64,
        parent_operation_id="b" * 64,
        parent_candidate_sha256="c" * 64,
    )
    assert corrected.key != operation.key
    with pytest.raises(publication.ConsolidationConflict, match="stored correction authority"):
        await publication.store_consolidation(corrected, result)
    assert not await rows("eval_consolidations")


async def test_signed_correction_late_parent_mutation_keeps_usage_without_child(
    candidate, correction_model, monkeypatch
):
    from sibyl_core.backends.surreal import SurrealContentClient

    execute = SurrealContentClient.execute_query
    mutated = False

    async def race(client, query, **params):
        nonlocal mutated
        if (
            "CREATE eval_consolidations" in query
            and "correction_execution" in params
            and not mutated
        ):
            mutated = True
            await execute(
                client,
                "UPDATE raw_captures SET metadata.changed=true WHERE uuid=$id;",
                id=candidate.id,
            )
        return await execute(client, query, **params)

    monkeypatch.setattr(SurrealContentClient, "execute_query", race)
    with pytest.raises(Exception, match="Correction sources changed"):
        await automatic.automatically_reconsider_procedure(
            organization_id="org",
            principal_id="owner",
            parent_id=candidate.id,
            authorize=AsyncMock(),
        )
    assert mutated and len(await rows("eval_consolidations")) == 1
    stages = await rows("memory_validation_executions")
    correction = next(
        row for row in stages if json.loads(row["result_json"])["status"] == "procedure_correction"
    )
    assert json.loads(correction["usage_json"])["requests"] == 1


@pytest.mark.parametrize("mutation", ["revision", "purge"])
async def test_consolidation_transaction_fences_concurrent_source(proposal, monkeypatch, mutation):
    import asyncio

    from sibyl_core.services import content_client

    async with content_client.surreal_content_client() as client:
        if not client._url.startswith(("ws://", "wss://")):
            pytest.skip("Requires independent native server transactions")
    operation, result = proposal
    source_id = result.group.episodes[0].stored_sources[0].source_id
    entered = asyncio.Event()
    original = publication._STORE
    monkeypatch.setattr(
        publication,
        "_STORE",
        original.replace("    LET $source_ids=", "    SLEEP 1s;\n    LET $source_ids="),
    )
    from sibyl_core.backends.surreal import SurrealContentClient

    execute = SurrealContentClient.execute_query

    async def observed(client, query, **params):
        if "SLEEP 1s" in query:
            entered.set()
        return await execute(client, query, **params)

    monkeypatch.setattr(SurrealContentClient, "execute_query", observed)
    storing = asyncio.create_task(publication.store_consolidation(operation, result))
    await entered.wait()
    await asyncio.sleep(0.3)
    async with content_client.surreal_content_client() as client:
        if mutation == "purge":
            await client.execute_query("DELETE raw_captures WHERE uuid=$id;", id=source_id)
        else:
            await client.execute_query(
                "UPDATE raw_captures SET metadata.changed=true WHERE uuid=$id;", id=source_id
            )
    assert not storing.done(), "Source write must commit inside the dependent transaction window"
    with pytest.raises(
        Exception,
        match=r"original admitted source changed|read or write conflict|transaction.*conflict",
    ):
        await storing
    assert not await rows("eval_consolidations")
    assert len(await rows("raw_captures")) == (1 if mutation == "purge" else 2)


@pytest.mark.parametrize("boundary", ["correction_abstain", "recheck_abstain"])
async def test_signed_automatic_insufficient_evidence_abstains(
    candidate, correction_model, boundary
):
    correction_model[boundary] = True
    result = await automatic.automatically_reconsider_procedure(
        organization_id="org",
        principal_id="owner",
        parent_id=candidate.id,
        authorize=AsyncMock(),
    )
    assert result.status == "abstained" and result.candidate_id is None
    assert result.reason and "evidence" in result.reason.lower()
    assert all(row.get("promoted_entity_id") is None for row in await rows("eval_consolidations"))
    assert len(result.executions) == (2 if boundary == "correction_abstain" else 3)


async def test_signed_correction_purge_removes_stored_source_bodies(candidate, correction_model):
    from sibyl_core.services import content_client
    from sibyl_core.services.validation_execution import validation_archive_guard

    await automatic.automatically_reconsider_procedure(
        organization_id="org",
        principal_id="owner",
        parent_id=candidate.id,
        authorize=AsyncMock(),
    )
    stages = await rows("memory_validation_executions")
    assert len(stages) == 3
    for stage in stages:
        assert validation_archive_guard("memory_validation_executions", stage)
    original = await validation.prepare_stored_procedure_validation("org", "owner", candidate.id)
    source = original.artifact.group.episodes[0].stored_sources[0].source_id
    async with content_client.surreal_content_client() as client:
        await client.execute_query("DELETE raw_captures WHERE uuid=$id;", id=source)
    after = await rows("memory_validation_executions")
    assert all(stage["purged"] and stage.get("result_json") is None for stage in after)
    assert all(json.loads(stage["usage_json"])["requests"] == 1 for stage in after)


@pytest.mark.parametrize("committed", [False, True])
async def test_signed_correction_result_write_recovery_preserves_replay(
    candidate, correction_model, monkeypatch, committed
):
    from sibyl_core.services.validation_execution import ValidationExecution
    from sibyl_core.tasks.procedure_correction_result import ProcedureCorrectionResult

    record = ValidationExecution.record_result
    corrections = []

    async def fail_correction(execution, result):
        if isinstance(result, ProcedureCorrectionResult):
            corrections.append(result)
            if committed:
                await record(execution, result)
            raise OSError("correction result acknowledgment lost")
        await record(execution, result)

    monkeypatch.setattr(ValidationExecution, "record_result", fail_correction)
    await test_signed_automatic_correction_rechecks_and_replays(candidate, correction_model)
    assert len(corrections) == 1
