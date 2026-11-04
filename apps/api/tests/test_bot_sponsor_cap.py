"""The sponsor cap: the one function that decides a bot's project role.

A bot holds the lower of its own grant, its sponsor's current role on the
project and contributor, and nothing once its sponsor left. These drive that
function and the resolver that hands it the sponsor's live role. The live
SurrealDB proof is tests/test_surreal_live_bot_accounts.py.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from sibyl.auth import authorization, dependencies
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import build_auth_context
from sibyl.persistence.auth_common import SuspendedPrincipalError
from sibyl.persistence.organization_common import (
    can_manage_project_members,
    can_manage_project_settings,
)
from sibyl.persistence.surreal import auth as surreal_auth
from sibyl.persistence.surreal.auth_runtime import projects as project_runtime
from sibyl_core.auth import AuthUser, OrganizationRole, PrincipalKind, ProjectRole
from tests.harness.auth import (
    DEFAULT_BOT_ID as BOT_ID,
    DEFAULT_ORG_ID,
    DEFAULT_USER_ID as SPONSOR_ID,
    stub_auth_context,
    stub_bot_context,
)

PROJECT_UUID = "00000000-0000-0000-0000-00000000c0de"
OWN = "project_alpha"


def _bot_ctx(*, sponsor_org_role: OrganizationRole | None = OrganizationRole.MEMBER) -> AuthContext:
    return stub_bot_context(sponsor_org_role=sponsor_org_role)


def _project(
    *,
    visibility: str = "private",
    default_role: str = ProjectRole.VIEWER.value,
    owner: UUID | None = None,
) -> dict[str, object]:
    return {
        "uuid": PROJECT_UUID,
        "graph_project_id": OWN,
        "visibility": visibility,
        "default_role": default_role,
        "owner_user_id": str(owner or uuid4()),
    }


def _grant(role: ProjectRole) -> dict[str, object]:
    return {"project_id": PROJECT_UUID, "role": role.value}


def _role(
    *,
    ctx: AuthContext,
    project: dict[str, object],
    bot_grant: ProjectRole | None = None,
    sponsor_grant: ProjectRole | None = None,
    sponsor_team_grant: ProjectRole | None = None,
) -> ProjectRole | None:
    return project_runtime._effective_project_role_from_records(
        ctx=ctx,
        project=project,
        direct_record=_grant(bot_grant) if bot_grant else None,
        team_project_records=[],
        sponsor_direct_record=_grant(sponsor_grant) if sponsor_grant else None,
        sponsor_team_project_records=[_grant(sponsor_team_grant)] if sponsor_team_grant else [],
    )


# --- The cap: one function decides every project role ---------------------


@pytest.mark.parametrize(
    ("bot_grant", "sponsor_grant", "expected"),
    [
        (ProjectRole.CONTRIBUTOR, ProjectRole.CONTRIBUTOR, ProjectRole.CONTRIBUTOR),
        (ProjectRole.CONTRIBUTOR, ProjectRole.MAINTAINER, ProjectRole.CONTRIBUTOR),
        (ProjectRole.CONTRIBUTOR, ProjectRole.OWNER, ProjectRole.CONTRIBUTOR),
        (ProjectRole.CONTRIBUTOR, ProjectRole.VIEWER, ProjectRole.VIEWER),
        (ProjectRole.VIEWER, ProjectRole.MAINTAINER, ProjectRole.VIEWER),
        (ProjectRole.CONTRIBUTOR, None, None),
        (None, ProjectRole.MAINTAINER, None),
        # A grant written above the ceiling before the cap existed still reads capped.
        (ProjectRole.MAINTAINER, ProjectRole.OWNER, ProjectRole.CONTRIBUTOR),
        (ProjectRole.OWNER, ProjectRole.OWNER, ProjectRole.CONTRIBUTOR),
    ],
)
def test_a_bot_holds_the_lower_of_its_grant_its_sponsor_and_contributor(
    bot_grant: ProjectRole | None,
    sponsor_grant: ProjectRole | None,
    expected: ProjectRole | None,
) -> None:
    assert (
        _role(ctx=_bot_ctx(), project=_project(), bot_grant=bot_grant, sponsor_grant=sponsor_grant)
        == expected
    )


@pytest.mark.parametrize("viewer", ["sponsor", "bot"])
def test_an_org_viewer_on_either_side_caps_the_bot_at_viewer(viewer: str) -> None:
    ctx = _bot_ctx(
        sponsor_org_role=OrganizationRole.VIEWER if viewer == "sponsor" else OrganizationRole.MEMBER
    )
    if viewer == "bot":
        ctx = replace(ctx, org_role=OrganizationRole.VIEWER)
    for project in (_project(), _project(visibility="org", default_role="project_contributor")):
        assert (
            _role(
                ctx=ctx,
                project=project,
                bot_grant=ProjectRole.CONTRIBUTOR,
                sponsor_grant=ProjectRole.CONTRIBUTOR,
            )
            is ProjectRole.VIEWER
        )


def test_a_sponsor_s_team_grant_counts_toward_the_cap() -> None:
    role = _role(
        ctx=_bot_ctx(),
        project=_project(),
        bot_grant=ProjectRole.CONTRIBUTOR,
        sponsor_team_grant=ProjectRole.MAINTAINER,
    )
    assert role is ProjectRole.CONTRIBUTOR


def test_an_org_admin_sponsor_caps_only_at_contributor() -> None:
    ctx = _bot_ctx(sponsor_org_role=OrganizationRole.ADMIN)
    assert _role(ctx=ctx, project=_project(), bot_grant=ProjectRole.CONTRIBUTOR) is (
        ProjectRole.CONTRIBUTOR
    )


def test_a_bot_whose_sponsor_left_holds_nothing() -> None:
    ctx = _bot_ctx(sponsor_org_role=None)
    project = _project(visibility="org")
    assert (
        _role(
            ctx=ctx,
            project=project,
            bot_grant=ProjectRole.CONTRIBUTOR,
            sponsor_grant=ProjectRole.OWNER,
        )
        is None
    )


def test_the_cap_reaches_org_visibility_defaults() -> None:
    # An org-visible project hands every member its default role, a bot
    # included; a maintainer default still lands at contributor for a bot.
    project = _project(visibility="org", default_role=ProjectRole.MAINTAINER.value)
    assert _role(ctx=_bot_ctx(), project=project) is ProjectRole.CONTRIBUTOR
    viewer_default = _project(visibility="org")
    assert _role(ctx=_bot_ctx(), project=viewer_default) is ProjectRole.VIEWER


def test_a_bot_that_owns_a_project_still_reads_capped() -> None:
    project = _project(owner=BOT_ID)
    assert _role(ctx=_bot_ctx(), project=project, sponsor_grant=ProjectRole.VIEWER) is (
        ProjectRole.VIEWER
    )


def test_people_are_not_capped() -> None:
    ctx = stub_auth_context(user_id=SPONSOR_ID)
    role = project_runtime._effective_project_role_from_records(
        ctx=ctx,
        project=_project(),
        direct_record=_grant(ProjectRole.MAINTAINER),
        team_project_records=[],
    )
    assert role is ProjectRole.MAINTAINER


async def test_a_suspended_bot_is_refused_even_on_unassigned_rows() -> None:
    with pytest.raises(authorization.ProjectAuthorizationError):
        await project_runtime.verify_entity_project_access(
            ctx=_bot_ctx(sponsor_org_role=None),
            entity_project_id=None,
            required_role=ProjectRole.VIEWER,
        )


def test_a_bot_never_manages_project_members_or_settings() -> None:
    bot = SimpleNamespace(id=BOT_ID, kind=PrincipalKind.SERVICE)
    owned = SimpleNamespace(owner_user_id=BOT_ID)
    assert not can_manage_project_members(ProjectRole.OWNER, owned, bot)
    assert not can_manage_project_settings(ProjectRole.OWNER, owned, bot, OrganizationRole.ADMIN)


# --- The resolver reads the sponsor with the bot ----------------------------


class _ResolverClient:
    def __init__(
        self,
        *,
        bot_role: str | None = "member",
        sponsor_active: bool = True,
        sponsor_role: str = "member",
    ) -> None:
        self.bot_role = bot_role
        self.sponsor_active = sponsor_active
        self.sponsor_role = sponsor_role
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> object:
        self.calls.append((query, params))
        if "sponsor_id" in params:
            return {
                "sponsor": {"uuid": str(SPONSOR_ID)} if self.sponsor_active else None,
                "membership": {"role": self.sponsor_role} if self.sponsor_active else None,
            }
        return {
            "user": {
                "uuid": str(BOT_ID),
                "name": "oncall",
                "kind": "service",
                "sponsor_user_id": str(SPONSOR_ID),
            },
            "organization": {"uuid": str(DEFAULT_ORG_ID), "name": "Sibyl", "slug": "sibyl"},
            "membership": {
                "uuid": str(uuid4()),
                "organization_id": str(DEFAULT_ORG_ID),
                "user_id": str(BOT_ID),
                "role": self.bot_role,
            }
            if self.bot_role is not None
            else None,
            "teams": [],
            "team_memberships": [],
            "delegations": [],
        }


def _claims() -> dict[str, object]:
    return {"sub": str(BOT_ID), "org": str(DEFAULT_ORG_ID), "scopes": ["api:write"]}


async def test_the_resolver_reads_the_sponsor_s_live_role() -> None:
    client = _ResolverClient()
    ctx = await surreal_auth.SurrealAuthContextResolver.from_client(client).resolve(_claims())
    assert ctx.is_service
    assert ctx.user.sponsor_user_id == SPONSOR_ID
    assert ctx.sponsor_org_role is OrganizationRole.MEMBER
    assert any(params.get("sponsor_id") == str(SPONSOR_ID) for _q, params in client.calls)


async def test_the_resolver_refuses_a_bot_whose_sponsor_left() -> None:
    client = _ResolverClient(sponsor_active=False)
    with pytest.raises(SuspendedPrincipalError):
        await surreal_auth.SurrealAuthContextResolver.from_client(client).resolve(_claims())


async def test_the_resolver_never_hands_a_bot_more_than_member() -> None:
    client = _ResolverClient(bot_role="admin")
    ctx = await surreal_auth.SurrealAuthContextResolver.from_client(client).resolve(_claims())
    assert ctx.org_role is OrganizationRole.MEMBER


async def test_the_resolver_holds_a_bot_to_its_sponsor_s_org_role() -> None:
    # Every org write gate reads org_role, so a viewer sponsor's bot is a viewer.
    client = _ResolverClient(sponsor_role="viewer")
    ctx = await surreal_auth.SurrealAuthContextResolver.from_client(client).resolve(_claims())
    assert ctx.sponsor_org_role is OrganizationRole.VIEWER
    assert ctx.org_role is OrganizationRole.VIEWER


async def test_the_resolver_refuses_a_bot_outside_its_organization() -> None:
    client = _ResolverClient(bot_role=None)
    with pytest.raises(SuspendedPrincipalError):
        await surreal_auth.SurrealAuthContextResolver.from_client(client).resolve(_claims())


def test_a_bot_is_never_a_deployment_admin() -> None:
    user = AuthUser(id=BOT_ID, kind=PrincipalKind.SERVICE, is_admin=True)
    assert user.is_admin is False
    ctx = AuthContext(user=user, organization=None, org_role=None)
    assert ctx.user.is_admin is False
    assert ctx.to_memory_policy_context().is_global_admin is False
    assert AuthUser(id=SPONSOR_ID, is_admin=True).is_admin is True


async def test_a_suspended_bot_s_request_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dependencies, "resolve_claims", AsyncMock(return_value=_claims()))
    monkeypatch.setattr(
        dependencies,
        "resolve_auth_context",
        AsyncMock(side_effect=SuspendedPrincipalError("suspended")),
    )
    request = Request({"type": "http", "method": "GET", "path": "/api/x", "headers": []})
    with pytest.raises(HTTPException) as caught:
        await build_auth_context(request)
    assert caught.value.status_code == 403
    assert caught.value.detail["error"] == "bot_suspended"


# --- Privacy: a bot is its own principal ---------------------------------------


@pytest.mark.parametrize(
    ("owner", "reader", "visible"),
    [
        (SPONSOR_ID, BOT_ID, False),
        (BOT_ID, SPONSOR_ID, False),
        (BOT_ID, BOT_ID, True),
        (SPONSOR_ID, SPONSOR_ID, True),
    ],
)
def test_a_bot_and_its_sponsor_never_read_each_other_s_private_memory(
    owner: UUID, reader: UUID, visible: bool
) -> None:
    from sibyl_core.auth.memory_policy import memory_metadata_read_allowed

    metadata = {"memory_scope": "private", "principal_id": str(owner), "scope_key": str(owner)}
    assert (
        memory_metadata_read_allowed(
            metadata,
            principal_id=str(reader),
            private_scope_granted=True,
            accessible_projects=frozenset({OWN}),
        )
        is visible
    )
