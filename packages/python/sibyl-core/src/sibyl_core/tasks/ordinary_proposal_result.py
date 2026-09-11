"""Durable output of a source-bound ordinary cohort proposal."""

from dataclasses import dataclass
from typing import Literal

from sibyl_core.ai.llm.extractor import ExtractionUsage
from sibyl_core.tasks.ordinary_proposals import PartialProposal


@dataclass(frozen=True)
class OrdinaryProposalResult:
    status: Literal["ordinary_cohort_proposal"]
    input_sha256: str
    proposal: PartialProposal
    usage: ExtractionUsage
    validation_error: str | None = None
