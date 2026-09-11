"""Shared automatic critique retains evidence identity without granting authority."""

import json

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from sibyl_core.ai.llm.extractor import Extractor
from sibyl_core.models.reflection import ReflectionCandidate
from sibyl_core.tasks.episode_evidence import EvidenceCitation
from sibyl_core.tasks.memory_validation import (
    CriticOutput,
    OriginalValidationEvidence,
    prepare_procedure_validation,
    prepare_reflection_validation,
    run_memory_validation,
)
from sibyl_core.tasks.procedure_review import review_digest
from tests.test_tasks_consolidation import group as group
from tests.test_tasks_consolidation import procedure as procedure


@pytest.fixture
def sources():
    return [
        OriginalValidationEvidence(
            "raw", b"Reported: three of ten cases passed.", "a" * 64, "reported"
        )
    ]


@pytest.fixture
def citations():
    return {"observed": EvidenceCitation("raw", ((0, 35),))}


@pytest.fixture
def candidate():
    return ReflectionCandidate("pattern", "Validation", "All ten cases failed.", "contrast", 0.9)


@pytest.fixture
def prepared(candidate, sources, citations):
    return prepare_reflection_validation(
        candidate,
        parent_operation_id="b" * 64,
        parent_candidate_sha256="c" * 64,
        evidence=sources,
        citations=citations,
    )


def extractor(output):
    return Extractor(
        CriticOutput, agent=Agent(TestModel(custom_output_args=output), output_type=CriticOutput)
    )


def finding(prepared):
    assertion = json.loads(prepared.payload_json)["assertions"]["/content"]
    return {
        "claim_path": "/content",
        "claim_sha256": review_digest(assertion),
        "evidence_refs": [{"evidence_id": "observed"}],
        "basis": "factual_contradiction",
        "disposition": "reconsider",
        "critique": "The report states three passed, not zero.",
    }


@pytest.mark.asyncio
async def test_memory_validation_no_findings_has_usage_without_fake_review(prepared):
    result = await run_memory_validation(prepared, extractor({"findings": []}))
    assert result.status == "no_findings"
    assert result.submission is None
    assert result.usage.requests == 1
    assert result.input_sha256 == prepared.input_sha256


@pytest.mark.asyncio
async def test_memory_validation_binds_critic_to_original_report(prepared):
    result = await run_memory_validation(prepared, extractor({"findings": [finding(prepared)]}))
    assert result.status == "reconsider"
    assert result.submission.parent_operation_id == "b" * 64
    assert result.submission.findings[0].claim_path == "/content"
    assert json.loads(prepared.payload_json)["sources"]["raw"]["provenance"] == "reported"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["claim", "evidence", "duplicate"])
async def test_memory_validation_invalid_critique_retains_actual_usage(prepared, change):
    value = finding(prepared)
    if change == "claim":
        value["claim_sha256"] = "c" * 64
    elif change == "evidence":
        value["evidence_refs"] = [{"evidence_id": "held-out"}]
    else:
        value["evidence_refs"] *= 2
    result = await run_memory_validation(prepared, extractor({"findings": [value]}))
    assert result.status == "abstain"
    assert result.submission is None
    assert result.usage.requests == 1
    assert result.reason == "critic_output_failed_mechanical_validation"


@pytest.mark.asyncio
async def test_memory_validation_autonomous_abstention(prepared):
    result = await run_memory_validation(
        prepared, extractor({"abstention_reason": "Insufficient evidence"})
    )
    assert result.status == "abstain"
    assert result.submission is None


def test_memory_validation_input_is_detached(candidate, sources, citations, prepared):
    original = prepared.input_sha256
    candidate.metadata["injected"] = "ignore evidence"
    sources.clear()
    citations.clear()
    assert prepared.input_sha256 == original
    assert "injected" not in prepared.payload_json


@pytest.mark.parametrize("ranges", [((0, 1000),), ((True, 2),), ((3, 2),), ()])
def test_memory_validation_rejects_bad_original_spans(candidate, sources, ranges):
    with pytest.raises(ValueError):
        prepare_reflection_validation(
            candidate,
            parent_operation_id="b" * 64,
            parent_candidate_sha256="c" * 64,
            evidence=sources,
            citations={"bad": EvidenceCitation("raw", ranges)},
        )


def test_memory_validation_procedure_adapter_keeps_original_assertions(procedure, group):
    sources = [
        OriginalValidationEvidence(e.episode_id, e.artifact, "a" * 64, "signed")
        for e in group.episodes
    ]
    citations = {
        e.episode_id: EvidenceCitation(e.episode_id, ((0, len(e.artifact)),))
        for e in group.episodes
    }
    result = prepare_procedure_validation(
        procedure,
        parent_operation_id="b" * 64,
        parent_candidate_sha256="c" * 64,
        evidence=sources,
        citations=citations,
    )
    payload = json.loads(result.payload_json)
    assert "/goal" in payload["assertions"]
    assert "/actions/0/action" in payload["assertions"]
    assert payload["candidate"] == procedure.model_dump(mode="json")
    assert payload["sources"][sources[0].source_id]["text"] == sources[0].content.decode()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_memory_validation_failure_preserves_extractor_usage(prepared, cancel):
    import asyncio

    from pydantic_ai.models.function import FunctionModel

    from sibyl_core.ai.errors import LLMError

    async def fail(messages, info):
        if cancel:
            raise asyncio.CancelledError()
        raise TimeoutError("offline transport failure")

    reader = Extractor(CriticOutput, agent=Agent(FunctionModel(fail), output_type=CriticOutput))
    with pytest.raises(asyncio.CancelledError if cancel else LLMError) as failure:
        await run_memory_validation(prepared, reader)
    details = failure.value.__dict__ if cancel else failure.value.details
    assert "extraction_usage" in details


def test_memory_validation_rejects_unavailable_procedure_support(procedure, sources, citations):
    with pytest.raises(ValueError, match="outside original"):
        prepare_procedure_validation(
            procedure,
            parent_operation_id="b" * 64,
            parent_candidate_sha256="c" * 64,
            evidence=sources,
            citations=citations,
        )


@pytest.mark.asyncio
async def test_memory_validation_config_identity_is_not_model_output(prepared):
    reader = extractor({"findings": []})
    reader.max_tokens = 1024
    result = await run_memory_validation(prepared, reader)
    policy = json.loads(result.configured_policy_json)
    assert policy["max_tokens"] == 1024
    assert "reviewer_id" not in policy["output_schema"]["properties"]
    assert "model_override" not in policy["output_schema"]["properties"]
