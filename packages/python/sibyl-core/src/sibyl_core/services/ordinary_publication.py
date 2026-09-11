"""Publish ordinary memories using the actual stored semantic review binding."""

import json
from dataclasses import dataclass
from typing import Any

from pydantic import TypeAdapter

from sibyl_core.backends.surreal.schema_source_witness import SOURCE_STATE_WRITE_WITNESS
from sibyl_core.services.content_models import RawMemory
from sibyl_core.services.memory_autonomy import reflection_autonomy_candidate_metadata
from sibyl_core.services.memory_source_validation import SourceAuthorityResolver
from sibyl_core.services.reflection_validation import ORDINARY_SNAPSHOT, prepare_stored_reflection
from sibyl_core.services.validation_execution import (
    ValidationExecution,
    ValidationExecutionUnavailable,
)
from sibyl_core.services.validation_promotion import (
    ValidatedPromotion,
    ValidationBinding,
    validated_result,
)
from sibyl_core.tasks.memory_validation import MemoryValidationResult, PreparedMemoryValidation
from sibyl_core.tasks.procedure_review import review_digest


def ordinary_semantic_digest(prepared: PreparedMemoryValidation) -> str:
    """Bind actual claims/evidence while permitting fenced publication bookkeeping."""
    payload = json.loads(prepared.payload_json)
    if payload["kind"] != "reflection":
        raise ValueError("Ordinary publication requires reflection evidence")
    payload.pop("parent_candidate_sha256")
    return review_digest(payload)


def ordinary_policy_digest(memories: list[RawMemory]) -> str:
    """Bind the existing policy interpreter's inputs, excluding publication bookkeeping."""
    return review_digest(
        [
            {
                "source_id": memory.id,
                "principal_id": memory.principal_id,
                "memory_scope": memory.memory_scope.value,
                "scope_key": memory.scope_key,
                "project_id": memory.project_id,
                "autonomy": reflection_autonomy_candidate_metadata(memory),
                "publication_context": {
                    key: memory.metadata.get(key)
                    for key in (
                        "suggested_memory_scope",
                        "suggested_scope_key",
                        "project_id",
                        "domain",
                        "related_to",
                    )
                },
            }
            for memory in sorted(memories, key=lambda item: item.id)
        ]
    )


@dataclass(frozen=True)
class OrdinaryValidatedPromotion(ValidatedPromotion):
    resolver: SourceAuthorityResolver

    async def current_guard(self) -> tuple[str, dict[str, Any]]:
        await self.authorize()
        current = await prepare_stored_reflection(
            self.organization_id,
            self.principal_id,
            self.candidate_id,
            self.resolver,
            publication=True,
        )
        row = await ValidationExecution(
            self.binding.execution_id, self.organization_id, self.principal_id
        ).load()
        if row is None:
            raise ValidationExecutionUnavailable("Ordinary validation disappeared")
        validated_result(
            row, self.binding, self.organization_id, self.principal_id, self.candidate_id
        )
        request = json.loads(row["request_json"])
        if request.get("kind") != "ordinary_reflection_validation-v2" or request.get(
            "ordinary_semantic_input"
        ) != ordinary_semantic_digest(current.prepared):
            raise ValidationExecutionUnavailable("Ordinary validated evidence changed")
        if request.get("ordinary_publication_policy") != current.publication_policy_sha256:
            raise ValidationExecutionUnavailable("Ordinary publication policy changed")
        prior = {v["source_id"]: v for v in request["source_bindings"]}
        present = {v["source_id"]: v for v in current.source_bindings}
        if set(prior) != set(present) or any(
            prior[k]["incarnation"] != present[k]["incarnation"]
            or (k != self.candidate_id and prior[k]["generation"] != present[k]["generation"])
            for k in prior
        ):
            raise ValidationExecutionUnavailable("Ordinary validated source identity changed")
        return (
            "LET $org=$organization_id; LET $parent=$uuid; LET $source_ids=$ordinary_source_ids;"
            + ORDINARY_SNAPSHOT
            + "IF $snapshot_digest!=$ordinary_snapshot { THROW 'publication_source_observation_changed'; };"
            + "LET $source_states_to_fence=$snapshot.states;"
            + SOURCE_STATE_WRITE_WITNESS,
            {
                "ordinary_snapshot": current.snapshot_sha256,
                "ordinary_source_ids": current.source_ids,
            },
        )


async def ordinary_promotion_binding(org, principal, parent, execution_id, resolver, authorize):
    row = await ValidationExecution(execution_id, org, principal).load()
    if row is None or not isinstance(row.get("result_json"), str):
        raise ValidationExecutionUnavailable("Ordinary promotion validation unavailable")
    result = TypeAdapter(MemoryValidationResult).validate_json(row["result_json"])
    from sibyl_core.services.validation_promotion import _sha

    binding = ValidationBinding(
        execution_id=execution_id,
        request_sha256=execution_id,
        result_sha256=_sha(row["result_json"]),
        input_sha256=result.input_sha256,
    )
    promotion = OrdinaryValidatedPromotion(org, principal, parent, binding, authorize, resolver)
    await promotion.current_guard()
    return promotion
