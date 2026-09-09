"""Register authorized eval attempts and admit signed private learning evidence."""

from __future__ import annotations

import base64
from dataclasses import replace
from typing import Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from sibyl.api.routes import memory_auth
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_organization, require_org_role
from sibyl.config import EvalIssuerSettings, settings
from sibyl_core.ai.errors import LLMError
from sibyl_core.ai.transport import FailedExtractionUsage
from sibyl_core.auth import AuthOrganization, OrganizationRole
from sibyl_core.auth.memory_policy import EVAL_CONSOLIDATION_METADATA_KEY, MemoryPolicyAction
from sibyl_core.memory_pipeline.source_lifecycle import public_memory_metadata
from sibyl_core.models.memory_scope import MemoryScope
from sibyl_core.models.reflection import ReflectionCandidate
from sibyl_core.services.content_models import raw_memory_recallable
from sibyl_core.services.eval_admission import (
    EvalAdmissionConflict,
    admit_eval_outcome,
    get_registered_eval_assignment,
    register_eval_assignment,
)
from sibyl_core.services.eval_publication import (
    ConsolidationConflict,
    ConsolidationOperation,
    consolidate_admitted_procedure,
    consolidation_extractor_configuration,
)
from sibyl_core.services.eval_publication_guards import verify_publication_admissions
from sibyl_core.services.memory_source_validation import reconcile_raw_source_lifecycle
from sibyl_core.tasks.consolidation import METADATA_KEY, ConsolidationInputBudgetExceeded
from sibyl_core.tasks.eval_receipts import ReceiptError, TaskAssignment


class EvalRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    issuer_id: str
    assignment: TaskAssignment


class EvalAdmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    issuer_id: str
    receipt_base64: str
    outcome_base64: str
    transcript_base64: str
    episode_base64: str


class EvalAdmissionResponse(BaseModel):
    memory_id: str
    revision: int
    admission_id: str
    receipt_sha256: str


router = APIRouter(
    prefix="/memory/eval",
    tags=["memory"],
    dependencies=[Depends(require_org_role(OrganizationRole.OWNER, OrganizationRole.ADMIN))],
)


def _trusted_issuer(issuer_id: str, assignment: TaskAssignment) -> EvalIssuerSettings:
    matches = [
        issuer
        for issuer in settings.eval_issuers
        if issuer.issuer_id == issuer_id
        and issuer.organization_id == assignment.organization_id
        and issuer.experiment_id == assignment.experiment_id
        and issuer.experiment_revision == assignment.experiment_revision
        and issuer.controller_policy_sha256 == assignment.controller_policy_sha256
    ]
    if len(matches) != 1:
        raise HTTPException(status_code=403, detail="Eval experiment issuer is not authorized")
    return matches[0]


async def _authorize_owner(
    *, ctx: AuthContext, organization_id: str, owner_principal_id: str, request: Request
) -> None:
    if not ctx.user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if str(ctx.organization_id) != organization_id or ctx.user_id != owner_principal_id:
        raise HTTPException(status_code=403, detail="Eval assignment is outside your private scope")
    await memory_auth.authorize_memory_policy(
        ctx=ctx,
        action=MemoryPolicyAction.WRITE,
        memory_scope=MemoryScope.PRIVATE,
        scope_key=None,
        surface="eval_learning_admission",
        request=request,
    )


@router.post("/assignments", response_model=TaskAssignment)
async def register_assignment(
    body: EvalRegistrationRequest,
    http_request: Request,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> TaskAssignment:
    assignment = body.assignment
    organization_id = str(org.id)
    if assignment.organization_id != organization_id:
        raise HTTPException(
            status_code=403, detail="Eval assignment belongs to another organization"
        )
    await _authorize_owner(
        ctx=ctx,
        organization_id=organization_id,
        owner_principal_id=assignment.owner_principal_id,
        request=http_request,
    )
    _trusted_issuer(body.issuer_id, assignment)
    try:
        return await register_eval_assignment(
            organization_id=organization_id, assignment=assignment
        )
    except EvalAdmissionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/experiments/{experiment_id}/attempts/{attempt_id}/admit",
    response_model=EvalAdmissionResponse,
)
async def admit_assignment_outcome(
    experiment_id: str,
    attempt_id: str,
    body: EvalAdmissionRequest,
    http_request: Request,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> EvalAdmissionResponse:
    organization_id = str(org.id)
    assignment = await get_registered_eval_assignment(
        organization_id=organization_id, experiment_id=experiment_id, attempt_id=attempt_id
    )
    if assignment is None:
        raise HTTPException(status_code=404, detail="Eval assignment not found")
    await _authorize_owner(
        ctx=ctx,
        organization_id=organization_id,
        owner_principal_id=assignment.owner_principal_id,
        request=http_request,
    )
    issuer = _trusted_issuer(body.issuer_id, assignment)
    try:
        evidence = {
            f"{name}_bytes": base64.b64decode(getattr(body, f"{name}_base64"), validate=True)
            for name in ("receipt", "outcome", "transcript", "episode")
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Eval evidence must be valid base64") from exc
    try:
        result = await admit_eval_outcome(
            organization_id=organization_id,
            experiment_id=experiment_id,
            attempt_id=attempt_id,
            principal_id=assignment.owner_principal_id,
            issuer_id=issuer.issuer_id,
            trusted_public_key=Ed25519PublicKey.from_public_bytes(
                base64.b64decode(issuer.public_key_base64, validate=True)
            ),
            expected_controller_policy_sha256=issuer.controller_policy_sha256,
            **evidence,
        )
    except EvalAdmissionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ReceiptError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Eval assignment not found") from exc
    return EvalAdmissionResponse(
        memory_id=result.memory.id,
        revision=result.memory.revision,
        admission_id=result.admission_id,
        receipt_sha256=result.receipt_sha256,
    )


class EvalConsolidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    issuer_id: str
    experiment_revision: str
    arm_id: str
    through_checkpoint: int = Field(ge=0)
    attempt_ids: list[str] = Field(min_length=2)
    group_id: str = Field(min_length=1)
    mechanism: str = Field(min_length=1)

    @field_validator("group_id", "mechanism")
    @classmethod
    def require_nonblank_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Consolidation text must not be blank")
        return value


class EvalConsolidationResponse(BaseModel):
    operation_id: str
    status: str
    memory_id: str | None
    candidate: ReflectionCandidate | None
    abstention_reason: str | None
    rejection_reason: str | None
    receipt_status: Literal["available", "unavailable"]
    build_receipt: dict[str, object]
    source_join: str
    source_freshness: str


@router.post("/experiments/{experiment_id}/consolidate", response_model=EvalConsolidationResponse)
async def consolidate_admitted_attempts(
    experiment_id: str,
    body: EvalConsolidationRequest,
    http_request: Request,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> EvalConsolidationResponse:
    organization_id = str(org.id)
    assignment = await get_registered_eval_assignment(
        organization_id=organization_id,
        experiment_id=experiment_id,
        attempt_id=body.attempt_ids[0],
    )
    if assignment is None:
        raise HTTPException(status_code=404, detail="Eval assignment not found")
    await _authorize_owner(
        ctx=ctx,
        organization_id=organization_id,
        owner_principal_id=assignment.owner_principal_id,
        request=http_request,
    )
    issuer = _trusted_issuer(body.issuer_id, assignment)
    model, extractor_revision = await consolidation_extractor_configuration()
    try:
        operation = ConsolidationOperation(
            organization_id=organization_id,
            principal_id=assignment.owner_principal_id,
            experiment_id=experiment_id,
            experiment_revision=body.experiment_revision,
            arm_id=body.arm_id,
            checkpoint=body.through_checkpoint,
            attempt_ids=tuple(body.attempt_ids),
            group_id=body.group_id,
            mechanism=body.mechanism,
            controller_policy_sha256=issuer.controller_policy_sha256,
            extractor_revision=extractor_revision,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        result = await consolidate_admitted_procedure(
            operation,
            trusted_issuer_id=issuer.issuer_id,
            trusted_public_key=Ed25519PublicKey.from_public_bytes(
                base64.b64decode(issuer.public_key_base64, validate=True)
            ),
            model_override=model,
        )
    except LLMError as exc:
        usage = FailedExtractionUsage.model_validate(exc.details.get("extraction_usage", {}))
        raise HTTPException(
            status_code=502,
            detail={
                "code": "consolidation_extraction_failed",
                "usage": usage.model_dump(mode="json"),
            },
        ) from exc
    except ConsolidationInputBudgetExceeded as exc:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "consolidation_input_budget_exceeded",
                "actual_chars": exc.actual_chars,
                "max_input_chars": exc.max_input_chars,
                "unit": "system_user_declared_schema_characters",
            },
        ) from exc
    except (ReceiptError, ConsolidationConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    memory = result.memory
    candidate = None
    receipt = result.build_receipt
    if memory is not None:
        memory = await reconcile_raw_source_lifecycle(
            memory,
            principal_id=assignment.owner_principal_id,
        )
        # A review response may show its own pending draft, but cannot waive
        # lifecycle exclusions or changes to the original admitted evidence.
        review_metadata = dict(memory.metadata)
        review_metadata.pop(EVAL_CONSOLIDATION_METADATA_KEY, None)
        if not raw_memory_recallable(
            replace(memory, metadata=review_metadata)
        ) or not await verify_publication_admissions(memory):
            raise HTTPException(
                status_code=409, detail="Stored consolidation sources are unavailable"
            )
        candidate = ReflectionCandidate(
            kind=memory.entity_type,
            title=memory.title,
            content=memory.raw_content,
            reason=str(memory.metadata.get("reflection_reason") or "Stored consolidation proposal"),
            confidence=float(memory.metadata.get("confidence") or 0),
            tags=list(memory.tags),
            metadata=public_memory_metadata(memory.metadata),
            review_state=memory.review_state,
        )
        payload = memory.metadata.get(METADATA_KEY)
        if not isinstance(payload, dict) or not isinstance(payload.get("build_receipt"), dict):
            raise HTTPException(status_code=409, detail="Stored consolidation receipt is invalid")
        if receipt is None:
            receipt = payload["build_receipt"]
    return EvalConsolidationResponse(
        operation_id=result.operation_id,
        status=result.status,
        memory_id=memory.id if memory else None,
        candidate=candidate,
        abstention_reason=receipt.get("reason")
        if receipt and result.status == "abstained"
        else None,
        rejection_reason=receipt.get("reason") if receipt and result.status == "rejected" else None,
        receipt_status="available" if receipt is not None else "unavailable",
        build_receipt=receipt if receipt is not None else {},
        source_join="authenticated_admission_ledger",
        source_freshness=(
            "checked_at_persistence; receipt_replay_is_historical; "
            "current_lifecycle_required_for_publication"
        ),
    )
