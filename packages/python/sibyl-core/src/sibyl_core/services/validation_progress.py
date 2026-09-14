"""Prepare a progress critic from a verified correction frontier."""

import json
from dataclasses import dataclass
from typing import Any

from sibyl_core.services.validation_dependencies import (
    dependency_reference,
    merge_dependency_references,
    resolve_dependencies,
)
from sibyl_core.services.validation_execution import (
    ValidationExecution,
    ValidationExecutionUnavailable,
)
from sibyl_core.services.validation_progress_history import bind_progress_history
from sibyl_core.services.validation_result_codec import decode_validation_result
from sibyl_core.tasks.memory_progress import prepare_progress_validation
from sibyl_core.tasks.memory_validation import MemoryValidationResult, PreparedMemoryValidation
from sibyl_core.tasks.procedure_correction_result import ProcedureCorrectionResult
from sibyl_core.tasks.procedure_review import review_digest
from sibyl_core.tasks.reflection_correction import ReflectionCorrectionResult


async def correction_request(
    request: dict[str, Any], prior: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    """Keep an existing physical operation's identity, including unknown attempts."""
    references = merge_dependency_references(
        request.get("execution_dependencies", []),
        [dependency_reference(prior).model_dump(mode="json")],
    )
    dependencies = {"execution_dependencies": references}
    extended = {**request, **dependencies}
    original = ValidationExecution(review_digest(request), request["org"], request["principal"])
    row = await original.load()
    if row is None:
        return extended, ""
    if not original._matches_request(row):
        raise ValidationExecutionUnavailable("Historical correction request is unavailable")

    async def load(identity: str):
        return await ValidationExecution(identity, request["org"], request["principal"]).load()

    _, guard = await resolve_dependencies(
        extended,
        execution_id=original.id,
        org=request["org"],
        principal=request["principal"],
        load=load,
    )
    return request, guard


@dataclass(frozen=True)
class ProgressContext:
    previous: PreparedMemoryValidation
    critic_execution_id: str
    correction_execution_id: str
    ancestor_candidate_digests: tuple[str, ...] = ()

    async def prepare(
        self, current: PreparedMemoryValidation, org: str, principal: str
    ) -> tuple[PreparedMemoryValidation, dict[str, Any]]:
        critic = ValidationExecution(self.critic_execution_id, org, principal)
        # The actual owner checks current transitive receipt availability before
        # the prompt is constructed. Begin/finalization repeat it transactionally.
        await critic.result()
        prior = await critic.load()
        correction = ValidationExecution(self.correction_execution_id, org, principal)
        await correction.result()
        birth = await correction.load()
        if prior is None or birth is None or prior["parent_id"] != birth["parent_id"]:
            raise ValidationExecutionUnavailable("Progress correction parent differs")
        result = decode_validation_result(json.loads(prior["result_json"]))
        if not isinstance(result, MemoryValidationResult) or result.submission is None:
            raise ValidationExecutionUnavailable("Progress critique has no findings")
        corrected = decode_validation_result(json.loads(birth["result_json"]))
        if isinstance(corrected, ReflectionCorrectionResult):
            matches = corrected.status == "corrected" and corrected.submission == result.submission
        elif isinstance(corrected, ProcedureCorrectionResult):
            matches = (
                corrected.review_execution_id == self.critic_execution_id
                and corrected.parent_operation_id == result.submission.parent_operation_id
                and corrected.parent_candidate_sha256 == result.submission.parent_candidate_sha256
            )
        else:
            matches = False
        if not matches:
            raise ValidationExecutionUnavailable("Progress correction critique differs")
        prepared = prepare_progress_validation(
            self.previous,
            current,
            result.submission,
            ancestor_candidate_digests=self.ancestor_candidate_digests,
        )
        history = bind_progress_history(prepared, prior, org, principal, previous=self.previous)
        return prepared, {
            "progress_history": history.model_dump(mode="json"),
            "execution_dependencies": [dependency_reference(birth).model_dump(mode="json")],
        }
