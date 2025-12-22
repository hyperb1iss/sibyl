from sibyl.auth.context import AuthContext
from sibyl.db.models import OrganizationRole


class _User:
    pass


def test_auth_context_properties() -> None:
    user = _User()
    ctx = AuthContext(user=user, organization=None, org_role=None)
    assert ctx.is_authenticated is True
    assert ctx.is_org_scoped is False

    ctx2 = AuthContext(user=user, organization=object(), org_role=OrganizationRole.OWNER)
    assert ctx2.is_org_scoped is True

