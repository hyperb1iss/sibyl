from sibyl.auth.context import AuthContext
from sibyl_core.auth import AuthOrganization, AuthUser, OrganizationRole


def test_auth_context_properties() -> None:
    user = AuthUser(id="00000000-0000-0000-0000-000000000001")
    ctx = AuthContext(user=user, organization=None, org_role=None)
    assert ctx.is_authenticated is True
    assert ctx.is_org_scoped is False

    org = AuthOrganization(id="00000000-0000-0000-0000-000000000002")
    ctx2 = AuthContext(user=user, organization=org, org_role=OrganizationRole.OWNER)
    assert ctx2.is_org_scoped is True


def test_bound_api_key_identity_wins_in_memory_policy_context() -> None:
    user = AuthUser(id="00000000-0000-0000-0000-000000000001")
    ctx = AuthContext(
        user=user,
        organization=None,
        org_role=None,
        agent_id="hermes:home:nova",
        delegated_authority="household-agent",
    )

    policy = ctx.to_memory_policy_context(
        agent_id="request-controlled-agent",
        delegated_authority="request-controlled-authority",
    )

    assert policy.agent_id == "hermes:home:nova"
    assert policy.delegated_authority == "household-agent"
