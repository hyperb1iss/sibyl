"""Source-bound review inputs and complete model assessments.

These contracts resolve critique against an already authorized proposal. They do
not establish source access, factual entailment, or permission to publish.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.procedure_evidence import EvidenceRef

if TYPE_CHECKING:
    from sibyl_core.tasks.consolidation import DraftConditionalProcedure
    from sibyl_core.tasks.episode_evidence import EvidenceCitation

REVIEW_VERSION = "sibyl-procedure-review-v1"
_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
_Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def review_digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ReviewFinding(_StrictModel):
    claim_path: _Text
    claim_sha256: _Digest
    evidence_refs: list[EvidenceRef] = Field(min_length=1)
    basis: Literal[
        "factual_contradiction",
        "unsupported_generalization",
        "unsupported_causality",
        "missing_condition",
        "misleading_certainty",
    ]
    disposition: Literal["reconsider", "qualify", "relabel_inference", "remove", "abstain"]
    critique: _Text


class ReviewSubmission(_StrictModel):
    parent_operation_id: _Digest
    parent_candidate_sha256: _Digest
    prior_review_id: _Digest | None = None
    findings: list[ReviewFinding] = Field(min_length=1)

    @property
    def submission_sha256(self) -> str:
        """Hash submitted content independently of server time and receipt identity."""
        return review_digest([REVIEW_VERSION, self.model_dump(mode="json")])

    def finding_ids(self) -> list[str]:
        submission_digest = self.submission_sha256
        return [review_digest([submission_digest, index]) for index in range(len(self.findings))]


class FindingAssessment(_StrictModel):
    finding_id: _Digest
    disposition: Literal["accepted", "partially_accepted", "rejected", "insufficient_evidence"]
    explanation: _Text
    evidence_refs: list[EvidenceRef]


def assertion_index(procedure: DraftConditionalProcedure) -> dict[str, dict[str, object]]:
    """Return only assertion pointers, excluding containers and arbitrary metadata."""
    assertions: dict[str, dict[str, object]] = {}

    def visit(value: object, path: str) -> None:
        if isinstance(value, dict):
            if set(value) == {"statement", "label", "support"}:
                assertions[path] = value
            else:
                for key, item in value.items():
                    escaped = key.replace("~", "~0").replace("/", "~1")
                    visit(item, f"{path}/{escaped}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{path}/{index}")

    visit(procedure.model_dump(mode="json"), "")
    return assertions


def _validate_references(refs: list[EvidenceRef], citations: dict[str, EvidenceCitation]) -> None:
    ids = [reference.evidence_id for reference in refs]
    if len(ids) != len(set(ids)):
        raise ValueError("review repeats an evidence reference")
    if any(identifier not in citations or not citations[identifier].ranges for identifier in ids):
        raise ValueError("review cites unknown or empty original evidence")


def resolve_review_findings(
    submission: ReviewSubmission,
    procedure: DraftConditionalProcedure,
    citations: dict[str, EvidenceCitation],
) -> list[dict[str, object]]:
    """Resolve assertion text server-side; critique never replaces source evidence.

    Callers must validate parent identity and current source authority before
    passing the original procedure and its evidence map.
    """
    snapshot = ReviewSubmission.model_validate(submission.model_dump())
    assertions = assertion_index(procedure)
    resolved: list[dict[str, object]] = []
    for finding_id, finding in zip(snapshot.finding_ids(), snapshot.findings, strict=True):
        assertion = assertions.get(finding.claim_path)
        if assertion is None or review_digest(assertion) != finding.claim_sha256:
            raise ValueError("review claim does not match the parent assertion")
        _validate_references(finding.evidence_refs, citations)
        resolved.append(
            {
                "finding_id": finding_id,
                **finding.model_dump(mode="json"),
                "parent_assertion": assertion,
            }
        )
    return resolved


def validate_review_assessments(
    submission: ReviewSubmission,
    assessments: list[FindingAssessment],
    citations: dict[str, EvidenceCitation],
) -> None:
    """Require one source-bound assessment per finding, including abstentions."""
    snapshot = ReviewSubmission.model_validate(submission.model_dump())
    checked = [FindingAssessment.model_validate(item.model_dump()) for item in assessments]
    observed = [assessment.finding_id for assessment in checked]
    if len(observed) != len(set(observed)) or set(observed) != set(snapshot.finding_ids()):
        raise ValueError("assessments must cover each review finding exactly once")
    for assessment in checked:
        if not assessment.evidence_refs and assessment.disposition != "insufficient_evidence":
            raise ValueError("an assessed finding requires original evidence")
        _validate_references(assessment.evidence_refs, citations)
