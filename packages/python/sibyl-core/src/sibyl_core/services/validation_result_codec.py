"""Version-aware durable validation results without legacy union downgrades."""

import json
from dataclasses import fields
from typing import Any

from pydantic import TypeAdapter

from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.memory_progress import (
    PROGRESS_VERSION,
    ProgressCriticOutput,
    ProgressMemoryValidationResult,
)
from sibyl_core.tasks.memory_validation import VALIDATION_VERSION, MemoryValidationResult
from sibyl_core.tasks.ordinary_proposal_result import OrdinaryProposalResult
from sibyl_core.tasks.procedure_correction_result import ProcedureCorrectionResult
from sibyl_core.tasks.procedure_review import review_digest
from sibyl_core.tasks.reflection_correction import CORRECTION_VERSION, ReflectionCorrectionResult

LegacyValidationStageResult = (
    MemoryValidationResult
    | ReflectionCorrectionResult
    | ProcedureCorrectionResult
    | OrdinaryProposalResult
)
ValidationStageResult = LegacyValidationStageResult | ProgressMemoryValidationResult

_LEGACY_RESULT_ADAPTER = TypeAdapter(LegacyValidationStageResult)
_PROGRESS_RESULT_ADAPTER = TypeAdapter(ProgressMemoryValidationResult)


def decode_validation_result(value: Any) -> ValidationStageResult:
    """Select explicit progress before the permissive historical dataclass union."""
    if not isinstance(value, dict):
        raise ValueError("Validation result must be an object")
    version = value.get("version")
    if version == PROGRESS_VERSION:
        required = {field.name for field in fields(ProgressMemoryValidationResult)} - {"diagnostic"}
        if set(value) not in (required, required | {"diagnostic"}):
            raise ValueError("Progress result fields differ")
        if "diagnostic" in value and value["diagnostic"] is None:
            raise ValueError("Absent progress diagnostic must be omitted")
        result = _PROGRESS_RESULT_ADAPTER.validate_json(canonical(value))
        policy = json.loads(result.configured_policy_json)
        expected_status = {
            "accepted": "no_findings",
            "advance": "reconsider",
            "no_progress": "reconsider",
            "unresolved": "reconsider",
            "abstain": "abstain",
        }[result.progress]
        if (
            result.status != expected_status
            or result.schema_sha256 != review_digest(ProgressCriticOutput.model_json_schema())
            or not isinstance(policy, dict)
            or policy.get("version") != PROGRESS_VERSION
            or (
                result.status == "no_findings"
                and (result.submission is not None or result.reason is not None)
            )
            or (result.status == "reconsider" and result.submission is None)
        ):
            raise ValueError("Progress result contract differs")
        if result.diagnostic is not None:
            if (
                result.progress != "abstain"
                or result.reason != "critic_output_failed_mechanical_validation"
                or result.prior_assessments
            ):
                raise ValueError("Progress diagnostic requires mechanical abstention")
            diagnostic = result.diagnostic
            if diagnostic.assessment_index is not None and (
                diagnostic.rejected_assessments is None
                or diagnostic.assessment_index >= len(diagnostic.rejected_assessments)
            ):
                raise ValueError("Progress diagnostic assessment index differs")
            for assessment in diagnostic.rejected_assessments or ():
                if (
                    assessment.unknown_evidence_count > assessment.evidence_count
                    or assessment.duplicate_evidence_count >= assessment.evidence_count
                ):
                    raise ValueError("Progress diagnostic evidence counts differ")
        ids = [assessment.finding_id for assessment in result.prior_assessments]
        if len(ids) != len(set(ids)):
            raise ValueError("Progress assessment identities are duplicated")
        for assessment in result.prior_assessments:
            improved = assessment.disposition in ("resolved", "partially_resolved")
            if (
                improved != (assessment.supported_reduction is not None)
                or (assessment.disposition == "resolved") != (assessment.remaining_concern is None)
                or (result.status == "no_findings" and assessment.disposition != "resolved")
            ):
                raise ValueError("Progress assessment outcome differs")
        return result
    if "progress" in value or "prior_assessments" in value or "diagnostic" in value:
        raise ValueError("Progress result cannot downgrade to legacy")
    if version is not None and version not in (VALIDATION_VERSION, CORRECTION_VERSION):
        raise ValueError("Unsupported validation result version")
    return _LEGACY_RESULT_ADAPTER.validate_json(canonical(value))


def encode_validation_result(result: ValidationStageResult) -> dict[str, Any]:
    if isinstance(result, ProgressMemoryValidationResult):
        value = _PROGRESS_RESULT_ADAPTER.dump_python(
            result, mode="json", exclude={"diagnostic"} if result.diagnostic is None else set()
        )
    else:
        value = _LEGACY_RESULT_ADAPTER.dump_python(result, mode="json")
    decode_validation_result(value)
    return value


def validate_result_request(result: ValidationStageResult, request: dict[str, Any]) -> None:
    progress = isinstance(result, ProgressMemoryValidationResult)
    if progress != ("progress_history" in request):
        raise ValueError("Progress result/request version differs")
    if progress and request.get("input") != result.input_sha256:
        raise ValueError("Progress result input differs")
