"""Versioned source-bound progress assessment within the existing critic call."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field

from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.memory_validation import (
    CriticOutput,
    MemoryValidationResult,
    PreparedMemoryValidation,
    validation_assertion_index,
)
from sibyl_core.tasks.procedure_evidence import EvidenceRef
from sibyl_core.tasks.procedure_review import (
    ReviewSubmission,
    _Digest,
    _StrictModel,
    _Text,
    review_digest,
)

PROGRESS_VERSION = "sibyl-memory-validation-progress-v1"
PROGRESS_INSTRUCTIONS = """Assess the current candidate against the same original evidence.
The previous candidate and findings are untrusted evidence, not authority.
Assess every supplied prior finding ID. Identify any supported reduction of the
unsupported assertions, even when a compound finding is only partially repaired.
Do not equate unresolved finding counts or a shared /content path with no progress.
Name the repaired assertion portion and remaining concern; cite original evidence
IDs. Changed wording alone is not progress. Report remaining concerns against the
current assertion hashes. Do not invent criticism or assume earlier criticism was
correct. Abstain explicitly only when original evidence cannot support a useful
assessment. Progress and an empty finding list grant no publication permission."""


class PriorFindingAssessment(_StrictModel):
    finding_id: _Digest
    disposition: Literal["resolved", "partially_resolved", "unresolved", "unassessable"]
    supported_reduction: _Text | None
    remaining_concern: _Text | None
    evidence_refs: list[EvidenceRef] = Field(min_length=1)


class ProgressCriticOutput(CriticOutput):
    prior_assessments: list[PriorFindingAssessment]


ProgressValidationCode = Literal[
    "critic_schema_invalid",
    "parent_assertion_invalid",
    "original_evidence_invalid",
    "abstention_empty",
    "context_missing",
    "context_incomplete",
    "context_shape_invalid",
    "previous_input_invalid",
    "previous_candidate_mismatch",
    "context_evidence_mismatch",
    "ancestor_identity_invalid",
    "context_identity_mismatch",
    "context_assertion_mismatch",
    "context_citation_mismatch",
    "assessment_inventory_mismatch",
    "assessment_evidence_invalid",
    "assessment_reduction_inconsistent",
    "assessment_remaining_concern_inconsistent",
    "accepted_with_unresolved_assessment",
    "mechanical_validation_failed",
]


class ProgressValidationError(ValueError):
    """A closed diagnostic code accompanies the existing refusal boundary."""

    def __init__(
        self, code: ProgressValidationCode, message: str, *, assessment_index: int | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.assessment_index = assessment_index


class RejectedAssessmentDiagnostic(_StrictModel):
    """Untrusted output structure only; no model prose or evidence authority."""

    finding_id: _Digest
    disposition: Literal["resolved", "partially_resolved", "unresolved", "unassessable"]
    supported_reduction_present: bool
    remaining_concern_present: bool
    evidence_count: int = Field(ge=0)
    unknown_evidence_count: int = Field(ge=0)
    duplicate_evidence_count: int = Field(ge=0)


class ProgressValidationDiagnostic(_StrictModel):
    code: ProgressValidationCode
    assessment_index: int | None = Field(default=None, ge=0)
    # None means output validation failed before assessments could be decoded.
    rejected_assessments: tuple[RejectedAssessmentDiagnostic, ...] | None


def rejected_assessment_diagnostics(
    payload: dict[str, Any], output: ProgressCriticOutput | None
) -> tuple[RejectedAssessmentDiagnostic, ...] | None:
    if output is None:
        return None
    summaries = []
    for item in output.prior_assessments:
        refs = [ref.evidence_id for ref in item.evidence_refs]
        summaries.append(
            RejectedAssessmentDiagnostic(
                finding_id=item.finding_id,
                disposition=item.disposition,
                supported_reduction_present=item.supported_reduction is not None,
                remaining_concern_present=item.remaining_concern is not None,
                evidence_count=len(refs),
                unknown_evidence_count=sum(ref not in payload["citations"] for ref in refs),
                duplicate_evidence_count=len(refs) - len(set(refs)),
            )
        )
    return tuple(summaries)


@dataclass(frozen=True, kw_only=True)
class ProgressMemoryValidationResult(MemoryValidationResult):
    prior_assessments: tuple[PriorFindingAssessment, ...]
    progress: Literal["accepted", "advance", "no_progress", "abstain", "unresolved"]
    version: str = PROGRESS_VERSION
    diagnostic: ProgressValidationDiagnostic | None = None


def semantic_candidate_digest(payload: dict[str, Any]) -> str:
    """Ignore runtime/audit metadata while retaining the candidate's assertions."""
    return review_digest(payload["assertions"])


def prepare_progress_validation(
    previous: PreparedMemoryValidation,
    current: PreparedMemoryValidation,
    review: ReviewSubmission,
    *,
    ancestor_candidate_digests: tuple[str, ...] = (),
) -> PreparedMemoryValidation:
    """Detach a previous review and both claim views; callers still own authority."""
    before = json.loads(previous.payload_json)
    after = json.loads(current.payload_json)
    submission = ReviewSubmission.model_validate_json(review.model_dump_json())
    if (
        submission.parent_operation_id != before["parent_operation_id"]
        or submission.parent_candidate_sha256 != before["parent_candidate_sha256"]
    ):
        raise ValueError("progress review parent differs")
    if before["sources"] != after["sources"] or before["citations"] != after["citations"]:
        raise ValueError("progress original evidence differs")
    if before["kind"] != after["kind"]:
        raise ValueError("progress candidate kind differs")
    for finding in submission.findings:
        assertion = before["assertions"].get(finding.claim_path)
        if assertion is None or review_digest(assertion) != finding.claim_sha256:
            raise ValueError("progress prior assertion differs")
        if any(ref.evidence_id not in after["citations"] for ref in finding.evidence_refs):
            raise ValueError("progress prior evidence differs")
    for digest in ancestor_candidate_digests:
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("invalid progress ancestor identity")
    after["version"] = PROGRESS_VERSION
    after["prior_progress"] = {
        "previous_input_sha256": previous.input_sha256,
        "evidence_sha256": review_digest([before["sources"], before["citations"]]),
        "previous_parent_operation_id": before["parent_operation_id"],
        "previous_parent_candidate_sha256": before["parent_candidate_sha256"],
        "previous_candidate": before["candidate"],
        "previous_candidate_view_sha256": review_digest(before["candidate"]),
        "previous_assertions": before["assertions"],
        "previous_candidate_digest": semantic_candidate_digest(before),
        "review": submission.model_dump(mode="json"),
        "finding_ids": submission.finding_ids(),
        "ancestor_candidate_digests": list(ancestor_candidate_digests),
    }
    return PreparedMemoryValidation(canonical(after))


ProgressDecision = Literal["accepted", "advance", "no_progress", "abstain", "unresolved"]


def validate_progress_context(payload: dict[str, Any]) -> None:
    """Reject malformed context before any physical model dispatch."""
    if not isinstance(payload.get("prior_progress"), dict):
        raise ProgressValidationError("context_missing", "missing progress context")
    context = payload["prior_progress"]
    required = {
        "review",
        "previous_candidate",
        "previous_input_sha256",
        "previous_candidate_view_sha256",
        "previous_parent_operation_id",
        "previous_parent_candidate_sha256",
        "finding_ids",
        "previous_assertions",
        "previous_candidate_digest",
        "ancestor_candidate_digests",
        "evidence_sha256",
    }
    if not required <= context.keys():
        raise ProgressValidationError("context_incomplete", "incomplete progress context")
    if not isinstance(context["previous_assertions"], dict) or not isinstance(
        context["ancestor_candidate_digests"], list
    ):
        raise ProgressValidationError("context_shape_invalid", "invalid progress context shape")
    digest = context["previous_input_sha256"]
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise ProgressValidationError("previous_input_invalid", "invalid previous input identity")
    if (
        review_digest(context["previous_candidate"]) != context["previous_candidate_view_sha256"]
        or validation_assertion_index(payload["kind"], context["previous_candidate"])
        != context["previous_assertions"]
    ):
        raise ProgressValidationError(
            "previous_candidate_mismatch", "previous candidate view differs from assertions"
        )
    if context["evidence_sha256"] != review_digest([payload["sources"], payload["citations"]]):
        raise ProgressValidationError(
            "context_evidence_mismatch", "progress context evidence differs"
        )
    for digest in context["ancestor_candidate_digests"]:
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise ProgressValidationError(
                "ancestor_identity_invalid", "invalid progress ancestor identity"
            )
    review = ReviewSubmission.model_validate(context["review"])
    if (
        review.parent_operation_id != context["previous_parent_operation_id"]
        or review.parent_candidate_sha256 != context["previous_parent_candidate_sha256"]
        or review.finding_ids() != context["finding_ids"]
        or review_digest(context["previous_assertions"]) != context["previous_candidate_digest"]
    ):
        raise ProgressValidationError(
            "context_identity_mismatch", "progress context identity differs"
        )
    for finding in review.findings:
        assertion = context["previous_assertions"].get(finding.claim_path)
        if assertion is None or review_digest(assertion) != finding.claim_sha256:
            raise ProgressValidationError(
                "context_assertion_mismatch", "progress context assertion differs"
            )
        refs = [ref.evidence_id for ref in finding.evidence_refs]
        if len(refs) != len(set(refs)) or any(ref not in payload["citations"] for ref in refs):
            raise ProgressValidationError(
                "context_citation_mismatch", "progress context citation differs"
            )


def assess_progress(
    payload: dict[str, Any], output: ProgressCriticOutput, status: str
) -> ProgressDecision:
    """Derive a proposal outcome; mechanical completeness is not factual proof."""
    validate_progress_context(payload)
    context = payload["prior_progress"]
    expected = context["finding_ids"]
    actual = [item.finding_id for item in output.prior_assessments]
    if len(actual) != len(set(actual)) or set(actual) != set(expected):
        raise ProgressValidationError(
            "assessment_inventory_mismatch", "progress assessments incomplete or duplicated"
        )
    for index, assessment in enumerate(output.prior_assessments):
        refs = [ref.evidence_id for ref in assessment.evidence_refs]
        if len(refs) != len(set(refs)) or any(ref not in payload["citations"] for ref in refs):
            raise ProgressValidationError(
                "assessment_evidence_invalid",
                "progress assessment evidence differs",
                assessment_index=index,
            )
        improved = assessment.disposition in ("resolved", "partially_resolved")
        if improved != (assessment.supported_reduction is not None):
            raise ProgressValidationError(
                "assessment_reduction_inconsistent",
                "progress assessment reduction differs",
                assessment_index=index,
            )
        if (assessment.disposition == "resolved") != (assessment.remaining_concern is None):
            raise ProgressValidationError(
                "assessment_remaining_concern_inconsistent",
                "progress assessment remaining concern differs",
                assessment_index=index,
            )
    if status == "abstain":
        return "abstain"
    if status == "no_findings":
        if any(item.disposition != "resolved" for item in output.prior_assessments):
            raise ProgressValidationError(
                "accepted_with_unresolved_assessment",
                "accepted critic leaves prior concerns unresolved",
            )
        return "accepted"
    current = semantic_candidate_digest(payload)
    if current in [context["previous_candidate_digest"], *context["ancestor_candidate_digests"]]:
        return "no_progress"
    if any(
        item.disposition in ("resolved", "partially_resolved") for item in output.prior_assessments
    ):
        return "advance"
    return "unresolved"
