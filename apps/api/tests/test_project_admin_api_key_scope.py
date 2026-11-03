"""API-key project narrowing on the project settings and members routers.

These drive the real routers through FastAPI so the router-level guard is
exercised exactly as production wires it. The refusal matrix enumerates each
router's routes, so a route added later is covered without editing this file.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from sibyl.api.routes import project_members, project_settings
from sibyl.auth import authorization
from sibyl.auth.api_key_common import api_key_memory_scope_key
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import (
    get_auth_context,
    get_current_org_role,
    get_current_organization,
    get_current_user,
)
from sibyl.persistence import organization_runtime
from sibyl.persistence.organization_common import (
    ProjectMemberChange,
    ProjectMembersResult,
    ProjectMemorySettings,
)
from sibyl_core.auth import OrganizationRole, ProjectRole
from sibyl_core.models.memory_scope import MemoryScope
from tests.harness.auth import DEFAULT_ORG_ID, DEFAULT_USER_ID, stub_auth_context

OWN = "project_alpha"
OTHER = "project_bravo"
OWN_UUID = UUID("00000000-0000-0000-0000-00000000a001")
OTHER_UUID = UUID("00000000-0000-0000-0000-00000000b002")
UNKNOWN_UUID = UUID("00000000-0000-0000-0000-00000000c003")
MEMBER_ID = UUID("00000000-0000-0000-0000-000000000333")
KEY_ID = "00000000-0000-0000-0000-00000000d004"

_GRAPH_IDS_BY_UUID = {OWN_UUID: OWN, OTHER_UUID: OTHER}

_ROUTERS = (project_settings.router, project_members.router)
_SETTINGS = project_settings.router.prefix
_MEMBERS = project_members.router.prefix

# (method, router prefix) -> the runtime function that route delegates to.
_RUNTIME_CALLS = {
    ("GET", _SETTINGS): "get_project_memory_settings",
    ("PATCH", _SETTINGS): "update_project_memory_settings",
    ("GET", _MEMBERS): "list_project_members",
    ("POST", _MEMBERS): "add_project_member",
    ("PATCH", _MEMBERS): "update_project_member_role",
    ("DELETE", _MEMBERS): "remove_project_member",
}

_BODIES = {
    ("PATCH", _SETTINGS): {"default_memory_scope": "project"},
    ("POST", _MEMBERS): {
        "user_id": str(MEMBER_ID),
        "role": ProjectRole.CONTRIBUTOR.value,
    },
    ("PATCH", _MEMBERS): {"role": ProjectRole.VIEWER.value},
}

type _Call = tuple[str, str, str]


def _route_calls(routers: tuple[APIRouter, ...]) -> Iterator[_Call]:
    for router in routers:
        for route in router.routes:
            assert isinstance(route, APIRoute)
            for method in sorted(route.methods):
                yield router.prefix, method, route.path


_ALL_CALLS = list(_route_calls(_ROUTERS))
_SETTINGS_PATCH: _Call = (_SETTINGS, "PATCH", _SETTINGS)


def _call_id(call: _Call) -> str:
    _prefix, method, path = call
    return f"{method} {path}"


def _narrowed_key(
    *,
    org_role: OrganizationRole = OrganizationRole.ADMIN,
    memory_scope_keys: set[str] | None = None,
) -> AuthContext:
    return stub_auth_context(
        org_role=org_role,
        api_key_id=KEY_ID,
        api_key_project_ids=frozenset({OWN}),
        api_key_memory_scope_keys=memory_scope_keys,
    )


def _project_memory_grant(graph_project_id: str) -> str:
    return api_key_memory_scope_key(MemoryScope.PROJECT.value, graph_project_id)


class _Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.ctx: AuthContext = stub_auth_context()
        self.runtime: dict[str, AsyncMock] = {}
        settings = ProjectMemorySettings(
            graph_project_id=OWN,
            default_memory_scope=MemoryScope.PRIVATE,
            can_manage=True,
        )
        change = ProjectMemberChange(
            org_id=DEFAULT_ORG_ID,
            project_db_id=OWN_UUID,
            user_id=MEMBER_ID,
            role=ProjectRole.VIEWER,
        )
        results = {
            "get_project_memory_settings": settings,
            "update_project_memory_settings": settings,
            "list_project_members": ProjectMembersResult(members=[], can_manage=True),
            "add_project_member": change,
            "update_project_member_role": change,
            "remove_project_member": change,
        }
        for name, result in results.items():
            mock = AsyncMock(return_value=result)
            self.runtime[name] = mock
            monkeypatch.setattr(organization_runtime, name, mock)
        monkeypatch.setattr(project_members, "broadcast_event", AsyncMock())

        async def record_by_id(*, organization_id: UUID, project_id: UUID) -> SimpleNamespace:
            assert organization_id == DEFAULT_ORG_ID
            graph_project_id = _GRAPH_IDS_BY_UUID.get(project_id)
            if graph_project_id is None:
                raise HTTPException(status_code=404, detail="Project not found")
            return SimpleNamespace(graph_project_id=graph_project_id)

        self.record_by_id = AsyncMock(side_effect=record_by_id)
        monkeypatch.setattr(authorization, "get_project_record_by_id", self.record_by_id)

        app = FastAPI()
        for router in _ROUTERS:
            app.include_router(router)
        app.dependency_overrides[get_auth_context] = lambda: self.ctx
        app.dependency_overrides[get_current_user] = lambda: self.ctx.user
        app.dependency_overrides[get_current_organization] = lambda: self.ctx.organization
        app.dependency_overrides[get_current_org_role] = lambda: self.ctx.org_role
        self.client = TestClient(app)

    def send(self, call: _Call, project_id: str) -> tuple[int, object]:
        prefix, method, path = call
        url = path.replace("{project_id}", project_id).replace("{user_id}", str(MEMBER_ID))
        response = self.client.request(method, url, json=_BODIES.get((method, prefix)))
        return response.status_code, response.json()

    def runtime_for(self, call: _Call) -> AsyncMock:
        prefix, method, _path = call
        return self.runtime[_RUNTIME_CALLS[(method, prefix)]]

    def assert_no_runtime_call(self) -> None:
        for name, mock in self.runtime.items():
            assert not mock.await_count, f"{name} ran for a refused request"


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> _Harness:
    return _Harness(monkeypatch)


def test_the_matrix_covers_every_route_on_both_routers() -> None:
    covered = {(method, prefix) for prefix, method, _path in _ALL_CALLS}
    assert covered == set(_RUNTIME_CALLS)


@pytest.mark.parametrize("call", _ALL_CALLS, ids=_call_id)
@pytest.mark.parametrize(
    "project_id",
    [OTHER, str(OTHER_UUID), "project_missing", str(UNKNOWN_UUID), "not-a-project"],
)
@pytest.mark.parametrize("org_role", [OrganizationRole.ADMIN, OrganizationRole.MEMBER])
def test_a_narrowed_key_is_refused_outside_its_projects(
    harness: _Harness,
    call: _Call,
    project_id: str,
    org_role: OrganizationRole,
) -> None:
    harness.ctx = _narrowed_key(org_role=org_role)
    status_code, body = harness.send(call, project_id)

    assert status_code == 403
    assert body == {"detail": "project_scope_denied"}
    harness.assert_no_runtime_call()


@pytest.mark.parametrize("call", _ALL_CALLS, ids=_call_id)
@pytest.mark.parametrize("project_id", [OWN, str(OWN_UUID)])
def test_a_key_bound_to_no_projects_is_refused_everywhere(
    harness: _Harness, call: _Call, project_id: str
) -> None:
    harness.ctx = stub_auth_context(
        org_role=OrganizationRole.ADMIN,
        api_key_id=KEY_ID,
        api_key_project_ids=frozenset(),
    )

    status_code, body = harness.send(call, project_id)

    assert status_code == 403
    assert body == {"detail": "project_scope_denied"}
    harness.assert_no_runtime_call()


@pytest.mark.parametrize("call", _ALL_CALLS, ids=_call_id)
@pytest.mark.parametrize("project_id", [OWN, str(OWN_UUID)])
def test_a_narrowed_key_works_on_its_own_project(
    harness: _Harness,
    call: _Call,
    project_id: str,
) -> None:
    harness.ctx = _narrowed_key()
    status_code, _body = harness.send(call, project_id)

    assert status_code == 200
    runtime = harness.runtime_for(call)
    runtime.assert_awaited_once()
    assert runtime.await_args.kwargs["project_id"] == project_id


@pytest.mark.parametrize("call", _ALL_CALLS, ids=_call_id)
@pytest.mark.parametrize("project_id", [OTHER, str(OTHER_UUID)])
@pytest.mark.parametrize(
    "ctx",
    [
        pytest.param(stub_auth_context(org_role=OrganizationRole.ADMIN), id="session"),
        pytest.param(
            stub_auth_context(org_role=OrganizationRole.ADMIN, api_key_id=KEY_ID),
            id="unrestricted-key",
        ),
    ],
)
def test_sessions_and_unrestricted_keys_are_unchanged(
    harness: _Harness,
    call: _Call,
    project_id: str,
    ctx: AuthContext,
) -> None:
    harness.ctx = ctx
    status_code, _body = harness.send(call, project_id)

    assert status_code == 200
    runtime = harness.runtime_for(call)
    runtime.assert_awaited_once()
    assert runtime.await_args.kwargs["project_id"] == project_id
    assert runtime.await_args.kwargs["actor"].id == DEFAULT_USER_ID
    harness.record_by_id.assert_not_awaited()


def test_a_session_still_reaches_the_runtime_refusal(harness: _Harness) -> None:
    """The guard adds nothing for a session, so the runtime's own answer stands."""
    harness.runtime["update_project_memory_settings"].side_effect = HTTPException(
        status_code=403, detail="Forbidden"
    )
    harness.ctx = stub_auth_context(org_role=OrganizationRole.MEMBER)

    status_code, body = harness.send(_SETTINGS_PATCH, OTHER)

    assert status_code == 403
    assert body == {"detail": "Forbidden"}


# --- Memory-space binding --------------------------------------------------

_MUTATIONS = [call for call in _ALL_CALLS if call[1] != "GET"]
_READS = [call for call in _ALL_CALLS if call[1] == "GET"]
_PRIVATE_ONLY = {api_key_memory_scope_key("private", None)}


@pytest.mark.parametrize("call", _MUTATIONS, ids=_call_id)
@pytest.mark.parametrize("project_id", [OWN, str(OWN_UUID)])
def test_a_mutation_needs_the_project_memory_space(
    harness: _Harness, call: _Call, project_id: str
) -> None:
    harness.ctx = _narrowed_key(memory_scope_keys=_PRIVATE_ONLY)

    status_code, body = harness.send(call, project_id)

    assert status_code == 403
    assert body == {"detail": "api_key_memory_space_denied"}
    harness.assert_no_runtime_call()


@pytest.mark.parametrize("call", _MUTATIONS, ids=_call_id)
def test_another_projects_memory_space_does_not_count(harness: _Harness, call: _Call) -> None:
    harness.ctx = stub_auth_context(
        org_role=OrganizationRole.ADMIN,
        api_key_id=KEY_ID,
        api_key_memory_scope_keys={_project_memory_grant(OWN)},
    )

    status_code, body = harness.send(call, OTHER)

    assert status_code == 403
    assert body == {"detail": "api_key_memory_space_denied"}
    harness.assert_no_runtime_call()


@pytest.mark.parametrize("call", _MUTATIONS, ids=_call_id)
@pytest.mark.parametrize("project_id", [OWN, str(OWN_UUID)])
def test_a_key_holding_the_project_memory_space_can_mutate(
    harness: _Harness, call: _Call, project_id: str
) -> None:
    harness.ctx = _narrowed_key(memory_scope_keys={_project_memory_grant(OWN)})

    status_code, _body = harness.send(call, project_id)

    assert status_code == 200
    runtime = harness.runtime_for(call)
    runtime.assert_awaited_once()
    assert runtime.await_args.kwargs["project_id"] == project_id


@pytest.mark.parametrize("call", _READS, ids=_call_id)
def test_memory_binding_leaves_reads_alone(harness: _Harness, call: _Call) -> None:
    harness.ctx = _narrowed_key(memory_scope_keys=_PRIVATE_ONLY)

    status_code, _body = harness.send(call, OWN)

    assert status_code == 200
    harness.runtime_for(call).assert_awaited_once()


@pytest.mark.parametrize("call", _MUTATIONS, ids=_call_id)
def test_project_binding_is_checked_before_the_memory_binding(
    harness: _Harness, call: _Call
) -> None:
    harness.ctx = _narrowed_key(memory_scope_keys={_project_memory_grant(OTHER)})

    status_code, body = harness.send(call, OTHER)

    assert status_code == 403
    assert body == {"detail": "project_scope_denied"}
    harness.assert_no_runtime_call()
