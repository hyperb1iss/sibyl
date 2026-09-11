"""Bind progress context to an existing scoped immutable critic receipt."""

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from sibyl_core.services.validation_result_codec import decode_validation_result
from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.memory_progress import PROGRESS_VERSION, validate_progress_context
from sibyl_core.tasks.memory_validation import MemoryValidationResult, PreparedMemoryValidation
from sibyl_core.tasks.procedure_review import review_digest


class ProgressHistoryBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    execution_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def validate_history_row(
    row: dict[str, Any], binding: ProgressHistoryBinding, org: str, principal: str
) -> MemoryValidationResult:
    if (
        row.get("uuid") != binding.execution_id
        or row.get("request_sha256") != binding.request_sha256
        or binding.execution_id != binding.request_sha256
        or row.get("organization_id") != org
        or row.get("principal_id") != principal
        or row.get("state") != "returned"
        or row.get("purged") is not False
        or not isinstance(row.get("request_json"), str)
        or not isinstance(row.get("result_json"), str)
    ):
        raise ValueError("Progress prior execution is unavailable")
    request = json.loads(row["request_json"])
    if (
        not isinstance(request, dict)
        or canonical(request) != row["request_json"]
        or review_digest(request) != binding.execution_id
        or request.get("org") != org
        or request.get("principal") != principal
        or request.get("parent") != row.get("parent_id")
        or request.get("policy") != row.get("policy_json")
        or request.get("input") != binding.input_sha256
        or hashlib.sha256(row["result_json"].encode()).hexdigest() != binding.result_sha256
    ):
        raise ValueError("Progress prior request or result identity differs")
    result = decode_validation_result(json.loads(row["result_json"]))
    if (
        not isinstance(result, MemoryValidationResult)
        or result.status != "reconsider"
        or result.submission is None
        or result.input_sha256 != binding.input_sha256
        or review_digest(result.submission.model_dump(mode="json")) != binding.review_sha256
    ):
        raise ValueError("Progress prior critique differs")
    return result


def bind_progress_history(
    prepared: PreparedMemoryValidation,
    row: dict[str, Any],
    org: str,
    principal: str,
    *,
    previous: PreparedMemoryValidation,
) -> ProgressHistoryBinding:
    """Compare actual historical input and submission with detached prompt context."""
    payload = json.loads(prepared.payload_json)
    if payload.get("version") != PROGRESS_VERSION:
        raise ValueError("Progress history requires the explicit contract")
    validate_progress_context(payload)
    context = payload["prior_progress"]
    before = json.loads(previous.payload_json)
    if (
        previous.input_sha256 != context["previous_input_sha256"]
        or before["candidate"] != context["previous_candidate"]
        or before["assertions"] != context["previous_assertions"]
        or before["parent_operation_id"] != context["previous_parent_operation_id"]
        or before["parent_candidate_sha256"] != context["previous_parent_candidate_sha256"]
        or before["sources"] != payload["sources"]
        or before["citations"] != payload["citations"]
        or before["kind"] != payload["kind"]
    ):
        raise ValueError("Resolved prior prompt differs from progress context")
    encoded = row.get("result_json")
    if not isinstance(encoded, str):
        raise ValueError("Progress prior result is unavailable")
    binding = ProgressHistoryBinding(
        execution_id=row["uuid"],
        request_sha256=row["request_sha256"],
        result_sha256=hashlib.sha256(encoded.encode()).hexdigest(),
        input_sha256=context["previous_input_sha256"],
        review_sha256=review_digest(context["review"]),
    )
    validate_history_row(row, binding, org, principal)
    return binding


def progress_history_guard(binding: ProgressHistoryBinding, org: str, principal: str) -> str:
    """Fence an exact prior receipt on the existing execution witness field."""
    return f"""
        LET $progress_prior = (SELECT * FROM memory_validation_executions
            WHERE uuid = {canonical(binding.execution_id)}
                AND organization_id = {canonical(org)}
                AND principal_id = {canonical(principal)} LIMIT 1)[0];
        IF $progress_prior = NONE OR $progress_prior.state != 'returned'
            OR $progress_prior.purged != false
            OR $progress_prior.request_sha256 != {canonical(binding.request_sha256)}
            OR crypto::sha256($progress_prior.request_json) != {canonical(binding.request_sha256)}
            OR crypto::sha256($progress_prior.result_json) != {canonical(binding.result_sha256)} {{
            THROW 'Progress prior receipt changed';
        }};
        UPDATE memory_validation_executions
            SET promotion_write_witness=(promotion_write_witness ?? 0)+1
            WHERE uuid={canonical(binding.execution_id)}
                AND organization_id={canonical(org)} AND principal_id={canonical(principal)};
    """


def validate_progress_assessments(result: Any, prior: MemoryValidationResult) -> None:
    from sibyl_core.tasks.memory_progress import ProgressMemoryValidationResult

    if not isinstance(result, ProgressMemoryValidationResult) or prior.submission is None:
        raise ValueError("Progress assessment history is unavailable")
    expected = set(prior.submission.finding_ids())
    actual = {assessment.finding_id for assessment in result.prior_assessments}
    mechanical_abstention = (
        result.status == "abstain"
        and result.reason == "critic_output_failed_mechanical_validation"
        and not result.prior_assessments
    )
    if actual != expected and not mechanical_abstention:
        raise ValueError("Progress prior assessment inventory differs")
