from __future__ import annotations

from types import SimpleNamespace

import pytest

from sibyl_core.auth.context import MemoryPolicyContext
from sibyl_core.auth.memory_policy import (
    MemoryPolicyAction,
    authorize_memory_read,
    authorize_memory_reflect,
    authorize_memory_share,
    authorize_memory_write,
    memory_metadata_read_allowed,
    memory_scope_policy_key,
    stamp_memory_scope_metadata,
)
from sibyl_core.auth.models import OrganizationRole
from sibyl_core.services.surreal_content import MemoryScope


def test_policy_context_authorizes_project_read_with_shared_payload() -> None:
    ctx = MemoryPolicyContext(
        actor_user_id="user-123",
        organization_id="org-123",
        organization_role="member",
        accessible_projects=["project_123", "project_123"],
        memory_space="project",
        scope_key="project_123",
        source_surface="mcp_context",
    )

    decision = authorize_memory_read(policy_context=ctx)

    assert ctx.organization_role is OrganizationRole.MEMBER
    assert ctx.accessible_projects == frozenset({"project_123"})
    assert decision.allowed
    assert decision.reason == "project_access_verified"
    assert decision.policy_context == ctx


def test_policy_context_denies_missing_actor_with_stable_reason() -> None:
    decision = authorize_memory_read(
        policy_context=MemoryPolicyContext(
            actor_user_id=None,
            memory_space="private",
            source_surface="rest_recall",
        )
    )

    assert not decision.allowed
    assert decision.reason == "principal_mismatch"


def test_policy_context_denies_missing_memory_space_with_stable_reason() -> None:
    decision = authorize_memory_write(
        policy_context=MemoryPolicyContext(
            actor_user_id="user-123",
            source_surface="mcp_remember",
        )
    )

    assert not decision.allowed
    assert decision.reason == "missing_memory_scope"


def test_policy_context_authorizes_delegated_read() -> None:
    ctx = MemoryPolicyContext(
        actor_user_id="user-123",
        accessible_delegations=["agent:nova"],
        delegated_authority="agent:nova",
        memory_space="delegated",
        scope_key="agent:nova",
        source_surface="mcp_context",
    )

    decision = authorize_memory_read(policy_context=ctx)

    assert decision.allowed
    assert decision.reason == "delegated_access_verified"


def test_policy_context_explicit_kwargs_take_precedence() -> None:
    ctx = MemoryPolicyContext(
        actor_user_id="user-123",
        accessible_projects={"project_a"},
        memory_space="project",
        scope_key="project_a",
        source_surface="mcp_context",
    )

    decision = authorize_memory_read(
        policy_context=ctx,
        scope_key="project_b",
        accessible_projects={"project_b"},
    )

    assert decision.allowed
    assert decision.reason == "project_access_verified"
    assert decision.scope_key == "project_b"
    assert decision.policy_context == ctx


def test_legacy_kwargs_missing_memory_scope_has_stable_reason() -> None:
    decision = authorize_memory_write(principal_id="user-123")

    assert not decision.allowed
    assert decision.reason == "missing_memory_scope"


def test_legacy_kwargs_do_not_attach_policy_context() -> None:
    decision = authorize_memory_read(
        principal_id="user-123",
        memory_scope=MemoryScope.PRIVATE,
    )

    assert decision.allowed
    assert decision.policy_context is None


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
    [MemoryScope.ORGANIZATION, MemoryScope.SHARED, MemoryScope.PUBLIC],
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


def test_stamped_metadata_replaces_every_caller_supplied_owner_field() -> None:
    stamped = stamp_memory_scope_metadata(
        {
            "memory_scope": "organization",
            "principal_id": "victim",
            "scope_key": "project_victim",
            "note": "kept",
        },
        memory_scope="private",
        scope_key=None,
        principal_id="author",
    )

    assert stamped == {"memory_scope": "private", "principal_id": "author", "note": "kept"}


def test_stamped_metadata_drops_owner_fields_when_no_scope_is_declared() -> None:
    stamped = stamp_memory_scope_metadata(
        {"principal_id": "victim", "scope_key": "project_victim", "note": "kept"},
        memory_scope=None,
        scope_key=None,
        principal_id="author",
    )

    assert stamped == {"note": "kept"}


def test_stamped_metadata_keeps_an_unrecognized_scope_so_reads_deny() -> None:
    """A typo must not read as "no scope", which is the fail-open case."""
    stamped = stamp_memory_scope_metadata(
        {"memory_scope": "Private", "principal_id": "victim"},
        memory_scope="Private",
        scope_key=None,
        principal_id="author",
    )

    assert stamped == {"memory_scope": "Private"}
    assert not memory_metadata_read_allowed(stamped, principal_id="author")
    assert not memory_metadata_read_allowed(stamped, principal_id="victim")


def test_stamped_metadata_keeps_a_project_row_on_its_verified_key() -> None:
    stamped = stamp_memory_scope_metadata(
        {"scope_key": "project_victim"},
        memory_scope="project",
        scope_key="project_verified",
        principal_id="author",
    )

    assert stamped == {
        "memory_scope": "project",
        "scope_key": "project_verified",
        "principal_id": "author",
    }


def test_metadata_read_is_open_only_when_no_scope_is_recorded() -> None:
    assert memory_metadata_read_allowed({"principal_id": "victim"}, principal_id="attacker")
    assert not memory_metadata_read_allowed(
        {"memory_scope": "private", "principal_id": "victim"},
        principal_id="attacker",
    )
    assert not memory_metadata_read_allowed(
        {"memory_scope": "private", "scope_key": "victim"},
        principal_id="attacker",
    )
    assert memory_metadata_read_allowed(
        {"memory_scope": "private", "scope_key": "victim"},
        principal_id="victim",
    )


def test_metadata_read_denies_a_private_row_without_a_private_grant() -> None:
    metadata = {"memory_scope": "private", "principal_id": "owner"}

    assert memory_metadata_read_allowed(metadata, principal_id="owner")
    assert not memory_metadata_read_allowed(
        metadata,
        principal_id="owner",
        private_scope_granted=False,
    )


def test_promoted_candidate_cannot_inherit_a_forged_owner_from_its_capture() -> None:
    """A capture's metadata bag reaches the promoted graph row.

    POST /memory/raw takes free-form metadata, and promotion spreads that bag
    into the entity. The raw memory's principal_id column is authoritative, but
    the bag copy is not, and retrieval resolves a private row's owner from the
    bag alone.
    """
    from sibyl_core.models.reflection import ReflectionCandidate
    from sibyl_core.services.memory import _entity_from_candidate

    candidate = ReflectionCandidate(
        kind="decision",
        title="Planted decision",
        content="Attacker-authored content.",
        reason="promote",
        confidence=0.9,
        metadata={"principal_id": "victim", "scope_key": "victim"},
    )

    entity = _entity_from_candidate(
        candidate,
        organization_id="org-1",
        principal_id="attacker",
        domain=None,
        project=None,
        source_id=None,
        memory_scope=MemoryScope.PRIVATE,
        scope_key=None,
        policy_metadata={"memory_scope": "private", "scope_key": None},
    )

    assert entity.metadata["memory_scope"] == "private"
    assert entity.metadata["principal_id"] == "attacker"
    assert "scope_key" not in entity.metadata
    assert not memory_metadata_read_allowed(entity.metadata, principal_id="victim")
    assert memory_metadata_read_allowed(entity.metadata, principal_id="attacker")


def test_operational_capture_cannot_name_an_owner_through_its_metadata_bag() -> None:
    """POST /experience verifies project_id and nothing else.

    Its metadata bag and its scope_key are both request-body fields, and every
    projected entity inherits them, so an unfiltered bag lets a contributor on
    one project plant rows that resolve as a named victim's private memory.
    """
    from sibyl_core.models.experience import (
        OperationalEvidencePart,
        OperationalExperience,
        OperationalObservation,
    )
    from sibyl_core.projection.experience import _common_metadata

    evidence = OperationalEvidencePart(id="e1", content_type="text", content="x")
    experience = OperationalExperience(
        source_id="src1",
        goal="g",
        observations=(OperationalObservation(id="o1", ordinal=1, evidence=(evidence,)),),
        project_id="project_verified",
        scope_key="project_victim",
        metadata={"memory_scope": "private", "principal_id": "victim"},
    )

    metadata = _common_metadata(experience, content_hash="h")

    assert "memory_scope" not in metadata
    assert "principal_id" not in metadata
    assert metadata["scope_key"] == "project_verified"


def test_api_key_grant_narrows_a_read_it_can_never_authorize_one() -> None:
    """A private grant names the reader, so it cannot stand in for the owner."""
    from sibyl_core.tools.search import _matches_memory_scope_policy

    victim_row = SimpleNamespace(metadata={"memory_scope": "private", "principal_id": "victim"})
    own_row = SimpleNamespace(metadata={"memory_scope": "private", "principal_id": "reader"})
    private_grant = {memory_scope_policy_key(MemoryScope.PRIVATE, "reader")}

    def allowed(entity: object, grants: set[str] | None) -> bool:
        return _matches_memory_scope_policy(
            entity,
            project=None,
            principal_id="reader",
            allowed_memory_scope_keys=grants,
            accessible_projects=set(),
        )

    assert not allowed(victim_row, None)
    assert not allowed(victim_row, private_grant)
    assert allowed(own_row, None)
    assert allowed(own_row, private_grant)
    assert not allowed(own_row, {memory_scope_policy_key(MemoryScope.PROJECT, "p1")})


def test_search_scope_policy_denies_bands_it_cannot_verify() -> None:
    from sibyl_core.tools.search import _matches_memory_scope_policy

    for scope in ("team", "delegated", "organization", "shared", "public"):
        entity = SimpleNamespace(metadata={"memory_scope": scope, "scope_key": "not_mine"})
        assert not _matches_memory_scope_policy(
            entity,
            project=None,
            principal_id="reader",
            allowed_memory_scope_keys=None,
            accessible_projects=set(),
        ), scope


def test_search_scope_policy_never_diverges_from_the_shared_rule() -> None:
    """Search kept its own copy of the read rule, with a fail-open branch.

    An unknown accessible-project set meant allow, so a credential carrying no
    user context saw every project-scoped row in the organization. Any shape
    the two implementations disagree on is reachable through one surface and
    hidden on the other, which is the drift this convergence exists to end.
    """
    from sibyl_core.tools.search import _matches_memory_scope_policy

    reader = "reader-1"
    shapes: list[tuple[dict[str, str], set[str] | None]] = [
        ({"memory_scope": "project", "scope_key": "proj-x"}, None),
        ({"memory_scope": "project", "scope_key": "proj-x"}, set()),
        ({"memory_scope": "project", "scope_key": "proj-x"}, {"proj-x"}),
        ({"memory_scope": "project", "project_id": "proj-x"}, None),
        ({"memory_scope": "private", "principal_id": "victim"}, set()),
        ({"memory_scope": "private", "principal_id": reader}, set()),
        ({"memory_scope": "private", "scope_key": reader}, set()),
        ({"memory_scope": "team", "scope_key": "team-x"}, set()),
        ({"memory_scope": "delegated", "scope_key": "del-x"}, set()),
        ({"memory_scope": "sooper_sekrit", "scope_key": "x"}, set()),
        ({}, set()),
    ]

    for metadata, projects in shapes:
        local = _matches_memory_scope_policy(
            SimpleNamespace(metadata=metadata),
            project=None,
            principal_id=reader,
            allowed_memory_scope_keys=None,
            accessible_projects=projects,
        )
        shared = memory_metadata_read_allowed(
            metadata,
            principal_id=reader,
            accessible_projects=projects,
        )
        assert local == shared, (metadata, projects, local, shared)


def test_search_scope_policy_still_serves_a_verified_project_filter() -> None:
    """A named project was proved accessible before retrieval ran."""
    from sibyl_core.tools.search import _matches_memory_scope_policy

    row = SimpleNamespace(metadata={"memory_scope": "project", "scope_key": "proj-x"})

    assert _matches_memory_scope_policy(
        row,
        project="proj-x",
        principal_id="reader-1",
        allowed_memory_scope_keys=None,
        accessible_projects=None,
    )
    assert not _matches_memory_scope_policy(
        row,
        project="proj-other",
        principal_id="reader-1",
        allowed_memory_scope_keys=None,
        accessible_projects=None,
    )


def test_search_scope_policy_wont_let_a_named_project_beat_membership() -> None:
    """MCP takes the project straight from a tool argument.

    A narrowing filter must not double as authorization, or naming someone
    else's project reads its rows.
    """
    from sibyl_core.tools.search import _matches_memory_scope_policy

    theirs = SimpleNamespace(metadata={"memory_scope": "project", "scope_key": "proj-theirs"})
    mine = SimpleNamespace(metadata={"memory_scope": "project", "scope_key": "proj-mine"})

    assert not _matches_memory_scope_policy(
        theirs,
        project="proj-theirs",
        principal_id="reader-1",
        allowed_memory_scope_keys=None,
        accessible_projects={"proj-mine"},
    )
    assert _matches_memory_scope_policy(
        mine,
        project="proj-mine",
        principal_id="reader-1",
        allowed_memory_scope_keys=None,
        accessible_projects={"proj-mine"},
    )
    # REST verifies membership itself and passes no set; the named project
    # stands alone there.
    assert _matches_memory_scope_policy(
        theirs,
        project="proj-theirs",
        principal_id="reader-1",
        allowed_memory_scope_keys=None,
        accessible_projects=None,
    )
