"""Ordinary corrections assess criticism without treating it as evidence."""

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from sibyl_core.ai.llm.extractor import Extractor
from sibyl_core.tasks.procedure_review import ReviewSubmission
from sibyl_core.tasks.reflection_correction import (
    ReflectionCorrection,
    prepare_reflection_correction,
    reconsider_reflection,
)
from tests.test_memory_validation import candidate as candidate
from tests.test_memory_validation import citations as citations
from tests.test_memory_validation import finding
from tests.test_memory_validation import prepared as prepared
from tests.test_memory_validation import sources as sources


@pytest.mark.parametrize("mode", ["correct", "abstain", "incomplete", "unknown_reference"])
async def test_reflection_correction_retains_assessments_and_usage(prepared, mode):
    submission = ReviewSubmission(
        parent_operation_id="b" * 64, parent_candidate_sha256="c" * 64, findings=[finding(prepared)]
    )
    assessments = [
        {
            "finding_id": submission.finding_ids()[0],
            "disposition": "accepted",
            "explanation": "The original report supports three passed.",
            "evidence_refs": [{"evidence_id": "observed"}],
        }
    ]
    if mode == "incomplete":
        assessments = []
    if mode == "unknown_reference":
        assessments[0]["evidence_refs"] = [{"evidence_id": "forged"}]
    output = {
        "content": None if mode == "abstain" else "The report states three of ten cases passed.",
        "abstention_reason": "The report is insufficient." if mode == "abstain" else None,
        "assessments": assessments,
    }
    extractor = Extractor(
        ReflectionCorrection,
        agent=Agent(TestModel(custom_output_args=output), output_type=ReflectionCorrection),
    )
    result = await reconsider_reflection(prepared, submission, extractor)
    assert result.status == ("corrected" if mode == "correct" else "abstain")
    assert result.usage.requests == 1
    assert len(result.assessments) == len(assessments)
    assert result.submission == submission


def test_reflection_correction_wrong_parent_precedes_provider(prepared):
    submission = ReviewSubmission(
        parent_operation_id="d" * 64, parent_candidate_sha256="c" * 64, findings=[finding(prepared)]
    )
    with pytest.raises(ValueError, match="parent identity"):
        prepare_reflection_correction(prepared, submission)


async def test_reflection_correction_detaches_submission_during_call(prepared):
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from sibyl_core.tasks.procedure_review import ReviewFinding

    submission = ReviewSubmission(
        parent_operation_id="b" * 64, parent_candidate_sha256="c" * 64, findings=[finding(prepared)]
    )
    identity = submission.submission_sha256
    fid = submission.finding_ids()[0]

    async def respond(messages, info):
        submission.findings.append(ReviewFinding.model_validate(finding(prepared)))
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "content": "Three of ten passed.",
                        "abstention_reason": None,
                        "assessments": [
                            {
                                "finding_id": fid,
                                "disposition": "accepted",
                                "explanation": "Original count.",
                                "evidence_refs": [{"evidence_id": "observed"}],
                            }
                        ],
                    },
                )
            ]
        )

    extractor = Extractor(
        ReflectionCorrection, agent=Agent(FunctionModel(respond), output_type=ReflectionCorrection)
    )
    result = await reconsider_reflection(prepared, submission, extractor)
    assert result.status == "corrected"
    assert result.submission.submission_sha256 == identity
    assert len(submission.findings) == 2 and len(result.submission.findings) == 1


async def test_reflection_correction_post_return_failure_retains_usage(prepared, monkeypatch):
    from sibyl_core.tasks import reflection_correction as correction

    submission = ReviewSubmission(
        parent_operation_id="b" * 64, parent_candidate_sha256="c" * 64, findings=[finding(prepared)]
    )
    extractor = Extractor(
        ReflectionCorrection,
        agent=Agent(
            TestModel(
                custom_output_args={
                    "content": None,
                    "abstention_reason": "Evidence insufficient.",
                    "assessments": [],
                }
            ),
            output_type=ReflectionCorrection,
        ),
    )
    error = RuntimeError("assessment consumer failed")

    def fail(*args):
        raise error

    monkeypatch.setattr(correction, "validate_review_assessments", fail)
    with pytest.raises(RuntimeError) as caught:
        await reconsider_reflection(prepared, submission, extractor)
    assert caught.value is error
    assert error.extraction_usage.requests == 1
