"""Memory correction preview and application services."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from sibyl_core.auth.memory_policy import MemoryPolicyDecision
from sibyl_core.models.reflection import (
    MemoryLifecycle,
    MemoryLifecycleFlag,
    MemoryLifecycleState,
    ReflectionFinding,
    correction_finding_kind,
    memory_lifecycle_from_metadata,
    with_memory_lifecycle_metadata,
    with_reflection_finding_metadata,
)
from sibyl_core.services.memory_contract import MemoryCorrectionPreview, MemoryCorrectionResult
from sibyl_core.services.memory_lifecycle import _project_correction_to_graph
from sibyl_core.services.memory_policy import (
    _authorize_correction_source_write,
    _authorize_share_source_read,
    _correction_derived_ids,
    _metadata_dict_values,
    _metadata_str,
    _metadata_str_values,
)
from sibyl_core.services.surreal_content import (
    RawMemory,
    get_raw_memory,
    get_raw_memory_by_source_id,
    raw_memory_recallable,
    save_raw_memory,
)

_CORRECTION_TARGET_STATES: dict[str, MemoryLifecycleState] = {
    "delete": MemoryLifecycleState.DELETED,
    "mark_duplicate": MemoryLifecycleState.CONTESTED,
    "mark_stale": MemoryLifecycleState.CONTESTED,
    "mark_wrong": MemoryLifecycleState.CONTESTED,
    "restore": MemoryLifecycleState.ACTIVE,
    "supersede": MemoryLifecycleState.SUPERSEDED,
}


_CORRECTION_TARGET_FLAGS: dict[str, MemoryLifecycleFlag] = {
    "hide": MemoryLifecycleFlag.HIDDEN,
    "mark_sensitive": MemoryLifecycleFlag.SENSITIVE,
    "redact": MemoryLifecycleFlag.REDACTED,
}


_CORRECTION_ACTIONS = frozenset({*_CORRECTION_TARGET_STATES, *_CORRECTION_TARGET_FLAGS, "revise"})


_CORRECTION_ACTION_ALIASES = {
    "duplicate": "mark_duplicate",
    "stale": "mark_stale",
    "superseded": "supersede",
    "wrong": "mark_wrong",
}


_CORRECTION_RECALL_EXCLUDED_STATES = frozenset(
    state.value
    for state in (
        MemoryLifecycleState.ARCHIVED,
        MemoryLifecycleState.CONTESTED,
        MemoryLifecycleState.DELETED,
        MemoryLifecycleState.SUPERSEDED,
    )
)


_CORRECTION_RECALL_EXCLUDED_FLAGS = frozenset(flag.value for flag in MemoryLifecycleFlag)


_CORRECTION_IRREVERSIBLE_ACTIONS = frozenset({"delete", "redact"})


async def _load_correction_memory(
    *,
    organization_id: str,
    source_id: str,
) -> RawMemory | None:
    memory = await get_raw_memory(organization_id=organization_id, memory_id=source_id)
    if memory is not None:
        return memory
    return await get_raw_memory_by_source_id(organization_id=organization_id, source_id=source_id)


def _correction_audit_action(action: str) -> str:
    return f"memory.correction.{action}"


def _correction_preview_denied(
    *,
    source_id: str,
    action: str,
    reason: str,
    target_lifecycle_state: str = "",
    target_lifecycle_flags: Sequence[str] = (),
    policy_decisions: Sequence[MemoryPolicyDecision] = (),
    metadata: dict[str, Any] | None = None,
) -> MemoryCorrectionPreview:
    return MemoryCorrectionPreview(
        allowed=False,
        source_id=source_id,
        action=action,
        reason=reason,
        target_lifecycle_state=target_lifecycle_state,
        target_lifecycle_flags=list(target_lifecycle_flags),
        affected_source_ids=[],
        affected_derived_ids=[],
        reversible=False,
        recall_impact={"excluded_from_recall": False, "reason": reason},
        synthesis_impact={"excluded_from_synthesis": False, "reason": reason},
        audit_action=_correction_audit_action(action or "unknown"),
        policy_decisions=tuple(policy_decisions),
        metadata=metadata or {"policy_allowed": False, "policy_reasons": [reason]},
    )


def _correction_requirement_reason(
    *,
    action: str,
    replacement_source_id: str | None,
    duplicate_of_source_id: str | None,
    revised_content: str | None,
    current_content: str,
) -> str | None:
    if action == "supersede" and not replacement_source_id:
        return "missing_replacement_source"
    if action == "mark_duplicate" and not duplicate_of_source_id:
        return "missing_duplicate_source"
    if action == "revise":
        if revised_content is None or not revised_content.strip():
            return "missing_revised_content"
        if revised_content == current_content:
            return "unchanged_revised_content"
    return None


async def _validate_correction_reference(
    *,
    organization_id: str,
    memory: RawMemory,
    reference_source_id: str,
    reference_kind: str,
    principal_id: str | None,
    accessible_projects: Iterable[str] | None,
    accessible_teams: Iterable[str] | None,
    accessible_delegations: Iterable[str] | None,
) -> tuple[RawMemory | None, str | None]:
    reference = await _load_correction_memory(
        organization_id=organization_id,
        source_id=reference_source_id,
    )
    if reference is None:
        return None, f"{reference_kind}_source_not_found"
    if reference.id == memory.id:
        return None, f"{reference_kind}_source_self_reference"
    read_decision = _authorize_share_source_read(
        memory=reference,
        principal_id=principal_id,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
    )
    if not read_decision.allowed:
        return None, f"{reference_kind}_source_not_found"
    if not raw_memory_recallable(reference):
        return None, f"{reference_kind}_source_not_recallable"
    return reference, None


def _correction_impact(
    target_lifecycle_state: str,
    target_lifecycle_flags: Sequence[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    excluded = target_lifecycle_state in _CORRECTION_RECALL_EXCLUDED_STATES or bool(
        _CORRECTION_RECALL_EXCLUDED_FLAGS.intersection(target_lifecycle_flags)
    )
    recall = {
        "excluded_from_recall": excluded,
        "target_lifecycle_state": target_lifecycle_state,
        "target_lifecycle_flags": list(target_lifecycle_flags),
    }
    synthesis = {
        "excluded_from_synthesis": excluded,
        "preserve_source_truth": True,
        "target_lifecycle_state": target_lifecycle_state,
        "target_lifecycle_flags": list(target_lifecycle_flags),
    }
    return recall, synthesis


async def preview_memory_correction(
    *,
    organization_id: str,
    source_id: str,
    principal_id: str | None,
    action: str,
    reason: str | None = None,
    accessible_projects: Iterable[str] | None = None,
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
    replacement_source_id: str | None = None,
    duplicate_of_source_id: str | None = None,
    revised_content: str | None = None,
) -> MemoryCorrectionPreview:
    requested_action = action.strip().lower()
    normalized_action = _CORRECTION_ACTION_ALIASES.get(requested_action, requested_action)
    if normalized_action not in _CORRECTION_ACTIONS:
        return _correction_preview_denied(
            source_id=source_id,
            action=normalized_action,
            reason="invalid_correction_action",
        )

    memory = await _load_correction_memory(
        organization_id=organization_id,
        source_id=source_id,
    )
    if memory is None:
        return _correction_preview_denied(
            source_id=source_id,
            action=normalized_action,
            reason="memory_source_not_found",
        )
    lifecycle = memory_lifecycle_from_metadata(
        memory.metadata,
        source_id=memory.id,
        review_state=memory.review_state,
    )
    target_state = _CORRECTION_TARGET_STATES.get(normalized_action)
    if target_state is None:
        target_state = MemoryLifecycleState(str(lifecycle.state))
    target_lifecycle_state = target_state.value
    target_lifecycle_flags = [] if normalized_action == "restore" else list(lifecycle.flags)
    if target_flag := _CORRECTION_TARGET_FLAGS.get(normalized_action):
        target_lifecycle_flags = list(dict.fromkeys([*target_lifecycle_flags, target_flag.value]))

    write_decision = _authorize_correction_source_write(
        memory=memory,
        principal_id=principal_id,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
    )
    if not write_decision.allowed:
        return _correction_preview_denied(
            source_id=memory.id,
            action=normalized_action,
            reason=write_decision.reason,
            target_lifecycle_state=target_lifecycle_state,
            target_lifecycle_flags=target_lifecycle_flags,
            policy_decisions=(write_decision,),
            metadata={
                "policy_allowed": False,
                "policy_reasons": [write_decision.reason],
                "requested_source_id": source_id,
            },
        )

    requirement_reason = _correction_requirement_reason(
        action=normalized_action,
        replacement_source_id=replacement_source_id,
        duplicate_of_source_id=duplicate_of_source_id,
        revised_content=revised_content,
        current_content=memory.raw_content,
    )
    if requirement_reason:
        return _correction_preview_denied(
            source_id=memory.id,
            action=normalized_action,
            reason=requirement_reason,
            target_lifecycle_state=target_lifecycle_state,
            target_lifecycle_flags=target_lifecycle_flags,
            policy_decisions=(write_decision,),
            metadata={
                "policy_allowed": False,
                "policy_reasons": [requirement_reason],
                "requested_source_id": source_id,
            },
        )

    canonical_replacement_source_id = replacement_source_id
    canonical_duplicate_of_source_id = duplicate_of_source_id
    if replacement_source_id:
        reference, reference_reason = await _validate_correction_reference(
            organization_id=organization_id,
            memory=memory,
            reference_source_id=replacement_source_id,
            reference_kind="replacement",
            principal_id=principal_id,
            accessible_projects=accessible_projects,
            accessible_teams=accessible_teams,
            accessible_delegations=accessible_delegations,
        )
        if reference_reason:
            return _correction_preview_denied(
                source_id=memory.id,
                action=normalized_action,
                reason=reference_reason,
                target_lifecycle_state=target_lifecycle_state,
                target_lifecycle_flags=target_lifecycle_flags,
                policy_decisions=(write_decision,),
                metadata={
                    "policy_allowed": False,
                    "policy_reasons": [reference_reason],
                    "requested_source_id": source_id,
                    "replacement_source_id": replacement_source_id,
                },
            )
        canonical_replacement_source_id = reference.id if reference else replacement_source_id
    if duplicate_of_source_id:
        reference, reference_reason = await _validate_correction_reference(
            organization_id=organization_id,
            memory=memory,
            reference_source_id=duplicate_of_source_id,
            reference_kind="duplicate",
            principal_id=principal_id,
            accessible_projects=accessible_projects,
            accessible_teams=accessible_teams,
            accessible_delegations=accessible_delegations,
        )
        if reference_reason:
            return _correction_preview_denied(
                source_id=memory.id,
                action=normalized_action,
                reason=reference_reason,
                target_lifecycle_state=target_lifecycle_state,
                target_lifecycle_flags=target_lifecycle_flags,
                policy_decisions=(write_decision,),
                metadata={
                    "duplicate_of_source_id": duplicate_of_source_id,
                    "policy_allowed": False,
                    "policy_reasons": [reference_reason],
                    "requested_source_id": source_id,
                },
            )
        canonical_duplicate_of_source_id = reference.id if reference else duplicate_of_source_id

    recall_impact, synthesis_impact = _correction_impact(
        target_lifecycle_state,
        target_lifecycle_flags,
    )
    affected_derived_ids = _correction_derived_ids(memory)
    metadata = {
        "duplicate_of_source_id": canonical_duplicate_of_source_id,
        "policy_allowed": True,
        "policy_reasons": [write_decision.reason],
        "replacement_source_id": canonical_replacement_source_id,
        "requested_source_id": source_id,
        "revises_content": normalized_action == "revise",
    }
    return MemoryCorrectionPreview(
        allowed=True,
        source_id=memory.id,
        action=normalized_action,
        reason=reason or f"{normalized_action}_preview_allowed",
        target_lifecycle_state=target_lifecycle_state,
        target_lifecycle_flags=target_lifecycle_flags,
        affected_source_ids=[memory.id],
        affected_derived_ids=affected_derived_ids,
        reversible=normalized_action not in _CORRECTION_IRREVERSIBLE_ACTIONS,
        recall_impact=recall_impact,
        synthesis_impact=synthesis_impact,
        audit_action=_correction_audit_action(normalized_action),
        policy_decisions=(write_decision,),
        metadata=metadata,
    )


def _correction_metadata(
    *,
    memory: RawMemory,
    preview: MemoryCorrectionPreview,
    reason: str | None,
    replacement_source_id: str | None,
    duplicate_of_source_id: str | None,
    revised_content: str | None,
    principal_id: str | None,
) -> dict[str, object]:
    metadata = dict(memory.metadata)
    history = list(_metadata_dict_values(metadata, "correction_history"))
    now = datetime.now(UTC).isoformat()
    prior_lifecycle = memory_lifecycle_from_metadata(
        metadata,
        source_id=memory.id,
        review_state=memory.review_state,
    )
    prior_state = str(prior_lifecycle.state)
    if preview.action == "revise" and revised_content is not None:
        revisions = list(_metadata_dict_values(metadata, "content_revisions"))
        revisions.append(
            {
                "revision": memory.revision,
                "content": memory.raw_content,
                "reason": reason or preview.reason,
                "created_at": now,
                "created_by_user_id": principal_id,
            }
        )
        metadata["content_revisions"] = revisions
    if preview.action == "restore":
        for key in (
            "deleted_at",
            "duplicate_at",
            "duplicate_of_source_id",
            "hidden_at",
            "lifecycle_action",
            "lifecycle_reason",
            "lifecycle_state",
            "lifecycle_flags",
            "prior_review_state",
            "redacted_at",
            "sensitive_at",
            "stale_at",
            "superseded_at",
            "superseded_by_source_id",
            "wrong_at",
        ):
            metadata.pop(key, None)
        metadata["restored_at"] = now
        lifecycle = MemoryLifecycle(
            state=preview.target_lifecycle_state,
            source_id=memory.id,
            action=preview.action,
            reason=reason or preview.reason,
            prior_state=prior_state,
            reversible=True,
        )
    else:
        metadata["lifecycle_action"] = preview.action
        metadata["lifecycle_state"] = preview.target_lifecycle_state
        metadata["lifecycle_flags"] = preview.target_lifecycle_flags
        metadata["lifecycle_reason"] = reason or preview.reason
        metadata[f"{preview.action.removeprefix('mark_')}_at"] = now
        if replacement_source_id:
            metadata["superseded_by_source_id"] = replacement_source_id
        if duplicate_of_source_id:
            metadata["duplicate_of_source_id"] = duplicate_of_source_id
        lifecycle = MemoryLifecycle(
            state=preview.target_lifecycle_state,
            source_id=memory.id,
            action=preview.action,
            reason=reason or preview.reason,
            prior_state=prior_state,
            replacement_source_id=replacement_source_id,
            duplicate_of_source_id=duplicate_of_source_id,
            derived_ids=preview.affected_derived_ids,
            flags=preview.target_lifecycle_flags,
            reversible=preview.reversible,
        )
    history.append(
        {
            "action": preview.action,
            "audit_action": preview.audit_action,
            "reason": reason or preview.reason,
            "target_lifecycle_state": preview.target_lifecycle_state,
            "target_lifecycle_flags": preview.target_lifecycle_flags,
            "created_at": now,
            "replacement_source_id": replacement_source_id,
            "duplicate_of_source_id": duplicate_of_source_id,
            "prior_revision": memory.revision,
            "created_by_user_id": principal_id,
        }
    )
    metadata["correction_history"] = history
    metadata = with_memory_lifecycle_metadata(metadata, lifecycle)
    return with_reflection_finding_metadata(
        metadata,
        ReflectionFinding(
            kind=correction_finding_kind(preview.action),
            target_source_id=memory.id,
            reason=reason or preview.reason,
            action=preview.action,
            lifecycle_state=lifecycle.state,
            source_ids=[memory.id],
            related_source_ids=[
                item for item in (replacement_source_id, duplicate_of_source_id) if item is not None
            ],
            policy_reasons=_metadata_str_values(preview.metadata or {}, "policy_reasons"),
            reversible=preview.reversible,
            metadata={
                "audit_action": preview.audit_action,
                "target_lifecycle_state": preview.target_lifecycle_state,
                "target_lifecycle_flags": preview.target_lifecycle_flags,
            },
        ),
    )


async def apply_memory_correction(
    *,
    organization_id: str,
    source_id: str,
    principal_id: str | None,
    action: str,
    reason: str | None = None,
    accessible_projects: Iterable[str] | None = None,
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
    replacement_source_id: str | None = None,
    duplicate_of_source_id: str | None = None,
    revised_content: str | None = None,
    expected_revision: int | None = None,
) -> MemoryCorrectionResult:
    preview = await preview_memory_correction(
        organization_id=organization_id,
        source_id=source_id,
        principal_id=principal_id,
        action=action,
        reason=reason,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
        replacement_source_id=replacement_source_id,
        duplicate_of_source_id=duplicate_of_source_id,
        revised_content=revised_content,
    )
    if not preview.allowed:
        return MemoryCorrectionResult(applied=False, preview=preview)

    memory = await _load_correction_memory(organization_id=organization_id, source_id=source_id)
    if memory is None:
        denied = _correction_preview_denied(
            source_id=source_id,
            action=preview.action,
            reason="memory_source_not_found",
            target_lifecycle_state=preview.target_lifecycle_state,
            target_lifecycle_flags=preview.target_lifecycle_flags,
        )
        return MemoryCorrectionResult(applied=False, preview=denied)
    preview_metadata = preview.metadata or {}
    canonical_replacement_source_id = (
        _metadata_str(preview_metadata, "replacement_source_id") or replacement_source_id
    )
    canonical_duplicate_of_source_id = (
        _metadata_str(preview_metadata, "duplicate_of_source_id") or duplicate_of_source_id
    )
    review_state = memory.review_state
    if preview.action == "restore" and review_state in {
        "deleted",
        "duplicate",
        "hidden",
        "redacted",
        "sensitive",
        "stale",
        "superseded",
        "wrong",
    }:
        review_state = _metadata_str(memory.metadata, "prior_review_state") or "pending"
    updated = replace(
        memory,
        raw_content=revised_content if preview.action == "revise" else memory.raw_content,
        review_state=review_state,
        metadata=_correction_metadata(
            memory=memory,
            preview=preview,
            reason=reason,
            replacement_source_id=canonical_replacement_source_id,
            duplicate_of_source_id=canonical_duplicate_of_source_id,
            revised_content=revised_content,
            principal_id=principal_id,
        ),
    )
    save_kwargs: dict[str, Any] = {}
    if expected_revision is not None:
        save_kwargs["expected_revision"] = expected_revision
    if preview.action == "supersede" and canonical_replacement_source_id is not None:
        save_kwargs["superseded_by_memory_id"] = canonical_replacement_source_id
    saved = await save_raw_memory(updated, **save_kwargs)
    (
        affected_entity_ids,
        refused_entity_ids,
        projection_walk_truncated,
    ) = await _project_correction_to_graph(
        organization_id=organization_id,
        memory=saved,
        preview=preview,
        principal_id=principal_id,
        accessible_projects=accessible_projects,
        replacement_source_id=canonical_replacement_source_id,
        duplicate_of_source_id=canonical_duplicate_of_source_id,
    )
    return MemoryCorrectionResult(
        applied=True,
        preview=preview,
        updated_memory=saved,
        affected_entity_ids=affected_entity_ids,
        refused_entity_ids=refused_entity_ids,
        projection_walk_truncated=projection_walk_truncated,
    )
