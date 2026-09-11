"""Consume a stored critic result through the existing promotion transaction."""

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from sibyl_core.services.validation_execution import (
    ValidationExecution,
    ValidationExecutionUnavailable,
)
from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.memory_validation import (
    VALIDATION_VERSION,
    CriticOutput,
    MemoryValidationResult,
)
from sibyl_core.tasks.procedure_review import review_digest


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class ValidationBinding(BaseModel):
    """Hash-only reverse reference retained with the mandatory source association."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    execution_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def validated_result(
    row: dict[str, Any], binding: ValidationBinding, org: str, principal: str, parent: str
) -> MemoryValidationResult:
    if (
        row.get("uuid") != binding.execution_id
        or row.get("organization_id") != org
        or row.get("principal_id") != principal
        or row.get("parent_id") != parent
        or row.get("state") != "returned"
        or row.get("purged") is not False
        or not isinstance(row.get("request_json"), str)
        or not isinstance(row.get("result_json"), str)
        or _sha(row["result_json"]) != binding.result_sha256
    ):
        raise ValidationExecutionUnavailable("Promotion validation is unavailable")
    request = json.loads(row["request_json"])
    if not isinstance(request, dict):
        raise ValidationExecutionUnavailable("Promotion validation request is malformed")
    if (
        canonical(request) != row["request_json"]
        or review_digest(request) != binding.execution_id
        or row.get("request_sha256") != binding.request_sha256
        or binding.request_sha256 != binding.execution_id
        or request.get("org") != org
        or request.get("principal") != principal
        or request.get("parent") != parent
        or request.get("policy") != row.get("policy_json")
        or request.get("input") != binding.input_sha256
    ):
        raise ValidationExecutionUnavailable("Promotion validation identity differs")
    result = TypeAdapter(MemoryValidationResult).validate_json(row["result_json"])
    if (
        result.version != VALIDATION_VERSION
        or result.schema_sha256 != review_digest(CriticOutput.model_json_schema())
        or result.status != "no_findings"
        or result.submission is not None
        or result.reason is not None
        or result.input_sha256 != binding.input_sha256
    ):
        raise ValidationExecutionUnavailable("Critic did not validate this candidate")
    return result


async def validation_binding_current(memory, association) -> bool:
    value = association.get("validation_binding_json")
    if value is None:
        return True
    try:
        binding = ValidationBinding.model_validate_json(value)
        row = await ValidationExecution(
            binding.execution_id, memory.organization_id, memory.principal_id
        ).load()
        if row is None:
            return False
        validated_result(row, binding, memory.organization_id, memory.principal_id, memory.id)
    except (ValueError, TypeError, KeyError):
        return False
    return True


@dataclass(frozen=True)
class ValidatedPromotion:
    organization_id: str
    principal_id: str
    candidate_id: str
    binding: ValidationBinding
    authorize: Callable[[], Awaitable[None]]

    async def current_guard(self) -> tuple[str, dict[str, Any]]:
        from sibyl_core.services.procedure_validation import (
            _SNAPSHOT,
            prepare_stored_procedure_validation,
        )

        await self.authorize()
        current = await prepare_stored_procedure_validation(
            self.organization_id, self.principal_id, self.candidate_id
        )
        row = await ValidationExecution(
            self.binding.execution_id, self.organization_id, self.principal_id
        ).load()
        if row is None:
            raise ValidationExecutionUnavailable("Promotion validation disappeared")
        validated_result(
            row, self.binding, self.organization_id, self.principal_id, self.candidate_id
        )
        request = json.loads(row["request_json"])
        prior = {item["source_id"]: item for item in request["source_bindings"]}
        present = {item["source_id"]: item for item in current.source_bindings}
        if set(prior) != set(present) or any(
            prior[key]["incarnation"] != present[key]["incarnation"]
            or (key != self.candidate_id and prior[key]["generation"] != present[key]["generation"])
            for key in prior
        ):
            raise ValidationExecutionUnavailable("Validated source identity changed")
        if current.prepared.input_sha256 != self.binding.input_sha256:
            raise ValidationExecutionUnavailable("Validated source evidence changed")
        return (
            "LET $org=$organization_id; LET $parent=$uuid;"
            + _SNAPSHOT
            + "IF $snapshot_digest!=$validation_snapshot { THROW 'publication_source_observation_changed'; };",
            {"validation_snapshot": current.snapshot_sha256},
        )


async def promote_validated_procedure(
    *,
    organization_id: str,
    principal_id: str,
    candidate_id: str,
    execution_id: str,
    authorize: Callable[[], Awaitable[None]],
):
    """Promote privately from durable evidence, never a caller's critique text."""
    from sibyl_core.services.memory_reflection import promote_reflection_candidate_review

    await authorize()
    row = await ValidationExecution(execution_id, organization_id, principal_id).load()
    if row is None or not isinstance(row.get("result_json"), str):
        raise ValidationExecutionUnavailable("Promotion validation unavailable")
    result = TypeAdapter(MemoryValidationResult).validate_json(row["result_json"])
    binding = ValidationBinding(
        execution_id=execution_id,
        request_sha256=execution_id,
        result_sha256=_sha(row["result_json"]),
        input_sha256=result.input_sha256,
    )
    promotion = ValidatedPromotion(organization_id, principal_id, candidate_id, binding, authorize)
    await promotion.current_guard()
    return await promote_reflection_candidate_review(
        candidate_id=candidate_id,
        organization_id=organization_id,
        principal_id=principal_id,
        promote_to_scope="private",
        promote_to_scope_key=principal_id,
        validation_promotion=promotion,
    )


VALIDATION_PROMOTION_GUARD = """
IF $validation_binding != NONE {
    LET $stage=(SELECT * FROM memory_validation_executions WHERE uuid=$validation_binding.execution_id
        AND organization_id=$organization_id AND principal_id=$validation_principal)[0];
    IF $stage=NONE OR $stage.state!='returned' OR $stage.purged!=false
        OR $stage.parent_id!=$uuid OR $stage.request_sha256!=$validation_binding.request_sha256
        OR crypto::sha256($stage.request_json)!=$validation_binding.request_sha256
        OR $stage.result_json=NONE OR crypto::sha256($stage.result_json)!=$validation_binding.result_sha256 {
        THROW 'publication_source_observation_changed';
    };
    LET $association=(SELECT * FROM memory_derivations WHERE organization_id=$organization_id
        AND target_kind='raw_capture' AND target_id=$uuid)[0];
    IF $association=NONE {
        IF $validation_derivation=NONE { THROW 'publication_source_observation_changed'; };
        CREATE memory_derivations CONTENT $validation_derivation;
    } ELSE {
        IF $association.active!=true OR ($association.validation_entity_id!=NONE AND $association.validation_entity_id!=$validation_entity_id) OR ($association.validation_binding_json!=NONE
            AND $association.validation_binding_json!=$validation_binding_json) {
            THROW 'publication_source_observation_changed';
        };
        UPDATE memory_derivations SET validation_binding_json=$validation_binding_json, validation_entity_id=$validation_entity_id
            WHERE id=$association.id;
    };
    UPDATE memory_validation_executions SET promotion_write_witness=(promotion_write_witness ?? 0)+1
        WHERE id=$stage.id;
};
"""


async def validated_graph_current(organization_id: str, entity_id: str) -> bool:
    """Follow protected reverse references, including legacy publication ledgers."""
    from sibyl_core.services.content_models import raw_memory_from_record
    from sibyl_core.services.validation_execution import _query

    rows = await _query(
        """RETURN {
        LET $direct=(SELECT * FROM memory_derivations WHERE organization_id=$org
            AND validation_entity_id=$entity);
        LET $published=(SELECT candidate_id FROM eval_consolidations WHERE organization_id=$org
            AND promoted_entity_id=$entity);
        LET $ids=array::distinct(array::concat($direct.map(|$a| $a.target_id),$published.map(|$p| $p.candidate_id)));
        RETURN {
            captures:(SELECT * FROM raw_captures WHERE organization_id=$org AND uuid IN $ids),
            associations:(SELECT * FROM memory_derivations WHERE organization_id=$org
                AND target_kind='raw_capture' AND target_id IN $ids),
            ids:$ids
        };
    };""",
        org=organization_id,
        entity=entity_id,
    )
    if len(rows) != 1:
        raise ValidationExecutionUnavailable("Validation recall snapshot unavailable")
    captures = {row["uuid"]: row for row in rows[0]["captures"]}
    associations = {row["target_id"]: row for row in rows[0]["associations"]}
    for identifier in rows[0]["ids"]:
        record = captures.get(identifier)
        if record is None:
            return False
        memory = raw_memory_from_record(record)
        association = associations.get(identifier)
        if association is None:
            if memory.derivation_required:
                return False
            continue
        if association.get("validation_binding_json") is None:
            if association.get("validation_entity_id") is not None:
                return False
            continue
        if (
            association.get("active") is not True
            or association.get("validation_entity_id") != entity_id
            or association.get("body_sha256") != _sha(memory.raw_content)
            or not await validation_binding_current(memory, association)
        ):
            return False
    return True
