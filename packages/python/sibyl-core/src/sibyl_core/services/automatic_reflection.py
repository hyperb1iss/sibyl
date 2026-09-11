"""Automatically validate, correct and abstain ordinary reflection candidates."""

from dataclasses import dataclass, replace
from typing import Any

from pydantic import TypeAdapter

from sibyl_core.backends.surreal.schema_source_witness import SOURCE_STATE_WRITE_WITNESS
from sibyl_core.services.content_models import RawMemory
from sibyl_core.services.content_raw_persistence import (
    remember_reflection_candidate_review,
    save_raw_memory,
)
from sibyl_core.services.memory_source_validation import SourceAuthorityResolver
from sibyl_core.services.reflection_validation import (
    ORDINARY_SNAPSHOT,
    AuthorizedReflection,
    prepare_stored_reflection,
    validate_reflection_stage,
)
from sibyl_core.services.source_observations import SourceUnavailableError
from sibyl_core.services.validation_candidate import ValidationCandidateWrite
from sibyl_core.services.validation_execution import ValidationExecution
from sibyl_core.tasks.procedure_review import ReviewSubmission
from sibyl_core.tasks.reflection_correction import ReflectionCorrectionResult


@dataclass(frozen=True)
class AutomaticReflectionResult:
    candidate: RawMemory | None
    status: str
    executions: tuple[str, ...]
    reason: str | None = None


async def _current(
    original: AuthorizedReflection, resolver: SourceAuthorityResolver
) -> AuthorizedReflection:
    memory = original.memory
    current = await prepare_stored_reflection(
        memory.organization_id, memory.principal_id, memory.id, resolver
    )
    if (
        current.snapshot_sha256 != original.snapshot_sha256
        or current.prepared.input_sha256 != original.prepared.input_sha256
    ):
        raise SourceUnavailableError()
    return current


async def _abstain(
    original: AuthorizedReflection,
    resolver: SourceAuthorityResolver,
    reason: str,
    executions: list[str],
) -> None:
    current = await _current(original, resolver)
    memory = current.memory
    await save_raw_memory(
        replace(
            memory,
            review_state="archived",
            metadata={
                **memory.metadata,
                "review_state": "archived",
                "autonomy_outcome": "abstained",
                "autonomy_recommended_action": "abstain",
                "automatic_validation_executions": executions,
                "archive_reason": reason,
            },
        ),
        expected_revision=memory.revision,
        source_observations=[memory, *current.sources],
    )


async def _persist_corrected(
    original: AuthorizedReflection, resolver: SourceAuthorityResolver, outcome: dict[str, Any]
) -> RawMemory:
    execution_id = str(outcome["execution_id"])
    current = await _current(original, resolver)
    parent = current.memory
    execution = ValidationExecution(execution_id, parent.organization_id, parent.principal_id)
    row = await execution.load()
    if row is None or row["state"] != "returned" or row.get("purged"):
        raise SourceUnavailableError()
    correction = TypeAdapter(ReflectionCorrectionResult).validate_json(row["result_json"])
    if correction.status != "corrected" or not correction.content:
        raise SourceUnavailableError()
    if correction.parent_candidate_sha256 != current.snapshot_sha256:
        raise SourceUnavailableError()
    write = ValidationCandidateWrite(
        execution.id,
        row["result_json"],
        ORDINARY_SNAPSHOT
        + "IF $snapshot_digest != $expected { THROW 'Correction source changed'; };"
        + "LET $source_states_to_fence=$snapshot.states;"
        + SOURCE_STATE_WRITE_WITNESS,
        {
            "org": parent.organization_id,
            "principal": parent.principal_id,
            "parent": parent.id,
            "source_ids": current.source_ids,
            "expected": current.snapshot_sha256,
        },
    )
    candidate = replace(
        current.candidate,
        content=correction.content,
        claim_records=[],
        metadata={"automatic_correction": {"parent_id": parent.id, "execution_id": execution.id}},
    )
    suggested_scope = parent.metadata.get("suggested_memory_scope")
    suggested_key = parent.metadata.get("suggested_scope_key")
    if suggested_scope is not None and not isinstance(suggested_scope, str):
        raise SourceUnavailableError()
    if suggested_key is not None and not isinstance(suggested_key, str):
        raise SourceUnavailableError()
    return await remember_reflection_candidate_review(
        organization_id=parent.organization_id,
        principal_id=parent.principal_id,
        candidate=candidate,
        raw_source_ids=[source.id for source in current.sources],
        source_id=current.sources[0].id,
        memory_scope=parent.memory_scope,
        scope_key=parent.scope_key,
        suggested_memory_scope=suggested_scope,
        suggested_scope_key=suggested_key,
        source_memories=current.sources,
        source_observations=current.observations,
        validation_write=write,
        accessible_projects=current.authority.projects,
        accessible_teams=current.authority.teams,
        accessible_delegations=current.authority.delegations,
        allowed_memory_scope_keys=current.authority.scope_keys,
    )


async def automatically_review_reflection(
    organization_id: str, principal_id: str, candidate_id: str, resolver: SourceAuthorityResolver
) -> AutomaticReflectionResult:
    """One correction plus independent recheck; unresolved evidence abstains automatically."""
    original = await prepare_stored_reflection(
        organization_id, principal_id, candidate_id, resolver
    )
    validation = await validate_reflection_stage(original, resolver)
    executions = [str(validation["execution_id"])]
    if validation["status"] == "no_findings":
        await _current(original, resolver)
        return AutomaticReflectionResult(original.memory, "validated", tuple(executions))
    if validation["status"] == "abstain" or original.memory.metadata.get("automatic_correction"):
        reason = str(validation.get("reason") or "corrected_claims_remain_unsupported")
        await _abstain(original, resolver, reason, executions)
        return AutomaticReflectionResult(None, "abstained", tuple(executions), reason)
    review = ReviewSubmission.model_validate(validation["submission"])
    correction = await validate_reflection_stage(original, resolver, review)
    executions.append(str(correction["execution_id"]))
    if correction["status"] == "abstain":
        reason = str(correction["reason"])
        await _abstain(original, resolver, reason, executions)
        return AutomaticReflectionResult(None, "abstained", tuple(executions), reason)
    corrected = await _persist_corrected(original, resolver, correction)
    child = await prepare_stored_reflection(organization_id, principal_id, corrected.id, resolver)
    recheck = await validate_reflection_stage(child, resolver)
    executions.append(str(recheck["execution_id"]))
    if recheck["status"] != "no_findings":
        reason = str(recheck.get("reason") or "corrected_claims_remain_unsupported")
        await _abstain(child, resolver, reason, executions)
        await _abstain(original, resolver, reason, executions)
        return AutomaticReflectionResult(None, "abstained", tuple(executions), reason)
    await _current(child, resolver)
    await _abstain(original, resolver, "replaced_by_validated_correction", executions)
    return AutomaticReflectionResult(corrected, "corrected", tuple(executions))
