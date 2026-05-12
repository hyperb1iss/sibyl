"""Memory policy decisions for scoped memory operations."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from sibyl_core.services.surreal_content import MemoryScope


class MemoryPolicyAction(StrEnum):
    READ = "read"
    REFLECT = "reflect"
    SHARE = "share"
    WRITE = "write"


@dataclass(frozen=True, slots=True)
class MemoryPolicyDecision:
    action: MemoryPolicyAction
    allowed: bool
    reason: str
    memory_scope: MemoryScope
    scope_key: str | None = None


def _coerce_scope(value: MemoryScope | str) -> MemoryScope | None:
    if isinstance(value, MemoryScope):
        return value
    try:
        return MemoryScope(str(value))
    except ValueError:
        return None


def _string_set(values: Iterable[str] | None) -> set[str] | None:
    if values is None:
        return None
    return {str(value) for value in values}


def _deny(
    *,
    action: MemoryPolicyAction = MemoryPolicyAction.READ,
    reason: str,
    memory_scope: MemoryScope,
    scope_key: str | None,
) -> MemoryPolicyDecision:
    return MemoryPolicyDecision(
        action=action,
        allowed=False,
        reason=reason,
        memory_scope=memory_scope,
        scope_key=scope_key,
    )


def _allow(
    *,
    action: MemoryPolicyAction = MemoryPolicyAction.READ,
    reason: str,
    memory_scope: MemoryScope,
    scope_key: str | None,
) -> MemoryPolicyDecision:
    return MemoryPolicyDecision(
        action=action,
        allowed=True,
        reason=reason,
        memory_scope=memory_scope,
        scope_key=scope_key,
    )


def authorize_memory_read(
    *,
    principal_id: str | None,
    memory_scope: MemoryScope | str,
    scope_key: str | None = None,
    project_id: str | None = None,
    agent_id: str | None = None,
    accessible_projects: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
) -> MemoryPolicyDecision:
    normalized_scope = _coerce_scope(memory_scope)
    if normalized_scope is None:
        return MemoryPolicyDecision(
            action=MemoryPolicyAction.READ,
            allowed=False,
            reason="scope_not_enabled",
            memory_scope=MemoryScope.PRIVATE,
            scope_key=scope_key,
        )

    if not principal_id:
        return _deny(
            reason="principal_mismatch",
            memory_scope=normalized_scope,
            scope_key=scope_key,
        )

    if normalized_scope is MemoryScope.PRIVATE:
        if agent_id and project_id:
            return _allow(
                reason="agent_diary_private_read_allowed",
                memory_scope=normalized_scope,
                scope_key=scope_key,
            )
        if agent_id:
            return _allow(
                reason="agent_diary_private_read_allowed",
                memory_scope=normalized_scope,
                scope_key=scope_key,
            )
        return _allow(
            reason="private_principal_bound",
            memory_scope=normalized_scope,
            scope_key=scope_key,
        )

    if normalized_scope is MemoryScope.PROJECT:
        if not scope_key:
            return _deny(
                reason="missing_scope_key",
                memory_scope=normalized_scope,
                scope_key=scope_key,
            )
        projects = _string_set(accessible_projects)
        if projects is None or scope_key not in projects:
            return _deny(
                reason="unverified_membership",
                memory_scope=normalized_scope,
                scope_key=scope_key,
            )
        return _allow(
            reason="project_access_verified",
            memory_scope=normalized_scope,
            scope_key=scope_key,
        )

    if normalized_scope is MemoryScope.DELEGATED:
        if not scope_key:
            return _deny(
                reason="missing_scope_key",
                memory_scope=normalized_scope,
                scope_key=scope_key,
            )
        delegations = _string_set(accessible_delegations)
        if delegations is None or scope_key not in delegations:
            return _deny(
                reason="unverified_membership",
                memory_scope=normalized_scope,
                scope_key=scope_key,
            )
        return _allow(
            reason="delegated_access_verified",
            memory_scope=normalized_scope,
            scope_key=scope_key,
        )

    return _deny(
        reason="scope_not_enabled",
        memory_scope=normalized_scope,
        scope_key=scope_key,
    )


def _authorize_mutating_action(
    *,
    action: MemoryPolicyAction,
    principal_id: str | None,
    memory_scope: MemoryScope | str,
    scope_key: str | None = None,
    accessible_projects: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
) -> MemoryPolicyDecision:
    normalized_scope = _coerce_scope(memory_scope)
    if normalized_scope is None:
        return MemoryPolicyDecision(
            action=action,
            allowed=False,
            reason="scope_not_enabled",
            memory_scope=MemoryScope.PRIVATE,
            scope_key=scope_key,
        )

    if not principal_id:
        return _deny(
            action=action,
            reason="principal_mismatch",
            memory_scope=normalized_scope,
            scope_key=scope_key,
        )

    if normalized_scope in {
        MemoryScope.TEAM,
        MemoryScope.SHARED,
        MemoryScope.ORGANIZATION,
        MemoryScope.PUBLIC,
    }:
        return _deny(
            action=action,
            reason="scope_not_enabled",
            memory_scope=normalized_scope,
            scope_key=scope_key,
        )

    if normalized_scope is MemoryScope.PRIVATE:
        if action is MemoryPolicyAction.SHARE:
            return _deny(
                action=action,
                reason="scope_crossing_requires_promotion",
                memory_scope=normalized_scope,
                scope_key=scope_key,
            )
        return _allow(
            action=action,
            reason=f"same_scope_{action.value}_allowed",
            memory_scope=normalized_scope,
            scope_key=scope_key,
        )

    if normalized_scope is MemoryScope.PROJECT:
        if not scope_key:
            return _deny(
                action=action,
                reason="missing_scope_key",
                memory_scope=normalized_scope,
                scope_key=scope_key,
            )
        projects = _string_set(accessible_projects)
        if projects is None or scope_key not in projects:
            return _deny(
                action=action,
                reason="unverified_membership",
                memory_scope=normalized_scope,
                scope_key=scope_key,
            )
        if action is MemoryPolicyAction.SHARE:
            return _deny(
                action=action,
                reason="scope_crossing_requires_promotion",
                memory_scope=normalized_scope,
                scope_key=scope_key,
            )
        return _allow(
            action=action,
            reason=f"same_scope_{action.value}_allowed",
            memory_scope=normalized_scope,
            scope_key=scope_key,
        )

    if normalized_scope is MemoryScope.DELEGATED:
        if not scope_key:
            return _deny(
                action=action,
                reason="missing_scope_key",
                memory_scope=normalized_scope,
                scope_key=scope_key,
            )
        delegations = _string_set(accessible_delegations)
        if delegations is None or scope_key not in delegations:
            return _deny(
                action=action,
                reason="unverified_membership",
                memory_scope=normalized_scope,
                scope_key=scope_key,
            )
        if action is MemoryPolicyAction.SHARE:
            return _deny(
                action=action,
                reason="scope_crossing_requires_promotion",
                memory_scope=normalized_scope,
                scope_key=scope_key,
            )
        return _allow(
            action=action,
            reason=f"same_scope_{action.value}_allowed",
            memory_scope=normalized_scope,
            scope_key=scope_key,
        )

    return _deny(
        action=action,
        reason="scope_not_enabled",
        memory_scope=normalized_scope,
        scope_key=scope_key,
    )


def authorize_memory_write(
    *,
    principal_id: str | None,
    memory_scope: MemoryScope | str,
    scope_key: str | None = None,
    accessible_projects: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
) -> MemoryPolicyDecision:
    return _authorize_mutating_action(
        action=MemoryPolicyAction.WRITE,
        principal_id=principal_id,
        memory_scope=memory_scope,
        scope_key=scope_key,
        accessible_projects=accessible_projects,
        accessible_delegations=accessible_delegations,
    )


def authorize_memory_share(
    *,
    principal_id: str | None,
    memory_scope: MemoryScope | str,
    scope_key: str | None = None,
    accessible_projects: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
) -> MemoryPolicyDecision:
    return _authorize_mutating_action(
        action=MemoryPolicyAction.SHARE,
        principal_id=principal_id,
        memory_scope=memory_scope,
        scope_key=scope_key,
        accessible_projects=accessible_projects,
        accessible_delegations=accessible_delegations,
    )


def authorize_memory_reflect(
    *,
    principal_id: str | None,
    memory_scope: MemoryScope | str,
    scope_key: str | None = None,
    accessible_projects: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
) -> MemoryPolicyDecision:
    return _authorize_mutating_action(
        action=MemoryPolicyAction.REFLECT,
        principal_id=principal_id,
        memory_scope=memory_scope,
        scope_key=scope_key,
        accessible_projects=accessible_projects,
        accessible_delegations=accessible_delegations,
    )


__all__ = [
    "MemoryPolicyAction",
    "MemoryPolicyDecision",
    "authorize_memory_read",
    "authorize_memory_reflect",
    "authorize_memory_share",
    "authorize_memory_write",
]
