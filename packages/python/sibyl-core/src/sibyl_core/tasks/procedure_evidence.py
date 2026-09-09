"""Internal citation-ID output contract resolved before procedure validation."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sibyl_core.tasks.episode_evidence import EvidenceCitation

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


class EvidenceProposal(_StrictModel):
    procedure: EvidenceProcedure | None = None
    abstention_reason: _Text | None = None


def resolve_evidence_proposal(
    proposal: EvidenceProposal, citations: dict[str, EvidenceCitation]
) -> dict[str, Any]:
    """Expand only server-known IDs; never accept model-supplied byte offsets."""

    def resolve(value: Any) -> Any:
        if isinstance(value, list):
            return [resolve(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {key: resolve(item) for key, item in value.items() if key != "support"}
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

    return resolve(proposal.model_dump(mode="json"))
