from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from sibyl.auth.api_keys import ApiKeyAuth
from sibyl.auth.dependencies import resolve_claims


def _make_request(*, method: str, path: str, token: str) -> Request:
    scope = {
        "type": "http",
        "method": method.upper(),
        "path": path,
        "headers": [
            (b"authorization", f"Bearer {token}".encode()),
            (b"host", b"testserver"),
        ],
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_api_key_rest_denies_without_api_scopes() -> None:
    request = _make_request(method="GET", path="/api/me", token="sk_live_test")
    session = object()

    with patch("sibyl.auth.dependencies.ApiKeyManager") as manager_cls:
        manager = manager_cls.return_value
        manager.authenticate = AsyncMock(
            return_value=ApiKeyAuth(
                api_key_id="00000000-0000-0000-0000-000000000000",
                user_id="00000000-0000-0000-0000-000000000001",
                organization_id="00000000-0000-0000-0000-000000000002",
                scopes=["mcp"],
            )
        )

        with pytest.raises(HTTPException) as exc:
            await resolve_claims(request, session=session)
        assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_api_key_rest_allows_api_read_for_get() -> None:
    request = _make_request(method="GET", path="/api/me", token="sk_live_test")
    session = object()

    with patch("sibyl.auth.dependencies.ApiKeyManager") as manager_cls:
        manager = manager_cls.return_value
        manager.authenticate = AsyncMock(
            return_value=ApiKeyAuth(
                api_key_id="00000000-0000-0000-0000-000000000000",
                user_id="00000000-0000-0000-0000-000000000001",
                organization_id="00000000-0000-0000-0000-000000000002",
                scopes=["api:read"],
            )
        )

        claims = await resolve_claims(request, session=session)
        assert claims is not None
        assert claims["typ"] == "api_key"
        assert "api:read" in claims["scopes"]


@pytest.mark.asyncio
async def test_api_key_rest_denies_write_without_api_write() -> None:
    request = _make_request(method="POST", path="/api/me", token="sk_live_test")
    session = object()

    with patch("sibyl.auth.dependencies.ApiKeyManager") as manager_cls:
        manager = manager_cls.return_value
        manager.authenticate = AsyncMock(
            return_value=ApiKeyAuth(
                api_key_id="00000000-0000-0000-0000-000000000000",
                user_id="00000000-0000-0000-0000-000000000001",
                organization_id="00000000-0000-0000-0000-000000000002",
                scopes=["api:read"],
            )
        )

        with pytest.raises(HTTPException) as exc:
            await resolve_claims(request, session=session)
        assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_api_key_rest_allows_write_with_api_write() -> None:
    request = _make_request(method="POST", path="/api/me", token="sk_live_test")
    session = object()

    with patch("sibyl.auth.dependencies.ApiKeyManager") as manager_cls:
        manager = manager_cls.return_value
        manager.authenticate = AsyncMock(
            return_value=ApiKeyAuth(
                api_key_id="00000000-0000-0000-0000-000000000000",
                user_id="00000000-0000-0000-0000-000000000001",
                organization_id="00000000-0000-0000-0000-000000000002",
                scopes=["api:write"],
            )
        )

        claims = await resolve_claims(request, session=session)
        assert claims is not None
        assert claims["typ"] == "api_key"
        assert "api:write" in claims["scopes"]
