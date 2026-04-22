from __future__ import annotations

import pytest

from sibyl.persistence import auth_runtime
from sibyl.persistence.auth_common import InvalidAuthClaimsError, UserNotFoundError
from sibyl.persistence.legacy.auth import LegacyAuthContextResolver
from sibyl.persistence.surreal.auth import SurrealAuthContextResolver


def test_auth_runtime_uses_shared_error_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_runtime.settings, "auth_store", "postgres")

    assert auth_runtime.InvalidAuthClaimsError is InvalidAuthClaimsError
    assert auth_runtime.UserNotFoundError is UserNotFoundError
    assert auth_runtime.LegacyAuthContextResolver is LegacyAuthContextResolver


def test_auth_runtime_maps_resolver_name_for_surreal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_runtime.settings, "auth_store", "surreal")

    assert auth_runtime.LegacyAuthContextResolver is SurrealAuthContextResolver


@pytest.mark.asyncio
async def test_auth_runtime_guards_missing_surreal_exports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_runtime.settings, "auth_store", "surreal")

    with pytest.raises(NotImplementedError, match="SIBYL_AUTH_STORE='surreal'"):
        await auth_runtime.authenticate_legacy_api_key("sk_test_placeholder")
