from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from mcp.server.auth.provider import RefreshToken

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

    @asynccontextmanager
    async def fake_session():  # type: ignore[no-untyped-def]
        yield object()

    class FakeSessionManager:
        def __init__(self, session):  # type: ignore[no-untyped-def]
            pass

        async def get_session_by_refresh_token(self, token: str):  # type: ignore[no-untyped-def]
            return None

    monkeypatch.setattr("sibyl.auth.mcp_oauth.get_session", fake_session)
    monkeypatch.setattr("sibyl.auth.mcp_oauth._jwt_decode", lambda t: claims)
    monkeypatch.setattr("sibyl.auth.mcp_oauth.SessionManager", FakeSessionManager)

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

    @asynccontextmanager
    async def fake_session():  # type: ignore[no-untyped-def]
        yield object()

    class FakeSessionManager:
        def __init__(self, session):  # type: ignore[no-untyped-def]
            pass

        async def get_session_by_refresh_token(self, token: str):  # type: ignore[no-untyped-def]
            return SimpleNamespace(user_id=user_id, organization_id=org_id)

    monkeypatch.setattr("sibyl.auth.mcp_oauth.get_session", fake_session)
    monkeypatch.setattr("sibyl.auth.mcp_oauth._jwt_decode", lambda t: claims)
    monkeypatch.setattr("sibyl.auth.mcp_oauth.SessionManager", FakeSessionManager)

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

    @asynccontextmanager
    async def fake_session():  # type: ignore[no-untyped-def]
        yield object()

    rotated = {"called": False}

    class FakeSessionManager:
        def __init__(self, session):  # type: ignore[no-untyped-def]
            pass

        async def get_session_by_refresh_token(self, token: str):  # type: ignore[no-untyped-def]
            return SimpleNamespace(user_id=user_id, organization_id=org_id)

        async def rotate_tokens(self, session_record, **kwargs):  # type: ignore[no-untyped-def]
            rotated["called"] = True
            return session_record

    monkeypatch.setattr("sibyl.auth.mcp_oauth.get_session", fake_session)
    monkeypatch.setattr("sibyl.auth.mcp_oauth._jwt_decode", lambda t: claims)
    monkeypatch.setattr(
        "sibyl.auth.mcp_oauth._create_refresh_token",
        lambda **k: ("new_refresh", datetime.now(UTC) + timedelta(days=30)),
    )
    monkeypatch.setattr("sibyl.auth.mcp_oauth.SessionManager", FakeSessionManager)
    monkeypatch.setattr("sibyl.auth.mcp_oauth.create_access_token", lambda **k: "new_access")

    incoming = RefreshToken(
        token="refresh_token",
        client_id="client1",
        scopes=["mcp"],
        expires_at=claims["exp"],
    )
    tok = await provider.exchange_refresh_token(client, incoming, ["mcp"])
    assert tok.refresh_token == "new_refresh"
    assert tok.access_token == "new_access"
    assert rotated["called"] is True
