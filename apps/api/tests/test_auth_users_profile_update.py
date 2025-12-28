from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl.auth.passwords import hash_password, verify_password
from sibyl.auth.users import PasswordChange, UserManager
from sibyl.config import Settings
from sibyl.db.models import User


@pytest.mark.asyncio
async def test_user_manager_update_profile_normalizes_fields() -> None:
    session = AsyncMock()
    session.flush = AsyncMock()

    manager = UserManager(session)
    manager.get_by_email = AsyncMock(return_value=None)  # type: ignore[method-assign]

    user = User(id=uuid4(), name="Old", email="old@example.com")
    updated = await manager.update_profile(
        user,
        email="  NEW@EXAMPLE.COM ",
        name="  New Name ",
        avatar_url="  https://example.com/a.png ",
    )

    assert updated is user
    assert user.email == "new@example.com"
    assert user.name == "New Name"
    assert user.avatar_url == "https://example.com/a.png"
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_user_manager_update_profile_rejects_duplicate_email() -> None:
    session = AsyncMock()
    session.flush = AsyncMock()

    manager = UserManager(session)
    other = User(id=uuid4(), name="Other", email="dup@example.com")
    manager.get_by_email = AsyncMock(return_value=other)  # type: ignore[method-assign]

    user = User(id=uuid4(), name="Me", email="me@example.com")
    with pytest.raises(ValueError, match="already in use"):
        await manager.update_profile(user, email="dup@example.com")


@pytest.mark.asyncio
async def test_user_manager_change_password_requires_current_password(monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_PASSWORD_PEPPER", "")
    monkeypatch.setenv("SIBYL_PASSWORD_ITERATIONS", "100000")
    from sibyl import config as config_module

    config_module.settings = Settings(_env_file=None)  # type: ignore[assignment]

    session = AsyncMock()
    session.flush = AsyncMock()
    manager = UserManager(session)

    pw = hash_password("oldpw", iterations=100000)
    user = User(
        id=uuid4(),
        name="Me",
        email="me@example.com",
        password_salt=pw.salt_hex,
        password_hash=pw.hash_hex,
        password_iterations=pw.iterations,
    )

    with pytest.raises(ValueError, match="Current password is required"):
        await manager.change_password(
            user,
            PasswordChange(current_password=None, new_password="newpw"),  # noqa: S106
        )


@pytest.mark.asyncio
async def test_user_manager_change_password_roundtrip(monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_PASSWORD_PEPPER", "")
    monkeypatch.setenv("SIBYL_PASSWORD_ITERATIONS", "100000")
    from sibyl import config as config_module

    config_module.settings = Settings(_env_file=None)  # type: ignore[assignment]

    session = AsyncMock()
    session.flush = AsyncMock()
    manager = UserManager(session)

    pw = hash_password("oldpw", iterations=100000)
    user = User(
        id=uuid4(),
        name="Me",
        email="me@example.com",
        password_salt=pw.salt_hex,
        password_hash=pw.hash_hex,
        password_iterations=pw.iterations,
    )

    await manager.change_password(
        user,
        PasswordChange(current_password="oldpw", new_password="newpw"),  # noqa: S106
    )

    assert user.password_salt
    assert user.password_hash
    assert user.password_iterations
    assert verify_password(
        "newpw",
        salt_hex=user.password_salt,
        hash_hex=user.password_hash,
        iterations=int(user.password_iterations),
    )
