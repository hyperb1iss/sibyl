"""Reconsider stored signed procedures through the existing extraction owner."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass

from pydantic import TypeAdapter

from sibyl_core.ai.llm.extractor import ExtractionUsage
from sibyl_core.backends.surreal.schema_source_witness import SOURCE_STATE_WRITE_WITNESS
from sibyl_core.services.eval_publication import (
    ConsolidationOperation,
    StoredConsolidation,
    _extractor_policy,
    store_consolidation,
)
from sibyl_core.services.procedure_validation import (
    _SNAPSHOT,
    prepare_stored_procedure_validation,
    validate_stored_procedure,
)
from sibyl_core.services.validation_candidate import ValidationCandidateWrite
from sibyl_core.services.validation_execution import (
    ValidationExecution,
    ValidationExecutionUnavailable,
)
from sibyl_core.services.validation_stages import run_validation_stage
from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.consolidation import (
    METADATA_KEY,
    _extraction_input,
    _review_input,
    propose_conditional_procedure,
)
from sibyl_core.tasks.procedure_correction_result import ProcedureCorrectionResult
from sibyl_core.tasks.procedure_review import ReviewSubmission, review_digest


@dataclass(frozen=True)
class AutomaticProcedureResult:
    status: str
    candidate_id: str | None
    executions: tuple[str, ...]
    reason: str | None = None
    stored: StoredConsolidation | None = None


async def automatically_reconsider_procedure(
    *,
    organization_id: str,
    principal_id: str,
    parent_id: str,
    authorize: Callable[[], Awaitable[None]],
) -> AutomaticProcedureResult:
    """Critique, correct and recheck; publication still requires its existing guards."""
    critique = await validate_stored_procedure(
        organization_id=organization_id,
        principal_id=principal_id,
        parent_id=parent_id,
        authorize=authorize,
    )
    executions = [str(critique["execution_id"])]
    if critique["status"] != "reconsider":
        return AutomaticProcedureResult(
            "validated" if critique["status"] == "no_findings" else "abstained",
            parent_id if critique["status"] == "no_findings" else None,
            tuple(executions),
            critique.get("reason"),
        )
    await authorize()
    original = await prepare_stored_procedure_validation(organization_id, principal_id, parent_id)
    review = ReviewSubmission.model_validate(critique["submission"])
    prepared = json.loads(original.prepared.payload_json)
    if (
        review.parent_operation_id != prepared["parent_operation_id"]
        or review.parent_candidate_sha256 != prepared["parent_candidate_sha256"]
    ):
        raise ValidationExecutionUnavailable("Stored critique parent changed")
    resolved = await _extractor_policy()
    evidence = await asyncio.to_thread(_extraction_input, original.artifact.group)
    evidence = await asyncio.to_thread(
        _review_input,
        original.artifact.group,
        evidence,
        {
            "submission": review.model_dump(mode="json"),
            "parent_procedure": original.artifact.candidate.metadata[METADATA_KEY]["procedure"],
        },
    )
    policy = canonical(
        {
            "kind": "signed_procedure_correction-v1",
            **asdict(resolved),
            "system_sha256": review_digest(evidence.system),
            "prompt_sha256": review_digest(evidence.prompt),
            "declared_schema_sha256": review_digest(evidence.output_type.model_json_schema()),
        }
    )
    request = {
        "kind": "signed_procedure_correction",
        "org": organization_id,
        "principal": principal_id,
        "parent": parent_id,
        "review_execution": executions[0],
        "review": review.model_dump(mode="json"),
        "snapshot": original.snapshot_sha256,
        "source_bindings": original.source_bindings,
        "policy": policy,
    }

    async def current():
        await authorize()
        refreshed = await prepare_stored_procedure_validation(
            organization_id, principal_id, parent_id
        )
        if (
            refreshed.snapshot_sha256 != original.snapshot_sha256
            or await _extractor_policy() != resolved
        ):
            raise ValidationExecutionUnavailable("Correction authority or policy changed")
        prior = ValidationExecution(executions[0], organization_id, principal_id)
        if await prior.result() != critique:
            raise ValidationExecutionUnavailable("Stored critique changed")

    guard = (
        _SNAPSHOT
        + "IF $snapshot_digest!=$expected { THROW 'Correction sources changed'; };"
        + "LET $source_states_to_fence=$states;"
        + SOURCE_STATE_WRITE_WITNESS
    )
    params = {"parent": parent_id, "expected": original.snapshot_sha256}
    execution = ValidationExecution(
        review_digest(request),
        organization_id,
        principal_id,
        authorize=current,
        dispatch_guard=guard,
        guard_params=params,
    )

    async def run():
        result = await propose_conditional_procedure(
            original.artifact.group,
            review=review,
            parent_artifact=original.artifact.candidate.metadata[METADATA_KEY],
            model_override=resolved.model,
            max_input_chars=resolved.max_input_chars,
            max_tokens=resolved.max_output_tokens,
            output_mode=resolved.output_mode,
            openrouter_provider=resolved.openrouter_provider,
        )
        usage = ExtractionUsage.model_validate(result.receipt["usage"])
        try:
            return ProcedureCorrectionResult(
                "procedure_correction",
                review.parent_operation_id,
                review.parent_candidate_sha256,
                executions[0],
                result,
                usage,
            )
        except BaseException as error:
            error.__dict__["extraction_usage"] = usage
            raise

    await run_validation_stage(
        execution=execution,
        parent_id=parent_id,
        source_ids=original.source_ids,
        request=request,
        policy=policy,
        check_current=current,
        run=run,
    )
    executions.append(execution.id)
    # Decode the persisted output, not a caller-provided proposal or critique.
    row = await execution.load()
    if row is None or row.get("state") != "returned" or row.get("purged"):
        raise ValidationExecutionUnavailable("Correction result unavailable")
    correction = TypeAdapter(ProcedureCorrectionResult).validate_json(row["result_json"])
    if correction.review_execution_id != executions[0]:
        raise ValidationExecutionUnavailable("Correction review changed")
    await current()
    assignment = original.assignments[0]
    operation = ConsolidationOperation(
        organization_id,
        principal_id,
        assignment["experiment_id"],
        assignment["experiment_revision"],
        assignment["arm_id"],
        max(item["checkpoint"] for item in original.assignments),
        original.artifact.group.group_id,
        tuple(episode.session_id for episode in original.artifact.group.episodes),
        original.artifact.group.mechanism,
        assignment["controller_policy_sha256"],
        review_digest(policy),
        execution.id,
        review.parent_operation_id,
        review.parent_candidate_sha256,
    )
    stored = await store_consolidation(
        operation,
        correction.result,
        validation_write=ValidationCandidateWrite(
            execution.id,
            row["result_json"],
            guard,
            {**params, "org": organization_id, "principal": principal_id},
        ),
    )
    if stored.memory is None:
        return AutomaticProcedureResult(
            "abstained",
            None,
            tuple(executions),
            (stored.build_receipt or {}).get("reason"),
            stored,
        )
    recheck = await validate_stored_procedure(
        organization_id=organization_id,
        principal_id=principal_id,
        parent_id=stored.memory.id,
        authorize=authorize,
    )
    executions.append(str(recheck["execution_id"]))
    return AutomaticProcedureResult(
        "corrected" if recheck["status"] == "no_findings" else "abstained",
        stored.memory.id if recheck["status"] == "no_findings" else None,
        tuple(executions),
        None
        if recheck["status"] == "no_findings"
        else str(recheck.get("reason") or "corrected_claims_remain_unsupported"),
        stored,
    )
