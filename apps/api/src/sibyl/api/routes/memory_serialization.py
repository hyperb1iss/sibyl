"""Response serialization and source projections for memory routes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import structlog
from fastapi import HTTPException

from sibyl.api.schemas import (
    MemoryAuditEventResponse,
    MemoryCorrectionResponse,
    MemoryDerivedRecordResponse,
    MemoryScopeInputResponse,
    MemoryScopeLiteral,
    MemorySharePreviewResponse,
    MemoryShareResponse,
    MemorySourceInspectResponse,
    MemorySpaceAccessPreviewResponse,
    MemorySpaceMemberResponse,
    MemorySpaceResponse,
    MemorySpaceStateLiteral,
    MutationReceipt,
    RawMemoryResponse,
    ReflectionAutonomyResponse,
    ReflectionPromotionPreviewResponse,
    ReflectionPromotionResponse,
    ReflectionReviewDrainItem,
    ReflectionReviewDrainRequest,
    ReflectionReviewDrainResponse,
)
from sibyl.auth.context import AuthContext
from sibyl.persistence.auth_runtime import (
    list_memory_audit_events,
)
from sibyl_core.auth import OrganizationRole
from sibyl_core.auth.memory_policy import (
    MemoryPolicyDecision,
)
from sibyl_core.models.reflection import (
    claim_records_from_metadata,
    memory_lifecycle_from_metadata,
    reflection_findings_from_metadata,
)
from sibyl_core.services.memory import (
    MemoryAccessPreview,
    MemoryCorrectionPreview,
    MemoryCorrectionResult,
    MemorySharePreview,
    MemoryShareResult,
    ReflectionPromotionPreview,
    ReflectionPromotionResult,
)
from sibyl_core.services.memory_autonomy import (
    ReflectionAutonomyDecision,
)
from sibyl_core.services.surreal_content import (
    RawMemory,
)

log = structlog.get_logger()

_READ_ROLES = (
    OrganizationRole.OWNER,
    OrganizationRole.ADMIN,
    OrganizationRole.MEMBER,
    OrganizationRole.VIEWER,
)
_WRITE_ROLES = (
    OrganizationRole.OWNER,
    OrganizationRole.ADMIN,
    OrganizationRole.MEMBER,
)
_ADMIN_ROLES = (OrganizationRole.OWNER, OrganizationRole.ADMIN)
_ARCHIVEABLE_REFLECTION_EXCEPTION_REASONS = frozenset(
    {
        "duplicate_candidate",
        "stale_candidate",
    }
)


def raw_memory_response(
    memory: RawMemory,
    *,
    policy_reason: str | None = None,
    receipt: MutationReceipt | None = None,
) -> RawMemoryResponse:
    return RawMemoryResponse(
        id=memory.id,
        organization_id=memory.organization_id,
        source_id=memory.source_id,
        principal_id=memory.principal_id,
        memory_scope=memory.memory_scope.value,
        scope_key=memory.scope_key,
        title=memory.title,
        raw_content=memory.raw_content,
        tags=memory.tags,
        metadata=memory.metadata,
        provenance=memory.provenance,
        capture_surface=memory.capture_surface,
        captured_at=memory.captured_at,
        created_at=memory.created_at,
        score=memory.score,
        snippet=memory.snippet,
        policy_reason=policy_reason,
        revision=memory.revision,
        mutation_receipt=receipt,
    )


def actor_user_uuid(ctx: AuthContext) -> UUID:
    if not ctx.user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        return UUID(str(ctx.user_id))
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="invalid_actor") from exc


def memory_space_member_response(member: Any) -> MemorySpaceMemberResponse:
    return MemorySpaceMemberResponse(
        id=str(member.id),
        organization_id=str(member.organization_id),
        space_id=str(member.space_id),
        principal_type=str(member.principal_type),
        principal_id=str(member.principal_id),
        role=str(member.role),
        permissions=list(getattr(member, "permissions", [])),
        expires_at=getattr(member, "expires_at", None),
        created_by_user_id=str(member.created_by_user_id),
        created_at=getattr(member, "created_at", None),
        updated_at=getattr(member, "updated_at", None),
    )


def memory_space_response(
    space: Any,
    *,
    members: list[Any] | None = None,
) -> MemorySpaceResponse:
    return MemorySpaceResponse(
        id=str(space.id),
        organization_id=str(space.organization_id),
        memory_scope=cast("MemoryScopeLiteral", str(space.memory_scope)),
        scope_key=getattr(space, "scope_key", None),
        name=str(space.name),
        description=getattr(space, "description", None),
        state=cast("MemorySpaceStateLiteral", str(space.state)),
        disabled_reason=getattr(space, "disabled_reason", None),
        metadata=dict(getattr(space, "metadata", {}) or {}),
        created_by_user_id=str(space.created_by_user_id),
        created_at=getattr(space, "created_at", None),
        updated_at=getattr(space, "updated_at", None),
        members=[memory_space_member_response(member) for member in members or []],
    )


def promotion_response(result: ReflectionPromotionResult) -> ReflectionPromotionResponse:
    metadata = dict(result.metadata or {})
    return ReflectionPromotionResponse(
        success=result.success,
        candidate_id=result.candidate_id,
        promoted_id=result.promoted_id,
        reason=result.reason,
        review_state=result.review_state,
        memory_scope=result.memory_scope.value if result.memory_scope else None,
        scope_key=result.scope_key,
        raw_source_ids=list(result.raw_source_ids),
        policy_reasons=_metadata_str_list(metadata.get("policy_reasons")),
        metadata=metadata,
    )


def promotion_preview_response(
    result: ReflectionPromotionPreview,
) -> ReflectionPromotionPreviewResponse:
    metadata = dict(result.metadata or {})
    source_count = metadata.get("source_count")
    return ReflectionPromotionPreviewResponse(
        allowed=result.allowed,
        candidate_id=result.candidate_id,
        reason=result.reason,
        review_state=result.review_state,
        promote_to_scope=result.memory_scope.value if result.memory_scope else None,
        promote_to_scope_key=result.scope_key,
        raw_source_ids=list(result.raw_source_ids),
        policy_reasons=_metadata_str_list(metadata.get("policy_reasons")),
        input_scopes=[
            MemoryScopeInputResponse(
                id=str(item.get("id") or ""),
                memory_scope=cast(
                    "MemoryScopeLiteral",
                    str(item.get("memory_scope") or "private"),
                ),
                scope_key=str(item["scope_key"]) if item.get("scope_key") else None,
            )
            for item in _metadata_dict_list(metadata.get("input_scopes"))
        ],
        source_count=source_count if isinstance(source_count, int) else 0,
        metadata=metadata,
    )


def autonomy_response(
    *,
    decision: ReflectionAutonomyDecision,
    preview: ReflectionPromotionPreview,
    promotion: ReflectionPromotionResult | None = None,
) -> ReflectionAutonomyResponse:
    promotion_payload = promotion_response(promotion) if promotion is not None else None
    promoted_id = promotion.promoted_id if promotion and promotion.success else None
    metadata = dict(decision.metadata or {})
    if promotion is not None:
        metadata["promotion_reason"] = promotion.reason
        metadata["promotion_success"] = promotion.success
    return ReflectionAutonomyResponse(
        outcome=decision.outcome.value,
        recommended_action=decision.recommended_action.value,
        applied=promotion is not None and promotion.success,
        dry_run=decision.dry_run,
        candidate_id=decision.candidate_id,
        reason=decision.reason,
        review_state=promotion.review_state if promotion else decision.review_state,
        promote_to_scope=decision.memory_scope.value if decision.memory_scope else None,
        promote_to_scope_key=decision.scope_key,
        promoted_id=promoted_id,
        raw_source_ids=list(decision.raw_source_ids),
        policy_reasons=list(decision.policy_reasons),
        exception_reasons=list(decision.exception_reasons),
        confidence=decision.confidence,
        confidence_threshold=decision.confidence_threshold,
        preview=promotion_preview_response(preview),
        promotion=promotion_payload,
        metadata=metadata,
    )


def drain_item_from_autonomy(
    response: ReflectionAutonomyResponse,
    *,
    archived: bool = False,
    review_state: str | None = None,
) -> ReflectionReviewDrainItem:
    return ReflectionReviewDrainItem(
        candidate_id=response.candidate_id,
        outcome=response.outcome,
        recommended_action=response.recommended_action,
        applied=response.applied,
        archived=archived,
        dry_run=response.dry_run,
        reason=response.reason,
        review_state=review_state or response.review_state,
        promoted_id=response.promoted_id,
        raw_source_ids=list(response.raw_source_ids),
        policy_reasons=list(response.policy_reasons),
        exception_reasons=list(response.exception_reasons),
        confidence=response.confidence,
    )


def drain_error_item(
    candidate_id: str,
    *,
    error: object,
    dry_run: bool,
) -> ReflectionReviewDrainItem:
    return ReflectionReviewDrainItem(
        candidate_id=candidate_id,
        outcome="error",
        recommended_action="error",
        dry_run=dry_run,
        reason="review_failed",
        review_state="unknown",
        error=str(error),
    )


def drain_response(
    *,
    request: ReflectionReviewDrainRequest,
    results: list[ReflectionReviewDrainItem],
) -> ReflectionReviewDrainResponse:
    effective_archive_reasons = sorted(
        {
            reason
            for reason in request.archive_exception_reasons
            if reason in _ARCHIVEABLE_REFLECTION_EXCEPTION_REASONS
        }
    )
    return ReflectionReviewDrainResponse(
        dry_run=request.dry_run,
        limit=request.limit,
        scanned_count=len(results),
        auto_promote_count=sum(1 for item in results if item.outcome == "auto_promote"),
        applied_count=sum(1 for item in results if item.applied),
        archived_count=sum(1 for item in results if item.archived),
        exception_count=sum(1 for item in results if item.outcome == "exception"),
        skip_count=sum(1 for item in results if item.outcome == "skip"),
        failed_count=sum(1 for item in results if item.outcome == "error"),
        results=results,
        metadata={
            "archive_exceptions": request.archive_exceptions,
            "archive_exception_reasons": effective_archive_reasons,
            "requested_archive_exception_reasons": list(request.archive_exception_reasons),
        },
    )


def share_preview_response(result: MemorySharePreview) -> MemorySharePreviewResponse:
    metadata = dict(result.metadata or {})
    return MemorySharePreviewResponse(
        allowed=result.allowed,
        reason=result.reason,
        target_scope=result.target_scope.value if result.target_scope else None,
        target_scope_key=result.target_scope_key,
        source_ids=list(result.source_ids),
        visible_source_ids=list(result.visible_source_ids),
        denied_source_ids=list(result.denied_source_ids),
        missing_source_ids=list(result.missing_source_ids),
        redacted_count=result.redacted_count,
        hidden_but_relevant_count=result.hidden_but_relevant_count,
        policy_reasons=_metadata_str_list(metadata.get("policy_reasons")),
        input_scopes=[
            MemoryScopeInputResponse(
                id=str(item.get("id") or ""),
                memory_scope=cast(
                    "MemoryScopeLiteral",
                    str(item.get("memory_scope") or "private"),
                ),
                scope_key=str(item["scope_key"]) if item.get("scope_key") else None,
            )
            for item in _metadata_dict_list(metadata.get("input_scopes"))
        ],
        metadata=metadata,
    )


def share_response(
    result: MemoryShareResult,
    *,
    audit_event_ids: list[str],
) -> MemoryShareResponse:
    preview = share_preview_response(result.preview)
    promotions = [promotion_response(promotion) for promotion in result.promotions]
    promoted_ids = [
        str(promotion.promoted_id)
        for promotion in result.promotions
        if promotion.success and promotion.promoted_id
    ]
    metadata = {
        **dict(result.metadata or {}),
        "audit_event_ids": list(audit_event_ids),
    }
    return MemoryShareResponse(
        applied=result.applied,
        reason=result.reason,
        target_scope=preview.target_scope,
        target_scope_key=preview.target_scope_key,
        source_ids=list(preview.source_ids),
        visible_source_ids=list(preview.visible_source_ids),
        denied_source_ids=list(preview.denied_source_ids),
        missing_source_ids=list(preview.missing_source_ids),
        promoted_ids=promoted_ids,
        audit_event_ids=list(audit_event_ids),
        preview=preview,
        promotions=promotions,
        metadata=metadata,
    )


def access_preview_response(result: MemoryAccessPreview) -> MemorySpaceAccessPreviewResponse:
    metadata = dict(result.metadata or {})
    return MemorySpaceAccessPreviewResponse(
        allowed=result.allowed,
        reason=result.reason,
        target_principal_type=result.target_principal_type,
        target_principal_id=result.target_principal_id,
        memory_space_ids=list(result.memory_space_ids),
        visible_source_ids=list(result.visible_source_ids),
        denied_source_ids=list(result.denied_source_ids),
        missing_source_ids=list(result.missing_source_ids),
        redacted_count=result.redacted_count,
        hidden_but_relevant_count=result.hidden_but_relevant_count,
        policy_reasons=[decision.reason for decision in result.policy_decisions]
        or _metadata_str_list(metadata.get("policy_reasons")),
        metadata=metadata,
    )


def correction_response(
    preview: MemoryCorrectionPreview,
    *,
    applied: bool = False,
    updated_memory: RawMemory | None = None,
    receipt: MutationReceipt | None = None,
) -> MemoryCorrectionResponse:
    metadata = dict(preview.metadata or {})
    lifecycle: dict[str, Any] = {}
    reflection_finding: dict[str, Any] | None = None
    if updated_memory is not None:
        lifecycle = memory_lifecycle_from_metadata(
            updated_memory.metadata,
            source_id=updated_memory.id,
            review_state=updated_memory.review_state,
        ).to_dict()
        findings = [
            finding.to_dict()
            for finding in reflection_findings_from_metadata(updated_memory.metadata)
        ]
        reflection_finding = findings[-1] if findings else None
    return MemoryCorrectionResponse(
        allowed=preview.allowed,
        applied=applied,
        source_id=preview.source_id,
        action=preview.action,
        reason=preview.reason,
        target_lifecycle_state=preview.target_lifecycle_state,
        target_lifecycle_flags=preview.target_lifecycle_flags,
        updated_review_state=updated_memory.review_state if updated_memory else None,
        lifecycle=lifecycle,
        reflection_finding=reflection_finding,
        affected_source_ids=list(preview.affected_source_ids),
        affected_derived_ids=list(preview.affected_derived_ids),
        reversible=preview.reversible,
        recall_impact=dict(preview.recall_impact),
        synthesis_impact=dict(preview.synthesis_impact),
        audit_action=preview.audit_action,
        policy_reasons=[decision.reason for decision in preview.policy_decisions]
        or _metadata_str_list(metadata.get("policy_reasons")),
        metadata=metadata,
        revision=updated_memory.revision if updated_memory else None,
        mutation_receipt=receipt,
    )


def correction_result_response(
    result: MemoryCorrectionResult,
    *,
    receipt: MutationReceipt | None = None,
) -> MemoryCorrectionResponse:
    response = correction_response(
        result.preview,
        applied=result.applied,
        updated_memory=result.updated_memory,
        receipt=receipt,
    )
    if not result.applied:
        return response
    # recall_impact is a claim about what recall will now do, so the graph
    # outcome belongs in it. A correction that reached the capture but not
    # every row the capture named is a partial write, and answering
    # `applied: true` with nothing else said would read as a complete one.
    recall_impact = dict(response.recall_impact)
    recall_impact["graph_entity_ids"] = list(result.affected_entity_ids)
    if result.refused_entity_ids:
        recall_impact["refused_entity_ids"] = list(result.refused_entity_ids)
        recall_impact["partially_applied"] = True
    if result.projection_walk_truncated:
        # The lineage walk stopped at its page ceiling, so rows projected from
        # this capture may still be servable. A caller told `applied: true`
        # with no qualifier has no other way to learn that.
        recall_impact["projection_walk_truncated"] = True
        recall_impact["partially_applied"] = True
    return response.model_copy(update={"recall_impact": recall_impact})


def _metadata_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def _metadata_dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [{str(key): item[key] for key in item} for item in value if isinstance(item, dict)]


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def audit_event_response(row: dict[str, object]) -> MemoryAuditEventResponse:
    details = row.get("details") if isinstance(row.get("details"), dict) else {}
    payload = (
        {str(key): value for key, value in details.items()} if isinstance(details, dict) else {}
    )
    return MemoryAuditEventResponse(
        id=str(row.get("uuid") or ""),
        organization_id=str(row["organization_id"]) if row.get("organization_id") else None,
        user_id=str(row["user_id"]) if row.get("user_id") else None,
        action=str(row.get("action") or ""),
        memory_scope=payload.get("memory_scope")
        if isinstance(payload.get("memory_scope"), str)
        else None,
        scope_key=payload.get("scope_key") if isinstance(payload.get("scope_key"), str) else None,
        project_id=payload.get("project_id")
        if isinstance(payload.get("project_id"), str)
        else None,
        source_surface=payload.get("source_surface")
        if isinstance(payload.get("source_surface"), str)
        else None,
        source_ids=_str_list(payload.get("source_ids")),
        source_ids_truncated=payload.get("source_ids_truncated")
        if isinstance(payload.get("source_ids_truncated"), int)
        else None,
        derived_ids=_str_list(payload.get("derived_ids")),
        derived_ids_truncated=payload.get("derived_ids_truncated")
        if isinstance(payload.get("derived_ids_truncated"), int)
        else None,
        policy_allowed=payload.get("policy_allowed")
        if isinstance(payload.get("policy_allowed"), bool)
        else None,
        policy_reason=payload.get("policy_reason")
        if isinstance(payload.get("policy_reason"), str)
        else None,
        details=payload.get("details") if isinstance(payload.get("details"), dict) else {},
        created_at=row.get("created_at") if isinstance(row.get("created_at"), datetime) else None,
    )


def _memory_metadata_str(memory: RawMemory, key: str) -> str | None:
    value = memory.metadata.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def memory_project_id(memory: RawMemory) -> str | None:
    return memory.project_id or _memory_metadata_str(memory, "project_id")


def _memory_lifecycle_state(memory: RawMemory) -> str:
    return str(
        memory_lifecycle_from_metadata(
            memory.metadata,
            source_id=memory.id,
            review_state=memory.review_state,
        ).state
    )


def _memory_lifecycle_redacts_content(memory: RawMemory) -> bool:
    lifecycle = memory_lifecycle_from_metadata(
        memory.metadata,
        source_id=memory.id,
        review_state=memory.review_state,
    )
    return str(lifecycle.state).lower() == "deleted" or "redacted" in lifecycle.flags


def _dedupe_audit_rows(rows: list[dict[str, object]], *, limit: int) -> list[dict[str, object]]:
    seen: set[str] = set()
    deduped: list[dict[str, object]] = []
    for row in rows:
        key = str(row.get("uuid") or row.get("id") or id(row))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    fallback = datetime.min.replace(tzinfo=UTC)
    deduped.sort(
        key=lambda row: (
            row.get("created_at") if isinstance(row.get("created_at"), datetime) else fallback
        ),
        reverse=True,
    )
    return deduped[:limit]


async def source_audit_events(
    *,
    organization_id: str,
    source_id: str,
    memory: RawMemory,
    limit: int = 20,
) -> list[MemoryAuditEventResponse]:
    rows: list[dict[str, object]] = []
    source_filters = list(dict.fromkeys([source_id, memory.id, memory.source_id]))
    for value in source_filters:
        rows.extend(
            await list_memory_audit_events(
                organization_id=organization_id,
                source_id=value,
                limit=limit,
            )
        )
    rows.extend(
        await list_memory_audit_events(
            organization_id=organization_id,
            derived_id=memory.id,
            limit=limit,
        )
    )
    return [audit_event_response(row) for row in _dedupe_audit_rows(rows, limit=limit)]


def _derived_record_type(
    *,
    source_action: str,
    derived_id: str,
    memory: RawMemory,
) -> str:
    if derived_id == memory.id:
        return "raw_memory"
    if "promote" in source_action:
        return "graph_entity"
    if "reflect" in source_action:
        return "reflection"
    if "context" in source_action:
        return "context_render"
    return source_action.removeprefix("memory.").replace(".", "_") or "memory_record"


def _derived_records_from_audit(
    *,
    events: list[MemoryAuditEventResponse],
    memory: RawMemory,
) -> list[MemoryDerivedRecordResponse]:
    records: dict[str, MemoryDerivedRecordResponse] = {}
    for event in events:
        for derived_id in event.derived_ids:
            if derived_id in records:
                continue
            records[derived_id] = MemoryDerivedRecordResponse(
                id=derived_id,
                record_type=_derived_record_type(
                    source_action=event.action,
                    derived_id=derived_id,
                    memory=memory,
                ),
                source_action=event.action,
            )
    return list(records.values())


def _audit_events_for_visibility(
    events: list[MemoryAuditEventResponse],
    *,
    content_visible: bool,
) -> list[MemoryAuditEventResponse]:
    if content_visible:
        return events
    return [event.model_copy(update={"details": {}}) for event in events]


def metadata_dicts(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _correction_history(
    *,
    memory: RawMemory,
    audit_events: list[MemoryAuditEventResponse],
) -> list[dict[str, Any]]:
    history = metadata_dicts(memory.metadata.get("correction_history"))
    for event in audit_events:
        if not (
            event.action.startswith("memory.correction")
            or event.action
            in {
                "memory.hide",
                "memory.redact",
                "memory.restore",
                "memory.delete",
            }
        ):
            continue
        history.append(
            {
                "audit_event_id": event.id,
                "action": event.action,
                "policy_reason": event.policy_reason,
                "derived_ids": list(event.derived_ids),
                "created_at": event.created_at,
            }
        )
    return history


def _promotion_state(
    *,
    memory: RawMemory,
    audit_events: list[MemoryAuditEventResponse],
) -> dict[str, Any]:
    promotion_events = [event.id for event in audit_events if "promote" in event.action]
    promoted_id = _memory_metadata_str(memory, "promoted_entity_id")
    state = "promoted" if memory.review_state == "promoted" or promoted_id else "not_promoted"
    return {
        "state": state,
        "promoted_id": promoted_id,
        "promoted_at": _memory_metadata_str(memory, "promoted_at"),
        "audit_event_ids": promotion_events,
    }


def _share_state(audit_events: list[MemoryAuditEventResponse]) -> dict[str, Any]:
    share_events = [event.id for event in audit_events if "share" in event.action]
    return {
        "state": "previewed" if share_events else "none",
        "audit_event_ids": share_events,
    }


def _transform_versions(metadata: dict[str, object]) -> dict[str, Any]:
    keys = (
        "adapter_version",
        "embedding_model",
        "embedding_model_version",
        "extraction_version",
        "schema_version",
        "source_adapter_version",
        "transform_version",
    )
    return {key: metadata[key] for key in keys if key in metadata}


def _available_source_actions(
    *,
    memory: RawMemory,
    policy_decision: MemoryPolicyDecision,
) -> list[dict[str, Any]]:
    visible = policy_decision.allowed
    lifecycle_open = memory.review_state not in {"archived", "promoted"}
    return [
        {"action": "inspect", "available": True, "preview_required": False},
        {
            "action": "promotion.preview",
            "available": visible and lifecycle_open,
            "preview_required": True,
        },
        {
            "action": "share.preview",
            "available": visible,
            "preview_required": True,
        },
        {
            "action": "correction.preview",
            "available": visible,
            "preview_required": True,
            "reason": None if visible else policy_decision.reason,
        },
    ]


def memory_source_inspect_response(
    *,
    memory: RawMemory,
    policy_decision: MemoryPolicyDecision,
    audit_events: list[MemoryAuditEventResponse],
) -> MemorySourceInspectResponse:
    content_redacted = not policy_decision.allowed or _memory_lifecycle_redacts_content(memory)
    metadata = dict(memory.metadata)
    if content_redacted:
        metadata.pop("memory_lifecycle", None)
        metadata.pop("reflection_findings", None)
        metadata.pop("claim_records", None)
        metadata.pop("content_revisions", None)
    visible_audit_events = _audit_events_for_visibility(
        audit_events,
        content_visible=policy_decision.allowed,
    )
    derived_records = _derived_records_from_audit(events=visible_audit_events, memory=memory)
    derived_ids = [record.id for record in derived_records]
    derived_types = list(dict.fromkeys(record.record_type for record in derived_records))
    project_id = memory_project_id(memory)
    lifecycle = (
        {}
        if content_redacted
        else memory_lifecycle_from_metadata(
            memory.metadata,
            source_id=memory.id,
            review_state=memory.review_state,
        ).to_dict()
    )
    reflection_findings = (
        []
        if content_redacted
        else [finding.to_dict() for finding in reflection_findings_from_metadata(memory.metadata)]
    )
    claim_records = (
        []
        if content_redacted
        else [claim.to_dict() for claim in claim_records_from_metadata(memory.metadata)]
    )
    return MemorySourceInspectResponse(
        id=memory.id,
        organization_id=memory.organization_id,
        source_id=memory.source_id,
        revision=memory.revision,
        principal_id=memory.principal_id,
        agent_id=memory.agent_id,
        project_id=project_id,
        memory_scope=memory.memory_scope.value,
        scope_key=memory.scope_key,
        review_state=memory.review_state,
        visibility={
            "content_visible": policy_decision.allowed,
            "content_redacted": content_redacted,
            "lifecycle_state": _memory_lifecycle_state(memory),
            "memory_scope": memory.memory_scope.value,
            "scope_key": memory.scope_key,
            "principal_id": memory.principal_id,
            "agent_id": memory.agent_id,
            "project_id": project_id,
            "policy_reason": policy_decision.reason,
        },
        lifecycle=lifecycle,
        reflection_findings=reflection_findings,
        claim_records=claim_records,
        correction_history=_correction_history(
            memory=memory,
            audit_events=visible_audit_events,
        ),
        promotion_state=_promotion_state(
            memory=memory,
            audit_events=visible_audit_events,
        ),
        share_state=_share_state(visible_audit_events),
        entity_type=memory.entity_type,
        title=memory.title,
        raw_content=None if content_redacted else memory.raw_content,
        content_redacted=content_redacted,
        raw_content_length=len(memory.raw_content),
        tags=memory.tags,
        metadata=metadata,
        provenance=memory.provenance,
        capture_surface=memory.capture_surface,
        captured_at=memory.captured_at,
        created_at=memory.created_at,
        freshness_timestamps={
            "captured_at": memory.captured_at,
            "created_at": memory.created_at,
        },
        transform_versions=_transform_versions(memory.metadata),
        policy_allowed=policy_decision.allowed,
        policy_reason=policy_decision.reason,
        policy_metadata={
            "policy_action": policy_decision.action.value,
            "content_redacted": content_redacted,
            "source_surface": "memory_inspect",
        },
        derived_ids=derived_ids,
        derived_types=derived_types,
        derived_records=derived_records,
        recent_audit_events=visible_audit_events,
        audit_event_count=len(visible_audit_events),
        available_actions=_available_source_actions(
            memory=memory,
            policy_decision=policy_decision,
        ),
    )
