from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any

import httpx
import pytest

from sibyl_cli import client as client_module
from sibyl_cli import client_auth as client_auth_module
from sibyl_cli import client_transport as client_transport_module
from sibyl_cli.client import SibylClient


@contextmanager
def _noop_lock():
    yield


def test_empty_auth_token_disables_stored_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        client_module, "_load_default_auth_token", lambda _api_url, _scope=None: "stored"
    )

    client = SibylClient(base_url="http://example.test/api", auth_token="")

    assert client.auth_token == ""
    assert "Authorization" not in client._default_headers()


def test_invalid_refresh_token_message_is_recoverable() -> None:
    assert (
        client_module._is_refresh_revoked("Invalid refresh token: Signature verification failed")
        is True
    )


def test_replay_scope_falls_back_to_loaded_token_when_credential_slot_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SIBYL_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(
        client_transport_module,
        "read_server_credentials",
        lambda _api_url, **_kwargs: {
            "access_token": "other-user-access",
            "pending_replay_scope": "credential:other-user",
        },
    )

    replay_scope = client_transport_module._load_default_replay_scope(
        "http://example.test/api",
        None,
        "original-access",
    )

    assert replay_scope == client_transport_module._auth_replay_scope(
        None, "original-access"
    )


@pytest.mark.asyncio
async def test_refresh_skips_manual_auth_token() -> None:
    client = SibylClient(base_url="http://example.test/api", auth_token="manual")

    refreshed, failure = await client._refresh_token()

    assert refreshed is False
    assert failure == "Automatic renewal is only available for stored CLI login tokens."


@pytest.mark.asyncio
async def test_silent_local_relogin_switches_back_to_the_scoped_organization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, dict[str, object]]] = []
    writes: list[dict[str, object]] = []

    class FakeResponse:
        def __init__(self, data: dict[str, object]) -> None:
            self.status_code = 200
            self._data = data

        def json(self) -> dict[str, object]:
            return self._data

    class FakeAsyncClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, path: str, **kwargs: object) -> FakeResponse:
            requests.append((path, kwargs))
            if path == "/auth/local/login":
                return FakeResponse({"access_token": "personal-token"})
            assert path == "/orgs/work/switch"
            return FakeResponse(
                {
                    "access_token": "work-token",
                    "refresh_token": "work-refresh",
                    "expires_in": 3600,
                    "organization": {"slug": "work"},
                }
            )

    monkeypatch.setattr(client_auth_module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(
        client_auth_module,
        "set_tokens",
        lambda *args, **kwargs: writes.append({"args": args, **kwargs}),
    )

    client = SibylClient(base_url="http://example.test/api", auth_token="old-token")
    client.credential_scope = "context:local:org:work"
    client._replay_scope = "credential:work-lineage"
    relogged, failure = await client._silent_local_relogin(
        {"local_login_email": "bliss@example.test", "local_login_password": "secret"}
    )

    assert relogged is True
    assert failure is None
    assert client.auth_token == "work-token"
    assert client._replay_scope == "credential:work-lineage"
    assert requests == [
        (
            "/auth/local/login",
            {"json": {"email": "bliss@example.test", "password": "secret"}},
        ),
        (
            "/orgs/work/switch",
            {"headers": {"Authorization": "Bearer personal-token"}},
        ),
    ]
    assert writes[0]["pending_replay_scope"] == "credential:work-lineage"


@pytest.mark.asyncio
async def test_silent_local_relogin_refuses_an_unscoped_organization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnexpectedAsyncClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("unscoped relogin must not contact the login endpoint")

    monkeypatch.setattr(client_auth_module.httpx, "AsyncClient", UnexpectedAsyncClient)
    client = SibylClient(base_url="http://example.test/api", auth_token="old-token")

    relogged, failure = await client._silent_local_relogin(
        {"local_login_email": "bliss@example.test", "local_login_password": "secret"}
    )

    assert relogged is False
    assert failure == "Silent local re-login requires an organization-scoped context."


@pytest.mark.asyncio
async def test_environment_token_ignores_expired_stored_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIBYL_AUTH_TOKEN", "automation-token")

    def unexpected_stored_expiry_check(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("environment auth must not read stored expiry metadata")

    monkeypatch.setattr(
        client_transport_module,
        "is_access_token_expired",
        unexpected_stored_expiry_check,
    )
    seen_authorization: list[str] = []

    def authorized(request: httpx.Request) -> httpx.Response:
        seen_authorization.append(request.headers["Authorization"])
        return httpx.Response(200, json={"ok": True})

    client = SibylClient(base_url="http://example.test/api")
    client._client = httpx.AsyncClient(
        base_url=client.base_url,
        transport=httpx.MockTransport(authorized),
        headers=client._default_headers(),
    )

    result = await client.get("/entities")

    await client.close()
    assert result == {"ok": True}
    assert seen_authorization == ["Bearer automation-token"]


@pytest.mark.asyncio
async def test_refresh_uses_newer_token_written_by_another_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SIBYL_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(
        client_module, "_load_default_auth_token", lambda _api_url, _scope=None: "old-access"
    )
    monkeypatch.setattr(client_auth_module, "auth_file_lock", lambda: _noop_lock())
    replay_scope = client_transport_module._auth_replay_scope(None, "old-access")
    monkeypatch.setattr(
        client_auth_module,
        "read_server_credentials",
        lambda _api_url, **_kwargs: {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "access_token_expires_at": int(time.time()) + 3600,
            "pending_replay_scope": replay_scope,
        },
    )
    monkeypatch.setattr(
        client_auth_module,
        "is_access_token_expired",
        lambda _api_url, **_kwargs: False,
    )

    class UnexpectedAsyncClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("refresh endpoint should not be called")

    monkeypatch.setattr(client_auth_module.httpx, "AsyncClient", UnexpectedAsyncClient)

    client = SibylClient(base_url="http://example.test/api")
    refreshed, failure = await client._refresh_token()

    assert refreshed is True
    assert failure is None
    assert client.auth_token == "new-access"
    assert client._replay_scope == replay_scope


@pytest.mark.asyncio
async def test_refresh_refuses_token_from_replaced_credential_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SIBYL_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(
        client_module, "_load_default_auth_token", lambda _api_url, _scope=None: "old-access"
    )
    monkeypatch.setattr(client_auth_module, "auth_file_lock", lambda: _noop_lock())
    monkeypatch.setattr(
        client_auth_module,
        "read_server_credentials",
        lambda _api_url, **_kwargs: {
            "access_token": "other-user-access",
            "refresh_token": "other-user-refresh",
            "access_token_expires_at": int(time.time()) + 3600,
            "pending_replay_scope": "credential:other-user",
        },
    )
    monkeypatch.setattr(
        client_auth_module,
        "is_access_token_expired",
        lambda _api_url, **_kwargs: False,
    )

    client = SibylClient(base_url="http://example.test/api")
    original_scope = client._replay_scope
    refreshed, failure = await client._refresh_token()

    assert refreshed is False
    assert failure == (
        "Stored credentials changed since this command started; retry the command with the "
        "current login."
    )
    assert client.auth_token == "old-access"
    assert client._replay_scope == original_scope


@pytest.mark.asyncio
async def test_refresh_refuses_expired_token_from_replaced_credential_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SIBYL_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(
        client_module, "_load_default_auth_token", lambda _api_url, _scope=None: "old-access"
    )
    monkeypatch.setattr(client_auth_module, "auth_file_lock", lambda: _noop_lock())
    monkeypatch.setattr(
        client_auth_module,
        "read_server_credentials",
        lambda _api_url, **_kwargs: {
            "access_token": "expired-other-user-access",
            "refresh_token": "other-user-refresh",
            "access_token_expires_at": 1,
            "pending_replay_scope": "credential:other-user",
        },
    )

    class UnexpectedAsyncClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("another credential's refresh token must never be used")

    monkeypatch.setattr(client_auth_module.httpx, "AsyncClient", UnexpectedAsyncClient)
    client = SibylClient(base_url="http://example.test/api")
    original_scope = client._replay_scope
    refreshed, failure = await client._refresh_token()

    assert refreshed is False
    assert failure == (
        "Stored credentials changed since this command started; retry the command with the "
        "current login."
    )
    assert client.auth_token == "old-access"
    assert client._replay_scope == original_scope


@pytest.mark.asyncio
async def test_refresh_rotates_and_writes_tokens_under_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SIBYL_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(
        client_module, "_load_default_auth_token", lambda _api_url, _scope=None: "old-access"
    )
    monkeypatch.setattr(client_auth_module, "auth_file_lock", lambda: _noop_lock())
    monkeypatch.setattr(
        client_auth_module,
        "read_server_credentials",
        lambda _api_url, **_kwargs: {
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "access_token_expires_at": 1,
        },
    )
    monkeypatch.setattr(
        client_auth_module, "get_refresh_token", lambda _api_url, **_kwargs: "old-refresh"
    )
    monkeypatch.setattr(
        client_auth_module,
        "is_access_token_expired",
        lambda _api_url, **_kwargs: True,
    )

    writes: list[dict[str, Any]] = []

    def fake_set_tokens(
        api_url: str,
        access_token: str,
        *,
        refresh_token: str | None = None,
        expires_in: int | None = None,
        lock: bool = True,
        credential_scope: str | None = None,
        pending_replay_scope: str | None = None,
    ) -> None:
        writes.append(
            {
                "api_url": api_url,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_in": expires_in,
                "lock": lock,
                "credential_scope": credential_scope,
                "pending_replay_scope": pending_replay_scope,
            }
        )

    monkeypatch.setattr(client_auth_module, "set_tokens", fake_set_tokens)
    class FakeResponse:
        status_code = 200
        text = ""

        def json(self) -> dict[str, object]:
            return {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
            }

    class FakeAsyncClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, path: str, *, json: dict[str, object]) -> FakeResponse:
            assert path == "/auth/refresh"
            assert json == {"refresh_token": "old-refresh"}
            return FakeResponse()

    monkeypatch.setattr(client_auth_module.httpx, "AsyncClient", FakeAsyncClient)

    client = SibylClient(base_url="http://example.test/api")
    refreshed, failure = await client._refresh_token()

    assert refreshed is True
    assert failure is None
    assert client.auth_token == "new-access"
    assert client._replay_scope == client_transport_module._auth_replay_scope(
        None, "old-access"
    )
    assert writes == [
        {
            "api_url": "http://example.test/api",
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
            "lock": False,
            "credential_scope": None,
            "pending_replay_scope": client_transport_module._auth_replay_scope(
                None, "old-access"
            ),
        }
    ]
