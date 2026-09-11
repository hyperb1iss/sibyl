"""Critique can target assertions without becoming evidence itself."""

import copy

import pytest
from pydantic import ValidationError

from sibyl_core.tasks.episode_evidence import EvidenceCitation
from sibyl_core.tasks.procedure_review import (
    FindingAssessment,
    ReviewFinding,
    ReviewSubmission,
    assertion_index,
    resolve_review_findings,
    review_digest,
    validate_review_assessments,
)
from tests.test_tasks_consolidation import group as group
from tests.test_tasks_consolidation import procedure as procedure


@pytest.fixture
def citations():
    return {"source-one": EvidenceCitation(episode_id="success", ranges=((0, 4),))}


@pytest.fixture
def submission(procedure):
    assertion = assertion_index(procedure)["/goal"]
    finding = ReviewFinding(
        claim_path="/goal",
        claim_sha256=review_digest(assertion),
        evidence_refs=[{"evidence_id": "source-one"}],
        basis="unsupported_generalization",
        disposition="qualify",
        critique="The evidence supports a missing validation step, not a universal prohibition.",
    )
    return ReviewSubmission(
        parent_operation_id="a" * 64,
        parent_candidate_sha256="b" * 64,
        findings=[finding],
    )


def test_review_resolves_original_assertion_without_using_critique_as_evidence(
    submission, procedure, citations
):
    resolved = resolve_review_findings(submission, procedure, citations)
    assert resolved[0]["parent_assertion"] == assertion_index(procedure)["/goal"]
    assert resolved[0]["critique"] == submission.findings[0].critique
    assert resolved[0]["finding_id"] == submission.finding_ids()[0]
    assert "/actions/0/action" in assertion_index(procedure)
    assert "/actions" not in assertion_index(procedure)


@pytest.mark.parametrize("field,value", [("claim_path", "/actions"), ("claim_sha256", "0" * 64)])
def test_review_rejects_forged_assertion(submission, procedure, citations, field, value):
    data = submission.model_dump()
    data["findings"][0][field] = value
    with pytest.raises(ValueError, match="parent assertion"):
        resolve_review_findings(ReviewSubmission.model_validate(data), procedure, citations)


def test_review_rejects_heldout_or_unknown_reference(submission, procedure):
    with pytest.raises(ValueError, match="original evidence"):
        resolve_review_findings(submission, procedure, {})


def test_review_identity_is_stable_and_changes_with_critique(submission):
    same = ReviewSubmission.model_validate_json(submission.model_dump_json())
    assert same.submission_sha256 == submission.submission_sha256
    assert same.finding_ids() == submission.finding_ids()
    changed = submission.model_dump()
    changed["findings"][0]["critique"] += " Require a condition."
    assert (
        ReviewSubmission.model_validate(changed).submission_sha256 != submission.submission_sha256
    )
    with pytest.raises(ValidationError):
        ReviewSubmission.model_validate({**submission.model_dump(), "reviewer_id": "forged"})


@pytest.mark.parametrize("shape", ["missing", "duplicate", "extra", "wrong_id"])
def test_review_assessment_correspondence(submission, citations, shape):
    assessment = FindingAssessment(
        finding_id=submission.finding_ids()[0],
        disposition="accepted",
        explanation="Qualify the precondition using the original evidence.",
        evidence_refs=[{"evidence_id": "source-one"}],
    )
    validate_review_assessments(submission, [assessment], citations)
    extra = assessment.model_copy(update={"finding_id": "c" * 64})
    candidates = {
        "missing": [],
        "duplicate": [assessment, assessment],
        "extra": [assessment, extra],
        "wrong_id": [extra],
    }
    with pytest.raises(ValueError, match="exactly once"):
        validate_review_assessments(submission, candidates[shape], citations)


def test_review_rejection_requires_evidence_but_insufficient_can_abstain(submission, citations):
    data = {
        "finding_id": submission.finding_ids()[0],
        "disposition": "rejected",
        "explanation": "The critique is unsupported.",
        "evidence_refs": [],
    }
    with pytest.raises(ValueError, match="requires original evidence"):
        validate_review_assessments(submission, [FindingAssessment.model_validate(data)], citations)
    data["disposition"] = "insufficient_evidence"
    validate_review_assessments(submission, [FindingAssessment.model_validate(data)], citations)


def test_review_duplicate_evidence_is_rejected(submission, procedure, citations):
    changed = copy.deepcopy(submission.model_dump())
    changed["findings"][0]["evidence_refs"] *= 2
    with pytest.raises(ValueError, match="repeats"):
        resolve_review_findings(ReviewSubmission.model_validate(changed), procedure, citations)


def test_review_assessment_revalidates_mutated_submission(submission, citations):
    submission.findings.clear()
    with pytest.raises(ValidationError):
        validate_review_assessments(submission, [], citations)


def test_review_assessment_revalidates_unchecked_model_copy(submission, citations):
    assessment = FindingAssessment(
        finding_id=submission.finding_ids()[0],
        disposition="insufficient_evidence",
        explanation="No sufficient original evidence.",
        evidence_refs=[],
    ).model_copy(update={"disposition": "automatically_promote", "explanation": ""})
    with pytest.raises(ValidationError):
        validate_review_assessments(submission, [assessment], citations)
