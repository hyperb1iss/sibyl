"""Memory scope, authorization, and metadata policy helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, cast

import structlog

from sibyl_core.auth.memory_policy import (
    MemoryPolicyAction,
    MemoryPolicyDecision,
    authorize_memory_read,
    authorize_memory_reflect,
    authorize_memory_share,
    authorize_memory_write,
    memory_metadata_read_allowed,
    memory_row_project_id,
    private_scope_granted_for,
)
from sibyl_core.errors import EntityNotFoundError
from sibyl_core.models.reflection import (
    MemoryLifecycle,
    MemoryLifecycleState,
    ReflectionCandidate,
    ReflectionFinding,
    ReflectionFindingKind,
    with_memory_lifecycle_metadata,
    with_reflection_finding_metadata,
)
from sibyl_core.services.memory_contract import (
    ReflectionPromotionPreview,
    ReflectionPromotionResult,
    WriteMode,
)
from sibyl_core.services.surreal_content import MemoryScope, RawMemory

log = structlog.get_logger()


def _correction_derived_ids(memory: RawMemory) -> list[str]:
    return list(
        dict.fromkeys(
            (
                *_metadata_str_values(
                    memory.metadata,
                    "derived_ids",
                    "promoted_entity_id",
                    "promoted_ids",
                    "relationship_ids",
                ),
            )
        )
    )


def _resolve_memory_scope(
    memory_scope: MemoryScope | str | None,
    project: str | None,
) -> MemoryScope:
    if memory_scope is not None:
        try:
            return MemoryScope(memory_scope)
        except ValueError:
            return MemoryScope.PRIVATE
    return MemoryScope.PROJECT if project else MemoryScope.PRIVATE


def _resolve_scope_key(
    memory_scope: MemoryScope,
    scope_key: str | None,
    project: str | None,
) -> str | None:
    if memory_scope is MemoryScope.PROJECT:
        return scope_key or project
    return scope_key


def _authorize_reflection_write(
    *,
    principal_id: str | None,
    memory_scope: MemoryScope,
    scope_key: str | None,
    accessible_projects: Iterable[str] | None,
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
) -> tuple[MemoryPolicyDecision, MemoryPolicyDecision]:
    reflect_decision = authorize_memory_reflect(
        principal_id=principal_id,
        memory_scope=memory_scope,
        scope_key=scope_key,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
    )
    write_decision = authorize_memory_write(
        principal_id=principal_id,
        memory_scope=memory_scope,
        scope_key=scope_key,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
    )
    return reflect_decision, write_decision


def _policy_metadata(decisions: Sequence[MemoryPolicyDecision]) -> dict[str, Any]:
    return {
        "native_write_mode": WriteMode.ENABLED.value,
        "memory_scope": decisions[0].memory_scope.value,
        "scope_key": decisions[0].scope_key,
        "policy_allowed": all(decision.allowed for decision in decisions),
        "policy_reasons": [decision.reason for decision in decisions],
        "policy_actions": [decision.action.value for decision in decisions],
    }


def _promotion_lifecycle_metadata(
    *,
    metadata: Mapping[str, Any],
    promoted_entity_id: str,
    source_ids: Sequence[str],
    source_id: str | None,
    reason: str,
    policy_metadata: Mapping[str, Any] | None,
) -> dict[str, object]:
    target_source_id = source_id or (source_ids[0] if source_ids else promoted_entity_id)
    next_metadata: dict[str, object] = dict(metadata)
    next_metadata = with_memory_lifecycle_metadata(
        next_metadata,
        MemoryLifecycle(
            state=MemoryLifecycleState.ACTIVE,
            source_id=target_source_id,
            action="promote",
            reason=reason or "reflection_promotion",
            prior_state=_metadata_str(next_metadata, "review_state"),
            derived_ids=[promoted_entity_id],
            reversible=True,
            metadata={"promoted_entity_id": promoted_entity_id},
        ),
    )
    return with_reflection_finding_metadata(
        next_metadata,
        ReflectionFinding(
            kind=ReflectionFindingKind.PROMOTION,
            target_source_id=target_source_id,
            reason=reason or "reflection_promotion",
            action="promote",
            lifecycle_state=MemoryLifecycleState.ACTIVE,
            source_ids=list(source_ids),
            related_source_ids=[promoted_entity_id],
            policy_reasons=_metadata_str_values(policy_metadata or {}, "policy_reasons"),
            metadata={"promoted_entity_id": promoted_entity_id},
        ),
    )


def _policy_denied_message(decisions: Sequence[MemoryPolicyDecision]) -> str:
    denied = [decision.reason for decision in decisions if not decision.allowed]
    reason = denied[0] if denied else "unknown"
    return f"Native reflection promotion denied: {reason}"


def _promotion_denied(
    *,
    candidate_id: str,
    reason: str,
    review_state: str,
    memory_scope: MemoryScope | None,
    scope_key: str | None,
    raw_source_ids: list[str],
    metadata: dict[str, Any] | None = None,
    policy_decisions: Sequence[MemoryPolicyDecision] = (),
) -> ReflectionPromotionResult:
    payload = {"policy_reasons": [reason], "policy_allowed": False}
    if metadata:
        payload.update(metadata)
    return ReflectionPromotionResult(
        success=False,
        candidate_id=candidate_id,
        promoted_id=None,
        reason=reason,
        review_state=review_state,
        memory_scope=memory_scope,
        scope_key=scope_key,
        raw_source_ids=raw_source_ids,
        metadata=payload,
        policy_decisions=tuple(policy_decisions),
    )


def _promotion_preview_from_denial(
    result: ReflectionPromotionResult,
) -> ReflectionPromotionPreview:
    return ReflectionPromotionPreview(
        allowed=False,
        candidate_id=result.candidate_id,
        reason=result.reason,
        review_state=result.review_state,
        memory_scope=result.memory_scope,
        scope_key=result.scope_key,
        raw_source_ids=result.raw_source_ids,
        policy_decisions=result.policy_decisions,
        metadata=result.metadata,
    )


def _authorize_share_target(
    *,
    principal_id: str | None,
    target_scope: MemoryScope | None,
    target_scope_key: str | None,
    recipient_organization_id: str | None,
    organization_id: str,
    accessible_projects: Iterable[str] | None,
    accessible_teams: Iterable[str] | None,
    accessible_delegations: Iterable[str] | None,
) -> MemoryPolicyDecision:
    if target_scope is None:
        return MemoryPolicyDecision(
            action=MemoryPolicyAction.SHARE,
            allowed=False,
            reason="missing_memory_scope",
            memory_scope=MemoryScope.PRIVATE,
            scope_key=target_scope_key,
        )
    if recipient_organization_id and str(recipient_organization_id) != str(organization_id):
        return MemoryPolicyDecision(
            action=MemoryPolicyAction.SHARE,
            allowed=False,
            reason="scope_not_enabled",
            memory_scope=target_scope,
            scope_key=target_scope_key,
        )
    return authorize_memory_share(
        principal_id=principal_id,
        memory_scope=target_scope,
        scope_key=target_scope_key,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
    )


def _authorize_share_source_read(
    *,
    memory: RawMemory,
    principal_id: str | None,
    accessible_projects: Iterable[str] | None,
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
) -> MemoryPolicyDecision:
    if memory.memory_scope is MemoryScope.PRIVATE and memory.principal_id != principal_id:
        return MemoryPolicyDecision(
            action=MemoryPolicyAction.READ,
            allowed=False,
            reason="principal_mismatch",
            memory_scope=memory.memory_scope,
            scope_key=memory.scope_key,
        )
    return authorize_memory_read(
        principal_id=principal_id,
        memory_scope=memory.memory_scope,
        scope_key=memory.scope_key,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
    )


def _authorize_correction_source_write(
    *,
    memory: RawMemory,
    principal_id: str | None,
    accessible_projects: Iterable[str] | None,
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
) -> MemoryPolicyDecision:
    if memory.memory_scope is MemoryScope.PRIVATE and memory.principal_id != principal_id:
        return MemoryPolicyDecision(
            action=MemoryPolicyAction.WRITE,
            allowed=False,
            reason="principal_mismatch",
            memory_scope=memory.memory_scope,
            scope_key=memory.scope_key,
        )
    return authorize_memory_write(
        principal_id=principal_id,
        memory_scope=memory.memory_scope,
        scope_key=memory.scope_key,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
    )


def _policy_denial_reason(metadata: Mapping[str, Any]) -> str:
    reasons = metadata.get("policy_reasons")
    if isinstance(reasons, list):
        denied = [str(reason) for reason in reasons if str(reason)]
        if denied:
            return denied[0]
    return "promotion_policy_denied"


def _metadata_str(metadata: Mapping[str, object], key: str) -> str | None:
    value = metadata.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _metadata_str_list(metadata: Mapping[str, object], key: str) -> list[str]:
    value = metadata.get(key)
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def _metadata_str_values(metadata: Mapping[str, object], *keys: str) -> list[str]:
    values: list[str] = []
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str):
            values.append(value)
            continue
        if isinstance(value, Iterable) and not isinstance(value, Mapping):
            values.extend(str(item) for item in value if str(item))
    return list(dict.fromkeys(item for item in values if item))


def _metadata_dict_values(metadata: Mapping[str, object], key: str) -> list[dict[str, object]]:
    value = metadata.get(key)
    if not isinstance(value, list):
        return []
    dictionaries: list[dict[str, object]] = []
    for item in value:
        if isinstance(item, Mapping):
            mapping = cast(Mapping[object, object], item)
            dictionaries.append({str(field): item_value for field, item_value in mapping.items()})
    return dictionaries


def _metadata_float(metadata: Mapping[str, object], key: str, default: float) -> float:
    value = metadata.get(key)
    if isinstance(value, int | float | str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _raw_source_ids(memory: RawMemory) -> list[str]:
    return list(dict.fromkeys(_metadata_str_list(memory.metadata, "raw_source_ids")))


def _candidate_source_ids(
    candidate: ReflectionCandidate,
    source_id: str | None,
) -> list[str]:
    return list(
        dict.fromkeys(
            item
            for item in (
                *([source_id] if source_id else []),
                *candidate.raw_source_ids,
                *_metadata_str_values(candidate.metadata, "raw_source_ids", "source_ids"),
            )
            if item
        )
    )


_SUPERSEDES_METADATA_KEYS = (
    "supersedes",
    "supersedes_ids",
    "superseded_ids",
    "supersedes_entity_ids",
)


def _superseded_entity_ids(metadata: Mapping[str, object]) -> list[str]:
    return _metadata_str_values(metadata, *_SUPERSEDES_METADATA_KEYS)


def _with_authorized_supersedes(
    metadata: Mapping[str, object], authorized_ids: Sequence[str]
) -> dict[str, object]:
    """Replace any supersedes id list in metadata with the authorized targets."""
    sanitized = dict(metadata)
    for key in _SUPERSEDES_METADATA_KEYS:
        if key in sanitized:
            sanitized[key] = list(authorized_ids)
    return sanitized


def _raw_memory_write_allowed(
    *,
    memory: RawMemory,
    principal_id: str | None,
    accessible_projects: Iterable[str] | None,
) -> bool:
    if memory.memory_scope is MemoryScope.PRIVATE and memory.principal_id != principal_id:
        return False
    decision = authorize_memory_write(
        principal_id=principal_id,
        memory_scope=memory.memory_scope,
        scope_key=memory.scope_key,
        accessible_projects=accessible_projects,
    )
    return decision.allowed


def _promoted_entity_owner_id(entity: Any, metadata: Mapping[str, object]) -> str | None:
    owner = getattr(entity, "created_by", None)
    if owner:
        return str(owner)
    for key in ("principal_id", "created_by_user_id"):
        value = _metadata_str(metadata, key)
        if value:
            return value
    return None


def _promoted_entity_write_allowed(
    *,
    entity: Any,
    principal_id: str | None,
    accessible_projects: Iterable[str] | None,
) -> bool:
    raw_metadata = getattr(entity, "metadata", {})
    metadata = raw_metadata if isinstance(raw_metadata, Mapping) else {}
    target_scope = _resolve_memory_scope(
        _metadata_str(metadata, "memory_scope"),
        _metadata_str(metadata, "project_id"),
    )
    if target_scope is MemoryScope.PRIVATE:
        owner_id = _promoted_entity_owner_id(entity, metadata)
        if not owner_id or owner_id != principal_id:
            return False
    target_scope_key = _resolve_scope_key(
        target_scope,
        _metadata_str(metadata, "scope_key"),
        _metadata_str(metadata, "project_id"),
    )
    decision = authorize_memory_write(
        principal_id=principal_id,
        memory_scope=target_scope,
        scope_key=target_scope_key,
        accessible_projects=accessible_projects,
    )
    return decision.allowed


def suppression_target_visible(
    target: Any,
    *,
    principal_id: str | None,
    accessible_projects: Iterable[str] | None,
    allowed_memory_scope_keys: Iterable[str] | None = None,
) -> bool:
    """May this principal declare that `target` is replaced or wrong?

    The question is visibility, not write authority. Asserting a memory is
    stale is a statement about a row the writer read, so the rule is the one
    every read surface already enforces: you may suppress what you can see.
    That blocks the attack worth blocking, another principal's private memory,
    while leaving ordinary org-visible rows suppressible by the people who work
    with them.

    Write authority is the wrong test and was the first thing tried here.
    `authorize_memory_write` refuses SHARED, ORGANIZATION, and PUBLIC outright
    (`auth/memory_policy.py`, `scope_not_enabled`) because those scopes are not
    writable destinations, and it reads an unstamped row as private-with-no-
    owner. Gating on it disabled supersession for most of a real corpus and for
    every row its own author created.
    """
    raw_metadata = getattr(target, "metadata", {})
    metadata = raw_metadata if isinstance(raw_metadata, Mapping) else {}
    return memory_metadata_read_allowed(
        metadata,
        principal_id=principal_id,
        private_scope_granted=private_scope_granted_for(
            allowed_memory_scope_keys, principal_id=principal_id
        ),
        accessible_projects=accessible_projects,
        row_project_id=memory_row_project_id(
            metadata,
            entity_type=str(getattr(target, "entity_type", "") or ""),
            entity_id=str(getattr(target, "id", "") or ""),
        ),
        allowed_memory_scope_keys=allowed_memory_scope_keys,
    )


async def declared_suppression_allowed(
    *,
    entity_manager: Any,
    target_id: str,
    principal_id: str | None,
    accessible_projects: Iterable[str] | None,
    allowed_memory_scope_keys: Iterable[str] | None = None,
) -> bool:
    """Load a declared suppression target and decide whether it may be claimed.

    A target that does not exist and a target the writer cannot see get the
    same answer, so the refusal cannot be read as evidence that a row is there.
    """
    try:
        target = await entity_manager.get(target_id)
    except Exception:
        return False
    if target is None:
        return False
    return suppression_target_visible(
        target,
        principal_id=principal_id,
        accessible_projects=accessible_projects,
        allowed_memory_scope_keys=allowed_memory_scope_keys,
    )


async def _authorized_superseded_entity_ids(
    *,
    runtime: Any,
    principal_id: str | None,
    accessible_projects: Iterable[str] | None,
    candidate: ReflectionCandidate,
) -> list[str]:
    authorized_ids: list[str] = []
    for entity_id in _superseded_entity_ids(candidate.metadata):
        # Absence only, for the same reason as the link targets: swallowing a
        # transient failure here promotes a memory that reports success while
        # the supersession it was written to record never happened.
        try:
            target_entity = await runtime.entity_manager.get(entity_id)
        except (EntityNotFoundError, KeyError):
            continue
        if target_entity is None:
            continue
        if _promoted_entity_write_allowed(
            entity=target_entity,
            principal_id=principal_id,
            accessible_projects=accessible_projects,
        ):
            authorized_ids.append(entity_id)
    return authorized_ids
