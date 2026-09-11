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


@dataclass(frozen=True, kw_only=True)
class ProgressMemoryValidationResult(MemoryValidationResult):
    prior_assessments: tuple[PriorFindingAssessment, ...]
    progress: Literal["accepted", "advance", "no_progress", "abstain", "unresolved"]
    version: str = PROGRESS_VERSION


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
        raise ValueError("missing progress context")
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
        raise ValueError("incomplete progress context")
    if not isinstance(context["previous_assertions"], dict) or not isinstance(
        context["ancestor_candidate_digests"], list
    ):
        raise ValueError("invalid progress context shape")
    digest = context["previous_input_sha256"]
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise ValueError("invalid previous input identity")
    if (
        review_digest(context["previous_candidate"]) != context["previous_candidate_view_sha256"]
        or validation_assertion_index(payload["kind"], context["previous_candidate"])
        != context["previous_assertions"]
    ):
        raise ValueError("previous candidate view differs from assertions")
    if context["evidence_sha256"] != review_digest([payload["sources"], payload["citations"]]):
        raise ValueError("progress context evidence differs")
    for digest in context["ancestor_candidate_digests"]:
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise ValueError("invalid progress ancestor identity")
    review = ReviewSubmission.model_validate(context["review"])
    if (
        review.parent_operation_id != context["previous_parent_operation_id"]
        or review.parent_candidate_sha256 != context["previous_parent_candidate_sha256"]
        or review.finding_ids() != context["finding_ids"]
        or review_digest(context["previous_assertions"]) != context["previous_candidate_digest"]
    ):
        raise ValueError("progress context identity differs")
    for finding in review.findings:
        assertion = context["previous_assertions"].get(finding.claim_path)
        if assertion is None or review_digest(assertion) != finding.claim_sha256:
            raise ValueError("progress context assertion differs")
        refs = [ref.evidence_id for ref in finding.evidence_refs]
        if len(refs) != len(set(refs)) or any(ref not in payload["citations"] for ref in refs):
            raise ValueError("progress context citation differs")


def assess_progress(
    payload: dict[str, Any], output: ProgressCriticOutput, status: str
) -> ProgressDecision:
    """Derive a proposal outcome; mechanical completeness is not factual proof."""
    validate_progress_context(payload)
    context = payload["prior_progress"]
    expected = context["finding_ids"]
    actual = [item.finding_id for item in output.prior_assessments]
    if len(actual) != len(set(actual)) or set(actual) != set(expected):
        raise ValueError("progress assessments incomplete or duplicated")
    for assessment in output.prior_assessments:
        refs = [ref.evidence_id for ref in assessment.evidence_refs]
        if len(refs) != len(set(refs)) or any(ref not in payload["citations"] for ref in refs):
            raise ValueError("progress assessment evidence differs")
        improved = assessment.disposition in ("resolved", "partially_resolved")
        if improved != (assessment.supported_reduction is not None):
            raise ValueError("progress assessment reduction differs")
        if (assessment.disposition == "resolved") != (assessment.remaining_concern is None):
            raise ValueError("progress assessment remaining concern differs")
    if status == "abstain":
        return "abstain"
    if status == "no_findings":
        if any(item.disposition != "resolved" for item in output.prior_assessments):
            raise ValueError("accepted critic leaves prior concerns unresolved")
        return "accepted"
    current = semantic_candidate_digest(payload)
    if current in [context["previous_candidate_digest"], *context["ancestor_candidate_digests"]]:
        return "no_progress"
    if any(
        item.disposition in ("resolved", "partially_resolved") for item in output.prior_assessments
    ):
        return "advance"
    return "unresolved"
