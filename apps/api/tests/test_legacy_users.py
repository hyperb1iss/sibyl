from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from sibyl.persistence.legacy import users as legacy_users


@pytest.mark.asyncio
async def test_request_password_reset_uses_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = AsyncMock()
    manager_cls = MagicMock(return_value=manager)
    session_manager = AsyncMock()
    session_manager.__aenter__.return_value = AsyncMock()
    session_manager.__aexit__.return_value = False

    monkeypatch.setattr(legacy_users, "get_session", lambda: session_manager)
    monkeypatch.setattr(legacy_users, "PasswordResetManager", manager_cls)
    monkeypatch.setattr(legacy_users, "get_email_client", lambda: "email-client")

    await legacy_users.request_password_reset("person@example.com")

    manager_cls.assert_called_once_with(session_manager.__aenter__.return_value, "email-client")
    manager.request_reset.assert_awaited_once_with("person@example.com")


@pytest.mark.asyncio
async def test_confirm_password_reset_translates_manager_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = AsyncMock()
    manager.confirm_reset.side_effect = legacy_users.PasswordResetError("broken token")
    manager_cls = MagicMock(return_value=manager)
    session_manager = AsyncMock()
    session_manager.__aenter__.return_value = AsyncMock()
    session_manager.__aexit__.return_value = False

    monkeypatch.setattr(legacy_users, "get_session", lambda: session_manager)
    monkeypatch.setattr(legacy_users, "PasswordResetManager", manager_cls)
    monkeypatch.setattr(legacy_users, "get_email_client", lambda: "email-client")

    with pytest.raises(HTTPException, match="broken token") as exc_info:
        await legacy_users.confirm_password_reset("token", "new-password")

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_list_oauth_connections_returns_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = SimpleNamespace(
        id=uuid4(),
        provider="github",
        provider_user_id="123",
        provider_email="person@example.com",
        created_at=object(),
    )
    scalars = MagicMock()
    scalars.all.return_value = [connection]
    result = MagicMock()
    result.scalars.return_value = scalars

    session = AsyncMock()
    session.execute.return_value = result

    rows = await legacy_users.list_oauth_connections(session, uuid4())

    assert rows == [connection]


@pytest.mark.asyncio
async def test_remove_oauth_connection_rejects_last_login_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = SimpleNamespace(id=uuid4(), provider="github")
    first = MagicMock()
    first.scalar_one_or_none.return_value = connection
    second = MagicMock()
    second.scalar_one_or_none.return_value = None

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[first, second])
    session.get.return_value = SimpleNamespace(password_hash=None)

    with pytest.raises(HTTPException, match="Cannot remove last login method") as exc_info:
        await legacy_users.remove_oauth_connection(
            session,
            user_id=uuid4(),
            connection_id=connection.id,
        )

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_remove_oauth_connection_deletes_and_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = SimpleNamespace(id=uuid4(), provider="github")
    first = MagicMock()
    first.scalar_one_or_none.return_value = connection
    second = MagicMock()
    second.scalar_one_or_none.return_value = SimpleNamespace(id=uuid4())

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[first, second])
    session.get.return_value = SimpleNamespace(password_hash=None)

    removed = await legacy_users.remove_oauth_connection(
        session,
        user_id=uuid4(),
        connection_id=connection.id,
    )

    assert removed is connection
    session.delete.assert_awaited_once_with(connection)
    session.commit.assert_awaited_once()
