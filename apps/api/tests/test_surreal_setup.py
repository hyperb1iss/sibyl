from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from sibyl.persistence.surreal import setup as surreal_setup
from sibyl_core.auth import OrganizationRole


def _request(headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/setup/config",
            "headers": headers or [],
        }
    )


@pytest.mark.asyncio
async def test_surreal_setup_mode_stays_open_until_admin_org_initialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.closed = False

        async def execute_query(self, query: str, **_params: object):
            self.calls.append(query)
            return {
                "users": [{"uuid": str(uuid4())}],
                "organizations": [{"uuid": str(uuid4())}],
                "initialized_memberships": [],
            }

        async def close(self) -> None:
            self.closed = True

    client = FakeClient()

    monkeypatch.setattr(surreal_setup, "build_surreal_auth_client", lambda: client)
    monkeypatch.setattr(
        surreal_setup.SurrealUserRepository,
        "from_client",
        lambda _client: (_ for _ in ()).throw(AssertionError("unexpected user repository")),
    )

    assert await surreal_setup.is_setup_mode() is True
    assert len(client.calls) == 1
    assert "FROM users" in client.calls[0]
    assert "FROM organizations" in client.calls[0]
    assert "FROM organization_members" in client.calls[0]
    assert client.closed is True


@pytest.mark.asyncio
async def test_surreal_setup_status_batches_user_and_org_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.closed = False

        async def execute_query(self, query: str, **_params: object):
            self.calls.append(query)
            return {
                "users": [{"uuid": str(uuid4())}],
                "organizations": [{"uuid": str(uuid4())}],
                "initialized_memberships": [{"uuid": str(uuid4())}],
            }

        async def close(self) -> None:
            self.closed = True

    client = FakeClient()

    monkeypatch.setattr(surreal_setup, "build_surreal_auth_client", lambda: client)
    monkeypatch.setattr(
        surreal_setup.SurrealUserRepository,
        "from_client",
        lambda _client: (_ for _ in ()).throw(AssertionError("unexpected user repository")),
    )
    monkeypatch.setattr(
        surreal_setup.SurrealOrganizationRepository,
        "from_client",
        lambda _client: (_ for _ in ()).throw(AssertionError("unexpected org repository")),
    )

    status = await surreal_setup.get_setup_status()

    assert status.has_users is True
    assert status.has_orgs is True
    assert status.setup_complete is True
    assert len(client.calls) == 1
    assert "RETURN" in client.calls[0]
    assert "FROM users" in client.calls[0]
    assert "FROM organizations" in client.calls[0]
    assert "FROM organization_members" in client.calls[0]
    assert client.closed is True


@pytest.mark.asyncio
async def test_require_setup_mode_or_admin_reports_initialized_without_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(surreal_setup, "is_setup_mode", AsyncMock(return_value=False))

    with pytest.raises(HTTPException) as exc_info:
        await surreal_setup.require_setup_mode_or_admin(_request())

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["code"] == "setup_already_initialized"


@pytest.mark.asyncio
async def test_require_setup_mode_or_admin_accepts_global_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = SimpleNamespace(is_admin=True)
    monkeypatch.setattr(surreal_setup, "is_setup_mode", AsyncMock(return_value=False))
    monkeypatch.setattr(
        surreal_setup,
        "verify_access_token",
        lambda _token: {"sub": str(uuid4())},
    )
    monkeypatch.setattr(
        surreal_setup,
        "build_auth_context",
        AsyncMock(
            return_value=SimpleNamespace(
                organization=object(),
                org_role=OrganizationRole.OWNER,
                user=user,
            )
        ),
    )

    result = await surreal_setup.require_setup_mode_or_admin(
        _request(headers=[(b"authorization", b"Bearer valid-token")])
    )

    assert result is user


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role", [OrganizationRole.OWNER, OrganizationRole.ADMIN, OrganizationRole.MEMBER]
)
async def test_require_setup_mode_or_admin_accepts_global_admin_in_any_org_role(
    monkeypatch: pytest.MonkeyPatch,
    role: OrganizationRole,
) -> None:
    user = SimpleNamespace(is_admin=True)
    monkeypatch.setattr(surreal_setup, "is_setup_mode", AsyncMock(return_value=False))
    monkeypatch.setattr(
        surreal_setup,
        "verify_access_token",
        lambda _token: {"sub": str(uuid4())},
    )
    monkeypatch.setattr(
        surreal_setup,
        "build_auth_context",
        AsyncMock(
            return_value=SimpleNamespace(
                organization=object(),
                org_role=role,
                user=user,
            )
        ),
    )

    result = await surreal_setup.require_setup_mode_or_admin(
        _request(headers=[(b"authorization", b"Bearer valid-token")])
    )

    assert result is user


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("organization", "role", "is_admin"),
    [
        (object(), OrganizationRole.OWNER, False),
        (object(), OrganizationRole.ADMIN, False),
        (object(), OrganizationRole.MEMBER, False),
        (None, OrganizationRole.OWNER, False),
    ],
)
async def test_require_setup_mode_or_admin_rejects_non_admin_context(
    monkeypatch: pytest.MonkeyPatch,
    organization: object | None,
    role: OrganizationRole,
    is_admin: bool,
) -> None:
    monkeypatch.setattr(surreal_setup, "is_setup_mode", AsyncMock(return_value=False))
    monkeypatch.setattr(
        surreal_setup,
        "verify_access_token",
        lambda _token: {"sub": str(uuid4())},
    )
    monkeypatch.setattr(
        surreal_setup,
        "build_auth_context",
        AsyncMock(
            return_value=SimpleNamespace(
                organization=organization,
                org_role=role,
                user=SimpleNamespace(is_admin=is_admin),
            )
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await surreal_setup.require_setup_mode_or_admin(
            _request(headers=[(b"authorization", b"Bearer valid-token")])
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Global admin required"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role",
    [OrganizationRole.OWNER, OrganizationRole.ADMIN, OrganizationRole.MEMBER],
)
async def test_require_settings_owner_accepts_global_admin(
    monkeypatch: pytest.MonkeyPatch,
    role: OrganizationRole,
) -> None:
    monkeypatch.setattr(surreal_setup, "is_setup_mode", AsyncMock(return_value=False))
    monkeypatch.setattr(
        surreal_setup,
        "build_auth_context",
        AsyncMock(
            return_value=SimpleNamespace(
                organization=object(),
                org_role=role,
                user=SimpleNamespace(is_admin=True),
            )
        ),
    )

    assert await surreal_setup.require_settings_owner(_request()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("organization", "role"),
    [
        (object(), OrganizationRole.OWNER),
        (object(), OrganizationRole.ADMIN),
        (object(), OrganizationRole.MEMBER),
        (None, OrganizationRole.OWNER),
    ],
)
async def test_require_settings_owner_rejects_non_global_admin(
    monkeypatch: pytest.MonkeyPatch,
    organization: object | None,
    role: OrganizationRole,
) -> None:
    monkeypatch.setattr(surreal_setup, "is_setup_mode", AsyncMock(return_value=False))
    monkeypatch.setattr(
        surreal_setup,
        "build_auth_context",
        AsyncMock(
            return_value=SimpleNamespace(
                organization=organization,
                org_role=role,
                user=SimpleNamespace(is_admin=False),
            )
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await surreal_setup.require_settings_owner(_request())

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Global admin required"


@pytest.mark.asyncio
async def test_require_settings_owner_allows_setup_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(surreal_setup, "is_setup_mode", AsyncMock(return_value=True))

    assert await surreal_setup.require_settings_owner(_request()) is None


def test_surreal_setup_exports_neutral_runtime_surface() -> None:
    assert surreal_setup.__all__ == [
        "SetupStatus",
        "SurrealOrganizationRepository",
        "SurrealUserRepository",
        "build_surreal_auth_client",
        "get_setup_status",
        "is_setup_mode",
        "require_settings_admin",
        "require_settings_owner",
        "require_setup_mode_or_admin",
        "require_setup_mode_or_auth",
    ]
