"""Packet ancestry survives the ordinary correction and progress owners."""

import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from sibyl_core.ai.llm.extractor import Extractor
from sibyl_core.services import ordinary_cohort, procedure_validation
from sibyl_core.services.automatic_reflection import _persist_corrected, _reflection_root
from sibyl_core.services.content_raw_persistence import remember_raw_memory, save_raw_memory
from sibyl_core.services.memory_source_validation import SourceReadAuthority
from sibyl_core.services.reflection_validation import (
    prepare_stored_reflection,
    validate_reflection_stage,
)
from sibyl_core.services.validation_progress import ProgressContext
from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.consolidation import ConsolidationInputBudgetExceeded
from sibyl_core.tasks.episode_evidence import project_episode
from sibyl_core.tasks.memory_progress import ProgressCriticOutput
from sibyl_core.tasks.memory_validation import CriticOutput
from sibyl_core.tasks.ordinary_packets import QUALIFICATION, prepare_ordinary_packets
from sibyl_core.tasks.procedure_review import ReviewSubmission
from tests.test_episode_evidence import _encoded, _episode
from tests.test_ordinary_cohort import content_store as content_store


def install(monkeypatch, output, output_type=CriticOutput):
    reader = Extractor(
        output_type, agent=Agent(TestModel(custom_output_args=output), output_type=output_type)
    )
    monkeypatch.setattr(
        procedure_validation,
        "validation_extractor",
        AsyncMock(return_value=(reader, '{"model":"offline"}')),
    )
    monkeypatch.setattr(
        procedure_validation,
        "_validation_extractor",
        AsyncMock(return_value=(reader, '{"model":"offline"}')),
    )
    return reader


async def create_candidate(content_store, monkeypatch, *, statement="Preserve packet scope"):
    monkeypatch.setattr(
        "sibyl_core.services.content_models.configured_raw_memory_embedding_provider", lambda: None
    )
    artifact = canonical(_episode()).encode()
    source = await remember_raw_memory(
        organization_id="org",
        principal_id="owner",
        source_id="controller",
        raw_content=artifact.decode(),
        embedding_provider=None,
    )
    citation = project_episode(source.id, artifact, prefix="s0").citations["s0.goal"]
    start, end = citation.ranges[0]
    output = {
        "procedure": {
            "kind": "pattern",
            "goal": {
                "statement": statement,
                "label": "inferred",
                "support": [{"episode_id": source.id, "start_byte": start, "end_byte": end}],
            },
        },
        "abstention_reason": None,
    }
    install(monkeypatch, output)
    resolver = AsyncMock(return_value=SourceReadAuthority("owner"))
    packets = await ordinary_cohort.prepare_stored_source_packets(
        "org", "owner", source.id, resolver
    )
    candidate, origin = await ordinary_cohort.propose_stored_cohort(
        "org",
        "owner",
        [source.id],
        resolver,
        authorize=AsyncMock(),
        packet_binding=packets[0].binding,
    )
    assert candidate is not None
    parent = await prepare_stored_reflection("org", "owner", candidate.id, resolver)
    return parent, resolver, origin


async def test_packet_correction_progress_and_removed_marker_keep_protected_root(
    content_store, monkeypatch
):
    parent, resolver, origin = await create_candidate(content_store, monkeypatch)
    payload = json.loads(parent.prepared.payload_json)
    finding = {
        "claim_path": "/content",
        "claim_sha256": payload["assertion_hashes"]["/content"],
        "evidence_refs": [{"evidence_id": "s0.goal"}],
        "basis": "factual_contradiction",
        "disposition": "reconsider",
        "critique": "Keep the observation explicitly local to this packet.",
    }
    install(monkeypatch, {"findings": [finding]})
    critique = await validate_reflection_stage(parent, resolver)
    review = ReviewSubmission.model_validate(critique["submission"])
    output = {
        "content": parent.candidate.content + "\nThis observation is local to the cited packet.",
        "abstention_reason": None,
        "assessments": [
            {
                "finding_id": review.finding_ids()[0],
                "disposition": "accepted",
                "explanation": "Retain the source-local qualification.",
                "evidence_refs": [{"evidence_id": "s0.goal"}],
            }
        ],
    }
    install(monkeypatch, output)
    correction = await validate_reflection_stage(
        parent, resolver, review, review_execution_id=str(critique["execution_id"])
    )
    assert correction["status"] == "corrected"
    child = await _persist_corrected(
        parent, resolver, correction, review_execution_id=str(critique["execution_id"])
    )
    prepared = await prepare_stored_reflection("org", "owner", child.id, resolver)
    assert prepared.packet == parent.packet and QUALIFICATION in prepared.candidate.content
    assert {ref["execution_id"] for ref in prepared.origin_dependencies} == {
        origin,
        correction["execution_id"],
    }
    install(
        monkeypatch,
        {
            "findings": [],
            "prior_assessments": [
                {
                    "finding_id": review.finding_ids()[0],
                    "disposition": "resolved",
                    "supported_reduction": "The observation remains local to the packet.",
                    "remaining_concern": None,
                    "evidence_refs": [{"evidence_id": "s0.goal"}],
                }
            ],
        },
        ProgressCriticOutput,
    )
    context = ProgressContext(
        parent.prepared, str(critique["execution_id"]), str(correction["execution_id"])
    )
    progress = await validate_reflection_stage(prepared, resolver, progress_context=context)
    assert progress["status"] == "no_findings" and progress["progress"] == "accepted"
    stages = await content_store.execute_query("SELECT * FROM memory_validation_executions;")
    assert len(stages) == 4
    progress_row = next(row for row in stages if row["uuid"] == progress["execution_id"])
    assert origin in progress_row["dependency_ids"]
    assert correction["execution_id"] in progress_row["dependency_ids"]
    await save_raw_memory(
        replace(
            child,
            metadata={
                key: value for key, value in child.metadata.items() if key != "automatic_correction"
            },
        ),
        expected_revision=child.revision,
    )
    assert await _reflection_root("org", "owner", child.id, resolver) == parent.memory.id
    assert (
        len(await content_store.execute_query("SELECT * FROM memory_validation_executions;")) == 4
    )


async def test_packet_oversized_candidate_keeps_returned_usage_and_sends_no_critic(
    content_store, monkeypatch
):
    parent, resolver, _ = await create_candidate(
        content_store, monkeypatch, statement="Observed boundary " * 4000
    )
    reader = install(monkeypatch, {"findings": []})
    reader.extract_with_usage = AsyncMock(wraps=reader.extract_with_usage)
    with pytest.raises(ConsolidationInputBudgetExceeded):
        await validate_reflection_stage(parent, resolver)
    reader.extract_with_usage.assert_not_awaited()
    stages = await content_store.execute_query("SELECT * FROM memory_validation_executions;")
    assert len(stages) == 1 and stages[0]["state"] == "returned"
    assert json.loads(stages[0]["usage_json"])["requests"] == 1
    assert stages[0]["result_json"]


async def test_packet_candidate_write_failure_keeps_execution_and_replays_returned_output(
    content_store, monkeypatch
):
    from sibyl_core.services import validation_candidate

    insert = validation_candidate.insert_validation_candidate
    extract = Extractor.extract_with_usage
    sends = []

    async def tracked_extract(self, prompt):
        sends.append(prompt)
        return await extract(self, prompt)

    async def interrupted(*args):
        raise ValueError("candidate insert interrupted")

    monkeypatch.setattr(Extractor, "extract_with_usage", tracked_extract)
    monkeypatch.setattr(validation_candidate, "insert_validation_candidate", interrupted)
    with pytest.raises(ValueError, match="candidate insert interrupted") as failure:
        await create_candidate(content_store, monkeypatch)
    stages = await content_store.execute_query("SELECT * FROM memory_validation_executions;")
    assert len(stages) == 1 and stages[0]["state"] == "returned"
    assert failure.value.execution_id == stages[0]["uuid"]
    assert failure.value.execution_state == "returned"
    assert len(sends) == 1
    request = json.loads(stages[0]["request_json"])
    monkeypatch.setattr(validation_candidate, "insert_validation_candidate", insert)
    candidate, execution = await ordinary_cohort.propose_stored_cohort(
        "org",
        "owner",
        stages[0]["source_ids"],
        AsyncMock(return_value=SourceReadAuthority("owner")),
        authorize=AsyncMock(),
        packet_binding=request["evidence_packet"],
    )
    assert candidate is not None and execution == stages[0]["uuid"]
    assert len(sends) == 1


def test_packet_indivisible_exchange_is_explicit_preparation_failure():
    episode = _episode()
    response = episode["trace"][2]["payload"]
    response["raw"]["choices"][0]["message"]["content"] = "λ" * 80_000
    response["body_base64"], response["body_sha256"] = _encoded(response["raw"])
    with pytest.raises(ConsolidationInputBudgetExceeded):
        prepare_ordinary_packets(
            "source",
            canonical(episode).encode(),
            input_chars=lambda packet: len(packet.payload_json),
            max_input_chars=40_000,
        )
