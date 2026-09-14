"""Stored signed critique drives the existing extraction and persistence owners."""

import json
import threading
from unittest.mock import AsyncMock

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from sibyl_core.ai.llm.extractor import Extractor
from sibyl_core.services import automatic_procedure as automatic
from sibyl_core.services import eval_publication as publication
from sibyl_core.services import procedure_validation as validation
from sibyl_core.tasks.memory_progress import ProgressCriticOutput
from sibyl_core.tasks.memory_validation import CriticOutput
from sibyl_core.tasks.procedure_review import ReviewSubmission, review_digest
from tests.test_eval_publication import admitted_pair as admitted_pair
from tests.test_eval_publication import content_store as content_store
from tests.test_eval_publication import evidence as evidence
from tests.test_eval_publication import proposal as proposal
from tests.test_eval_publication import rows
from tests.test_eval_publication_promotion import runtime as runtime
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

    async def critic(output_type=CriticOutput):
        calls.append("critic")
        output = {"findings": [finding] if len(calls) == 1 else []}
        if len(calls) > 1 and controls["recheck_abstain"]:
            output["abstention_reason"] = "Original evidence remains insufficient."
        if output_type is ProgressCriticOutput:
            output["prior_assessments"] = [
                {
                    "finding_id": review.finding_ids()[0],
                    "disposition": "resolved",
                    "supported_reduction": "The original qualification is sufficient.",
                    "remaining_concern": None,
                    "evidence_refs": [{"evidence_id": "episode:0"}],
                }
            ]
        return Extractor(
            output_type,
            agent=Agent(TestModel(custom_output_args=output), output_type=output_type),
        ), '{"model":"offline"}'

    async def agent(extractor):
        calls.append("correction")
        output = {"outcome": {"kind": "edits", "edits": []}}
        if controls["correction_abstain"]:
            output["outcome"] = {
                "kind": "abstention",
                "reason": "Original evidence cannot support a correction.",
            }
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
    monkeypatch.setattr(validation, "_validation_extractor", critic)
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
        if extractor.output_type in (CriticOutput, ProgressCriticOutput):
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
    from sibyl_core.memory_pipeline.observations import SourceKind
    from sibyl_core.migrate.validation_receipt_archive import capture, prepare_payload
    from sibyl_core.services.content_client import surreal_content_client
    from sibyl_core.services.source_archive_store import export_source_integrity

    async with surreal_content_client() as client:
        integrity = await export_source_integrity(
            client.execute_query, kind=SourceKind.RAW_CAPTURE, organizations=["org"]
        )
    # The signed procedure owner persists its correction in eval_consolidations,
    # independently of the ordinary candidate writer's derivation origin.
    assert not any(item["target_id"] == first.candidate_id for item in integrity["derivations"])
    archive = {
        "version": "2.3",
        "tables": {"memory_validation_executions": stages},
        "source_integrity": integrity,
        "validation_receipts": capture(stages),
    }
    prepare_payload(json.loads(json.dumps(archive, default=str)))
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


@pytest.mark.asyncio
async def test_stored_procedure_validation_prepares_evidence_off_event_loop(candidate, monkeypatch):
    original = validation.prepare_procedure_validation
    event_loop_thread = threading.get_ident()
    threads = []

    def observed(*args, **kwargs):
        threads.append(threading.get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(validation, "prepare_procedure_validation", observed)
    result = await validation.prepare_stored_procedure_validation("org", "owner", candidate.id)
    assert result.prepared.input_sha256
    assert len(threads) == 1 and threads[0] != event_loop_thread


@pytest.mark.parametrize("interrupt", [False, True])
async def test_signed_two_repairs_preserve_edits_and_reenter_from_child(
    candidate, monkeypatch, runtime, interrupt
):
    from sibyl_core.tasks.procedure_edits import ProcedureEdits

    original_extract = Extractor.extract_with_usage
    calls = []
    final_goal = "Use the observed replay condition and retain its original source boundary."
    partial_goal = (
        "Use the observed replay condition, with an unsupported universal source boundary."
    )

    async def factory(output_type=CriticOutput):
        return Extractor(output_type), '{"model":"offline"}'

    async def extract(reader, prompt):
        payload = json.loads(prompt.splitlines()[-1])
        calls.append(reader.output_type.__name__)
        if reader.output_type in (CriticOutput, ProgressCriticOutput):
            statement = payload["assertions"]["/goal"]["statement"]
            resolved = statement == final_goal
            output = {
                "findings": []
                if resolved
                else [
                    {
                        "claim_path": "/goal",
                        "claim_sha256": payload["assertion_hashes"]["/goal"],
                        "evidence_refs": [{"evidence_id": "episode:0"}],
                        "basis": "unsupported_generalization",
                        "disposition": "qualify",
                        "critique": "Limit both the replay condition and source boundary to observed evidence.",
                    }
                ]
            }
            if reader.output_type is ProgressCriticOutput:
                review = ReviewSubmission.model_validate(payload["prior_progress"]["review"])
                output["prior_assessments"] = [
                    {
                        "finding_id": identity,
                        "disposition": "resolved" if resolved else "partially_resolved",
                        "supported_reduction": "The replay condition now matches the trace."
                        if not resolved
                        else "Both restrictions now match the trace.",
                        "remaining_concern": None
                        if resolved
                        else "The universal source boundary remains unsupported.",
                        "evidence_refs": [{"evidence_id": "episode:0"}],
                    }
                    for identity in review.finding_ids()
                ]
        else:
            assert reader.output_type is ProcedureEdits
            goal = payload["parent_procedure"]["goal"]["statement"]
            output = {
                "outcome": {
                    "kind": "edits",
                    "edits": [
                        {
                            "claim_path": "/goal",
                            "claim_sha256": payload["claim_sha256_by_path"]["/goal"],
                            "replacement": {
                                "statement": final_goal if goal == partial_goal else partial_goal,
                                "label": "inferred",
                                "support": [{"evidence_id": "episode:0"}],
                            },
                        }
                    ],
                },
                "assessments": [
                    {
                        "finding_id": finding["finding_id"],
                        "disposition": "accepted",
                        "explanation": "Apply the evidence-supported restriction while preserving other assertions.",
                        "evidence_refs": [{"evidence_id": "episode:0"}],
                    }
                    for finding in payload["findings"]
                ],
            }
        reader._agent = Agent(TestModel(custom_output_args=output), output_type=reader.output_type)
        return await original_extract(reader, prompt)

    monkeypatch.setattr(validation, "validation_extractor", factory)
    monkeypatch.setattr(validation, "_validation_extractor", factory)
    monkeypatch.setattr(Extractor, "extract_with_usage", extract)
    monkeypatch.setattr(
        automatic,
        "_extractor_policy",
        AsyncMock(
            return_value=publication._ExtractorPolicy("test", "r", 100000, 2048, "tool", None)
        ),
    )
    args = dict(
        organization_id="org", principal_id="owner", parent_id=candidate.id, authorize=AsyncMock()
    )
    original = await validation.prepare_stored_procedure_validation("org", "owner", candidate.id)
    if interrupt:
        import asyncio

        store = automatic.store_consolidation
        interrupted = False

        async def after_child(*args, **kwargs):
            nonlocal interrupted
            stored = await store(*args, **kwargs)
            if not interrupted:
                interrupted = True
                raise asyncio.CancelledError()
            return stored

        monkeypatch.setattr(automatic, "store_consolidation", after_child)
        with pytest.raises(asyncio.CancelledError):
            await automatic.automatically_reconsider_procedure(**args)
        assert len(await rows("eval_consolidations")) == 2
    result = await automatic.automatically_reconsider_procedure(**args)
    assert result.status == "corrected" and len(result.executions) == 5
    assert calls == [
        "CriticOutput",
        "ProcedureEdits",
        "ProgressCriticOutput",
        "ProcedureEdits",
        "ProgressCriticOutput",
    ]
    final = await validation.prepare_stored_procedure_validation(
        "org", "owner", result.candidate_id
    )
    before = json.loads(original.prepared.payload_json)["assertions"]
    after = json.loads(final.prepared.payload_json)["assertions"]
    assert after["/goal"]["statement"] == final_goal
    assert {k: v for k, v in before.items() if k != "/goal"} == {
        k: v for k, v in after.items() if k != "/goal"
    }
    monkeypatch.setattr(
        Extractor,
        "extract_with_usage",
        AsyncMock(side_effect=AssertionError("completed frontier redispatched")),
    )
    assert await automatic.automatically_reconsider_procedure(**args) == result
    assert (
        await automatic.automatically_reconsider_procedure(
            **{**args, "parent_id": result.candidate_id}
        )
        == result
    )
    stages = await rows("memory_validation_executions")
    assert len(stages) == 5
    assert all(json.loads(row["usage_json"])["requests"] == 1 for row in stages)
    assert max(len(row["dependency_ids"]) for row in stages) == 4
    from sibyl_core.services.content_raw_recall import recall_raw_memory
    from sibyl_core.services.validation_promotion import promote_validated_procedure

    promoted = await promote_validated_procedure(
        organization_id="org",
        principal_id="owner",
        candidate_id=result.candidate_id,
        execution_id=result.executions[-1],
        authorize=AsyncMock(),
    )
    assert promoted.success, promoted
    assert result.candidate_id in {
        memory.id
        for memory in await recall_raw_memory(
            organization_id="org", principal_id="owner", query="observed replay condition"
        )
    }
    replay = await automatic.automatically_reconsider_procedure(**args)
    assert replay.candidate_id == result.candidate_id and replay.executions == result.executions
    replay_child = await automatic.automatically_reconsider_procedure(
        **{**args, "parent_id": result.candidate_id}
    )
    assert (
        replay_child.candidate_id == result.candidate_id
        and replay_child.executions == result.executions
    )
    assert len(await rows("memory_validation_executions")) == 5
    from sibyl_core.services.content_client import surreal_content_client

    async with surreal_content_client() as replay_client:
        native_client = replay_client

    import os

    if os.environ.get("SIBYL_OPERATIONAL_TEST_URL"):
        import asyncio
        import subprocess
        import sys
        from pathlib import Path

        import sibyl_core

        payload = dict(
            kind="procedure",
            org="org",
            root=candidate.id,
            candidate=result.candidate_id,
            executions=list(result.executions),
            url=os.environ["SIBYL_OPERATIONAL_TEST_URL"],
            namespace=native_client._namespace,
        )
        worker = Path(sibyl_core.__file__).resolve().parents[2] / "tests" / "frontier_replay.py"
        completed = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, str(worker)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
        )
        assert completed.returncode == 0, completed.stdout[-3000:] + completed.stderr[-3000:]
        assert "FRESH_PROCESS_ROOT_CHILD_REPLAY_PASS" in completed.stdout

    from sibyl_core.services.validation_execution import _query

    await _query(
        "UPDATE memory_validation_executions SET purged=true, result_json=NONE, recovery_key=NONE WHERE uuid=$uuid;",
        uuid=result.executions[0],
    )
    assert result.candidate_id not in {
        memory.id
        for memory in await recall_raw_memory(
            organization_id="org", principal_id="owner", query="observed replay condition"
        )
    }
    with pytest.raises(ValueError):
        await automatic.automatically_reconsider_procedure(**args)
    retired = await rows("memory_validation_executions")
    assert len(retired) == 5 and all(row["purged"] for row in retired)
    assert all(json.loads(row["usage_json"])["requests"] == 1 for row in retired)
