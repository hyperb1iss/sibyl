import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl.api.routes import users as user_routes
from sibyl_core.auth import AuthUser


def _auth() -> SimpleNamespace:
    return SimpleNamespace(
        user=SimpleNamespace(
            id=uuid4(),
            email="nova@example.com",
            name="Nova",
            bio="hello",
            timezone="UTC",
            avatar_url="https://example.com/avatar.png",
            email_verified_at=datetime.now(UTC).replace(tzinfo=None),
            created_at=datetime.now(UTC).replace(tzinfo=None),
            preferences={},
        ),
        organization=None,
    )


@pytest.mark.asyncio
async def test_request_password_reset_uses_runtime_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_reset = AsyncMock()
    monkeypatch.setattr(user_routes, "request_password_reset_token", request_reset)

    response = await user_routes.request_password_reset(
        user_routes.PasswordResetRequest(email="person@example.com")
    )

    assert response["message"].startswith("If an account exists")
    request_reset.assert_awaited_once_with("person@example.com")


@pytest.mark.asyncio
async def test_confirm_password_reset_uses_runtime_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    confirm_reset = AsyncMock()
    monkeypatch.setattr(user_routes, "confirm_password_reset_token", confirm_reset)

    await user_routes.confirm_password_reset(
        user_routes.PasswordResetConfirmRequest(
            token="reset-token",
            new_password="new-password-123",
        )
    )

    confirm_reset.assert_awaited_once_with("reset-token", "new-password-123")


@pytest.mark.asyncio
async def test_list_connections_uses_runtime_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    auth = _auth()
    connected_at = datetime.now(UTC).replace(tzinfo=None)
    monkeypatch.setattr(
        user_routes,
        "list_oauth_connections",
        AsyncMock(
            return_value=[
                SimpleNamespace(
                    id=uuid4(),
                    provider="github",
                    provider_user_id="123",
                    provider_email="person@example.com",
                    created_at=connected_at,
                )
            ]
        ),
    )

    response = await user_routes.list_connections(auth=auth)

    assert len(response) == 1
    assert response[0].provider == "github"
    assert response[0].connected_at is connected_at


@pytest.mark.asyncio
async def test_remove_connection_uses_runtime_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    auth = _auth()
    connection_id = uuid4()
    remove_connection = AsyncMock(return_value=SimpleNamespace(provider="github"))
    monkeypatch.setattr(user_routes, "remove_oauth_connection", remove_connection)

    await user_routes.remove_connection(connection_id=connection_id, auth=auth)

    remove_connection.assert_awaited_once_with(
        user_id=auth.user.id,
        connection_id=connection_id,
    )


@pytest.mark.asyncio
async def test_get_profile_uses_auth_context_user() -> None:
    auth = _auth()

    response = await user_routes.get_profile(auth=auth)

    assert response.id == auth.user.id
    assert response.email == "nova@example.com"
    assert response.created_at is auth.user.created_at


@pytest.mark.asyncio
async def test_get_profile_accepts_normalized_auth_user() -> None:
    created_at = datetime.now(UTC).replace(tzinfo=None)
    auth = SimpleNamespace(
        user=AuthUser(
            id=uuid4(),
            email="nova@example.com",
            name="Nova",
            created_at=created_at,
        ),
        organization=None,
    )

    response = await user_routes.get_profile(auth=auth)

    assert response.email_verified_at is None
    assert response.created_at is created_at


@pytest.mark.asyncio
async def test_get_preferences_uses_auth_context_user() -> None:
    auth = _auth()
    auth.user.preferences = {"theme": "dark"}

    response = await user_routes.get_preferences(auth=auth)

    assert response.preferences == {"theme": "dark"}


@pytest.mark.asyncio
async def test_update_preferences_merges_and_uses_runtime_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = _auth()
    auth.organization = SimpleNamespace(id=uuid4())
    auth.user.preferences = {"theme": "light"}
    patch_user = AsyncMock(
        return_value=SimpleNamespace(
            id=auth.user.id,
            preferences={"theme": "light", "compact": True},
        )
    )
    monkeypatch.setattr(user_routes, "patch_auth_user", patch_user)

    response = await user_routes.update_preferences(
        data=user_routes.PreferencesUpdateRequest(preferences={"compact": True}),
        auth=auth,
    )

    patch_user.assert_awaited_once_with(
        user_id=auth.user.id,
        updates={"preferences": {"theme": "light", "compact": True}},
        organization_id=auth.organization.id,
        request=None,
    )
    assert response.preferences == {"theme": "light", "compact": True}


@pytest.mark.asyncio
async def test_change_password_uses_auth_runtime_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    auth = _auth()
    auth.organization = SimpleNamespace(id=uuid4())
    update_user = AsyncMock(return_value=SimpleNamespace(id=auth.user.id))
    monkeypatch.setattr(user_routes, "update_auth_user", update_user)

    await user_routes.change_password(
        data=user_routes.PasswordChangeRequest(
            current_password="old-password",
            new_password="new-password-123",
        ),
        auth=auth,
    )

    update_user.assert_awaited_once_with(
        user_id=auth.user.id,
        email=None,
        name=None,
        avatar_url=None,
        current_password="old-password",
        new_password="new-password-123",
        organization_id=auth.organization.id,
        request=None,
    )


@pytest.mark.asyncio
async def test_list_sessions_uses_runtime_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    auth = _auth()
    session_row = SimpleNamespace(
        id=uuid4(),
        user_agent="Firefox",
        ip_address="127.0.0.1",
        created_at=datetime.now(UTC).replace(tzinfo=None),
        expires_at=datetime.now(UTC).replace(tzinfo=None),
        last_active_at=None,
        token_hash="token-hash",
    )
    list_sessions = AsyncMock(return_value=[session_row])
    monkeypatch.setattr(user_routes, "list_user_sessions", list_sessions)
    request = SimpleNamespace(headers={"authorization": "Bearer no-match"}, cookies={})

    response = await user_routes.list_sessions(request=request, auth=auth)

    list_sessions.assert_awaited_once_with(user_id=auth.user.id)
    assert len(response) == 1
    assert response[0].is_current is False


@pytest.mark.asyncio
async def test_revoke_all_sessions_returns_revoked_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = _auth()
    revoke_all = AsyncMock(return_value=2)
    monkeypatch.setattr(user_routes, "revoke_all_user_sessions", revoke_all)
    request = SimpleNamespace(headers={"authorization": "Bearer keep-token"}, cookies={})

    response = await user_routes.revoke_all_sessions(request=request, auth=auth)

    assert response.revoked == 2
    revoke_all.assert_awaited_once_with(
        user_id=auth.user.id,
        exclude_token_hash=hashlib.sha256(b"keep-token").hexdigest(),
    )
