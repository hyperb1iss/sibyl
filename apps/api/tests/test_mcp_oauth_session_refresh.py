from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from mcp.server.auth.provider import RefreshToken, TokenError

from sibyl.auth.mcp_oauth import SibylMcpOAuthProvider


@pytest.mark.asyncio
async def test_mcp_oauth_load_refresh_token_requires_db_session(monkeypatch) -> None:
    provider = SibylMcpOAuthProvider()

    user_id = uuid4()
    org_id = uuid4()
    client = SimpleNamespace(client_id="client1")

    claims = {
        "sub": str(user_id),
        "org": str(org_id),
        "typ": "refresh",
        "cid": "client1",
        "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        "scope": "mcp",
    }

    monkeypatch.setattr("sibyl.auth.mcp_oauth._jwt_decode", lambda t: claims)
    monkeypatch.setattr(provider, "_load_refresh_session_record", AsyncMock(return_value=None))

    assert await provider.load_refresh_token(client, "refresh_token") is None


@pytest.mark.asyncio
async def test_mcp_oauth_load_refresh_token_accepts_when_session_matches(monkeypatch) -> None:
    provider = SibylMcpOAuthProvider()

    user_id = uuid4()
    org_id = uuid4()
    client = SimpleNamespace(client_id="client1")

    claims = {
        "sub": str(user_id),
        "org": str(org_id),
        "typ": "refresh",
        "cid": "client1",
        "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        "scope": "mcp",
    }

    monkeypatch.setattr("sibyl.auth.mcp_oauth._jwt_decode", lambda t: claims)
    monkeypatch.setattr(
        provider,
        "_load_refresh_session_record",
        AsyncMock(return_value=SimpleNamespace(user_id=user_id, organization_id=org_id)),
    )

    token = await provider.load_refresh_token(client, "refresh_token")
    assert token is not None
    assert token.client_id == "client1"
    assert "mcp" in token.scopes


@pytest.mark.asyncio
async def test_mcp_oauth_exchange_refresh_rotates_session(monkeypatch) -> None:
    provider = SibylMcpOAuthProvider()

    user_id = uuid4()
    org_id = uuid4()
    client = SimpleNamespace(client_id="client1")

    claims = {
        "sub": str(user_id),
        "org": str(org_id),
        "typ": "refresh",
        "cid": "client1",
        "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        "scope": "mcp",
    }

    monkeypatch.setattr("sibyl.auth.mcp_oauth._jwt_decode", lambda t: claims)
    monkeypatch.setattr(
        "sibyl.auth.mcp_oauth._create_refresh_token",
        lambda **k: ("new_refresh", datetime.now(UTC) + timedelta(days=30)),
    )
    monkeypatch.setattr("sibyl.auth.mcp_oauth.create_access_token", lambda **k: "new_access")
    rotate_refresh = AsyncMock(
        return_value=SimpleNamespace(user_id=user_id, organization_id=org_id)
    )
    monkeypatch.setattr(provider, "_rotate_refresh_session_record", rotate_refresh)

    incoming = RefreshToken(
        token="refresh_token",
        client_id="client1",
        scopes=["mcp"],
        expires_at=claims["exp"],
    )
    tok = await provider.exchange_refresh_token(client, incoming, ["mcp"])
    assert tok.refresh_token == "new_refresh"
    assert tok.access_token == "new_access"
    rotate_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_oauth_exchange_refresh_rejects_invalid_org_claim(monkeypatch) -> None:
    provider = SibylMcpOAuthProvider()

    user_id = uuid4()
    client = SimpleNamespace(client_id="client1")
    claims = {
        "sub": str(user_id),
        "org": "not-a-uuid",
        "typ": "refresh",
        "cid": "client1",
        "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        "scope": "mcp",
    }
    rotate_refresh = AsyncMock()

    monkeypatch.setattr("sibyl.auth.mcp_oauth._jwt_decode", lambda t: claims)
    monkeypatch.setattr(provider, "_rotate_refresh_session_record", rotate_refresh)

    incoming = RefreshToken(
        token="refresh_token",
        client_id="client1",
        scopes=["mcp"],
        expires_at=claims["exp"],
    )
    with pytest.raises(TokenError) as exc_info:
        await provider.exchange_refresh_token(client, incoming, ["mcp"])

    assert exc_info.value.error == "invalid_grant"
    assert exc_info.value.error_description == "invalid refresh token claims"
    rotate_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_oauth_load_access_token_rejects_invalid_sub_claim(monkeypatch) -> None:
    provider = SibylMcpOAuthProvider()

    monkeypatch.setattr(
        "sibyl.auth.mcp_oauth.verify_access_token",
        lambda _: {"sub": "not-a-uuid", "exp": 123, "scope": "mcp"},
    )

    assert await provider.load_access_token("access-token") is None
