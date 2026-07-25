"""Memory policy decisions for scoped memory operations."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sibyl_core.auth.context import MemoryPolicyContext
from sibyl_core.models.memory_scope import MemoryScope

# A graph row has no scope column: retrieval reads its audience out of these
# three metadata fields alone. A private row's owner resolves as principal_id
# or scope_key, and a project row is served to every member named by scope_key,
# so all three are authorization inputs and none may come from a request body.
MEMORY_OWNER_METADATA_KEYS = frozenset({"memory_scope", "principal_id", "scope_key"})


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


def _optional_str(value: object) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def memory_metadata_read_allowed(
    metadata: Mapping[str, Any] | None,
    *,
    principal_id: str | None,
    project_id: str | None = None,
    accessible_projects: Iterable[str] | None = None,
    agent_id: str | None = None,
    allowed_memory_scope_keys: Iterable[str] | None = None,
    private_scope_granted: bool = True,
) -> bool:
    """Decide whether a reader may see a graph row carrying this metadata.

    Every surface that hands graph rows back to a caller runs this, so a
    private memory stays reachable only by its owner no matter which tool
    walked to it.
    """
    fields = metadata if isinstance(metadata, Mapping) else {}
    raw_scope = fields.get("memory_scope")
    if raw_scope is None:
        return True
    memory_scope = _coerce_scope(raw_scope)
    if memory_scope is None:
        return False

    scope_key = _optional_str(fields.get("scope_key"))
    reader = _optional_str(principal_id)
    if memory_scope is MemoryScope.PRIVATE:
        owner = _optional_str(fields.get("principal_id")) or scope_key
        allowed = private_scope_granted and reader is not None and owner == reader
    else:
        allowed = authorize_memory_read(
            principal_id=reader,
            memory_scope=memory_scope,
            scope_key=scope_key,
            project_id=project_id,
            agent_id=agent_id,
            accessible_projects=accessible_projects,
        ).allowed
    if not allowed:
        return False
    if allowed_memory_scope_keys is None:
        return True

    effective_scope_key = reader if memory_scope is MemoryScope.PRIVATE else scope_key
    return memory_scope_policy_key(memory_scope, effective_scope_key) in {
        str(granted) for granted in allowed_memory_scope_keys
    }


def stamp_memory_scope_metadata(
    metadata: Mapping[str, Any] | None,
    *,
    memory_scope: MemoryScope | str | None,
    scope_key: str | None,
    principal_id: str | None,
) -> dict[str, Any]:
    """Rebuild a write's scope metadata from authorized values.

    Caller-supplied owner fields are dropped unconditionally, so a payload can
    never name the principal or project a row reads as. A declared scope with
    no authorized owner keeps the scope and loses the owner, which fails the
    read check closed rather than falling back to the payload.
    """
    stamped = {
        key: value
        for key, value in (metadata or {}).items()
        if key not in MEMORY_OWNER_METADATA_KEYS
    }
    scope = _coerce_scope(memory_scope) if memory_scope is not None else None
    if scope is None:
        # An unrecognized scope is kept rather than dropped: the read check
        # denies it, while an absent scope is the fail-open case.
        if memory_scope is not None:
            stamped["memory_scope"] = memory_scope
        return stamped

    stamped["memory_scope"] = scope.value
    if scope is MemoryScope.PRIVATE:
        if owner := _optional_str(principal_id):
            stamped["principal_id"] = owner
        return stamped
    if key := _optional_str(scope_key):
        stamped["scope_key"] = key
    return stamped


__all__ = [
    "MEMORY_OWNER_METADATA_KEYS",
    "MemoryPolicyAction",
    "MemoryPolicyDecision",
    "authorize_memory_read",
    "authorize_memory_reflect",
    "authorize_memory_share",
    "authorize_memory_write",
    "memory_metadata_read_allowed",
    "memory_scope_policy_key",
    "stamp_memory_scope_metadata",
]
