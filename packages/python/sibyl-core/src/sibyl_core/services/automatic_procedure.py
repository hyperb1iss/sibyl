"""Reconsider stored signed procedures through the existing extraction owner."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from uuid import NAMESPACE_URL, uuid5

from pydantic import TypeAdapter

from sibyl_core.ai.llm.extractor import ExtractionUsage
from sibyl_core.backends.surreal.schema_source_witness import SOURCE_STATE_WRITE_WITNESS
from sibyl_core.services.automatic_correction import advance_correction
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
    _query,
)
from sibyl_core.services.validation_result_codec import decode_validation_result
from sibyl_core.services.validation_stages import run_validation_stage
from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.consolidation import (
    METADATA_KEY,
    _extraction_input,
    _review_input,
    propose_conditional_procedure,
)
from sibyl_core.tasks.procedure_correction_result import ProcedureCorrectionResult
from sibyl_core.tasks.procedure_edits import VERSION as CORRECTION_VERSION
from sibyl_core.tasks.procedure_edits import correction_record
from sibyl_core.tasks.procedure_review import ReviewSubmission, review_digest


@dataclass(frozen=True)
class AutomaticProcedureResult:
    status: str
    candidate_id: str | None
    executions: tuple[str, ...]
    reason: str | None = None
    stored: StoredConsolidation | None = None


async def _correct_procedure(
    original, critique, *, organization_id, principal_id, parent_id, authorize
):
    executions = [str(critique["execution_id"])]
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
        correction_record(review, original.artifact.candidate.metadata[METADATA_KEY]["procedure"]),
    )
    policy = canonical(
        {
            "kind": CORRECTION_VERSION,
            **asdict(resolved),
            "system_sha256": review_digest(evidence.system),
            "prompt_sha256": review_digest(evidence.prompt),
            "declared_schema_sha256": review_digest(evidence.output_type.model_json_schema()),
        }
    )
    prior_row = await ValidationExecution(executions[0], organization_id, principal_id).load()
    if prior_row is None:
        raise ValidationExecutionUnavailable("Stored critique disappeared")
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
    from sibyl_core.services.validation_progress import correction_request

    request, prior_guard = await correction_request(request, prior_row)

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
        prior_guard
        + _SNAPSHOT
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
            execution.dispatch_guard,
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
    return AutomaticProcedureResult("corrected", stored.memory.id, tuple(executions), stored=stored)


async def _procedure_root(org, principal, candidate_id, authorize):
    seen = set()
    while True:
        await authorize()
        if candidate_id in seen:
            raise ValidationExecutionUnavailable("Correction candidate cycle")
        seen.add(candidate_id)
        current = await prepare_stored_procedure_validation(org, principal, candidate_id)
        audit = current.artifact.candidate.metadata[METADATA_KEY]
        reconsideration = audit["build_receipt"].get("reconsideration")
        if reconsideration is None:
            return candidate_id
        review = ReviewSubmission.model_validate(reconsideration["submission"])
        parents = await _query(
            "SELECT * FROM eval_consolidations WHERE organization_id=$org AND principal_id=$principal AND uuid=$parent_identity LIMIT 1;",
            org=org,
            principal=principal,
            parent_identity=review.parent_operation_id,
        )
        if len(parents) != 1 or not parents[0].get("candidate_id"):
            raise ValidationExecutionUnavailable("Correction parent ledger disappeared")
        parent_id = parents[0]["candidate_id"]
        parent = await prepare_stored_procedure_validation(org, principal, parent_id)
        if review.parent_candidate_sha256 != review_digest(
            parent.artifact.candidate.metadata[METADATA_KEY]
        ):
            raise ValidationExecutionUnavailable("Correction parent artifact changed")
        rows = await _query(
            "SELECT * FROM memory_validation_executions WHERE organization_id=$org AND principal_id=$principal AND parent_id=$parent_identity;",
            org=org,
            principal=principal,
            parent_identity=parent_id,
        )
        matches = []
        for row in rows:
            if row.get("state") != "returned" or row.get("purged") or not row.get("result_json"):
                continue
            value = decode_validation_result(json.loads(row["result_json"]))
            if not isinstance(value, ProcedureCorrectionResult) or value.result.candidate is None:
                continue
            if value.result.candidate.metadata.get(METADATA_KEY) != audit:
                continue
            await ValidationExecution(row["uuid"], org, principal).result()
            assignment = parent.assignments[0]
            operation = ConsolidationOperation(
                org,
                principal,
                assignment["experiment_id"],
                assignment["experiment_revision"],
                assignment["arm_id"],
                max(item["checkpoint"] for item in parent.assignments),
                parent.artifact.group.group_id,
                tuple(episode.session_id for episode in parent.artifact.group.episodes),
                parent.artifact.group.mechanism,
                assignment["controller_policy_sha256"],
                review_digest(row["policy_json"]),
                row["uuid"],
                review.parent_operation_id,
                review.parent_candidate_sha256,
            )
            if str(uuid5(NAMESPACE_URL, "sibyl-consolidation:" + operation.key)) == candidate_id:
                matches.append(row)
        if len(matches) != 1:
            raise ValidationExecutionUnavailable(
                "Correction child execution is ambiguous or unavailable"
            )
        candidate_id = parent_id


@dataclass
class _ProcedureAdapter:
    organization_id: str
    principal_id: str
    authorize: Callable[[], Awaitable[None]]
    stored: StoredConsolidation | None = None

    async def resolve(self, candidate_id):
        await self.authorize()
        return await prepare_stored_procedure_validation(
            self.organization_id, self.principal_id, candidate_id
        )

    def prepared(self, candidate):
        return candidate.prepared

    async def critique(self, candidate, context):
        from sibyl_core.services.surreal_content import get_raw_memory
        from sibyl_core.services.validation_promotion import ValidatedPromotion, ValidationBinding

        candidate_id = str(
            uuid5(
                NAMESPACE_URL,
                "sibyl-consolidation:"
                + json.loads(candidate.prepared.payload_json)["parent_operation_id"],
            )
        )
        memory = await get_raw_memory(organization_id=self.organization_id, memory_id=candidate_id)
        if memory is not None and memory.review_state == "promoted":
            associations = await _query(
                "SELECT * FROM memory_derivations WHERE organization_id=$org AND target_id=$candidate_id AND validation_binding_json!=NONE;",
                org=self.organization_id,
                candidate_id=candidate_id,
            )
            if len(associations) != 1:
                raise ValidationExecutionUnavailable(
                    "Published correction validation is unavailable"
                )
            binding = ValidationBinding.model_validate_json(
                associations[0]["validation_binding_json"]
            )
            await ValidatedPromotion(
                self.organization_id, self.principal_id, candidate_id, binding, self.authorize
            ).current_guard()
            return await ValidationExecution(
                binding.execution_id, self.organization_id, self.principal_id
            ).result()
        return await validate_stored_procedure(
            organization_id=self.organization_id,
            principal_id=self.principal_id,
            parent_id=str(
                uuid5(
                    NAMESPACE_URL,
                    "sibyl-consolidation:"
                    + json.loads(candidate.prepared.payload_json)["parent_operation_id"],
                )
            ),
            authorize=self.authorize,
            progress_context=context,
        )

    async def correct(self, candidate, critique):
        outcome = await _correct_procedure(
            candidate,
            critique,
            organization_id=self.organization_id,
            principal_id=self.principal_id,
            parent_id=str(
                uuid5(
                    NAMESPACE_URL,
                    "sibyl-consolidation:"
                    + json.loads(candidate.prepared.payload_json)["parent_operation_id"],
                )
            ),
            authorize=self.authorize,
        )
        self.stored = outcome.stored
        return outcome.candidate_id, outcome.executions[-1], outcome.reason

    async def current(self, candidate):
        await self.authorize()
        parent_id = str(
            uuid5(
                NAMESPACE_URL,
                "sibyl-consolidation:"
                + json.loads(candidate.prepared.payload_json)["parent_operation_id"],
            )
        )
        current = await self.resolve(parent_id)
        if current.snapshot_sha256 != candidate.snapshot_sha256:
            raise ValidationExecutionUnavailable("Correction frontier changed")


async def automatically_reconsider_procedure(
    *,
    organization_id: str,
    principal_id: str,
    parent_id: str,
    authorize: Callable[[], Awaitable[None]],
) -> AutomaticProcedureResult:
    """Advance preserved signed edits through independently critiqued children."""
    root = await _procedure_root(organization_id, principal_id, parent_id, authorize)
    adapter = _ProcedureAdapter(organization_id, principal_id, authorize)
    frontier = await advance_correction(adapter, root)
    accepted = frontier.status == "validated" and frontier.candidate is not None
    candidate_id = None
    if accepted:
        operation_id = json.loads(frontier.candidate.prepared.payload_json)["parent_operation_id"]
        candidate_id = str(uuid5(NAMESPACE_URL, "sibyl-consolidation:" + operation_id))
    return AutomaticProcedureResult(
        "validated"
        if accepted and candidate_id == root
        else "corrected"
        if accepted
        else frontier.status,
        candidate_id,
        frontier.executions,
        frontier.reason,
        adapter.stored,
    )
