"""Internal citation-ID output contract resolved before procedure validation."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sibyl_core.tasks.episode_evidence import EvidenceCitation

EVIDENCE_PROPOSAL_VERSION = "sibyl-evidence-proposal-v3"

_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class EvidenceRef(_StrictModel):
    evidence_id: _Text


class EvidenceAssertion(_StrictModel):
    statement: _Text
    label: Literal["observed", "inferred"]
    support: list[EvidenceRef] = Field(min_length=1)


class EvidenceAction(_StrictModel):
    order: int = Field(ge=1)
    action: EvidenceAssertion
    success_criteria: EvidenceAssertion


class EvidenceProcedure(_StrictModel):
    goal: EvidenceAssertion
    environment: list[EvidenceAssertion] = Field(min_length=1)
    preconditions: list[EvidenceAssertion] = Field(min_length=1)
    required_tools: list[EvidenceAssertion] = Field(default_factory=list)
    actions: list[EvidenceAction] = Field(min_length=1)
    expected_result: EvidenceAssertion
    failure_modes: list[EvidenceAssertion] = Field(min_length=1)
    abstain_when: list[EvidenceAssertion] = Field(min_length=1)


class EvidenceProcedureOutcome(_StrictModel):
    kind: Literal["procedure"]
    procedure: EvidenceProcedure


class EvidenceAbstentionOutcome(_StrictModel):
    kind: Literal["abstention"]
    reason: _Text


class EvidenceProposal(_StrictModel):
    # Literal tags make the branches exclusive without a provider-specific
    # discriminator keyword or a root-level union in the native JSON schema.
    outcome: EvidenceProcedureOutcome | EvidenceAbstentionOutcome


def resolve_evidence_assertion(
    assertion: EvidenceAssertion, citations: dict[str, EvidenceCitation]
) -> dict[str, Any]:
    """Resolve a typed assertion through the same citation owner as proposals."""
    checked = EvidenceAssertion.model_validate(assertion.model_dump())
    return _resolve_evidence_value(checked.model_dump(mode="json"), citations)


def _resolve_evidence_value(value: Any, citations: dict[str, EvidenceCitation]) -> Any:
    if isinstance(value, list):
        return [_resolve_evidence_value(item, citations) for item in value]
    if not isinstance(value, dict):
        return value
    result = {
        key: _resolve_evidence_value(item, citations)
        for key, item in value.items()
        if key != "support"
    }
    if "support" in value:
        support = []
        for reference in value["support"]:
            citation = citations.get(reference["evidence_id"])
            if citation is None or not citation.ranges:
                raise ValueError("proposal cites an unknown or empty evidence ID")
            support.extend(
                {"episode_id": citation.episode_id, "start_byte": start, "end_byte": end}
                for start, end in citation.ranges
            )
        result["support"] = support
    return result


def resolve_evidence_proposal(
    proposal: EvidenceProposal, citations: dict[str, EvidenceCitation]
) -> dict[str, Any]:
    """Expand only server-known IDs; never accept model-supplied byte offsets."""

    if isinstance(proposal.outcome, EvidenceAbstentionOutcome):
        return {"procedure": None, "abstention_reason": proposal.outcome.reason}
    return {
        "procedure": _resolve_evidence_value(
            proposal.outcome.procedure.model_dump(mode="json"), citations
        ),
        "abstention_reason": None,
    }
