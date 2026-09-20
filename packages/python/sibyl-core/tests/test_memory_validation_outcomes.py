"""Completed reviews, abstentions and citation failures retain distinct meanings."""

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
    prepare_reflection_validation,
    run_memory_validation,
)


@pytest.fixture
def prepared():
    content = b"The observed valve remained closed throughout the inspection."
    return prepare_reflection_validation(
        ReflectionCandidate(
            "fact", "Valve inspection", "The valve stayed closed during inspection.", "report", 0.5
        ),
        parent_operation_id="a" * 64,
        parent_candidate_sha256="b" * 64,
        evidence=[OriginalValidationEvidence("raw-inspection", content, "c" * 64, "signed")],
        citations={"inspection-span": EvidenceCitation("raw-inspection", ((0, len(content)),))},
    )


def extractor(output):
    return Extractor(
        CriticOutput,
        agent=Agent(TestModel(custom_output_args=output), output_type=CriticOutput),
        output_retries=0,
    )


@pytest.mark.asyncio
async def test_completed_review_has_no_abstention_or_synthetic_submission(prepared):
    result = await run_memory_validation(
        prepared, extractor({"findings": [], "abstention_reason": None})
    )
    assert result.status == "no_findings"
    assert result.reason is None
    assert result.submission is None
    assert result.usage.requests == 1
    assert result.input_sha256 == prepared.input_sha256


@pytest.mark.asyncio
async def test_unavailable_evidence_remains_an_explicit_abstention():
    content = b"The inspection image was unreadable; valve position could not be determined."
    prepared = prepare_reflection_validation(
        ReflectionCandidate("fact", "Valve inspection", "The valve was closed.", "report", 0.5),
        parent_operation_id="a" * 64,
        parent_candidate_sha256="b" * 64,
        evidence=[OriginalValidationEvidence("raw-inspection", content, "c" * 64, "signed")],
        citations={"inspection-span": EvidenceCitation("raw-inspection", ((0, len(content)),))},
    )
    reason = "The unreadable inspection provides no basis to assess valve position."
    result = await run_memory_validation(
        prepared, extractor({"findings": [], "abstention_reason": reason})
    )
    assert result.status == "abstain"
    assert result.reason == reason
    assert result.submission is None
    assert result.usage.requests == 1


@pytest.mark.asyncio
async def test_no_concern_text_in_abstention_field_is_not_automatically_repaired(prepared):
    reason = "No concerns were found."
    result = await run_memory_validation(
        prepared, extractor({"findings": [], "abstention_reason": reason})
    )
    assert result.status == "abstain"
    assert result.reason == reason
    assert result.submission is None
    assert result.usage.requests == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence_id", ["inspection-span", "raw-inspection", "unknown-span"])
async def test_only_citation_map_keys_can_support_findings(prepared, evidence_id):
    payload = json.loads(prepared.payload_json)
    finding = {
        "claim_path": "/content",
        "claim_sha256": payload["assertion_hashes"]["/content"],
        "evidence_refs": [{"evidence_id": evidence_id}],
        "basis": "missing_condition",
        "disposition": "qualify",
        "critique": "Keep the statement bounded to the inspected interval.",
    }
    result = await run_memory_validation(
        prepared, extractor({"findings": [finding], "abstention_reason": None})
    )
    assert result.usage.requests == 1
    if evidence_id == "inspection-span":
        assert result.status == "reconsider"
        assert result.reason is None
        assert result.submission is not None
        assert result.submission.findings[0].evidence_refs[0].evidence_id == evidence_id
    else:
        assert result.status == "abstain"
        assert result.reason == "critic_output_failed_mechanical_validation"
        assert result.submission is None
