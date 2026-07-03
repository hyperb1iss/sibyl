"""Memory policy decisions for scoped memory operations."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from sibyl_core.auth.context import MemoryPolicyContext
from sibyl_core.models.memory_scope import MemoryScope


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
    policy_context: MemoryPolicyContext | None = None


@dataclass(frozen=True, slots=True)
class _PolicyInputs:
    principal_id: str | None
    memory_scope: MemoryScope | str | None
    scope_key: str | None
    project_id: str | None
    agent_id: str | None
    accessible_projects: Iterable[str] | None
    accessible_teams: Iterable[str] | None
    accessible_delegations: Iterable[str] | None


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


def memory_scope_policy_key(memory_scope: MemoryScope | str, scope_key: str | None) -> str:
    """Canonical policy key for an API-key memory-space grant.

    Must stay byte-identical to the API's api_key_memory_scope_key so native
    retrieval scope filtering matches the keys minted onto API keys.
    """
    return f"{str(memory_scope).strip()}\x1f{'' if scope_key is None else str(scope_key).strip()}"


def _deny(
    *,
    action: MemoryPolicyAction = MemoryPolicyAction.READ,
    reason: str,
    memory_scope: MemoryScope,
    scope_key: str | None,
    policy_context: MemoryPolicyContext | None = None,
) -> MemoryPolicyDecision:
    return MemoryPolicyDecision(
        action=action,
        allowed=False,
        reason=reason,
        memory_scope=memory_scope,
        scope_key=scope_key,
        policy_context=policy_context,
    )


def _allow(
    *,
    action: MemoryPolicyAction = MemoryPolicyAction.READ,
    reason: str,
    memory_scope: MemoryScope,
    scope_key: str | None,
    policy_context: MemoryPolicyContext | None = None,
) -> MemoryPolicyDecision:
    return MemoryPolicyDecision(
        action=action,
        allowed=True,
        reason=reason,
        memory_scope=memory_scope,
        scope_key=scope_key,
        policy_context=policy_context,
    )


def _resolve_policy_inputs(
    *,
    policy_context: MemoryPolicyContext | None,
    principal_id: str | None,
    memory_scope: MemoryScope | str | None,
    scope_key: str | None,
    project_id: str | None,
    agent_id: str | None,
    accessible_projects: Iterable[str] | None,
    accessible_teams: Iterable[str] | None,
    accessible_delegations: Iterable[str] | None,
) -> _PolicyInputs:
    if policy_context is None:
        return _PolicyInputs(
            principal_id=principal_id,
            memory_scope=memory_scope,
            scope_key=scope_key,
            project_id=project_id,
            agent_id=agent_id,
            accessible_projects=accessible_projects,
            accessible_teams=accessible_teams,
            accessible_delegations=accessible_delegations,
        )

    return _PolicyInputs(
        principal_id=principal_id if principal_id is not None else policy_context.actor_user_id,
        memory_scope=memory_scope if memory_scope is not None else policy_context.memory_space,
        scope_key=scope_key if scope_key is not None else policy_context.scope_key,
        project_id=project_id if project_id is not None else policy_context.project_id,
        agent_id=agent_id if agent_id is not None else policy_context.agent_id,
        accessible_projects=accessible_projects
        if accessible_projects is not None
        else policy_context.accessible_projects,
        accessible_teams=accessible_teams
        if accessible_teams is not None
        else policy_context.accessible_teams,
        accessible_delegations=accessible_delegations
        if accessible_delegations is not None
        else policy_context.accessible_delegations,
    )


def _missing_memory_scope(
    *,
    action: MemoryPolicyAction,
    scope_key: str | None,
    policy_context: MemoryPolicyContext | None,
) -> MemoryPolicyDecision:
    return MemoryPolicyDecision(
        action=action,
        allowed=False,
        reason="missing_memory_scope",
        memory_scope=MemoryScope.PRIVATE,
        scope_key=scope_key,
        policy_context=policy_context,
    )


def authorize_memory_read(
    *,
    principal_id: str | None = None,
    memory_scope: MemoryScope | str | None = None,
    scope_key: str | None = None,
    project_id: str | None = None,
    agent_id: str | None = None,
    accessible_projects: Iterable[str] | None = None,
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
    policy_context: MemoryPolicyContext | None = None,
) -> MemoryPolicyDecision:
    inputs = _resolve_policy_inputs(
        policy_context=policy_context,
        principal_id=principal_id,
        memory_scope=memory_scope,
        scope_key=scope_key,
        project_id=project_id,
        agent_id=agent_id,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
    )
    if inputs.memory_scope is None:
        return _missing_memory_scope(
            action=MemoryPolicyAction.READ,
            scope_key=inputs.scope_key,
            policy_context=policy_context,
        )

    normalized_scope = _coerce_scope(inputs.memory_scope)
    if normalized_scope is None:
        return MemoryPolicyDecision(
            action=MemoryPolicyAction.READ,
            allowed=False,
            reason="scope_not_enabled",
            memory_scope=MemoryScope.PRIVATE,
            scope_key=inputs.scope_key,
            policy_context=policy_context,
        )

    if not inputs.principal_id:
        return _deny(
            reason="principal_mismatch",
            memory_scope=normalized_scope,
            scope_key=inputs.scope_key,
            policy_context=policy_context,
        )

    if normalized_scope is MemoryScope.PRIVATE:
        if inputs.agent_id:
            return _allow(
                reason="agent_diary_private_read_allowed",
                memory_scope=normalized_scope,
                scope_key=inputs.scope_key,
                policy_context=policy_context,
            )
        return _allow(
            reason="private_principal_bound",
            memory_scope=normalized_scope,
            scope_key=inputs.scope_key,
            policy_context=policy_context,
        )

    if normalized_scope is MemoryScope.PROJECT:
        if not inputs.scope_key:
            return _deny(
                reason="missing_scope_key",
                memory_scope=normalized_scope,
                scope_key=inputs.scope_key,
                policy_context=policy_context,
            )
        projects = _string_set(inputs.accessible_projects)
        if projects is None or inputs.scope_key not in projects:
            return _deny(
                reason="unverified_membership",
                memory_scope=normalized_scope,
                scope_key=inputs.scope_key,
                policy_context=policy_context,
            )
        return _allow(
            reason="project_access_verified",
            memory_scope=normalized_scope,
            scope_key=inputs.scope_key,
            policy_context=policy_context,
        )

    if normalized_scope is MemoryScope.DELEGATED:
        if not inputs.scope_key:
            return _deny(
                reason="missing_scope_key",
                memory_scope=normalized_scope,
                scope_key=inputs.scope_key,
                policy_context=policy_context,
            )
        delegations = _string_set(inputs.accessible_delegations)
        if delegations is None or inputs.scope_key not in delegations:
            return _deny(
                reason="unverified_membership",
                memory_scope=normalized_scope,
                scope_key=inputs.scope_key,
                policy_context=policy_context,
            )
        return _allow(
            reason="delegated_access_verified",
            memory_scope=normalized_scope,
            scope_key=inputs.scope_key,
            policy_context=policy_context,
        )

    if normalized_scope is MemoryScope.TEAM:
        if not inputs.scope_key:
            return _deny(
                reason="missing_scope_key",
                memory_scope=normalized_scope,
                scope_key=inputs.scope_key,
                policy_context=policy_context,
            )
        teams = _string_set(inputs.accessible_teams)
        if teams is None or inputs.scope_key not in teams:
            return _deny(
                reason="unverified_membership",
                memory_scope=normalized_scope,
                scope_key=inputs.scope_key,
                policy_context=policy_context,
            )
        return _allow(
            reason="team_access_verified",
            memory_scope=normalized_scope,
            scope_key=inputs.scope_key,
            policy_context=policy_context,
        )

    return _deny(
        reason="scope_not_enabled",
        memory_scope=normalized_scope,
        scope_key=inputs.scope_key,
        policy_context=policy_context,
    )


def _authorize_mutating_action(
    *,
    action: MemoryPolicyAction,
    principal_id: str | None = None,
    memory_scope: MemoryScope | str | None = None,
    scope_key: str | None = None,
    accessible_projects: Iterable[str] | None = None,
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
    policy_context: MemoryPolicyContext | None = None,
) -> MemoryPolicyDecision:
    inputs = _resolve_policy_inputs(
        policy_context=policy_context,
        principal_id=principal_id,
        memory_scope=memory_scope,
        scope_key=scope_key,
        project_id=None,
        agent_id=None,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
    )
    if inputs.memory_scope is None:
        return _missing_memory_scope(
            action=action,
            scope_key=inputs.scope_key,
            policy_context=policy_context,
        )

    normalized_scope = _coerce_scope(inputs.memory_scope)
    if normalized_scope is None:
        return MemoryPolicyDecision(
            action=action,
            allowed=False,
            reason="scope_not_enabled",
            memory_scope=MemoryScope.PRIVATE,
            scope_key=inputs.scope_key,
            policy_context=policy_context,
        )

    if not inputs.principal_id:
        return _deny(
            action=action,
            reason="principal_mismatch",
            memory_scope=normalized_scope,
            scope_key=inputs.scope_key,
            policy_context=policy_context,
        )

    if normalized_scope in {
        MemoryScope.SHARED,
        MemoryScope.ORGANIZATION,
        MemoryScope.PUBLIC,
    }:
        return _deny(
            action=action,
            reason="scope_not_enabled",
            memory_scope=normalized_scope,
            scope_key=inputs.scope_key,
            policy_context=policy_context,
        )

    if normalized_scope is MemoryScope.TEAM:
        if not inputs.scope_key:
            return _deny(
                action=action,
                reason="missing_scope_key",
                memory_scope=normalized_scope,
                scope_key=inputs.scope_key,
                policy_context=policy_context,
            )
        teams = _string_set(inputs.accessible_teams)
        if teams is None or inputs.scope_key not in teams:
            return _deny(
                action=action,
                reason="unverified_membership",
                memory_scope=normalized_scope,
                scope_key=inputs.scope_key,
                policy_context=policy_context,
            )
        if action is MemoryPolicyAction.SHARE:
            return _deny(
                action=action,
                reason="scope_crossing_requires_promotion",
                memory_scope=normalized_scope,
                scope_key=inputs.scope_key,
                policy_context=policy_context,
            )
        return _allow(
            action=action,
            reason=f"same_scope_{action.value}_allowed",
            memory_scope=normalized_scope,
            scope_key=inputs.scope_key,
            policy_context=policy_context,
        )

    if normalized_scope is MemoryScope.PRIVATE:
        if action is MemoryPolicyAction.SHARE:
            return _deny(
                action=action,
                reason="scope_crossing_requires_promotion",
                memory_scope=normalized_scope,
                scope_key=inputs.scope_key,
                policy_context=policy_context,
            )
        return _allow(
            action=action,
            reason=f"same_scope_{action.value}_allowed",
            memory_scope=normalized_scope,
            scope_key=inputs.scope_key,
            policy_context=policy_context,
        )

    if normalized_scope is MemoryScope.PROJECT:
        if not inputs.scope_key:
            return _deny(
                action=action,
                reason="missing_scope_key",
                memory_scope=normalized_scope,
                scope_key=inputs.scope_key,
                policy_context=policy_context,
            )
        projects = _string_set(inputs.accessible_projects)
        if projects is None or inputs.scope_key not in projects:
            return _deny(
                action=action,
                reason="unverified_membership",
                memory_scope=normalized_scope,
                scope_key=inputs.scope_key,
                policy_context=policy_context,
            )
        if action is MemoryPolicyAction.SHARE:
            return _deny(
                action=action,
                reason="scope_crossing_requires_promotion",
                memory_scope=normalized_scope,
                scope_key=inputs.scope_key,
                policy_context=policy_context,
            )
        return _allow(
            action=action,
            reason=f"same_scope_{action.value}_allowed",
            memory_scope=normalized_scope,
            scope_key=inputs.scope_key,
            policy_context=policy_context,
        )

    if normalized_scope is MemoryScope.DELEGATED:
        if not inputs.scope_key:
            return _deny(
                action=action,
                reason="missing_scope_key",
                memory_scope=normalized_scope,
                scope_key=inputs.scope_key,
                policy_context=policy_context,
            )
        delegations = _string_set(inputs.accessible_delegations)
        if delegations is None or inputs.scope_key not in delegations:
            return _deny(
                action=action,
                reason="unverified_membership",
                memory_scope=normalized_scope,
                scope_key=inputs.scope_key,
                policy_context=policy_context,
            )
        if action is MemoryPolicyAction.SHARE:
            return _deny(
                action=action,
                reason="scope_crossing_requires_promotion",
                memory_scope=normalized_scope,
                scope_key=inputs.scope_key,
                policy_context=policy_context,
            )
        return _allow(
            action=action,
            reason=f"same_scope_{action.value}_allowed",
            memory_scope=normalized_scope,
            scope_key=inputs.scope_key,
            policy_context=policy_context,
        )

    return _deny(
        action=action,
        reason="scope_not_enabled",
        memory_scope=normalized_scope,
        scope_key=inputs.scope_key,
        policy_context=policy_context,
    )


def authorize_memory_write(
    *,
    principal_id: str | None = None,
    memory_scope: MemoryScope | str | None = None,
    scope_key: str | None = None,
    accessible_projects: Iterable[str] | None = None,
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
    policy_context: MemoryPolicyContext | None = None,
) -> MemoryPolicyDecision:
    return _authorize_mutating_action(
        action=MemoryPolicyAction.WRITE,
        principal_id=principal_id,
        memory_scope=memory_scope,
        scope_key=scope_key,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
        policy_context=policy_context,
    )


def authorize_memory_share(
    *,
    principal_id: str | None = None,
    memory_scope: MemoryScope | str | None = None,
    scope_key: str | None = None,
    accessible_projects: Iterable[str] | None = None,
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
    policy_context: MemoryPolicyContext | None = None,
) -> MemoryPolicyDecision:
    return _authorize_mutating_action(
        action=MemoryPolicyAction.SHARE,
        principal_id=principal_id,
        memory_scope=memory_scope,
        scope_key=scope_key,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
        policy_context=policy_context,
    )


def authorize_memory_reflect(
    *,
    principal_id: str | None = None,
    memory_scope: MemoryScope | str | None = None,
    scope_key: str | None = None,
    accessible_projects: Iterable[str] | None = None,
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
    policy_context: MemoryPolicyContext | None = None,
) -> MemoryPolicyDecision:
    return _authorize_mutating_action(
        action=MemoryPolicyAction.REFLECT,
        principal_id=principal_id,
        memory_scope=memory_scope,
        scope_key=scope_key,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        accessible_delegations=accessible_delegations,
        policy_context=policy_context,
    )


__all__ = [
    "MemoryPolicyAction",
    "MemoryPolicyDecision",
    "authorize_memory_read",
    "authorize_memory_reflect",
    "authorize_memory_share",
    "authorize_memory_write",
]
