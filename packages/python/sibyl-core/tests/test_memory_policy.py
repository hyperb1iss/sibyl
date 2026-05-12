from __future__ import annotations

import pytest

from sibyl_core.auth.memory_policy import (
    MemoryPolicyAction,
    authorize_memory_read,
    authorize_memory_reflect,
    authorize_memory_share,
    authorize_memory_write,
)
from sibyl_core.services.surreal_content import MemoryScope


def test_private_read_requires_principal() -> None:
    decision = authorize_memory_read(
        principal_id=None,
        memory_scope=MemoryScope.PRIVATE,
    )

    assert decision.action is MemoryPolicyAction.READ
    assert not decision.allowed
    assert decision.reason == "principal_mismatch"


def test_private_read_is_principal_bound() -> None:
    decision = authorize_memory_read(
        principal_id="user-123",
        memory_scope=MemoryScope.PRIVATE,
    )

    assert decision.allowed
    assert decision.reason == "private_principal_bound"


def test_agent_diary_read_names_agent_and_project_scope() -> None:
    decision = authorize_memory_read(
        principal_id="user-123",
        memory_scope="private",
        agent_id="nova",
        project_id="project_123",
    )

    assert decision.allowed
    assert decision.reason == "agent_diary_private_read_allowed"


def test_project_read_requires_scope_key() -> None:
    decision = authorize_memory_read(
        principal_id="user-123",
        memory_scope=MemoryScope.PROJECT,
    )

    assert not decision.allowed
    assert decision.reason == "missing_scope_key"


def test_project_read_requires_membership_when_projects_are_supplied() -> None:
    decision = authorize_memory_read(
        principal_id="user-123",
        memory_scope=MemoryScope.PROJECT,
        scope_key="project_456",
        accessible_projects={"project_123"},
    )

    assert not decision.allowed
    assert decision.reason == "unverified_membership"


def test_project_read_requires_verified_membership_context() -> None:
    decision = authorize_memory_read(
        principal_id="user-123",
        memory_scope=MemoryScope.PROJECT,
        scope_key="project_123",
    )

    assert not decision.allowed
    assert decision.reason == "unverified_membership"


def test_project_read_allows_preverified_project_access() -> None:
    decision = authorize_memory_read(
        principal_id="user-123",
        memory_scope=MemoryScope.PROJECT,
        scope_key="project_123",
        accessible_projects={"project_123"},
    )

    assert decision.allowed
    assert decision.reason == "project_access_verified"


def test_delegated_read_requires_explicit_delegation_access() -> None:
    decision = authorize_memory_read(
        principal_id="user-123",
        memory_scope=MemoryScope.DELEGATED,
        scope_key="agent:nova",
        accessible_delegations={"agent:iris"},
    )

    assert not decision.allowed
    assert decision.reason == "unverified_membership"


def test_delegated_read_allows_explicit_delegation_access() -> None:
    decision = authorize_memory_read(
        principal_id="user-123",
        memory_scope=MemoryScope.DELEGATED,
        scope_key="agent:nova",
        accessible_delegations={"agent:nova"},
    )

    assert decision.allowed
    assert decision.reason == "delegated_access_verified"


@pytest.mark.parametrize(
    "memory_scope",
    [MemoryScope.TEAM, MemoryScope.ORGANIZATION, MemoryScope.SHARED, MemoryScope.PUBLIC],
)
def test_unenabled_scopes_are_denied(memory_scope: MemoryScope) -> None:
    decision = authorize_memory_read(
        principal_id="user-123",
        memory_scope=memory_scope,
        scope_key="scope-key",
    )

    assert not decision.allowed
    assert decision.reason == "scope_not_enabled"


@pytest.mark.parametrize(
    ("action", "authorize", "reason"),
    [
        (MemoryPolicyAction.WRITE, authorize_memory_write, "same_scope_write_allowed"),
        (MemoryPolicyAction.REFLECT, authorize_memory_reflect, "same_scope_reflect_allowed"),
    ],
)
def test_write_and_reflect_allow_same_scope_private_actions(action, authorize, reason) -> None:
    decision = authorize(
        principal_id="user-123",
        memory_scope=MemoryScope.PRIVATE,
    )

    assert decision.action is action
    assert decision.allowed
    assert decision.reason == reason


def test_share_is_deny_only_until_memory_spaces_enable_it() -> None:
    decision = authorize_memory_share(
        principal_id="user-123",
        memory_scope=MemoryScope.PRIVATE,
    )

    assert decision.action is MemoryPolicyAction.SHARE
    assert not decision.allowed
    assert decision.reason == "scope_crossing_requires_promotion"


@pytest.mark.parametrize(
    "authorize",
    [authorize_memory_write, authorize_memory_share, authorize_memory_reflect],
)
def test_project_mutation_policy_requires_scope_key(authorize) -> None:
    decision = authorize(
        principal_id="user-123",
        memory_scope=MemoryScope.PROJECT,
    )

    assert not decision.allowed
    assert decision.reason == "missing_scope_key"


@pytest.mark.parametrize(
    "authorize",
    [authorize_memory_write, authorize_memory_share, authorize_memory_reflect],
)
def test_project_mutation_policy_requires_membership(authorize) -> None:
    decision = authorize(
        principal_id="user-123",
        memory_scope=MemoryScope.PROJECT,
        scope_key="project_456",
        accessible_projects={"project_123"},
    )

    assert not decision.allowed
    assert decision.reason == "unverified_membership"


@pytest.mark.parametrize(
    ("action", "authorize", "reason"),
    [
        (MemoryPolicyAction.WRITE, authorize_memory_write, "same_scope_write_allowed"),
        (MemoryPolicyAction.REFLECT, authorize_memory_reflect, "same_scope_reflect_allowed"),
    ],
)
def test_project_write_and_reflect_allow_verified_membership(action, authorize, reason) -> None:
    decision = authorize(
        principal_id="user-123",
        memory_scope=MemoryScope.PROJECT,
        scope_key="project_123",
        accessible_projects={"project_123"},
    )

    assert decision.action is action
    assert decision.allowed
    assert decision.reason == reason


def test_project_share_remains_denied_with_verified_membership() -> None:
    decision = authorize_memory_share(
        principal_id="user-123",
        memory_scope=MemoryScope.PROJECT,
        scope_key="project_123",
        accessible_projects={"project_123"},
    )

    assert not decision.allowed
    assert decision.reason == "scope_crossing_requires_promotion"


@pytest.mark.parametrize(
    "authorize",
    [authorize_memory_write, authorize_memory_share, authorize_memory_reflect],
)
def test_disabled_mutation_scopes_have_stable_v0_7_reason(authorize) -> None:
    decision = authorize(
        principal_id="user-123",
        memory_scope=MemoryScope.PUBLIC,
    )

    assert not decision.allowed
    assert decision.reason == "scope_not_enabled"
