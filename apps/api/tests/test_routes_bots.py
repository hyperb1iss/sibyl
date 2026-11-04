"""The bot routes and every route a bot could climb through.

These mount the real routers and override only the caller, so a gate a route
forgot fails here. The runtime behind them is proven against live SurrealDB in
tests/test_surreal_live_bot_accounts.py.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from sibyl.api.routes import (
    auth as auth_routes,
    bots as bot_routes,
    orgs as org_routes,
    project_members,
    project_settings,
)
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import (
    get_auth_context,
    get_current_org_role,
    get_current_organization,
    get_current_user,
)
from sibyl.persistence import auth_runtime, organization_runtime
from sibyl.persistence.organization_common import ProjectMembersResult
from sibyl_core.auth import AuthOrganization, OrganizationRole, ProjectRole
from tests.harness.auth import (
    DEFAULT_BOT_ID as BOT_ID,
    DEFAULT_ORG_ID,
    DEFAULT_USER_ID as SPONSOR_ID,
    stub_auth_context,
    stub_bot_context,
    stub_mcp_context,
)

KEY_ID = UUID("00000000-0000-0000-0000-00000000d00d")
OWN = "project_alpha"
OTHER = "project_bravo"


def _bot_ctx(*, api_key_project_ids: frozenset[str] | None = None) -> AuthContext:
    return stub_bot_context(api_key_project_ids=api_key_project_ids)


# --- The routes ---------------------------------------------------------------


class _App:
    """The real routers with only the caller overridden."""

    def __init__(self, *routers) -> None:
        self.ctx: AuthContext = stub_auth_context(user_id=SPONSOR_ID)
        app = FastAPI()
        for router in routers:
            app.include_router(router)
        app.dependency_overrides[get_auth_context] = lambda: self.ctx
        app.dependency_overrides[get_current_user] = lambda: self.ctx.user
        app.dependency_overrides[get_current_organization] = lambda: self.ctx.organization
        app.dependency_overrides[get_current_org_role] = lambda: self.ctx.org_role
        self.client = TestClient(app)


_BOT_ROUTES = [
    ("GET", "/bots", None),
    ("POST", "/bots", {"name": "oncall"}),
    ("GET", f"/bots/{BOT_ID}", None),
    ("DELETE", f"/bots/{BOT_ID}", None),
    ("POST", f"/bots/{BOT_ID}/transfer", {"sponsor_user_id": str(SPONSOR_ID)}),
    ("POST", f"/bots/{BOT_ID}/projects", {"project_id": OWN, "role": "project_contributor"}),
    ("DELETE", f"/bots/{BOT_ID}/projects/{OWN}", None),
    ("GET", f"/bots/{BOT_ID}/keys", None),
    ("POST", f"/bots/{BOT_ID}/keys", {"name": "pager", "scopes": ["api:write"]}),
    ("POST", f"/bots/{BOT_ID}/keys/{KEY_ID}/revoke", None),
]


def _stub_bot_runtime(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    names = [
        "list_bots",
        "create_bot",
        "get_bot",
        "delete_bot",
        "transfer_bot",
        "grant_bot_project",
        "revoke_bot_project",
        "list_bot_api_keys",
        "create_bot_api_key",
        "revoke_bot_api_key",
    ]
    mocks: dict[str, AsyncMock] = {}
    for name in names:
        result: object = {}
        if name in {"list_bots", "list_bot_api_keys"}:
            result = []
        elif name == "create_bot_api_key":
            result = (SimpleNamespace(id=uuid4(), name="pager", key_prefix="sk_live_x"), "sk_raw")
        mock = AsyncMock(return_value=result)
        mocks[name] = mock
        monkeypatch.setattr(auth_runtime, name, mock)
    return mocks


def _assert_untouched(mocks: dict[str, AsyncMock]) -> None:
    for name, mock in mocks.items():
        assert not mock.await_count, f"{name} ran for a refused request"


@pytest.mark.parametrize(("method", "path", "body"), _BOT_ROUTES)
def test_a_bot_cannot_manage_bots_or_mint_keys(
    monkeypatch: pytest.MonkeyPatch, method: str, path: str, body: object
) -> None:
    mocks = _stub_bot_runtime(monkeypatch)
    app = _App(bot_routes.router)
    app.ctx = _bot_ctx()
    response = app.client.request(method, path, json=body)
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "bot_forbidden"
    _assert_untouched(mocks)


@pytest.mark.parametrize(("method", "path", "body"), _BOT_ROUTES)
def test_a_narrowed_key_cannot_manage_bots(
    monkeypatch: pytest.MonkeyPatch, method: str, path: str, body: object
) -> None:
    # A bot made through a narrowed key could carry a key that reaches past it.
    mocks = _stub_bot_runtime(monkeypatch)
    app = _App(bot_routes.router)
    app.ctx = stub_auth_context(
        user_id=SPONSOR_ID,
        org_role=OrganizationRole.ADMIN,
        api_key_id=str(uuid4()),
        api_key_project_ids=frozenset({OWN}),
    )
    response = app.client.request(method, path, json=body)
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "api_key_scope_denied"
    _assert_untouched(mocks)


def test_a_viewer_cannot_create_a_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    mocks = _stub_bot_runtime(monkeypatch)
    app = _App(bot_routes.router)
    app.ctx = stub_auth_context(user_id=SPONSOR_ID, org_role=OrganizationRole.VIEWER)
    response = app.client.post("/bots", json={"name": "oncall"})
    assert response.status_code == 403
    _assert_untouched(mocks)


@pytest.mark.parametrize(
    "role", [OrganizationRole.OWNER, OrganizationRole.ADMIN, OrganizationRole.MEMBER]
)
def test_any_owner_admin_or_member_creates_a_bot_they_sponsor(
    monkeypatch: pytest.MonkeyPatch, role: OrganizationRole
) -> None:
    mocks = _stub_bot_runtime(monkeypatch)
    app = _App(bot_routes.router)
    app.ctx = stub_auth_context(user_id=SPONSOR_ID, org_role=role)
    response = app.client.post("/bots", json={"name": "oncall"})
    assert response.status_code == 201
    kwargs = mocks["create_bot"].await_args.kwargs
    assert kwargs["ctx"].user.id == SPONSOR_ID
    assert kwargs["name"] == "oncall"


def test_a_member_cannot_create_a_bot_sponsored_by_someone_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mocks = _stub_bot_runtime(monkeypatch)
    app = _App(bot_routes.router)
    response = app.client.post("/bots", json={"name": "oncall", "sponsor_user_id": str(uuid4())})
    assert response.status_code == 422
    _assert_untouched(mocks)


@pytest.mark.parametrize("role", [OrganizationRole.MEMBER, OrganizationRole.VIEWER])
def test_only_an_org_admin_transfers_a_bot(
    monkeypatch: pytest.MonkeyPatch, role: OrganizationRole
) -> None:
    mocks = _stub_bot_runtime(monkeypatch)
    app = _App(bot_routes.router)
    app.ctx = stub_auth_context(user_id=SPONSOR_ID, org_role=role)
    response = app.client.post(f"/bots/{BOT_ID}/transfer", json={"sponsor_user_id": str(uuid4())})
    assert response.status_code == 403
    _assert_untouched(mocks)


@pytest.mark.parametrize("role", [ProjectRole.MAINTAINER, ProjectRole.OWNER])
async def test_a_bot_is_never_granted_more_than_contributor(
    monkeypatch: pytest.MonkeyPatch, role: ProjectRole
) -> None:
    from sibyl.persistence.surreal.auth_runtime import bots as bot_runtime

    lookup = AsyncMock(side_effect=AssertionError("read a project for a refused role"))
    monkeypatch.setattr(bot_runtime, "_resolve_project_for_team_link", lookup)
    with pytest.raises(HTTPException) as caught:
        await bot_runtime.grant_bot_project(
            ctx=stub_auth_context(user_id=SPONSOR_ID),
            bot_id=BOT_ID,
            project_ref=OWN,
            role=role,
            request=None,
        )
    assert caught.value.status_code == 400
    assert caught.value.detail["error"] == "bot_role_ceiling"


async def test_a_bot_cannot_create_a_project_over_rest() -> None:
    from sibyl.api.routes import entity_mutations
    from sibyl.api.schemas import EntityCreate
    from sibyl_core.models.entities import EntityType

    request = Request({"type": "http", "method": "POST", "path": "/entities", "headers": []})
    with pytest.raises(HTTPException) as caught:
        await entity_mutations.create_entity(
            request=request,
            entity=EntityCreate(entity_type=EntityType.PROJECT, name="bot project"),
            org=AuthOrganization(id=DEFAULT_ORG_ID, name="Sibyl", slug="sibyl"),
            ctx=_bot_ctx(),
            content_session=None,
            sync=True,
            protect_ownership=False,
            replay_interrupted=True,
        )
    assert caught.value.status_code == 403
    assert caught.value.detail["error"] == "bot_forbidden"


def test_a_bot_cannot_revoke_keys_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    # Its sponsor or an org admin revokes a bot's keys.
    revoke = AsyncMock()
    monkeypatch.setattr(auth_routes, "revoke_api_key_for_user", revoke)
    app = _App(auth_routes.router)
    app.ctx = _bot_ctx()
    response = app.client.post(f"/auth/api-keys/{KEY_ID}/revoke")
    assert response.status_code == 403
    assert revoke.await_count == 0


def test_a_bot_cannot_create_an_api_key_for_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    create = AsyncMock()
    monkeypatch.setattr(auth_routes, "create_api_key_for_user", create)
    app = _App(auth_routes.router)
    app.ctx = _bot_ctx()
    response = app.client.post("/auth/api-keys", json={"name": "self", "scopes": ["api:write"]})
    assert response.status_code == 403
    assert create.await_count == 0


@pytest.mark.parametrize(("method", "path"), [("POST", "/orgs"), ("POST", "/orgs/sibyl/switch")])
def test_a_bot_cannot_create_or_switch_organizations(
    monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    create = AsyncMock()
    switch = AsyncMock()
    monkeypatch.setattr(org_routes.organization_runtime, "create_org", create)
    monkeypatch.setattr(org_routes.organization_runtime, "switch_org", switch)
    app = _App(org_routes.router)
    app.ctx = _bot_ctx()
    response = app.client.request(method, path, json={"name": "Bot Org"})
    assert response.status_code == 403
    assert create.await_count == 0
    assert switch.await_count == 0


def test_a_bot_key_narrowed_by_project_behaves_like_a_user_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "list_project_members",
        "add_project_member",
        "get_project_memory_settings",
        "update_project_memory_settings",
    ):
        monkeypatch.setattr(
            organization_runtime,
            name,
            AsyncMock(return_value=ProjectMembersResult(members=[], can_manage=False)),
        )
    app = _App(project_members.router, project_settings.router)
    app.ctx = _bot_ctx(api_key_project_ids=frozenset({OWN}))
    for method, path in (
        ("GET", f"/projects/{OTHER}/members"),
        ("POST", f"/projects/{OTHER}/members"),
        ("GET", f"/projects/{OTHER}/settings"),
        ("PATCH", f"/projects/{OTHER}/settings"),
    ):
        response = app.client.request(
            method,
            path,
            json={"user_id": str(uuid4()), "role": "project_viewer"}
            if method == "POST"
            else {"default_memory_scope": "project"},
        )
        assert response.status_code == 403, (method, path)
        assert response.json() == {"detail": "project_scope_denied"}


async def test_mcp_refuses_a_bot_creating_a_project(monkeypatch: pytest.MonkeyPatch) -> None:
    from sibyl.mcp_tools import memory as mcp_memory

    ctx = replace(
        stub_mcp_context(org_id=str(DEFAULT_ORG_ID), user_id=str(BOT_ID)), authority=_bot_ctx()
    )
    monkeypatch.setattr(mcp_memory.mcp_context, "require_context", AsyncMock(return_value=ctx))
    scope = AsyncMock(side_effect=AssertionError("resolved scope for a refused project"))
    monkeypatch.setattr(mcp_memory.mcp_context, "resolve_project_scope", scope)
    with pytest.raises(ValueError, match="bot_forbidden"):
        await mcp_memory._add_mcp_entity(
            title="bot project",
            content="",
            entity_type="project",
            category=None,
            languages=None,
            tags=None,
            related_to=None,
            metadata=None,
            project=None,
            priority=None,
            assignees=None,
            due_date=None,
            technologies=None,
            depends_on=None,
            repository_url=None,
        )
