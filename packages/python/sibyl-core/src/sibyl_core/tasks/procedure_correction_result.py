"""Lossless durable output of the existing procedure reconsideration owner."""

from dataclasses import dataclass
from typing import Literal

from sibyl_core.ai.llm.extractor import ExtractionUsage
from sibyl_core.tasks.consolidation import ConsolidationResult


@dataclass(frozen=True)
class ProcedureCorrectionResult:
    status: Literal["procedure_correction"]
    parent_operation_id: str
    parent_candidate_sha256: str
    review_execution_id: str
    result: ConsolidationResult
    usage: ExtractionUsage
