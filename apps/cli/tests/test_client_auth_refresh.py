from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any

import pytest

from sibyl_cli import client as client_module
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


@pytest.mark.asyncio
async def test_refresh_skips_manual_auth_token() -> None:
    client = SibylClient(base_url="http://example.test/api", auth_token="manual")

    refreshed, failure = await client._refresh_token()

    assert refreshed is False
    assert failure == "Automatic renewal is only available for stored CLI login tokens."


@pytest.mark.asyncio
async def test_refresh_uses_newer_token_written_by_another_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SIBYL_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(
        client_module, "_load_default_auth_token", lambda _api_url, _scope=None: "old-access"
    )
    monkeypatch.setattr(client_module, "auth_file_lock", lambda: _noop_lock())
    monkeypatch.setattr(
        client_module,
        "read_server_credentials",
        lambda _api_url, **_kwargs: {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "access_token_expires_at": int(time.time()) + 3600,
        },
    )
    monkeypatch.setattr(client_module, "is_access_token_expired", lambda _api_url, **_kwargs: False)

    class UnexpectedAsyncClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("refresh endpoint should not be called")

    monkeypatch.setattr(client_module.httpx, "AsyncClient", UnexpectedAsyncClient)

    client = SibylClient(base_url="http://example.test/api")
    refreshed, failure = await client._refresh_token()

    assert refreshed is True
    assert failure is None
    assert client.auth_token == "new-access"


@pytest.mark.asyncio
async def test_refresh_rotates_and_writes_tokens_under_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SIBYL_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(
        client_module, "_load_default_auth_token", lambda _api_url, _scope=None: "old-access"
    )
    monkeypatch.setattr(client_module, "auth_file_lock", lambda: _noop_lock())
    monkeypatch.setattr(
        client_module,
        "read_server_credentials",
        lambda _api_url, **_kwargs: {
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "access_token_expires_at": 1,
        },
    )
    monkeypatch.setattr(client_module, "get_refresh_token", lambda _api_url, **_kwargs: "old-refresh")
    monkeypatch.setattr(client_module, "is_access_token_expired", lambda _api_url, **_kwargs: True)

    writes: list[dict[str, Any]] = []

    def fake_set_tokens(
        api_url: str,
        access_token: str,
        *,
        refresh_token: str | None = None,
        expires_in: int | None = None,
        lock: bool = True,
        credential_scope: str | None = None,
    ) -> None:
        writes.append(
            {
                "api_url": api_url,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_in": expires_in,
                "lock": lock,
                "credential_scope": credential_scope,
            }
        )

    monkeypatch.setattr(client_module, "set_tokens", fake_set_tokens)

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

    monkeypatch.setattr(client_module.httpx, "AsyncClient", FakeAsyncClient)

    client = SibylClient(base_url="http://example.test/api")
    refreshed, failure = await client._refresh_token()

    assert refreshed is True
    assert failure is None
    assert client.auth_token == "new-access"
    assert writes == [
        {
            "api_url": "http://example.test/api",
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
            "lock": False,
            "credential_scope": None,
        }
    ]
